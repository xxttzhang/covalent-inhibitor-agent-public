import { expect, test } from "bun:test"
import path from "node:path"
import { compileDeNovoRequest } from "../../src/tool/denovo-plan"
import { denovoRequest } from "../../src/session/denovo-entry"

const workspace = path.resolve(import.meta.dir, "../../../../..")
const profile = await Bun.file(path.join(workspace, "routes/de-novo/registry/egfr_c797_v1/profile.json")).json()
const input = { id: "denovo-0123456789abcdef01234567", profile: "egfr-c797-acrylamide-v1", slots: 1,
  seed: 123, session: "ses_plan_test", message: "msg_plan_test", directory: profile.allowed_directories[0],
  text: "针对 EGFR C797，固定丙烯酰胺，用 PocketXMol 从头生成 1 个候选分子。",
  package: path.join(workspace, "routes/de-novo/registry/egfr_c797_v1"),
  worker: path.join(workspace, "routes/de-novo/runtime/worker.py") }

test("real generation package compiles explicit slots and identity without launching", async () => {
  const first = await compileDeNovoRequest(input)
  const second = await compileDeNovoRequest({ ...input, id: "denovo-1123456789abcdef01234567", slots: 2, seed: 456 })
  expect(first.slots).toBe(1)
  expect(second.slots).toBe(2)
  expect(second.seed).toBe(456)
  expect(second.id).not.toBe(first.id)
  expect(first.package_hashes["profile.json"]).toMatch(/^[a-f0-9]{64}$/)
  expect(first.worker_sha256).toMatch(/^[a-f0-9]{64}$/)
  expect(first.session_id).toBe(input.session)
  expect(first).not.toHaveProperty("approved")
  expect(first).not.toHaveProperty("candidates")
})

test("actual native admission and compiler never substitute a different target or package", async () => {
  expect(denovoRequest(input.text)?.slots).toBe(1)
  expect(denovoRequest(input.text.replace("EGFR C797", "BTK C481"))).toBeUndefined()
  await expect(compileDeNovoRequest({ ...input, profile: "btk-c481" })).rejects.toThrow("differs")
  await expect(compileDeNovoRequest({ ...input, slots: 97 })).rejects.toThrow("limits")
  await expect(compileDeNovoRequest({ ...input, directory: "/unregistered" })).rejects.toThrow("no registered")
})
