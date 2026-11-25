import { expect, test } from "bun:test"
import { ligandCancellationTask, ligandCancellationTools } from "../../src/session/ligand-cancellation"
import { Storage } from "../../src/storage/storage"

const workflow = "ligand-flow-" + "a".repeat(24)
function observation() {
  return { structuredContent: { route: "ligand-first", workflow: { workflow_id: workflow }, pending: null,
    vs_execution: { run_id: "synthetic-vs-run", request_id: "synthetic-vs-request", job_state: "QUEUED",
      job_identity_verified: true },
    covdock_execution: { run_id: "synthetic-lo-run", request_id: "synthetic-lo-request", job_state: "RUNNING",
      approval: "APPROVED" } } }
}

test("selects distinct existing VS and LO identities from the bound workflow", () => {
  expect(ligandCancellationTask(observation(), workflow, "vs").run_id).toBe("synthetic-vs-run")
  expect(ligandCancellationTask(observation(), workflow, "covdock").run_id).toBe("synthetic-lo-run")
})

test("foreign route/workflow, pending operation and invalid evidence fail closed", () => {
  const original = observation()
  for (const change of [
    { route: "library-first" }, { workflow: { workflow_id: "ligand-flow-" + "b".repeat(24) } },
    { pending: { action: "start_vs" } }, { vs_execution: null },
    { vs_execution: { ...original.structuredContent.vs_execution, job_identity_verified: false } },
    { vs_execution: { ...original.structuredContent.vs_execution, evidence_error: "changed source" } },
    { vs_execution: { ...original.structuredContent.vs_execution, run_id: "../../foreign" } },
    { vs_execution: { ...original.structuredContent.vs_execution, job_state: "UNKNOWN" } },
  ]) expect(() => ligandCancellationTask({ structuredContent: { ...original.structuredContent, ...change } }, workflow, "vs")).toThrow()
  expect(() => ligandCancellationTask({ ...original, isError: true }, workflow, "vs")).toThrow()
  expect(() => ligandCancellationTask({ structuredContent: { ...original.structuredContent,
    covdock_execution: { ...original.structuredContent.covdock_execution, approval: "PENDING" } } }, workflow, "covdock")).toThrow()
})

test("unbound, suspended and unavailable servers expose no task-management tools", async () => {
  const scope = { project: "ligand-cancellation-test", session: "ses-" + crypto.randomUUID() }
  const key = ["ligand-workflow-bindings", scope.project, scope.session]
  try {
    expect(await ligandCancellationTools({}, scope, async () => true)).toEqual({})
    await Storage.write(key, { entry: "science_covalent_ligand_workflow", workflow_id: workflow, active: false })
    expect(await ligandCancellationTools({}, scope, async () => true)).toEqual({})
    await Storage.write(key, { entry: "science_covalent_ligand_workflow", workflow_id: workflow, active: true })
    expect(await ligandCancellationTools({}, scope, async () => true)).toEqual({})
  } finally { await Storage.remove(key) }
})

test("active Ligand route exposes the bound adapter and preserves approval denial", async () => {
  const { researchWorkflowTools } = await import("../../src/session/research-workflows")
  const { guardCovalentExecution } = await import("../../src/session/covalent-execution")
  const { jsonSchema } = await import("ai")
  const scope = { project: "ligand-cancellation-test", session: "ses-" + crypto.randomUUID(), text: "" }
  const key = ["ligand-workflow-bindings", scope.project, scope.session]
  const entry = "science_covalent_ligand_workflow"
  const raw = "science_covalent_cancel_job"
  const inputSchema = jsonSchema({ type: "object" })
  const options = { toolCallId: "cancel-test", messages: [] }
  const value = observation()
  const guarded = guardCovalentExecution({
    [entry]: { inputSchema, execute: async () => ({ structuredContent: {
      ...value.structuredContent,
      workflow: { workflow_id: workflow, protocol: "ligand-workflow-v1", revision: 81,
        stage: "VS_EXECUTION_RUNNING", tools: { covalent_ligand_workflow: { actions: ["status"], fields: ["action"] } } },
    } }) },
    [raw]: { inputSchema, execute: async () => { throw Error("unexpected raw execution") } },
  }, async (name, args) => {
    expect(name).toBe(raw)
    expect(args).toEqual({ run_id: "synthetic-vs-run", human_approved: true })
    throw Error("approval denied by fixture")
  })
  try {
    await Storage.write(key, { entry, workflow_id: workflow, active: true })
    const { armLigandContinuation } = await import("../../src/session/ligand-continuation")
    await armLigandContinuation(scope, { entry, workflow_id: workflow,
      run_id: "synthetic-vs-run", request_id: "existing-request" })
    const { tools } = await researchWorkflowTools(guarded, scope)
    const status = await tools.ligand_continuation.execute!({ action: "status" }, options) as {
      content: { type: string; text: string }[]; structuredContent: unknown }
    expect(status.content[0].type).toBe("text")
    expect(JSON.parse(status.content[0].text)).toEqual(status.structuredContent)
    expect(status.structuredContent).toMatchObject({ compute_submitted: false,
      continuation: { state: "waiting", run_id: "synthetic-vs-run" } })

    expect(tools[raw]).toBeUndefined()
    await expect(tools.ligand_cancel_task.execute!({ stage: "vs" }, options)).rejects.toThrow("approval denied by fixture")
    await Storage.write(key, { entry, workflow_id: workflow, active: false })
    await expect(tools.ligand_cancel_task.execute!({ stage: "vs" }, options)).rejects.toThrow("Research route changed")
  } finally {
    await Storage.remove(key)
    await Storage.remove(["ligand-continuations", scope.project, scope.session])
  }
})

test("Ligand activation hides host tools and rejects previously queued Bash", async () => {
  const { projectResearchNativeTools, guardResearchNativeTools } = await import("../../src/session/research-workflows")
  const { jsonSchema } = await import("ai")
  const scope = { project: "ligand-tool-boundary-test", session: "ses-" + crypto.randomUUID() }
  const key = ["ligand-workflow-bindings", scope.project, scope.session]
  const inputSchema = jsonSchema({ type: "object" })
  const tools = {
    bash: { inputSchema, execute: async () => "executed" },
    grep: { inputSchema },
    question: { inputSchema },
  }
  const queued = guardResearchNativeTools(projectResearchNativeTools(tools), scope)
  const options = { toolCallId: "boundary-test", messages: [] }
  try {
    expect(await queued.bash.execute!({}, options)).toBe("executed")
    await Storage.write(key, { entry: "science_covalent_ligand_workflow", workflow_id: workflow, active: true })
    expect(Object.keys(projectResearchNativeTools(tools, "ligand-first"))).toEqual(["question"])
    await expect(queued.bash.execute!({}, options)).rejects.toThrow("A research workflow is active")
  } finally { await Storage.remove(key) }
})
