import z from "zod"
import path from "node:path"
import { lstat, mkdir, mkdtemp, realpath, link, unlink, rmdir, chmod } from "node:fs/promises"
import { type Task, type Stage, digest } from "./model"
import { type Transport, ReviewRequired } from "./runner"
import { readReport } from "./report"
import { failureInventory, readFailureReport } from "./failure-report"
import type { Store } from "./store"

export const artifact = z.object({
  phase: z.enum(["prepare", "carsidock", "rtmscore", "report"]),
  name: z.string().min(1).max(1024).refine((value) => !/[\\\x00-\x1f\x7f]/.test(value) &&
    value.split("/").every((part) => part && part !== "." && part !== "..")),
}).strict()
export const MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024

export function downloadSpec(task: Task, stages: Stage[], input: z.infer<typeof artifact>, store?: Store) {
  const selected = artifact.parse(input)
  if (task.state === "FAILED") {
    if (!store) throw new ReviewRequired("Failure delivery requires its retained witness and policy")
    const entry = failureInventory(store, task).find((item) => item.phase === selected.phase && item.name === selected.name)
    if (!entry) throw new ReviewRequired("File is not in the diagnostic/verified-prefix inventory")
    return { ...selected, sha256: entry.sha256, remote: `${task.plan.runtime.root}/tasks/${task.id}/${selected.phase}/${selected.name}` }
  }
  const stage = stages.find((item) => item.phase === selected.phase)
  const manifest = stage?.manifest ? JSON.parse(stage.manifest) : null
  const sha256 = manifest?.artifacts?.[selected.name]
  if (task.state !== "SUCCEEDED" || !stage || stage.state !== "SUCCEEDED" ||
      manifest?.task_id !== task.id || manifest?.plan_digest !== task.digest ||
      typeof sha256 !== "string" || !/^[a-f0-9]{64}$/.test(sha256)) {
    throw new ReviewRequired("Only an exact artifact in this completed task's verified inventory can be downloaded")
  }
  return { ...selected, sha256, remote: `${task.plan.runtime.root}/tasks/${task.id}/${selected.phase}/${selected.name}` }
}

export async function checksum(file: string) {
  const info = await lstat(file)
  if (!info.isFile() || info.isSymbolicLink() || info.size > MAX_DOWNLOAD_BYTES) {
    throw new ReviewRequired("Download must be a regular file within the 128 MiB delivery limit")
  }
  const hash = new Bun.CryptoHasher("sha256")
  const reader = Bun.file(file).stream().getReader()
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      hash.update(chunk.value)
    }
  } finally { reader.releaseLock() }
  const after = await lstat(file)
  if (after.ino !== info.ino || after.size !== info.size || after.mtimeMs !== info.mtimeMs) {
    throw new ReviewRequired("Local artifact changed while checking its contents")
  }
  return { sha256: hash.digest("hex"), bytes: info.size }
}

export async function deliveryPath(task: Task, spec: { phase: string; name: string; sha256: string }) {
  const root = await realpath(task.plan.directory)
  const filename = `${spec.sha256}-${path.basename(spec.name).replace(/[^a-zA-Z0-9._-]/g, "_").slice(-100)}`
  return path.join(root, "inspire-results", task.id, spec.phase, filename)
}

