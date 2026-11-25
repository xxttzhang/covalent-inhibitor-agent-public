import path from "node:path"
import { mkdtemp, mkdir, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
const root = path.resolve(import.meta.dir, "../..")
const directory = await mkdtemp(path.join(tmpdir(), "workbench-cold-"))
const output = path.join(root, "reports/workbench-standardization-20261008/verification")
await mkdir(output, { recursive: true })
const env = { ...process.env, OPENSCIENCE_TEST_HOME: directory, OPENSCIENCE_DATA_DIR: path.join(directory, "data"), OPENSCIENCE_CONFIG_DIR: path.join(directory, "config"), XDG_DATA_HOME: path.join(directory, "xdg-data"), XDG_CONFIG_HOME: path.join(directory, "xdg-config"), XDG_CACHE_HOME: path.join(directory, "xdg-cache"), XDG_STATE_HOME: path.join(directory, "xdg-state"), OPENSCIENCE_DISABLE_DEFAULT_PLUGINS: "true", OPENSCIENCE_DISABLE_PROJECT_CONFIG: "true", OPENSCIENCE_DISABLE_MODELS_FETCH: "true", OPENSCIENCE_DISABLE_AUTOUPDATE: "true", OPENSCIENCE_EXPERIMENTAL_DISABLE_FILEWATCHER: "true", OPENSCIENCE_CONFIG_CONTENT: '{"plugin":[]}', OPENSCIENCE_DISABLE_LSP_DOWNLOAD: "true" }
const checks = []
try {
  for (const iteration of [1, 2]) {
    const child = Bun.spawn([process.execPath, "run", "--conditions=browser", path.join(root, "openscience-custom/backend/cli/integrations/discovery-platform/cold-check.ts")], { cwd: path.join(root, "openscience-custom/backend/cli"), env, stdout: "pipe", stderr: "pipe" })
    const timeout = setTimeout(() => child.kill(), 45000)
    const [stdout, stderr, code] = await Promise.all([new Response(child.stdout).text(), new Response(child.stderr).text(), child.exited])
    clearTimeout(timeout)
    await Bun.write(path.join(output, `cold-${iteration}.log`), stdout + stderr)
    if (code !== 0) throw Error("Cold startup failed; inspect acceptance log (code " + code + ")")
    const line = stdout.split("\n").find(line => line.startsWith("COLD_ACCEPTANCE "))
    if (!line) throw Error("Missing cold startup receipt")
    checks.push(JSON.parse(line.slice("COLD_ACCEPTANCE ".length)))
  }
  if (checks[0].project !== checks[1].project || checks[0].evidenceUpdatedAt !== checks[1].evidenceUpdatedAt || checks.some(c => c.globalPlugins.length)) throw Error("Startup continuity failed")
  await Bun.write(path.join(output, "cold-start.json"), JSON.stringify({ passed: true, productionRestarted: false, checks }, null, 2))
  console.log(JSON.stringify({ passed: true, checks }))
} finally { await rm(directory, { recursive: true, force: true }) }
