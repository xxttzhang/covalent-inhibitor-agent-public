import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { generationState, registeredBatchState } from "../../src/session/denovo-continuation-compute"
import type { DeNovoContinuation } from "../../src/session/denovo-continuation"

test("generation observation preserves unknown outcomes and rejects unknown states", () => {
  expect(generationState("GENERATING")).toBe("RUNNING")
  expect(generationState("COMPLETED")).toBe("COMPLETED")
  expect(generationState("OUTCOME_UNKNOWN")).toBe("INTERRUPTED")
  expect(generationState("NEEDS_REVIEW")).toBe("INTERRUPTED")
  expect(() => generationState("SUCCESS")).toThrow()
})

test("batch observation requires the original run, request, route and engine", () => {
  const saved: DeNovoContinuation = { study: "study-test", stage: "lo", task: "generation-original",
    request: "lo-original", state: "waiting", messageID: "msg_test", next: 0, expires: 1000 }
  const project = "de-flow-" + createHash("sha256").update(saved.task).digest("hex").slice(0, 24)
  const job = { run_id: "run-original", project_id: project, route: "de-novo",
    engine: "schrodinger-covdock-lo", state: "RUNNING" }
  const spec = { ...job, request_id: saved.request, execution_mode: "execute" }
  const output = { status: "PASS", spec, job }
  expect(registeredBatchState(saved, job.run_id, output)).toBe("RUNNING")
  expect(registeredBatchState(saved, job.run_id, { ...output, job: { ...job, state: "FAILED" } })).toBe("FAILED")
  expect(registeredBatchState(saved, job.run_id, { ...output,
    worker_observation: { reconciliation_required: true } })).toBe("RECONCILE")
  for (const change of [{ run_id: "foreign" }, { request_id: "foreign" },
    { route: "ligand-first" }, { engine: "schrodinger-covdock-vs" }, { execution_mode: "dry-run" }]) {
    expect(() => registeredBatchState(saved, job.run_id, { ...output, spec: { ...spec, ...change } })).toThrow()
  }
  expect(() => registeredBatchState(saved, job.run_id, { ...output, job: { ...job, run_id: "foreign" } })).toThrow()
})