// No model-selected local destination, overwrite, archive extraction, external
// upload, or retry of computation. The transport copies one witnessed file only.
export async function deliverArtifact(task: Task, stages: Stage[], input: z.infer<typeof artifact>,
  context: { directory: string; sessionID: string; abort: AbortSignal },
  transport: Pick<Transport, "verify"> & { download(task: Task, remote: string, destination: string, limit: number): Promise<void>;
    failure?(task: Task, stage: Stage, witness?: string): Promise<unknown> },
  limit = MAX_DOWNLOAD_BYTES, store?: Store) {
  if (!Number.isSafeInteger(limit) || limit < 0 || limit > MAX_DOWNLOAD_BYTES) throw new ReviewRequired("Invalid artifact delivery byte limit")
  if (context.directory !== task.plan.directory || context.sessionID !== task.plan.session) {
    throw new ReviewRequired("Artifact delivery belongs to another conversation/project")
  }
  context.abort.throwIfAborted()
  const spec = downloadSpec(task, stages, input, store)
  const witness = async () => {
    if (task.state === "FAILED") {
      if (!store || !transport.failure) throw new ReviewRequired("Failure readback transport is unavailable")
      const result = await readFailureReport(store, task.id, { verify: transport.verify.bind(transport),
        failure: transport.failure.bind(transport) }, context.abort)
      if (!result.failure_evidence_ready) throw new ReviewRequired(`Failure evidence cannot be delivered: ${result.reason}`)
      return digest({ diagnostics: result.diagnostics, upstream: result.upstream_manifest_digests })
    }
    const result = await readReport(task, stages, transport)
    if (!result.report_ready) throw new ReviewRequired(`Report cannot be delivered: ${result.verification}`)
    return digest(result.artifacts)
  }
  const report = await witness()
  context.abort.throwIfAborted()
  const root = await realpath(context.directory)
  const components = ["inspire-results", task.id, spec.phase]
  for (const index of components.keys()) {
    const current = path.join(root, ...components.slice(0, index + 1))
    await mkdir(current, { mode: 0o700 }).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== "EEXIST") throw error
    })
    if (!(await lstat(current)).isDirectory() || await realpath(current) !== current) {
      throw new ReviewRequired("Artifact delivery directory cannot be redirected")
    }
  }
  const directory = path.join(root, ...components)
  const target = await deliveryPath(task, spec)
  const existing = await lstat(target).catch((error: NodeJS.ErrnoException) => {
    if (error.code !== "ENOENT") throw error
    return null
  })
  if (!existing) {
    const temporary = await mkdtemp(path.join(directory, ".download-"))
    const partial = path.join(temporary, "artifact.part")
    try {
      await transport.download(task, spec.remote, partial, limit)
      context.abort.throwIfAborted()
      const copied = await checksum(partial)
      if (copied.bytes > limit) throw new ReviewRequired("Artifact exceeds the remaining approved delivery byte limit")
      if (copied.sha256 !== spec.sha256) throw new ReviewRequired("Downloaded artifact checksum differs from the frozen result")
      // Recheck the entire report after transfer, not just the copied file.
      const fresh = await witness()
      if (fresh !== report) {
        throw new ReviewRequired("Remote report changed during download; no file published")
      }
      context.abort.throwIfAborted()
      if (await realpath(directory) !== directory) throw new ReviewRequired("Artifact destination changed during download")
      await chmod(partial, 0o400)
      await link(partial, target).catch((error: NodeJS.ErrnoException) => {
        if (error.code !== "EEXIST") throw error
      }) // Exclusive publication; a concurrent caller must have identical bytes.
    } finally {
      await unlink(partial).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error })
      await rmdir(temporary)
    }
  }
  const verified = await checksum(target)
  if (verified.bytes > limit) throw new ReviewRequired("Artifact exceeds the remaining approved delivery byte limit")
  if (verified.sha256 !== spec.sha256) throw new ReviewRequired("Existing delivery differs; never overwrite or repair it silently")
  return { task_id: task.id, plan_digest: task.digest, phase: spec.phase, name: spec.name,
    ...verified, local_path: target, delivery_verified_at: Date.now(),
    evidence_kind: task.state === "FAILED" ? stages.find((stage) => stage.phase === spec.phase)?.state === "FAILED"
      ? "failure_diagnostic" as const : "verified_prefix" as const : "core_result" as const,
    note: "Verified local copy only. Failed-stage diagnostics are not validated molecular outputs; verified upstream files do not make a failed screening successful. Not experimental validation or AF3/CovDock completion. No file contents sent to the model." }
}
