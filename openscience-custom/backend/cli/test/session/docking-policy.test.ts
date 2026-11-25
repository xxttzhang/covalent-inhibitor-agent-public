import { expect, test } from "bun:test"
import { jsonSchema } from "ai"
import { dockingPolicy, dockingRoutingSummary, requireDockingEngine, requireDockingTool, requireDockingScope } from "../../src/session/docking-policy"
import { guardCovalentExecution } from "../../src/session/covalent-execution"

test("suspended pose actions fail before approval and MCP invocation", async () => {
  for (const action of ["plan_pose", "check_pose_runtime", "start_pose"]) {
    const key = "science_covalent_ligand_design_step"
    const tools = guardCovalentExecution({ [key]: { inputSchema: jsonSchema({ type: "object" }), execute: async () => { throw Error("MCP reached") } } },
      async () => { throw Error("approval reached") })
    await expect(tools[key].execute!({ action }, { toolCallId: "policy-test", messages: [] }))
      .rejects.toThrow("docking_engine_disabled")
  }
})

test("generic new requests and resolved retry plans cannot bypass policy", () => {
  expect(() => requireDockingTool("science_covalent_submit_job", { engine: "carsidock-cov" })).toThrow("docking_engine_disabled")
  expect(() => requireDockingScope({ spec: { engine: "carsidock-cov", attempt: 2 } })).toThrow("docking_engine_disabled")
  expect(() => requireDockingScope({ jobs: [{ spec: { engine: "carsidock-cov" } }] })).toThrow("docking_engine_disabled")
  expect(() => requireDockingEngine("carsidock-cov", "ligand-first")).toThrow("docking_engine_disabled")
  expect(() => requireDockingEngine("carsidock-cov", "de-novo")).toThrow("docking_engine_disabled")
})

test("library-first allows carsidock-cov while keeping automatic fallback disabled", () => {
  expect(() => requireDockingEngine("carsidock-cov", "library-first")).not.toThrow()
  expect(() => requireDockingTool("science_covalent_submit_job", { engine: "carsidock-cov", route: "library-first" })).not.toThrow()
  expect(() => requireDockingScope({ spec: { route: "library-first", engine: "carsidock-cov" } })).not.toThrow()
  expect(dockingRoutingSummary).toContain("library-first")
})

test("historical reads and independent refinement remain available without automatic fallback", () => {
  for (const action of ["pose_status", "evaluate_pose", "report_pose", "start_covdock", "start_af3"])
    expect(() => requireDockingTool("science_covalent_ligand_design_step", { action })).not.toThrow()
  expect(() => requireDockingScope({ spec: { engine: "schrodinger-covdock-lo" } })).not.toThrow()
  expect(dockingPolicy.automatic_fallback).toBe(false)
  expect(dockingPolicy.native_vs_workflow_ready).toBe(true)
  expect(dockingPolicy.route_policy_modules["ligand-first"]).toBe("covscreen.ligand_docking_policy")
})
