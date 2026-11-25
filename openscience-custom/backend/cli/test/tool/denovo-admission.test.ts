import { expect, test } from "bun:test"
import { DeNovoTool } from "../../src/tool/denovo"
import z from "zod"
import { jsonSchema, type Tool, type JSONSchema7 } from "ai"
import { projectResearchNativeTools } from "../../src/session/research-workflows"

test("model schema hides legacy create while retaining frozen execution and historical read actions", async () => {
  const tool = await DeNovoTool.init()
  const original = z.toJSONSchema(tool.parameters) as unknown as JSONSchema7
  const native: Record<string, Tool> = { denovo_generate: { description: tool.description, inputSchema: jsonSchema(original) } }
  const projected = projectResearchNativeTools(native)
  const schema = (projected.denovo_generate.inputSchema as { jsonSchema: typeof original }).jsonSchema
  expect(JSON.stringify(schema)).not.toContain('"const":"create"')
  for (const action of ["workflow", "plan_study", "create_study", "retry_study", "status", "results"])
    expect(JSON.stringify(schema)).toContain(`"const":"${action}"`)
  expect(tool.parameters.safeParse({ action: "create" }).success).toBe(true)
  expect(JSON.stringify(original)).toContain('"const":"create"')
  expect(projected.denovo_generate.description).not.toContain("Legacy create")
})

test("legacy create cannot reserve, approve or launch a task without a scientific study", async () => {
  const tool = await DeNovoTool.init()
  const result = await tool.execute({ action: "create" }, {
    sessionID: "ses_no_science", messageID: "msg_no_science", agent: "build",
    abort: new AbortController().signal, messages: [],
    metadata() { throw Error("Legacy rejection must not create progress") },
    async ask() { throw Error("Legacy rejection must not request compute approval") },
  })
  expect(result.metadata.state).toBe("NEEDS_SCIENTIFIC_DECISION")
  expect(result.metadata.compute_submitted).toBe(false)
  expect(result.metadata).not.toHaveProperty("task_id")
})
