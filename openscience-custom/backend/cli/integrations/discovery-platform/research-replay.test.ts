import { afterAll, expect, test } from "bun:test"
import { mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { ResearchLoopStore } from "./research-loop-store"
import { emptyResources, researchContract } from "./research-loop-contract"
import { importHistoricalResearchReplay } from "./research-replay"

const scratch = await mkdtemp(path.join(tmpdir(), "research-replay-"))
const workspace = path.resolve(import.meta.dir, "../../../../..")
const scope = { directory: workspace, session: "offline-replay-test", project: "offline-audit" }
afterAll(() => rm(scratch, { recursive: true, force: true }))

test("real historical import persists across store restart and does not create duplicate observations", async () => {
  const filename = path.join(scratch, "replay.sqlite")
  const first = new ResearchLoopStore(filename)
  const imported = await importHistoricalResearchReplay(first, scope)
  expect(imported.resumed).toBe(false)
  expect(imported.contract.mode).toBe("offline-replay")
  expect(imported.contract.directory).toBe(workspace)
  expect(imported.contract.session).toBe(scope.session)
  expect(imported.contract.project).toBe(scope.project)
  expect(imported.contract.limits.resources).toEqual(emptyResources())
  expect(imported.contract.limits.max_decisions).toBe(12)
  expect(imported.contract.inputs.subset_sha256).toBeNull()
  expect(imported.contract.target.mapping_sha256).toBeNull()
  expect(imported.contract.quality.manifest_sha256).toBeNull()
  expect(imported.observation.classification).toBe("incomplete")
  expect(imported.observation.origin).toBe("historical_import")
  expect(imported.new_compute).toBe(false)
  expect(imported.model_called).toBe(false)
  first.close()

  const reopened = new ResearchLoopStore(filename)
  const restored = await importHistoricalResearchReplay(reopened, scope)
  expect(restored.resumed).toBe(true)
  expect(restored.loop_id).toBe(imported.loop_id)
  expect(restored.state_revision).toBe(imported.state_revision)
  expect(restored.observation).toEqual(imported.observation)
  expect(restored.events).toEqual(imported.events)
  expect(reopened.evidence(restored.loop_id, "observation")).toHaveLength(1)
  expect(restored.ledger.total).toEqual(emptyResources())
  reopened.close()
})

test("offline zero budgets reject model usage and any scientific compute reservation", async () => {
  const store = new ResearchLoopStore(path.join(scratch, "budgets.sqlite"))
  const imported = await importHistoricalResearchReplay(store, scope)
  const base = { key: "not-authorized", resources: emptyResources(), round: 0, candidate_slots: 0,
    plan_digest: "a".repeat(64), source_release: imported.contract.inputs.source_release }
  expect(() => store.reserve(imported.loop_id, imported.state_revision, { ...base, kind: "model", resources: { ...base.resources, model_tokens: 1 } })).toThrow("budget exceeded")
  expect(() => store.reserve(imported.loop_id, imported.state_revision, { ...base, kind: "compute", round: 1, candidate_slots: 1 })).toThrow("Offline replay")
  expect(researchContract.safeParse({ ...imported.contract, mode: "native-compute" }).success).toBe(false)
  expect(store.reservations(imported.loop_id)).toHaveLength(0)
  store.close()
})

test("different caller sessions receive separate immutable replay contracts", async () => {
  const store = new ResearchLoopStore(path.join(scratch, "scopes.sqlite"))
  const first = await importHistoricalResearchReplay(store, scope)
  const second = await importHistoricalResearchReplay(store, { ...scope, session: "another-offline-session" })
  expect(first.loop_id).not.toBe(second.loop_id)
  expect(first.contract_digest).not.toBe(second.contract_digest)
  expect(store.list(workspace)).toHaveLength(2)
  store.close()
})
