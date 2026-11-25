import path from "node:path"
import { lstat, realpath, readFile } from "node:fs/promises"
import { createHash } from "node:crypto"
import { isDeepStrictEqual } from "node:util"
import z from "zod"

export function taskRoot(value?: string) {
  if (value === undefined) return path.resolve(import.meta.dir, "../../../../../.cache/denovo/tasks")
  if (!path.isAbsolute(value) || path.normalize(value) !== value)
    throw Error("De novo task storage must be an absolute normalized host path")
  return value
}

// Host deployments keep task evidence at its original path across source upgrades.
export const deNovoTasks = taskRoot(process.env.COVALENT_DENOVO_TASKS_ROOT)
export const deNovoStatus = z.object({ id: z.string(), state: z.string(), error: z.string().optional(),
  summary: z.object({ requested_slots: z.number().int().positive(),
    raw: z.object({ pass_count: z.number().int().nonnegative() }),
    relaxed: z.object({ pass_count: z.number().int().nonnegative() }) }).passthrough()
    .refine(value => value.raw.pass_count <= value.requested_slots
      && value.relaxed.pass_count <= value.requested_slots, "Candidate counts exceed requested slots").optional(),
  artifacts: z.record(z.string(), z.object({ source: z.string(), artifact_id: z.string() }).passthrough()).optional(),
}).passthrough()

async function exists(filename: string) {
  return lstat(filename).then(() => true, (error: NodeJS.ErrnoException) => {
    if (error.code === "ENOENT") return false
    throw error
  })
}

async function regular(filename: string) {
  if (!(await lstat(filename)).isFile() || await realpath(filename) !== filename)
    throw Error("Native task files must be regular files without symlinks")
  return readFile(filename, "utf8")
}

/** Read only the original task; never launch, reserve, approve or rewrite it.
 * A partially reserved task is an unknown outcome, not permission to recreate.
 */
export async function readDeNovoTask(base: string, id: string, expected: {
  directory: string; session?: string; study?: string; request?: Record<string, unknown>;
}) {
  if (!/^denovo-[a-f0-9]{24}$/.test(id)) throw Error("Invalid De novo task identity")
  const root = path.join(base, id)
  if (!await exists(root)) return null
  if (!(await lstat(root)).isDirectory() || await realpath(root) !== root)
    throw Error("Native task directory must not be a symlink")
  if (!await exists(path.join(root, "request.json")))
    return { id, state: "OUTCOME_UNKNOWN", approval_verified: false,
      reason: "Task reservation exists without a complete request; inspect, never recreate automatically." }
  const raw = await regular(path.join(root, "request.json"))
  const request = z.object({ id: z.literal(id), directory: z.literal(expected.directory),
    session_id: z.string(), user_message_id: z.string().optional(), study_id: z.string().optional(),
  }).passthrough().parse(JSON.parse(raw))
  if (expected.session && request.session_id !== expected.session) throw Error("Task belongs to another session")
  if (expected.study && request.study_id !== expected.study) throw Error("Task belongs to another study")
  if (expected.request) {
    const rebound = new Set(["session_id", "user_message_id", "user_text", "user_text_sha256", "native_binding_created"])
    for (const [key, value] of Object.entries(expected.request))
      if (!rebound.has(key) && !isDeepStrictEqual(request[key], value))
        throw Error(`Native task differs from frozen study plan: ${key}`)
    if (request.planning_session_id !== expected.request.session_id)
      throw Error("Native task planning session differs")
  }
  const authorization = path.join(root, "authorization.json")
  const approved = await exists(authorization)
  if (approved) {
    const proof = z.object({ request_sha256: z.string(), session_id: z.literal(request.session_id),
      user_message_id: z.literal(request.user_message_id), mechanism: z.literal("native_remote_compute"),
    }).passthrough().parse(JSON.parse(await regular(authorization)))
    if (proof.request_sha256 !== createHash("sha256").update(raw).digest("hex"))
      throw Error("Native approval differs from task request")
  }
  if (!await exists(path.join(root, "status.json")))
    return { id, state: "OUTCOME_UNKNOWN", approval_verified: approved,
      reason: "Native status is missing; inspect the original task without resubmission." }
  const status = deNovoStatus.parse(JSON.parse(await regular(path.join(root, "status.json"))))
  if (status.id !== id) throw Error("Native status belongs to another task")
  if (!approved && !["AWAITING_APPROVAL", "CANCELLED", "NEEDS_REVIEW"].includes(status.state))
    throw Error("Native execution status has no matching approval")
  return { ...status, approval_verified: approved }
}
