// Executed only by the isolated acceptance harness, never by the service.
import path from "node:path"
import { Server } from "../../src/server/server"
import { discoveryProject, routeDefaults } from "@synsci/util/discovery-config"
import { parseResult } from "@synsci/util/discovery-result"
const file = path.join(process.env.OPENSCIENCE_DATA_DIR!, "reuse-check.json")
const app = Server.App()
const request = async (url: string, init?: RequestInit) => {
  const response = await app.request("http://localhost" + url, init)
  if (!response.ok) throw Error(url + " => " + response.status + " " + await response.text())
  return response
}
const existing = await Bun.file(file).exists()
const project = existing ? await Bun.file(file).json() : await (await request("/global/project", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name: "Cold startup acceptance", sources: [] }) })).json()
if (!existing) {
  await Bun.write(file, JSON.stringify(project))
  const config = discoveryProject.parse({ schema: "discovery-project-v1", project: project.id, target: { id: "cold-target", name: "Test", label: "Test" }, routes: Object.fromEntries(Object.entries(routeDefaults).map(([route, settings]) => [route, { ...settings, dataPath: `results/discovery/${route}.json`, inputPath: `.discovery/inbox/${route}.json` }])) })
  await Bun.write(path.join(project.worktree, ".discovery/project.json"), JSON.stringify(config))
  await Bun.write(path.join(project.worktree, ".discovery/inbox/de-novo.json"), JSON.stringify({ schema: "discovery-result-v1", project: project.id, target: "cold-target", route: "de-novo", run: "cold-fixture", collectedAt: new Date().toISOString(), evidenceUpdatedAt: new Date().toISOString(), observation: "local-evidence", current: "", notice: "Acceptance fixture", stages: [], stats: [], candidates: [], analysis: { state: "waiting", expectedReplicas: 0, observedReplicas: 0, message: "待运行" }, charts: [], reports: [], sources: [] }))
}
await request("/experimental/tool/ids?project=" + project.id)
const record = parseResult(await Bun.file(path.join(project.worktree, "results/discovery/de-novo.json")).json(), { project: project.id, target: "cold-target", route: "de-novo" })
const html = await (await request(`/${project.id}/discovery/de-novo`, { headers: { Accept: "text/html" } })).text()
const entry = html.match(/src="(\/assets\/index-[^"]+\.js)"/)?.[1]
if (!entry) throw Error("Released page entry missing")
await request(entry)
console.log("COLD_ACCEPTANCE " + JSON.stringify({ fresh: !existing, project: project.id, run: record.run, evidenceUpdatedAt: record.evidenceUpdatedAt, collectedAt: record.collectedAt, entry, builtinRuntime: true, globalPlugins: (await (await request("/global/config")).json()).plugin ?? [] }))
process.exit(0)
