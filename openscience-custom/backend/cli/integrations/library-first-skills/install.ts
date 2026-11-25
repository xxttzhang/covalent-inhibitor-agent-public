import path from "node:path"
import { cp, mkdir, readdir, writeFile } from "node:fs/promises"

// Native skill authoring invalidates only the skill cache; no server restart or
// global configuration replacement is necessary.
const directory = path.resolve(import.meta.dir, "../../../..")
const suffix = "?directory=" + encodeURIComponent(directory)
const base = "http://127.0.0.1:4096"
const root = path.join(import.meta.dir, "skills")
const read = async (route: string) => {
  const response = await fetch(base + route + suffix)
  if (!response.ok) throw Error("Platform read failed: " + route + " " + response.status)
  return response.json()
}
const prior = await read("/skill")
const receipts = []
for (const name of (await readdir(root)).sort()) {
  const existing = prior.find((item: { name: string }) => item.name === name)
  if (existing && (existing.origin !== "user" || !existing.location.includes("/user-skills/" + name + "/")))
    throw Error("Skill name owned by another source: " + name)
  const content = await Bun.file(path.join(root, name, "SKILL.md")).text()
  const response = await fetch(base + "/skill/" + name + suffix, { method: "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }) })
  if (!response.ok) throw Error("Native skill install failed: " + name + " " + await response.text())
  const receipt = await response.json()
  const folder = path.dirname(receipt.location)
  if (receipt.name !== name || receipt.origin !== "user" || path.basename(folder) !== name)
    throw Error("Unexpected native skill location")
  for (const resource of await readdir(path.join(root, name), { withFileTypes: true })) {
    if (resource.name === "SKILL.md") continue
    if (!resource.isDirectory() || resource.name !== "references") throw Error("Unexpected resource")
    await cp(path.join(root, name, resource.name), path.join(folder, resource.name), { recursive: true })
  }
  if (await Bun.file(receipt.location).text() !== content) throw Error("Skill readback mismatch")
  receipts.push(receipt)
}
const after = await read("/skill")
for (const receipt of receipts) {
  const actual = after.find((item: { name: string }) => item.name === receipt.name)
  if (actual?.location !== receipt.location) throw Error("Installed skill not discovered: " + receipt.name)
  const content = await Bun.file(actual.location).text()
  for (const match of content.matchAll(/\]\(([^)]+\.md)\)/g)) {
    if (!(await Bun.file(path.resolve(path.dirname(actual.location), match[1])).exists())) throw Error("Installed reference missing")
  }
}
const report = path.resolve(directory, "../reports/library-first-skills")
await mkdir(report, { recursive: true })
const result = { installed_at: new Date().toISOString(), directory, receipts, discovered: receipts.length,
  total_skills: after.length, restarted: false, scientific_jobs_submitted: 0 }
// Keep earlier deployment receipts intact when the same maintained skills are updated.
await writeFile(path.join(report, "installation-" + result.installed_at.replace(/[:.]/g, "-") + ".json"),
  JSON.stringify(result, null, 2), { flag: "wx" })
console.log(JSON.stringify(result, null, 2))
