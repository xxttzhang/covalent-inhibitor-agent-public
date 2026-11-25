import { expect, test } from "bun:test"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { Storage } from "../../src/storage/storage"
import { Identifier } from "../../src/id/id"
import { ProjectRoutes } from "../../src/server/routes/project"
import { workflowObservations } from "@synsci/util/discovery-workflows"
import { tmpdir } from "../fixture/fixture"

const workflowID = "ligand-flow-" + "a".repeat(24)
const entry = "science_covalent_ligand_workflow"

async function record(sessionID: string, output: string, tool = entry, role = "assistant") {
  const id = Identifier.ascending("message")
  await Storage.write(["message", sessionID, id], { id, sessionID, role, time: { created: Date.now() } })
  const part = Identifier.ascending("part")
  await Storage.write(["part", id, part], {
    id: part,
    sessionID,
    messageID: id,
    type: "tool",
    tool,
    state: { status: "completed", output, time: { start: 100, end: 200 } },
  })
}

function receipt(revision: number, id = workflowID) {
  return JSON.stringify({
    schema_version: "research-workflow-result-v1",
    route: "ligand-first",
    workflow: { protocol: "ligand-workflow-v1", workflow_id: id, stage: "PARENT_REVIEW", revision },
    target: { target: "EGFR", site: "C797" },
    execution: { state: "not_submitted" },
    covdock_execution: { run_id: "existing-lo-task", job_state: "RUNNING", path: "/private/not-for-the-browser" },
    refinement_input: { source_kind: "sealed-ligand-direct-lo-v1", generated_candidate_count: 4, input_count: 2,
      selected_candidate_ids: ["candidate-a", "candidate-b"],
      candidate_deferrals: ["candidate-c", "candidate-d"].map(candidate_id => ({ candidate_id,
        reason: "UNRESOLVED_STEREOCHEMISTRY", path: "/private/not-for-the-browser" })),
      candidate_table: "/private/not-for-the-browser" },
    artifacts: [{ kind: "evidence", id: "test-evidence", path: "/private/not-for-the-browser" }],
    credentials: "must-not-be-returned",
  })
}

test("workbench reads only bound tool receipts and exposes no arbitrary output or paths", async () => {
  await using tmp = await tmpdir()
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      const session = await Session.create({ title: "Parent comparison" })
      const key = ["ligand-workflow-bindings", Instance.project.id, session.id]
      try {
        await Storage.write(key, { entry, workflow_id: workflowID })
        await record(session.id, receipt(3))
        await record(session.id, receipt(2)) // Late older observation must not regress the view.
        await record(session.id, receipt(99), "other_covalent_ligand_workflow")
        await record(session.id, receipt(100, "ligand-flow-" + "b".repeat(24)))
        await record(session.id, receipt(101), entry, "user")
        await record(session.id, "not JSON")
        const response = await ProjectRoutes().request("/workflows")
        expect(response.status).toBe(200)
        expect(response.headers.get("cache-control")).toBe("no-store")
        const text = await response.text()
        const value = workflowObservations.parse(JSON.parse(text))
        expect(value.projectID).toBe(Instance.project.id)
        expect(value.workflows).toHaveLength(1)
        expect(value.workflows[0]).toMatchObject({
          workflowID,
          sessionID: session.id,
          active: true,
          observed: {
            revision: 3,
            stage: "PARENT_REVIEW",
            target: "EGFR",
            site: "C797",
            execution: "not_submitted",
            task: { runID: "existing-lo-task", state: "RUNNING" },
            artifacts: 1,
            candidates: { generated: 4, admitted: 2, deferred: [
              { id: "candidate-c", reason: "UNRESOLVED_STEREOCHEMISTRY" },
              { id: "candidate-d", reason: "UNRESOLVED_STEREOCHEMISTRY" },
            ] },
          },
        })
        expect(text).not.toContain("not-for-the-browser")
        expect(text).not.toContain("must-not-be-returned")
        const invalid = JSON.parse(receipt(4))
        invalid.refinement_input.input_count = 4
        await record(session.id, JSON.stringify(invalid))
        const next = workflowObservations.parse(await (await ProjectRoutes().request("/workflows")).json())
        expect(next.workflows[0]?.observed?.revision).toBe(4)
        expect(next.workflows[0]?.observed?.candidates).toBeUndefined()
      } finally {
        await Storage.remove(key)
        await Session.remove(session.id)
        await Instance.dispose()
      }
    },
  })
})

test("missing observations remain unknown and orphan or foreign bindings are not listed", async () => {
  await using first = await tmpdir()
  await using second = await tmpdir()
  await Instance.provide({
    directory: first.path,
    fn: async () => {
      const session = await Session.create({})
      const key = ["ligand-workflow-bindings", Instance.project.id, session.id]
      const orphan = ["ligand-workflow-bindings", Instance.project.id, Identifier.descending("session")]
      try {
        await Storage.write(key, { entry, workflow_id: workflowID, active: false })
        await Storage.write(orphan, { entry, workflow_id: "ligand-flow-" + "c".repeat(24) })
        const response = await ProjectRoutes().request("/workflows")
        const local = workflowObservations.parse(await response.json())
        expect(local.workflows).toHaveLength(1)
        expect(local.workflows[0].active).toBe(false)
        expect(local.workflows[0].observed).toBeUndefined()
        await Instance.provide({
          directory: second.path,
          fn: async () => {
            const response = await ProjectRoutes().request("/workflows")
            expect(workflowObservations.parse(await response.json()).workflows).toEqual([])
            await Instance.dispose()
          },
        })
      } finally {
        await Storage.remove(key)
        await Storage.remove(orphan)
        await Session.remove(session.id)
        await Instance.dispose()
      }
    },
  })
})
