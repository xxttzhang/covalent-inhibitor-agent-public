import { expect, test } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, realpath, mkdir, rm, readFile, symlink } from "node:fs/promises"
import { createHash } from "node:crypto"
import { readDeNovoTask, taskRoot } from "../../src/tool/denovo-task"

const id = "denovo-0123456789abcdef01234567"
const sha = (s: string) => createHash("sha256").update(s).digest("hex")
test("host-selected task storage preserves the original ledger after a source upgrade", async () => {
  const f = await fixture()
  try {
    expect(await readDeNovoTask(taskRoot(f.base), id, f.expected)).toMatchObject({ id, state: "AWAITING_APPROVAL" })
    expect(() => taskRoot("relative/tasks")).toThrow("absolute normalized")
    expect(() => taskRoot(f.base + "/../tasks")).toThrow("absolute normalized")
  } finally { await rm(f.base, { recursive: true, force: true }) }
})
async function fixture() {
  const base = await realpath(await mkdtemp(path.join(os.tmpdir(), "denovo-ledger-test-")))
  const root = path.join(base, id)
  await mkdir(root)
  const plan = { id, session_id: "planning", slots: 1, seed: 123,
    package_hashes: { "pocket.pdb": "a".repeat(64) }, scientific_basis: { hypothesis_sha256: "b".repeat(64) } }
  const request = { ...plan, directory: base, session_id: "execution", planning_session_id: "planning",
    study_id: "study-test", user_message_id: "msg-test" }
  const raw = JSON.stringify(request)
  await Bun.write(path.join(root, "request.json"), raw)
  await Bun.write(path.join(root, "status.json"), JSON.stringify({ id, state: "AWAITING_APPROVAL" }))
  const expected = { directory: base, study: "study-test", request: plan }
  return { base, root, raw, expected }
}

test("task recovery preserves identity and distinguishes rejection, approval and zero candidates", async () => {
  const f = await fixture()
  try {
    expect((await readDeNovoTask(f.base, id, f.expected))?.approval_verified).toBe(false)
    await Bun.write(path.join(f.root, "status.json"), JSON.stringify({ id, state: "CANCELLED" }))
    expect((await readDeNovoTask(f.base, id, f.expected))?.state).toBe("CANCELLED")
    // Synthetic receipt tests the reader only; no permission or worker is invoked.
    await Bun.write(path.join(f.root, "authorization.json"), JSON.stringify({ request_sha256: sha(f.raw),
      session_id: "execution", user_message_id: "msg-test", mechanism: "native_remote_compute" }))
    await Bun.write(path.join(f.root, "status.json"), JSON.stringify({ id, state: "COMPLETED",
      summary: { requested_slots: 1, raw: { pass_count: 0 }, relaxed: { pass_count: 0 } } }))
    const recovered = await readDeNovoTask(f.base, id, f.expected)
    expect(recovered?.approval_verified).toBe(true)
    expect(recovered).toHaveProperty("summary.raw.pass_count", 0)
    expect(await readDeNovoTask(f.base, id, f.expected)).toEqual(recovered)
    for (const count of [-1, 0.5, 2]) {
      await Bun.write(path.join(f.root, "status.json"), JSON.stringify({ id, state: "COMPLETED",
        summary: { requested_slots: 1, raw: { pass_count: count }, relaxed: { pass_count: 0 } } }))
      await expect(readDeNovoTask(f.base, id, f.expected)).rejects.toThrow()
    }
    // Raw and relaxed are overlapping populations, not additive.
    await Bun.write(path.join(f.root, "status.json"), JSON.stringify({ id, state: "COMPLETED",
      summary: { requested_slots: 1, raw: { pass_count: 1 }, relaxed: { pass_count: 1 } } }))
    expect(await readDeNovoTask(f.base, id, f.expected)).toHaveProperty("summary.relaxed.pass_count", 1)
    expect(await readFile(path.join(f.root, "request.json"), "utf8")).toBe(f.raw)
    await expect(readDeNovoTask(f.base, id, { ...f.expected, session: "other" })).rejects.toThrow("another session")
    await expect(readDeNovoTask(f.base, id, { ...f.expected, study: "other" })).rejects.toThrow("another study")
    await expect(readDeNovoTask(f.base, id, { ...f.expected, request: { ...f.expected.request, seed: 999 } })).rejects.toThrow("frozen study")
    await Bun.write(path.join(f.root, "request.json"), f.raw + "\n")
    await expect(readDeNovoTask(f.base, id, f.expected)).rejects.toThrow("approval differs")
  } finally { await rm(f.base, { recursive: true, force: true }) }
})

test("partial reservation is unknown; absent task is distinct; symlinks and unapproved execution reject", async () => {
  const f = await fixture()
  try {
    await Bun.write(path.join(f.root, "status.json"), JSON.stringify({ id, state: "GENERATING" }))
    await expect(readDeNovoTask(f.base, id, f.expected)).rejects.toThrow("no matching approval")
    await rm(path.join(f.root, "request.json"))
    expect((await readDeNovoTask(f.base, id, f.expected))?.state).toBe("OUTCOME_UNKNOWN")
    await symlink(path.join(f.root, "status.json"), path.join(f.root, "request.json"))
    await expect(readDeNovoTask(f.base, id, f.expected)).rejects.toThrow("regular files")
    await rm(f.root, { recursive: true })
    expect(await readDeNovoTask(f.base, id, f.expected)).toBeNull()
  } finally { await rm(f.base, { recursive: true, force: true }) }
})
