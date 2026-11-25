import path from "node:path"
import { createHash } from "node:crypto"
import { readdir } from "node:fs/promises"
import z from "zod"

const digest = (input: string | Uint8Array) => createHash("sha256").update(input).digest("hex")
async function files(root: string, base = root): Promise<Record<string, string>> {
  const result: Record<string, string> = {}
  for (const entry of await readdir(root, { withFileTypes: true })) {
    if (entry.name === "__pycache__") continue
    const filename = path.join(root, entry.name)
    if (entry.isSymbolicLink()) throw Error("Registered package must not contain symlinks")
    if (entry.isDirectory()) Object.assign(result, await files(filename, base))
    if (entry.isFile()) result[path.relative(base, filename)] = digest(new Uint8Array(await Bun.file(filename).arrayBuffer()))
  }
  return result
}

/** The request consumed by the real worker. Read-only: no reservation, approval,
 * launch or remote call. Host passes the explicit registered package and worker. */
export async function compileDeNovoRequest(input: {
  id: string; profile: string; slots: number; seed: number; session: string;
  message?: string; directory: string; text: string; package: string; worker: string;
  source_release?: { release_id: string; manifest_sha256: string; root: string };
}) {
  if (!/^denovo-[a-f0-9]{24}$/.test(input.id)) throw Error("Invalid De novo task identity")
  const profile = z.object({ id: z.string(), max_slots: z.number().int().positive(),
    allowed_directories: z.array(z.string()) }).passthrough().parse(await Bun.file(path.join(input.package, "profile.json")).json())
  if (profile.id !== input.profile) throw Error("Requested De novo profile differs from registered package")
  if (!profile.allowed_directories.includes(input.directory)) throw Error("Project has no registered De novo profile")
  if (!Number.isSafeInteger(input.slots) || input.slots < 1 || input.slots > profile.max_slots) throw Error("Slots exceed registered generation limits")
  if (!Number.isSafeInteger(input.seed) || input.seed < 0) throw Error("Explicit nonnegative seed required")
  return { source_release: input.source_release, id: input.id, profile: input.profile, slots: input.slots, seed: input.seed,
    session_id: input.session, user_message_id: input.message, directory: input.directory,
    user_text: input.text, user_text_sha256: digest(input.text), package_hashes: await files(input.package),
    worker_sha256: digest(new Uint8Array(await Bun.file(input.worker).arrayBuffer())), created: Date.now() }
}
