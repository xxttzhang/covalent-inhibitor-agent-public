import path from "node:path"
import { readdir } from "node:fs/promises"
import { ConfigMarkdown } from "../../src/config/markdown"
import { Skill } from "../../src/skill/skill"
import { DeNovoTool } from "../../src/tool/denovo"
import { denovoRequest } from "../../src/session/denovo-entry"
import { runtimeRegexPass, classifierInjectionRegexPass } from "../../src/skill/install/review"

const root = path.join(import.meta.dir, "skills")
const checked = []
for (const name of (await readdir(root)).sort()) {
  const filename = path.join(root, name, "SKILL.md")
  const parsed = await ConfigMarkdown.parse(filename)
  const info = Skill.Info.pick({ name: true, description: true, category: true, tags: true }).parse(parsed.data)
  if (info.name !== name) throw Error("Skill name/directory mismatch")
  const content = await Bun.file(filename).text()
  const references = []
  for await (const file of new Bun.Glob("references/**/*.md").scan(path.dirname(filename)))
    references.push({ path: file, content: await Bun.file(path.join(path.dirname(filename), file)).text() })
  const entry = { namespace: "user", name, description: info.description, content, scripts: [], references }
  const rejected = [...runtimeRegexPass([entry]).rejected, ...classifierInjectionRegexPass([entry]).rejected]
  if (rejected.length) throw Error(JSON.stringify(rejected))
  for await (const file of new Bun.Glob("**/*.md").scan(path.dirname(filename))) {
    const source = path.join(path.dirname(filename), file)
    for (const match of (await Bun.file(source).text()).matchAll(/\]\(([^)]+\.md)\)/g)) {
      if (!(await Bun.file(path.resolve(path.dirname(source), match[1])).exists())) throw Error("Broken reference " + match[1])
    }
  }
  checked.push(name)
}
const examples = await Bun.file(path.join(import.meta.dir, "examples.json")).json()
const tool = await DeNovoTool.init()
for (const example of examples.arguments) {
  if (tool.parameters.safeParse(example.arguments).success !== example.valid) throw Error("Schema mismatch: " + example.name)
}
for (const example of examples.requests) {
  if ((denovoRequest(example.text)?.slots ?? null) !== example.slots) throw Error("Request mismatch: " + example.name)
}
const result = { skills: checked, parameter_examples: examples.arguments.length,
  request_examples: examples.requests.length, scientific_jobs_submitted: 0, passed: true,
  scope: "real platform frontmatter/review, DeNovo parameter schema and original request parser; no tool execution" }
const report = path.resolve(import.meta.dir, "../../../../../reports/design-route-skills-20261009")
await Bun.write(path.join(report, "validation.json"), JSON.stringify(result, null, 2))
console.log(JSON.stringify(result, null, 2))
