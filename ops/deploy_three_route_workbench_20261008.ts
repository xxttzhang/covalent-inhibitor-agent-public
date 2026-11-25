import path from "node:path"
import { pathToFileURL } from "node:url"
import { mkdir, copyFile, rename } from "node:fs/promises"

const root = path.resolve(import.meta.dir, "../openscience-custom")
const dist = path.join(root, "frontend/workspace/dist-three-routes")
const folder = path.resolve(import.meta.dir, "../reports/three-route-workbench-20261008")
const release = path.join(root, "backend/cli/integrations/live-web-assets")
const digest = (bytes: string | ArrayBuffer) => new Bun.CryptoHasher("sha256").update(bytes).digest("hex")
await mkdir(folder, { recursive: true })
const files = (await Array.fromAsync(new Bun.Glob("**/*").scan({ cwd: dist, onlyFiles: true, dot: true }))).filter(name => !name.split("/").some(part => part.startsWith("._") || part === ".DS_Store")).sort()
const entries: Record<string, { file: string; sha256: string }> = {}
const hashes: Record<string, string> = {}
for (const name of files) hashes["/" + name] = digest(await Bun.file(path.join(dist, name)).arrayBuffer())
const html = await Bun.file(path.join(dist, "index.html")).text()
const entry = html.match(/src="(\/assets\/index-[^"]+\.js)"/)?.[1]
if (!entry || !hashes[entry]) throw Error("Missing frontend entry")
const version = digest(JSON.stringify(hashes))
for (const name of files) {
  const bytes = await Bun.file(path.join(dist, name)).arrayBuffer()
  if (digest(bytes) !== hashes["/" + name]) throw Error("Build changed during release")
  const filename = path.join(release, "release-" + version, name)
  if (await Bun.file(filename).exists()) {
    if (digest(await Bun.file(filename).arrayBuffer()) !== hashes["/" + name]) throw Error("Snapshot mismatch")
  } else await Bun.write(filename, bytes)
  entries["/" + name] = { file: filename, sha256: hashes["/" + name] }
}
const manifest = path.join(release, version + ".json")
await Bun.write(manifest, JSON.stringify(entries, null, 2))
const runtime = path.join(release, "runtime-" + version + ".mjs")
await Bun.write(runtime, `import { WEB_ASSETS } from ${JSON.stringify(pathToFileURL(path.join(root, "backend/cli/src/web/assets.ts")).href)};
const active={done:false};
export default async()=>{if(active.done)return {};const entries=await Bun.file(${JSON.stringify(manifest)}).json();const additions={};for(const [url,record] of Object.entries(entries)){const bytes=await Bun.file(record.file).arrayBuffer();if(new Bun.CryptoHasher('sha256').update(bytes).digest('hex')!==record.sha256)throw Error('Frontend release integrity failed');additions[url]=record.file;}Object.assign(WEB_ASSETS,additions);active.done=true;return {};};`)
const plugin = pathToFileURL(runtime).href
await (await import(plugin)).default()
const monitor = pathToFileURL(path.join(root, "backend/cli/integrations/discovery-monitor/plugin.ts")).href
if (!process.argv.includes("--apply")) {
  console.log(JSON.stringify({ prepared: true, version, entry, assets: files.length, restart: false }))
  process.exit(0)
}
const base = "http://127.0.0.1:4096"
const get = async (route: string) => {
  const result = await fetch(base + route, { signal: AbortSignal.timeout(10000) })
  if (!result.ok) throw Error("Inspection failed: " + route.split("?")[0])
  return result.json()
}
const before = (await get("/global/config")).plugin as string[]
const projects = await get("/project") as { id: string }[]
for (const project of projects) {
  const suffix = "?project=" + project.id
  const [states, permissions, questions] = await Promise.all([get("/session/status" + suffix), get("/permission" + suffix), get("/question" + suffix)])
  if (Object.values(states).some(state => (state as { type: string }).type !== "idle") || permissions.length || questions.length)
    throw Error("Active session or pending input; release not registered")
}
if (JSON.stringify((await get("/global/config")).plugin) !== JSON.stringify(before)) throw Error("Concurrent registry change")
const prefix = pathToFileURL(path.join(release, "runtime-")).href
const analysis = pathToFileURL(path.join(root, "backend/cli/integrations/md-analysis/plugin.ts")).href
const refinement = pathToFileURL(path.join(root, "backend/cli/integrations/refinement-monitor/plugin.ts")).href
const after = [...before.filter(p => !p.startsWith(prefix) && p !== monitor && p !== analysis && p !== refinement), plugin, monitor, analysis, refinement]
const previous = before.find(p => p.startsWith(prefix))
if (previous) {
  const old = path.basename(previous).replace("runtime-", "").replace(".mjs", "")
  await copyFile(path.join(release, "release-" + old, "index.html"), path.join(folder, "previous-index.html"))
}
await Bun.write(path.join(folder, "deployment-reservation.json"), JSON.stringify({ before, after, version, entry, created: new Date().toISOString() }, null, 2))
const applied = await fetch(base + "/global/config", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plugin: after }) })
if (!applied.ok) throw Error("Registry update failed: " + applied.status)
await get("/experimental/tool/ids")
const response = await fetch(base + entry)
if (!response.ok || digest(await response.arrayBuffer()) !== hashes[entry]) throw Error("Published entry mismatch")
const index = path.join(root, "frontend/workspace/dist/index.html")
await copyFile(path.join(dist, "index.html"), index + ".tmp")
await rename(index + ".tmp", index)
const evidence = await get("/file/content?project=prj_dbfd2a34da2e4c78977238fcf59d39ae&path=results%2Fdiscovery%2Fde-novo.json")
if (JSON.parse(evidence.content).schema !== "discovery-workbench-v1") throw Error("Monitor did not produce valid evidence")
for (const route of ["library-first", "ligand-first"]) {
  const snapshot = await get("/file/content?project=prj_dbfd2a34da2e4c78977238fcf59d39ae&path=" + encodeURIComponent("results/discovery/" + route + ".json"))
  if (JSON.parse(snapshot.content).schema !== "refinement-workbench-v1") throw Error("Refinement monitor failed: " + route)
}
await Bun.write(path.join(folder, "deployment.json"), JSON.stringify({ version, entry, assets: files.length, before, after,
  restarted: false, computeSubmitted: false, automatedLocalAnalysis: true, url: base + "/prj_dbfd2a34da2e4c78977238fcf59d39ae/discovery/de-novo", created: new Date().toISOString() }, null, 2))
console.log(JSON.stringify({ applied: true, version, entry, restart: false, monitor: true }))
