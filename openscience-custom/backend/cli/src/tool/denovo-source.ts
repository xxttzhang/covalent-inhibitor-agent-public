import path from "node:path"
import { createHash } from "node:crypto"
import { lstat, readdir, realpath } from "node:fs/promises"
import z from "zod"

const workspace = path.resolve(import.meta.dir, "../../../../..")
const sha = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex")
const identity = z.string().regex(/^[a-f0-9]{64}$/)
const pointer = z.object({ release_id: identity, manifest_sha256: identity, remote_root: z.string() }).strict()

/** Host-maintained code selection. Frozen tasks keep their own package and worker. */
export async function registeredDeNovoSource(base = workspace, frozen?: unknown) {
  const saved = frozen === undefined ? null : z.object({
    release_id: identity, manifest_sha256: identity, root: z.string(),
  }).strict().parse(frozen)
  const file = path.join(base, "ops/denovo-release.json")
  if (!saved && !await Bun.file(file).exists()) return {
    package: path.join(base, "routes/de-novo/registry/egfr_c797_v1"),
    worker: path.join(base, "routes/de-novo/runtime/worker.py"),
    evaluation: path.join(base, "routes/de-novo/evaluation/source.py"),
    reviewTransport: path.join(base, "routes/de-novo/runtime/review_transport.py"),
  }
  if (!saved && (await lstat(file)).isSymbolicLink()) throw Error("De novo release pointer must be a regular file")
  const selected = saved ? { release_id: saved.release_id, manifest_sha256: saved.manifest_sha256,
    remote_root: saved.root } : pointer.parse(await Bun.file(file).json())
  const root = path.join(base, ".cache/denovo/releases", selected.release_id)
  if (await realpath(root) !== root) throw Error("De novo release path must not contain symlinks")
  const bytes = new Uint8Array(await Bun.file(path.join(root, "manifest.json")).arrayBuffer())
  if (sha(bytes) !== selected.manifest_sha256) throw Error("De novo manifest checksum differs")
  // The Python publisher hashes the canonical manifest without its final newline.
  if (bytes[bytes.length - 1] !== 10 || sha(bytes.slice(0, -1)) !== selected.release_id)
    throw Error("De novo release identity differs")
  const manifest = z.object({ version: z.literal("screening-code-release-v1"), files: z.record(z.string(), identity) }).strict().parse(JSON.parse(new TextDecoder().decode(bytes)))
  const actual: string[] = []
  async function inspect(folder: string) {
    for (const entry of await readdir(folder, { withFileTypes: true })) {
      const filename = path.join(folder, entry.name)
      if (entry.isSymbolicLink()) throw Error("De novo release contains a symlink")
      if (entry.isDirectory()) { await inspect(filename); continue }
      if (!entry.isFile()) throw Error("De novo release contains a nonregular file")
      const name = path.relative(root, filename)
      if (name === "manifest.json") continue
      if (!manifest.files[name] || sha(new Uint8Array(await Bun.file(filename).arrayBuffer())) !== manifest.files[name])
        throw Error("De novo release file checksum differs: " + name)
      actual.push(name)
    }
  }
  await inspect(root)
  if (actual.length !== Object.keys(manifest.files).length) throw Error("De novo release inventory differs")
  return {
    package: path.join(root, "routes/de-novo/registry/egfr_c797_v1"),
    worker: path.join(root, "routes/de-novo/runtime/worker.py"),
    evaluation: path.join(root, "routes/de-novo/evaluation/source.py"),
    reviewTransport: path.join(root, "routes/de-novo/runtime/review_transport.py"),
    source_release: { release_id: selected.release_id, manifest_sha256: selected.manifest_sha256, root: selected.remote_root },
  }
}
