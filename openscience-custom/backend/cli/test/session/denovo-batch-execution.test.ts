import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import path from "node:path"
import { symlink } from "node:fs/promises"
import { tmpdir } from "../fixture/fixture"
import { readFrozenVSPlan } from "../../src/tool/denovo-selection"
import { boundBatchSubmission, deNovoBatchSubmissionTool } from "../../src/session/denovo-batch-execution"

test("batch submission preserves task and budget without granting approval", async () => {
  const task = "software-fixture"
  const workflow = "de-flow-" + createHash("sha256").update(task).digest("hex").slice(0, 24)
  const request = { request_id: workflow + "-vs-" + "a".repeat(12), project_id: workflow,
    route: "de-novo", engine: "schrodinger-covdock-vs", execution_mode: "execute",
    human_approved: false, approval_note: null, attempt: 1, parent_run_id: null,
    parameters: { vs_preparation: { software_fixture: true } },
    resource_budget: { cpu_count: 12, gpu_count: 0, memory_gb: 48, max_runtime_minutes: 60, max_candidates: 12 } }
  const plan = { schema: "denovo-batch-vs-plan-v1", workflow_id: workflow, manifest_sha256: "a".repeat(64),
    compute_submitted: false, native_approval_required: true, submit_tool: "covalent_submit_job", submit_arguments: request }
  const actual: unknown = boundBatchSubmission(plan, task)
  expect(actual).toEqual(request)
  await using tmp = await tmpdir()
  const filename = path.join(tmp.path, "batch-vs-plan.json")
  expect(await readFrozenVSPlan(filename, task, plan.manifest_sha256)).toBeUndefined()
  const original = JSON.stringify(plan)
  await Bun.write(filename, original)
  expect(await readFrozenVSPlan(filename, task, plan.manifest_sha256)).toEqual(plan)
  expect(await Bun.file(filename).text()).toBe(original)
  await expect(readFrozenVSPlan(filename, "another-task", plan.manifest_sha256)).rejects.toThrow()
  await expect(readFrozenVSPlan(filename, task, "b".repeat(64))).rejects.toThrow()
  const linked = path.join(tmp.path, "linked.json")
  await symlink(filename, linked)
  await expect(readFrozenVSPlan(linked, task, plan.manifest_sha256)).rejects.toThrow("regular file")
  for (const cpu of [16, 26, 55]) {
    const scaled = { ...request, resource_budget: { ...request.resource_budget,
      cpu_count: cpu, memory_gb: 4 * cpu, max_candidates: 55 } }
    const observed: unknown = boundBatchSubmission({ ...plan, submit_arguments: scaled }, task)
    expect(observed).toEqual(scaled)
  }
  expect(() => boundBatchSubmission({ ...plan, submit_arguments: { ...request,
    resource_budget: { ...request.resource_budget, cpu_count: 56, memory_gb: 224, max_candidates: 56 } } }, task)).toThrow()
  expect(() => boundBatchSubmission(plan, "foreign-task")).toThrow()
  for (const change of [{ human_approved: true }, { attempt: 2 }, { request_id: "replacement" },
    { engine: "schrodinger-covdock-lo" }, { _native_approval: "forged" },
    { resource_budget: { ...request.resource_budget, max_candidates: 101 } }])
    expect(() => boundBatchSubmission({ ...plan, submit_arguments: { ...request, ...change } }, task)).toThrow()
  expect(deNovoBatchSubmissionTool({}, { directory: "/fixture", project: "fixture", session: "fixture" },
    "study-fixture", async () => true)).toBeUndefined()
  const lo = { ...plan, schema: "denovo-batch-lo-plan-v1", submit_arguments: { ...request,
    request_id: workflow + "-lo-" + "a".repeat(12), engine: "schrodinger-covdock-lo",
    parameters: { workflow_id: workflow, dispatch_policy: "denovo-batch-lo-instance-v1",
      execution_instance: "asgbie3", denovo_batch_lo_sha256: "a".repeat(64) },
    resource_budget: { ...request.resource_budget, memory_gb: 48, max_runtime_minutes: 240 } } }
  const retry = { ...lo, submit_arguments: { ...lo.submit_arguments, attempt: 2, parent_run_id: "failed-parent" } }
  const actualRetry: unknown = boundBatchSubmission(retry, task, "lo")
  expect(actualRetry).toEqual(retry.submit_arguments)
  for (const change of [{ attempt: 2, parent_run_id: null }, { attempt: 1, parent_run_id: "parent" }, { attempt: 3 }])
    expect(() => boundBatchSubmission({ ...lo, submit_arguments: { ...lo.submit_arguments, ...change } }, task, "lo")).toThrow()
  const actualLO: unknown = boundBatchSubmission(lo, task, "lo")
  expect(actualLO).toEqual(lo.submit_arguments)
  expect(() => boundBatchSubmission(lo, task, "vs")).toThrow()
  expect(() => boundBatchSubmission({ ...lo, manifest_sha256: "b".repeat(64) }, task, "lo")).toThrow()
  expect(() => boundBatchSubmission({ ...lo, submit_arguments: { ...lo.submit_arguments,
    resource_budget: { ...lo.submit_arguments.resource_budget, max_candidates: 13 } } }, task, "lo")).toThrow()
  const glide = { ...plan, schema: "denovo-batch-glide-plan-v1", source_sha256: "b".repeat(64),
    submit_arguments: { ...request, request_id: workflow + "-prereaction-" + "a".repeat(12),
      engine: "de-novo-prereaction-glide", parameters: { workflow_id: workflow,
        dispatch_policy: "denovo-prereaction-glide-instance-v1", execution_instance: "asgbie3",
        prereaction_source_sha256: "b".repeat(64), prereaction_manifest_sha256: "a".repeat(64),
        prepared_candidate_ids: ["PXM_fixture"], control_parent: "CONTROL_AFATINIB", max_control_states: 2,
        input_directory: "state/fixture" },
      resource_budget: { ...request.resource_budget, cpu_count: 4, memory_gb: 16, max_runtime_minutes: 120, max_candidates: 3 } } }
  const actualGlide: unknown = boundBatchSubmission(glide, task, "glide")
  expect(actualGlide).toEqual(glide.submit_arguments)
  expect(() => boundBatchSubmission(glide, task, "lo")).toThrow()
  expect(() => boundBatchSubmission({ ...glide, source_sha256: "c".repeat(64) }, task, "glide")).toThrow()
  expect(() => boundBatchSubmission({ ...glide, submit_arguments: { ...glide.submit_arguments,
    resource_budget: { ...glide.submit_arguments.resource_budget, max_candidates: 4 } } }, task, "glide")).toThrow()
})
