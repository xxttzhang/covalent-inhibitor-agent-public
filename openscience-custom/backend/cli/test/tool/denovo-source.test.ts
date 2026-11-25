import { test, expect } from "bun:test"
import { createHash } from "node:crypto"
import { mkdtemp, mkdir, writeFile, rm } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { registeredDeNovoSource } from "../../src/tool/denovo-source"

const sha = (text: string) => createHash("sha256").update(text).digest("hex")
test("De novo code selection verifies immutable content and fails closed on tampering", async () => {
  const base = await mkdtemp(path.join(os.tmpdir(), "denovo-source-"))
  try {
    const canonical = await registeredDeNovoSource(base)
    expect(canonical.package).toBe(path.join(base, "routes/de-novo/registry/egfr_c797_v1"))
    const name = "routes/de-novo/runtime/worker.py"
    const data = "# frozen worker\n"
    const manifest = JSON.stringify({ version: "screening-code-release-v1", files: { [name]: sha(data) } })
    const id = sha(manifest)
    const root = path.join(base, ".cache/denovo/releases", id)
    await mkdir(path.dirname(path.join(root, name)), { recursive: true })
    await mkdir(path.join(base, "ops"))
    await writeFile(path.join(root, name), data)
    await writeFile(path.join(root, "manifest.json"), manifest + "\n")
    await writeFile(path.join(base, "ops/denovo-release.json"), JSON.stringify({ release_id: id, manifest_sha256: sha(manifest + "\n"), remote_root: "/registered/releases/" + id }))
    // macOS /var is a symlink; use the actual base for the verifier.
    const { realpath } = await import("node:fs/promises")
    const resolved = await realpath(base)
    expect((await registeredDeNovoSource(resolved)).source_release?.release_id).toBe(id)
    const frozen = { release_id: id, manifest_sha256: sha(manifest + "\n"), root: "/registered/releases/" + id }
    const pointerfile = path.join(base, "ops/denovo-release.json")
    const previous = await Bun.file(pointerfile).text()
    await writeFile(pointerfile, JSON.stringify({
      release_id: "b".repeat(64), manifest_sha256: "c".repeat(64), remote_root: "/new/release",
    }))
    expect((await registeredDeNovoSource(resolved, frozen)).source_release).toEqual(frozen)
    await expect(registeredDeNovoSource(resolved)).rejects.toThrow()
    await writeFile(pointerfile, previous)
    await writeFile(path.join(root, name), "# altered\n")
    await expect(registeredDeNovoSource(resolved)).rejects.toThrow("checksum differs")
    await expect(registeredDeNovoSource(resolved, frozen)).rejects.toThrow("checksum differs")
    await writeFile(path.join(root, name), data)
    await writeFile(path.join(root, "unexpected.py"), "# extra\n")
    await expect(registeredDeNovoSource(resolved)).rejects.toThrow("checksum differs")
    await rm(path.join(root, "unexpected.py"))
    await rm(path.join(root, name))
    await expect(registeredDeNovoSource(resolved)).rejects.toThrow("inventory differs")
  } finally { await rm(base, { recursive: true, force: true }) }
})
