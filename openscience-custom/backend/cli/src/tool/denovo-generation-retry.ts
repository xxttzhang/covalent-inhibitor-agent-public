import path from "node:path"
import { createHash } from "node:crypto"
import { copyFile, lstat, mkdir, readFile, realpath, rename, writeFile } from "node:fs/promises"
import z from "zod"

const sha256 = (value: string) => createHash("sha256").update(value).digest("hex")

function embedded(error: string, command: "create" | "status") {
  const prefix = `Inspire command failed: job ${command} `
  if (!error.startsWith(prefix)) throw Error(`Generation retry requires the original Inspire job ${command} failure`)
  return JSON.parse(error.slice(prefix.length).trim())
}

export function quotaRejection(value: unknown) {
  const parsed = z.object({ error: z.string() }).parse(value)
  const remote = z.object({ success: z.literal(false), error: z.object({ type: z.literal("APIError"),
    code: z.literal(13), message: z.string() }) }).parse(embedded(parsed.error, "create"))
  if (!remote.error.message.includes("项目配额不足") || !remote.error.message.includes("该任务提交失败"))
    throw Error("Generation retry is limited to an explicit project-quota submission rejection")
  return remote
}

export function jobNotFound(value: unknown, task: string) {
  const remote = z.object({ success: z.literal(false), error: z.object({ type: z.literal("JobNotFound"),
    code: z.literal(16), message: z.string() }) }).parse(value)
  if (!remote.error.message.includes(`'${task}'`)) throw Error("JobNotFound proof belongs to another task")
  return remote
}

export async function observeMissingInspireJob(profile: { cli: string; account: string; workspace: string }, task: string) {
  const child = Bun.spawn([profile.cli, "--json", "--no-env-file", "--account", profile.account,
    "job", "status", task, "--workspace", profile.workspace], { stdout: "pipe", stderr: "pipe" })
  const [stdout, stderr, exit] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
  const raw = stdout.trim() || stderr.trim()
  let value: unknown
  try { value = JSON.parse(raw) } catch { throw Error(`Inspire job observation was not structured JSON (exit ${exit})`) }
  return jobNotFound(value, task)
}

async function regular(filename: string) {
  if (!(await lstat(filename)).isFile() || await realpath(filename) !== filename)
    throw Error("Generation retry evidence must be regular files without symlinks")
  return readFile(filename, "utf8")
}

export async function inspectGenerationRetry(root: string, task: string, observe: () => Promise<unknown>) {
  const statusRaw = await regular(path.join(root, "status.json"))
  const status = z.object({ id: z.literal(task), state: z.literal("NEEDS_REVIEW") }).passthrough().parse(JSON.parse(statusRaw))
  const submissionRaw = await regular(path.join(root, "submission.json"))
  z.object({ name: z.literal(task), state: z.literal("SUBMITTING") }).passthrough().parse(JSON.parse(submissionRaw))
  const errorRaw = await regular(path.join(root, "submission_error.json"))
  quotaRejection(JSON.parse(errorRaw))
  const observed = jobNotFound(await observe(), task)
  return { task, attempt: 2, reason: "project_quota_rejected_before_job_creation",
    evidence: { status_sha256: sha256(statusRaw), submission_sha256: sha256(submissionRaw),
      submission_error_sha256: sha256(errorRaw), observation_sha256: sha256(JSON.stringify(observed)) },
    observed }
}

export async function armGenerationRetry(root: string, receipt: Record<string, unknown>) {
  const attempt = path.join(root, "recovery-attempts", "attempt-1")
  const armed = path.join(attempt, "armed.json")
  await mkdir(path.dirname(attempt), { recursive: true, mode: 0o700 })
  const created = await mkdir(attempt, { mode: 0o700 }).then(() => true, (error: NodeJS.ErrnoException) => {
    if (error.code === "EEXIST") return false
    throw error
  })
  if (!created) {
    const saved = JSON.parse(await regular(armed))
    if (sha256(JSON.stringify(saved.receipt)) !== sha256(JSON.stringify(receipt)))
      throw Error("Generation retry was already armed with different evidence")
    return { attempt: 2, already_armed: true }
  }
  for (const name of ["status.json", "submission.json", "submission_error.json"])
    await copyFile(path.join(root, name), path.join(attempt, name), 0)
  await writeFile(armed, JSON.stringify({ receipt, armed_at: Date.now() }, null, 2) + "\n", { flag: "wx", mode: 0o600 })
  // Rename only after the immutable archive is complete; a crash remains fail-closed.
  for (const name of ["submission_error.json", "submission.json", "status.json"])
    await rename(path.join(root, name), path.join(attempt, `original-${name}`))
  await writeFile(path.join(root, "status.json"), JSON.stringify({ id: path.basename(root), state: "PREPARING",
    retry_attempt: 2, previous_attempt: path.relative(root, attempt) }) + "\n", { flag: "wx", mode: 0o600 })
  return { attempt: 2, already_armed: false }
}
