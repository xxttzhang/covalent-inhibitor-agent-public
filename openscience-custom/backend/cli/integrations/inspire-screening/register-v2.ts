// Replace only our old pilot-first plugin; preserve credentials, MCP, sandbox,
// other plugins and active research sessions. No process restart.
import path from "node:path"
import { pathToFileURL } from "node:url"

const base = "http://127.0.0.1:4096"
const old = pathToFileURL(path.join(import.meta.dir, "plugin.ts")).href
const plugin = pathToFileURL(path.join(import.meta.dir, "plugin-v2.ts")).href
const get = async (url: string) => {
  const response = await fetch(base + url)
  if (!response.ok) throw new Error(`Cannot inspect platform: ${response.status}`)
  return response.json()
}
const config = await get("/global/config")
const plugins: string[] = config.plugin ?? []
if (!plugins.includes(plugin) || plugins.includes(old)) {
  const projects = await get("/project")
  const directories = [...new Set<string>(projects.map((item: { worktree: string }) => item.worktree).filter(Boolean))]
  for (const directory of [undefined, ...directories]) {
    const states = await get(`/session/status${directory ? `?directory=${encodeURIComponent(directory)}` : ""}`)
    if (Object.values(states).some((state) => (state as { type?: string }).type !== "idle")) {
      throw new Error("Active research session detected; registration paused. No session was interrupted.")
    }
  }
  const response = await fetch(base + "/global/config", { method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ plugin: [...new Set([...plugins.filter((item) => item !== old), plugin])] }) })
  if (!response.ok) throw new Error(`Registration failed: ${response.status}`)
}
const ids = await get("/experimental/tool/ids")
if (!ids.includes("inspire_library_workflow") || !ids.includes("inspire_screening_smoke") || ids.includes("inspire_screening")) {
  throw new Error("Unexpected tool registration; verify new workflow and removal of ambiguous pilot entry")
}
console.log(JSON.stringify({ registered: true, default_tool: "inspire_library_workflow", legacy_test: "inspire_screening_smoke", restarted: false, submitted: false }))
