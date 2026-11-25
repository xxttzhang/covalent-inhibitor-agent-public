import { expect, test } from "bun:test"
import z from "zod"
import { ExperimentalRoutes } from "../../src/server/routes/experimental"
import { ToolRegistry } from "../../src/tool/registry"
import { Tool } from "../../src/tool/tool"
import { Instance } from "../../src/project/instance"
import { tmpdir } from "../fixture/fixture"
import { schema } from "../../integrations/inspire-screening/executor/tool"

test("tool inspection preserves the executor's model-facing object contract without executing it", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    // Same shape wrapper as plugin registration; execution must never occur
    // when the user only inspects available tools. No live config or compute.
    const parameters = z.object(schema.shape)
    await ToolRegistry.register(Tool.define("inspire_screening_task", {
      description: "Schema-only screening fixture",
      parameters,
      execute: async () => { throw new Error("Inspection must not execute the screening task") },
    }))
    const response = await ExperimentalRoutes().request("/tool?provider=fixture&model=fixture")
    expect(response.status).toBe(200)
    const rows = await response.json()
    const actual = rows.find((row: { id: string }) => row.id === "inspire_screening_task")
    expect(actual.parameters).toEqual(z.toJSONSchema(parameters))
    expect(actual.parameters.type).toBe("object")
    expect(actual.parameters.required).toContain("action")
    expect(actual.parameters.properties.action.enum).toContain("create")
    expect(actual.parameters.properties.library.const).toBe("CovInDB/Virtual_Screening_Library.csv")
    expect(actual.parameters.properties.shortlist.maximum).toBe(20000)
    expect(actual.parameters.properties.selection_policy.properties.max_candidates.maximum).toBe(100)
  } })
})
