import path from "node:path"
import { pathToFileURL } from "node:url"

const base = "http://127.0.0.1:4096"
// Config invalidation does not invalidate Bun's ESM module cache. Build an
// immutable, content-addressed entry including the executor's local modules.
// Keep it beside the sources: host/daemon paths depend on import.meta.dir.
const build = await Bun.build({ entrypoints: [path.join(import.meta.dir, "plugin.ts")], target: "bun",
  packages: "external",
  plugins: [{ name: "workspace-path-base", setup(builder) {
    // This workspace module must travel inside the content-addressed plugin;
    // the host's dynamic plugin loader does not share its package-resolution base.
    builder.onResolve({ filter: /^@synsci\/util\/screening-target$/ }, () => ({
      path: path.resolve(import.meta.dir, "../../../../../tooling/util/src/screening-target.ts"),
    }))
  } }] })
if (!build.success) throw new AggregateError(build.logs, "Executor plugin build failed")
const artifact = await build.outputs[0].text()
const version = new Bun.CryptoHasher("sha256").update(artifact).digest("hex")
const filename = path.join(import.meta.dir, `.runtime-${version}.mjs`)
const plugin = pathToFileURL(filename).href
if (await Bun.file(filename).exists()) {
  if (await Bun.file(filename).text() !== artifact) throw new Error("Versioned plugin artifact has changed")
} else await Bun.write(filename, artifact)
await import(plugin) // Validate before touching any live configuration.
if (process.argv.includes("--build-only")) {
  console.log(JSON.stringify({ built: true, version, registered: false, submitted: false }))
  process.exit(0)
}
const retired = ["plugin.ts", "../plugin.ts", "../plugin-v2.ts"].map((file) => pathToFileURL(path.resolve(import.meta.dir, file)).href)
const prefix = pathToFileURL(path.join(import.meta.dir, ".runtime-")).href
const obsolete = (item: string) => retired.includes(item) || (item.startsWith(prefix) && item !== plugin)
const get = async (url: string) => {
  const response = await fetch(base + url)
  if (!response.ok) throw new Error(`Platform inspection failed at ${url.split("?")[0]}: ${response.status}`)
  return response.json()
}
const config = await get("/global/config")
const plugins: string[] = config.plugin ?? []
if (process.argv.includes("--reload") || !plugins.includes(plugin) || plugins.some(obsolete)) {
  const projects = await get("/project")
  const dirs = [...new Set<string>(projects.map((item: { worktree: string }) => item.worktree).filter(Boolean))]
  for (const directory of [undefined, ...dirs]) {
    const suffix = directory ? `?directory=${encodeURIComponent(directory)}` : ""
    const states = await get(`/session/status${suffix}`)
    if (Object.values(states).some((state) => (state as { type?: string }).type !== "idle")) throw new Error("Active research session: do not reload or restart the platform")
    if ((await get(`/permission${suffix}`)).length || (await get(`/question${suffix}`)).length) {
      throw new Error("Pending approval or clarification: do not reload the platform")
    }
  }
  const response = await fetch(base + "/global/config", { method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plugin: [...new Set([...plugins.filter((item) => !obsolete(item)), plugin])] }) })
  if (!response.ok) throw new Error("Executor registration failed")
}
const tools = await get("/experimental/tool/ids")
if (!["inspire_screening_task", "inspire_screening_capabilities"].every(name => tools.includes(name)))
  throw new Error("Host-bound MCP executor or capability tool not discovered")
if (["inspire_library_workflow", "inspire_screening_smoke"].some(name => tools.includes(name)))
  throw new Error("Historical Library-first tools remain in the default tool set; inspect other plugin registrations")
const descriptions = await get("/experimental/tool?provider=boyue-medicine&model=deepseek-v4-pro")
const actual = descriptions.find((item: { id: string }) => item.id === "inspire_screening_task")
if (!actual?.description.includes("AUTOMATICALLY presents") || !actual.description.includes("screening_mode") ||
  !actual.parameters?.properties?.target_parameters?.properties?.reaction_type ||
  !actual.description.includes("Never ask the user to choose raw record caps") ||
  !actual.parameters?.properties?.action?.enum?.includes("diagnose_refinement") ||
  !actual.parameters?.properties?.action?.enum?.includes("plan_refinement") ||
  !actual.parameters?.properties?.action?.enum?.includes("approve_refinement") ||
  !actual.parameters?.properties?.action?.enum?.includes("cancel_refinement") ||
  !actual.parameters?.properties?.action?.enum?.includes("refinement_status") ||
  !actual.parameters?.properties?.action?.enum?.includes("refinement_report") ||
  !actual.parameters?.properties?.action?.enum?.includes("resume_refinement_delivery")) {
  throw new Error("Platform still serves an outdated executor")
}
console.log(JSON.stringify({ registered: true, tool: "inspire_screening_task", capability: "inspire_screening_capabilities",
  transport: "host-bound-mcp", historical_tools_in_default: false, version, restart: false, submitted: false }))
