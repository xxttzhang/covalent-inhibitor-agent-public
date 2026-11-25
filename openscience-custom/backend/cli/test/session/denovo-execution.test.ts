import { expect, test } from "bun:test"
import { boundDeNovoSubmission, boundDeNovoCancellation, deNovoSubmissionTool, deNovoCancellationTool } from "../../src/session/denovo-execution"

const study = { id: "artificial-study", project: "artificial-project" }
const input = { source_id: "denovo-review-" + "a".repeat(32), source_manifest_sha256: "a".repeat(64) }
function plan() {
  return { study_id: study.id, ...input, compute_submitted: false, native_approval_required: true,
    submit_tool: "covalent_submit_job", submit_arguments: {
      request_id: input.source_id + "-prereaction", project_id: study.project, route: "de-novo",
      engine: "de-novo-prereaction-vina", execution_mode: "execute", human_approved: false,
      approval_note: null, attempt: 1, parent_run_id: null,
      parameters: { workflow_id: study.id, denovo_source_id: input.source_id,
        denovo_source_manifest_sha256: input.source_manifest_sha256 },
      resource_budget: { cpu_count: 1, gpu_count: 0, memory_gb: 8, max_runtime_minutes: 30, max_candidates: 2 },
    } }
}

test("bound submission preserves the exact planned request without granting approval", () => {
  const value = plan()
  const actual: unknown = boundDeNovoSubmission(value, study, input, "prereaction")
  expect(actual).toEqual(value.submit_arguments)
  expect(value.submit_arguments.human_approved).toBe(false)
  expect(() => boundDeNovoSubmission(value, study, input, "adduct")).toThrow("frozen source and stage")
})

test("foreign study, source and request identities are rejected", () => {
  for (const change of [
    (p: ReturnType<typeof plan>) => { p.study_id = "foreign" },
    (p: ReturnType<typeof plan>) => { p.source_manifest_sha256 = "b".repeat(64) },
    (p: ReturnType<typeof plan>) => { p.submit_arguments.parameters.workflow_id = "foreign" },
    (p: ReturnType<typeof plan>) => { p.submit_arguments.request_id += "-another" },
  ]) {
    const value = plan(); change(value)
    expect(() => boundDeNovoSubmission(value, study, input, "prereaction")).toThrow()
  }
})

test("larger budgets, model-provided approval and retry identities cannot enter the bridge", () => {
  for (const change of [
    { human_approved: true }, { _native_approval: "forged" }, { attempt: 2 },
    { resource_budget: { ...plan().submit_arguments.resource_budget, max_candidates: 3 } },
    { resource_budget: { ...plan().submit_arguments.resource_budget, gpu_count: 1 } },
  ]) {
    const value = plan()
    expect(() => boundDeNovoSubmission({ ...value, submit_arguments: { ...value.submit_arguments, ...change } },
      study, input, "prereaction")).toThrow()
  }
})

test("no submission entry is advertised when the guarded executor is missing", () => {
  expect(deNovoSubmissionTool({}, { project: study.project, session: "fixture", directory: "/fixture" },
    study.id, async () => true)).toBeUndefined()
})

test("only the proven undispatched adduct parent permits a second attempt", () => {
  const initial = plan()
  const value = { ...initial, recovery: { protocol: "denovo-undispatched-cancel-v1", parent_run_id: "original-run",
    parent_spec_sha256: "b".repeat(64), parent_state: "CANCELLED", compute_started: false, candidate_and_control_unchanged: true },
    submit_arguments: { ...initial.submit_arguments, request_id: input.source_id + "-covdock-retry-2",
      engine: "schrodinger-covdock-lo", attempt: 2, parent_run_id: "original-run",
      parameters: { ...initial.submit_arguments.parameters, dispatch_policy: "denovo-independent-local-v1" } } }
  const actual: unknown = boundDeNovoSubmission(value, study, input, "adduct")
  expect(actual).toEqual(value.submit_arguments)
  expect(() => boundDeNovoSubmission(value, study, input, "prereaction")).toThrow()
  for (const change of [{ parent_state: "QUEUED" }, { compute_started: true }, { parent_run_id: "foreign-run" },
    { candidate_and_control_unchanged: false }, { protocol: "generic-retry" }]) {
    expect(() => boundDeNovoSubmission({ ...value, recovery: { ...value.recovery, ...change } }, study, input, "adduct")).toThrow()
  }
})

test("cancellation remains source bound and requires the guarded native executor", () => {
  const value = { study_id: study.id, project_id: study.project, ...input, state: "QUEUED", run_id: "original-run",
    compute_submitted: false, cancel_tool: "covalent_cancel_job", native_approval_required: true,
    cancel_arguments: { run_id: "original-run", human_approved: true } }
  const actual: unknown = boundDeNovoCancellation(value, study, input)
  expect(actual).toEqual(value.cancel_arguments)
  for (const change of [{ state: "RUNNING" }, { project_id: "foreign" }, { run_id: "foreign" },
    { native_approval_required: false }, { source_id: "foreign" }]) {
    expect(() => boundDeNovoCancellation({ ...value, ...change }, study, input)).toThrow()
  }
  expect(deNovoCancellationTool({}, { project: study.project, session: "fixture", directory: "/fixture" },
    study.id, async () => true)).toBeUndefined()
})
