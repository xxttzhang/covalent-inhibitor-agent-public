import path from "node:path"
import { Instance } from "../../src/project/instance"
import { SkillTool } from "../../src/tool/skill"
import { ConfigMarkdown } from "../../src/config/markdown"
import { Skill } from "../../src/skill/skill"

const directory = path.resolve(import.meta.dir, "../../../..")
const report = path.resolve(directory, "../reports/design-route-skills-20261009")
const receipt = await Bun.file(path.join(report, "installation.json")).json()
const loaded: { name: string; chars: number; permission: string[] }[] = []
await Instance.provide({ directory, fn: async () => {
  const tool = await SkillTool.init()
  for (const item of receipt.receipts) {
    const info = await Skill.get(item.name)
    if (info?.location !== item.location) throw Error("Skill resolves to wrong source")
    const permissions: string[] = []
    // Exercise the real SkillTool loader without a model or scientific tool call.
    const result = await tool.execute({ name: item.name }, {
      sessionID: "design-route-skill-validation", messageID: "design-route-skill-validation",
      agent: "research", abort: new AbortController().signal, messages: [], metadata() {},
      async ask(input) {
        if (input.permission !== "skill") throw Error("Unexpected permission request")
        permissions.push(input.permission)
      },
    })
    if (result.metadata.name !== item.name || !result.output.includes((await ConfigMarkdown.parse(item.location)).content.trim())) throw Error("Skill loading failed")
    const source = path.join(import.meta.dir, "skills", item.name)
    for await (const file of new Bun.Glob("**/*.md").scan(source)) {
      const expected = await Bun.file(path.join(source, file)).text()
      if (await Bun.file(path.join(path.dirname(item.location), file)).text() !== expected)
        throw Error("Installed reference differs: " + file)
    }
    loaded.push({ name: item.name, chars: result.output.length, permission: permissions })
  }
} })
const result = { loaded, passed: loaded.length === 8, scientific_jobs_submitted: 0,
  scope: "real SkillTool load and byte-identical installed references; no LLM behavior evaluation" }
await Bun.write(path.join(report, "loading-validation.json"), JSON.stringify(result, null, 2))
console.log(JSON.stringify(result, null, 2))
await Instance.disposeAll()
