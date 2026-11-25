// Read-only acceptance of the actual platform task and every served Result.
// Never dispatches, approves, retries, or publishes a scientific computation.
import { open } from "./host"
import { Inspire } from "./inspire"
import { digest, verifyOutcome, type Outcome } from "./model"
import { readReport } from "./report"
import { publications } from "./publication"
import { deliveryStatus } from "./delivery-queue"

const attempt = process.argv.find((arg) => arg.startsWith("--attempt="))?.slice(10)
if (!attempt || !/^[a-z0-9-]{1,40}$/.test(attempt)) throw new Error("An explicit acceptance attempt is required")
const workspace = process.env.COVSCREEN_ACCEPTANCE_ROOT
if (!workspace?.startsWith("/")) throw Error("Set an absolute COVSCREEN_ACCEPTANCE_ROOT for this historical acceptance")
const root = `${workspace}/reports`
const receipt = await Bun.file(`${root}/inspire-test100-live-acceptance-${attempt}.json`).json()
const output = `${root}/inspire-test100-completion-audit-${attempt}.json`
const store = await open()
try {
  const task = store.get(receipt.task)
  const stages = store.stages(task.id)
  if (task.plan.session !== receipt.session || task.digest !== receipt.digest ||
    task.plan.request !== receipt.request || task.plan.automation?.selected_mode !== "test100" ||
    task.plan.shortlist !== 100 || task.plan.max_records !== 100000 || task.plan.max_points !== null ||
    task.plan.steps.map((step) => step.minutes).join(",") !== "60,120,60,10")
    throw new Error("Current task differs from the authorized one-sentence test100 request")
  const delivery = deliveryStatus(store, task)
  const initial = { checked_at: new Date().toISOString(), task: task.id, session: task.plan.session,
    directory: task.plan.directory, plan_digest: task.digest, state: task.state, delivery }
  if (task.state !== "SUCCEEDED" || delivery?.state !== "COMPLETE") {
    await Bun.write(output, JSON.stringify({ ...initial, passed: false, reason: "Core stages or complete delivery not finished" }, null, 2))
    console.log(JSON.stringify({ ...initial, passed: false }))
  } else {
    const report = await readReport(task, stages, new Inspire(task.plan.connection))
    if (report.verification !== "verified") throw new Error(`Fresh remote verification: ${report.verification}`)
    const chain = stages.reduce<Outcome[]>((values, stage) => [
      ...values, verifyOutcome(JSON.parse(stage.manifest!), task, stage, values.at(-1) ?? null),
    ], [])
    if (chain.some((phase, index) => phase.input_count !== 100 || phase.runtime_seconds > task.plan.steps[index].minutes * 60))
      throw new Error("Candidate denominator or runtime cap was violated")
    for (const index of [1, 2]) {
      const environment = chain[index].environment as { gpu?: { visible_count?: number } }
      if (environment?.gpu?.visible_count !== 1) throw new Error("GPU execution was not witnessed on exactly one GPU")
    }
    const events = store.events(task.id) as { kind: string; at: number; payload: string }[]
    const submits = events.filter((event) => event.kind === "dispatch_reserved")
    if (submits.length !== 4 || submits.some((event, index) => JSON.parse(event.payload).ordinal !== index))
      throw new Error("Expected exactly one ordered dispatch for each of four stages")
    for (const index of [1, 2, 3]) {
      const parent = events.find((event) => event.kind === "scientifically_verified" && JSON.parse(event.payload).phase === stages[index - 1].phase)
      if (!parent || parent.at > submits[index].at) throw new Error("Downstream stage preceded parent verification")
    }
    const request = async (route: string) => {
      const url = new URL(route, "http://127.0.0.1:4096")
      url.searchParams.set("directory", task.plan.directory)
      const response = await fetch(url, { redirect: "error", signal: AbortSignal.timeout(30000) })
      if (!response.ok) throw new Error(`Platform read HTTP ${response.status}`)
      return response
    }
    const message = await (await request(`/session/${task.plan.session}/message/${task.plan.origin!.user_message_id}`)).json()
    const text = message.parts.filter((part: { id: string }) => task.plan.origin!.part_ids.includes(part.id))
      .map((part: { text: string }) => part.text).join("\n")
    if (message.info.role !== "user" || text !== task.plan.request) throw new Error("Original platform message no longer matches the plan")
    const expected = chain.flatMap((phase) => Object.entries(phase.artifacts).map(([name, sha256]) => ({ phase: phase.phase, name, sha256 })))
    const saved = publications(store, task)
    if (saved.length !== expected.length || delivery.total_files !== expected.length || delivery.completed_files !== expected.length)
      throw new Error("Result count differs from the complete manifest inventory")
    const native = []
    for (const item of expected) {
      const record = saved.filter((result) => result.phase === item.phase && result.name === item.name)
      if (record.length !== 1 || record[0].sha256 !== item.sha256 || record[0].evidence_kind !== "core_result")
        throw new Error("Result publication is missing, duplicated, or not core evidence")
      const result = record[0]
      const detail = await (await request(`/file/artifact-store/${result.artifact_id}`)).json()
      if (detail.state !== "active" || detail.projectID !== receipt.project || detail.currentVersionID !== result.version_id ||
        detail.current.id !== result.version_id || detail.current.sha256 !== item.sha256 ||
        detail.current.sessionID !== task.plan.session || detail.current.size !== result.bytes)
        throw new Error("Native Result details differ from this task's verified publication")
      const response = await request(`/file/artifact-store/${result.artifact_id}/raw?versionID=${result.version_id}&download=true`)
      if (!response.headers.get("content-disposition")?.includes("attachment")) throw new Error("Download response lacks attachment disposition")
      const reader = response.body!.getReader()
      const hash = new Bun.CryptoHasher("sha256")
      const size = { bytes: 0 }
      try {
        while (true) {
          const chunk = await reader.read()
          if (chunk.done) break
          size.bytes += chunk.value.byteLength
          if (size.bytes > result.bytes) throw new Error("Served Result exceeds its verified byte count")
          hash.update(chunk.value)
        }
      } finally { await reader.cancel(); reader.releaseLock() }
      if (size.bytes !== result.bytes || hash.digest("hex") !== item.sha256) throw new Error("Downloaded Result hash/size mismatch")
      native.push({ ...item, artifact_id: result.artifact_id, version_id: result.version_id, title: detail.title,
        bytes: size.bytes, http_download_verified: true })
    }
    if (store.get(task.id).digest !== task.digest || store.get(task.id).state !== "SUCCEEDED") throw new Error("Task changed during audit")
    const result = { ...initial, passed: true, browser_acceptance: "still_required", remote_verified_at: report.verified_at,
      original_user_message_verified: true, ordered_single_dispatch_verified: true,
      stages: chain.map((phase) => ({ phase: phase.phase, input: phase.input_count, success: phase.success_count,
        failed: phase.failed_count, gates: phase.gates, seconds: phase.runtime_seconds, manifest_digest: digest(phase) })), native_files: native }
    await Bun.write(output, JSON.stringify(result, null, 2))
    console.log(JSON.stringify({ task: task.id, passed: true, verified_files: native.length, browser_acceptance: "still_required", output }))
  }
} catch (error) {
  await Bun.write(output, JSON.stringify({ checked_at: new Date().toISOString(), passed: false,
    task: receipt.task, error: error instanceof Error ? error.message : "Audit error" }, null, 2))
  throw error
} finally { store.close() }
