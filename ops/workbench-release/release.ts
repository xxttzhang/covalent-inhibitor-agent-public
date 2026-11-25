import path from "node:path"
import { pathToFileURL } from "node:url"
import { mkdir, copyFile, rename } from "node:fs/promises"

const root = path.resolve(import.meta.dir, "../../openscience-custom")
const option = (name: string) => { const index = process.argv.indexOf(name); return index < 0 ? undefined : process.argv[index + 1] }
const dist = path.resolve(option("--dist") ?? path.join(root, "frontend/workspace/dist-workbench"))
const projectConfig = await Bun.file(path.join(root, ".discovery/project.json")).json()
const folder = path.resolve(option("--report") ?? path.join(import.meta.dir, "../../reports/workbench-standardization-20261008/release"))
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
const build = await Bun.build({ entrypoints: [path.join(root, "backend/cli/integrations/discovery-platform/plugin.ts")], target: "bun", minify: false, plugins: [{ name: "host-authority", setup(build) { build.onResolve({ filter: /^\.\.\/\.\.\/src(?:\/|$)/ }, args => ({ path: args.path, external: true })) } }] })
if (!build.success || build.outputs.length !== 1) throw Error("Runtime bundle failed: " + build.logs.join("\n"))
const runtimeHash = digest(await build.outputs[0].arrayBuffer())
const compatibilityBytes = await Bun.file(path.join(release, "route-compat.ts")).arrayBuffer()
const compatibilityHash = digest(compatibilityBytes)
const compatibilityFile = path.join(release, "entry-compat-" + compatibilityHash + ".ts")
await Bun.write(compatibilityFile, compatibilityBytes)
const version = digest(JSON.stringify({ hashes, runtimeHash, compatibilityHash, routeEntry: 6 }))
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
import { Server } from ${JSON.stringify(pathToFileURL(path.join(root, "backend/cli/src/server/server.ts")).href)};
import { discoveryEntries } from ${JSON.stringify(pathToFileURL(compatibilityFile).href)};
const compatibility=Symbol.for(${JSON.stringify("openscience.discovery.entry-compat." + compatibilityHash)});
if(!globalThis[compatibility]){discoveryEntries(Server.App(),WEB_ASSETS);globalThis[compatibility]=true;}
const active={done:false};
export default async(input)=>{if(!active.done){const entries=await Bun.file(${JSON.stringify(manifest)}).json();const additions={};for(const [url,record] of Object.entries(entries)){const bytes=await Bun.file(record.file).arrayBuffer();if(new Bun.CryptoHasher('sha256').update(bytes).digest('hex')!==record.sha256)throw Error('Frontend release integrity failed');additions[url]=record.file;}Object.assign(WEB_ASSETS,additions);active.done=true;}if(input?.project?.id){for(const route of ["studies","library-first","ligand-first","de-novo"]){WEB_ASSETS["/"+input.project.id+"/discovery/"+route]=WEB_ASSETS["/index.html"];}}return {};};`)
const compiled = path.join(release, "discovery-runtime-" + version + ".mjs")
if (await Bun.file(compiled).exists() && digest(await Bun.file(compiled).arrayBuffer()) !== runtimeHash) throw Error("Immutable runtime bundle changed")
await Bun.write(compiled, build.outputs[0])
const science = pathToFileURL(compiled).href
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
const sciencePrefix = pathToFileURL(path.join(release, "discovery-runtime-")).href
const after = [...before.filter(p => !p.startsWith(prefix) && !p.startsWith(sciencePrefix) && p !== monitor && p !== analysis && p !== refinement), plugin, science]
const previous = before.find(p => p.startsWith(prefix))
if (previous) {
  const old = path.basename(previous).replace("runtime-", "").replace(".mjs", "")
  await copyFile(path.join(release, "release-" + old, "index.html"), path.join(folder, "previous-index.html"))
}
await Bun.write(path.join(folder, "deployment-reservation.json"), JSON.stringify({ before, after, version, entry, created: new Date().toISOString() }, null, 2))
const registeredAt = Date.now()
const applied = await fetch(base + "/global/config", { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plugin: after }) })
if (!applied.ok) throw Error("Registry update failed: " + applied.status)
await get("/experimental/tool/ids?project=" + projectConfig.project)
const response = await fetch(base + entry)
if (!response.ok || digest(await response.arrayBuffer()) !== hashes[entry]) throw Error("Published entry mismatch")
const health = JSON.parse((await get("/file/content?project=" + projectConfig.project + "&path=.discovery%2Fruntime-status.json")).content)
if (health.state !== "ready" || Date.parse(health.collectedAt) < registeredAt) throw Error("New observer did not pass startup checks: " + JSON.stringify(health))
for (const route of ["library-first", "ligand-first", "de-novo"]) {
  const page = await fetch(base + "/" + projectConfig.project + "/discovery/" + route)
  if (!page.ok || !page.headers.get("content-type")?.includes("text/html") || !(await page.text()).includes(entry)) throw Error("Published route entry mismatch: " + route)
  const snapshot = await get("/file/content?project=" + projectConfig.project + "&path=" + encodeURIComponent(projectConfig.routes[route].dataPath))
  const data = JSON.parse(snapshot.content)
  if (data.schema !== "discovery-result-v1" || data.project !== projectConfig.project || data.route !== route) throw Error("Scoped runtime failed: " + route)
}
const index = path.join(root, "frontend/workspace/dist/index.html")
await copyFile(path.join(dist, "index.html"), index + ".tmp")
await rename(index + ".tmp", index)
await Bun.write(path.join(release, "active.json.tmp"), JSON.stringify({ version, directory: "release-" + version, files: hashes }, null, 2))
await rename(path.join(release, "active.json.tmp"), path.join(release, "active.json"))
await Bun.write(path.join(folder, "deployment.json"), JSON.stringify({ version, entry, assets: files.length, before, after,
  restarted: false, computeSubmitted: false, automatedLocalAnalysis: true, workbenchVersion: "1.1.0", runtimeBundle: compiled, runtimeSha256: digest(await Bun.file(compiled).arrayBuffer()), url: base + "/" + projectConfig.project + "/discovery/de-novo", created: new Date().toISOString() }, null, 2))
console.log(JSON.stringify({ applied: true, version, entry, restart: false, monitor: true }))
