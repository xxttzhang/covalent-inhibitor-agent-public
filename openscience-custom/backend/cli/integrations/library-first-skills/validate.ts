import path from "node:path"
import { readdir } from "node:fs/promises"
import { ConfigMarkdown } from "../../src/config/markdown"
import { Skill } from "../../src/skill/skill"
import { schema } from "../inspire-screening/executor/tool"
import { querySchema } from "../inspire-covindb/server"
import { MolecularDynamicsTool } from "../../src/tool/molecular-dynamics"
import { capability } from "../inspire-screening/executor/mcp"
import { runtimeRegexPass, classifierInjectionRegexPass } from "../../src/skill/install/review"

const root = path.join(import.meta.dir, "skills")
const names = (await readdir(root)).sort()
const checked = []
for (const name of names) {
  const filename = path.join(root, name, "SKILL.md")
  const parsed = await ConfigMarkdown.parse(filename)
  const info = Skill.Info.pick({ name: true, description: true, category: true, tags: true }).parse(parsed.data)
  if (info.name !== name) throw Error("Skill name/directory mismatch")
  const content = await Bun.file(filename).text()
  const entry = { namespace: "user", name, description: info.description, content, scripts: [], references: [] }
  const rejected = [...runtimeRegexPass([entry]).rejected, ...classifierInjectionRegexPass([entry]).rejected]
  if (rejected.length) throw Error(JSON.stringify(rejected))
  for (const match of content.matchAll(/\]\(([^)]+\.md)\)/g)) {
    if (!(await Bun.file(path.resolve(path.dirname(filename), match[1])).exists())) throw Error("Broken reference " + match[1])
  }
  checked.push(name)
}
const examples = await Bun.file(path.join(import.meta.dir, "examples.json")).json()
const md = await MolecularDynamicsTool.init()
for (const example of examples) {
  const parser = example.tool === "inspire_screening_task" ? schema :
    example.tool === "inspire_screening_capabilities" ? capability.schema :
    example.tool === "molecular_dynamics" ? md.parameters : querySchema
  const result = parser.safeParse(example.arguments)
  if (result.success !== example.valid) throw Error("Example mismatch: " + example.name)
}
console.log(JSON.stringify({ skills: checked, schema_examples: examples.length, scientific_jobs_submitted: 0, passed: true }, null, 2))
