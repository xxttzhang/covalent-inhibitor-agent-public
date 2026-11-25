import path from "node:path"
import { mkdir, rename, realpath } from "node:fs/promises"
import { discoveryProject, projectPath, type DiscoveryProject } from "@synsci/util/discovery-config"
import { parseResult } from "@synsci/util/discovery-result"
import { binding } from "./binding"
import { adapt } from "./adapter"
import { libraryMD } from "./library-md"
import { ligandMD } from "./ligand-md"
import { refinement as refinementSchema } from "../../../../frontend/workspace/src/discovery/refinement-model"
import { snapshot as denovo } from "../discovery-monitor/monitor"
import { snapshot as refinement } from "../refinement-monitor/monitor"
import { collector } from "../md-analysis/worker"

export async function publish(directory: string, relative: string, value: unknown) {
  const filename = path.join(directory, projectPath.parse(relative))
  await mkdir(path.dirname(filename), { recursive: true })
  const resolved = await realpath(path.dirname(filename))
  const root = await realpath(directory)
  if (resolved !== root && !resolved.startsWith(root + path.sep)) throw Error("Output escapes project")
  const temporary = filename + ".tmp-" + process.pid + "-" + crypto.randomUUID()
  await Bun.write(temporary, JSON.stringify(value))
  await rename(temporary, filename)
}

/** A project-scoped observer. No remote job submission or shell supplied by a project. */
export function runtime(directory: string, project: string, authorize?: () => Promise<void>, researchTick?: () => Promise<void>) {
  const state = { busy: false, stopped: false, signature: "", analysis: undefined as ReturnType<typeof collector> | undefined }
  const tick = async () => {
    if (state.busy || state.stopped) return
    state.busy = true
    const status = { schema: "discovery-runtime-status-v1", project, collectedAt: new Date().toISOString(), state: "waiting", errors: [] as string[] }
    try {
      const file = Bun.file(path.join(directory, ".discovery/project.json"))
      if (!await file.exists()) return
      const config = discoveryProject.parse(await file.json())
      if (config.project !== project) throw Error("Project configuration identity mismatch")
      if (!config.target) return
      const bindingFile = Bun.file(path.join(directory, ".discovery/runtime.json"))
      const registered = await bindingFile.exists() ? binding.parse(await bindingFile.json()) : undefined
      if (registered && (registered.project !== project || registered.study.project !== project || registered.target !== config.target.id)) throw Error("Scientific adapter binding mismatch")
      if (registered) {
        const signature = JSON.stringify(registered)
        if (signature !== state.signature) { state.signature = signature; state.analysis = collector(registered.sourceRoot, directory, project, registered.md, authorize) }
        await state.analysis!()
      }
      for (const route of ["library-first", "ligand-first", "de-novo"] as const) {
        const settings = config.routes[route]
        if (!settings.dataPath) continue
        try {
          if (settings.view === "standard") {
            if (!settings.inputPath) continue
            if (settings.inputPath === settings.dataPath) throw Error("Input and output must differ")
            const input = Bun.file(path.join(directory, settings.inputPath))
            if (!await input.exists()) continue
            const data = parseResult(await input.json(), { project, route, target: config.target.id })
            // Native tools own scientific timestamps. A heartbeat never fabricates new evidence.
            await publish(directory, settings.dataPath, { ...data, collectedAt: new Date().toISOString() })
            continue
          }
          if (!registered) throw Error("Audited view requires an explicit scientific adapter")
          const raw = route === "de-novo" ? await denovo(registered.sourceRoot, registered) : await refinement(route, registered.sourceRoot, registered)
          const analysis = route === "de-novo" ? await Bun.file(path.join(directory, registered.md.statePath)).json() : undefined
          const packet = adapt(raw, config, route, analysis)
          await publish(directory, settings.dataPath, route === "library-first" && registered.libraryMD ? await libraryMD(packet, refinementSchema.parse(raw), registered.sourceRoot, registered.libraryMD) : route === "ligand-first" && registered.ligandMD ? await ligandMD(packet, refinementSchema.parse(raw), registered.sourceRoot, registered.ligandMD) : packet)
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error)
          status.errors.push(route + ": " + message)
          await publish(directory, settings.dataPath, { schema: "discovery-result-error-v1", project, route, collectedAt: new Date().toISOString(), error: message })
        }
      }
      status.state = status.errors.length ? "degraded" : "ready"
    } catch (error) { status.state = "failed"; status.errors.push(error instanceof Error ? error.message : String(error)) }
    finally {
      // Optional trusted host work reuses this observer; without a configured
      // host, completely unconfigured projects remain untouched.
      try { await researchTick?.() }
      catch (error) { status.state = "degraded"; status.errors.push(error instanceof Error ? error.message : "Research observer failed") }
      try { if (await Bun.file(path.join(directory, ".discovery/project.json")).exists()) await publish(directory, ".discovery/runtime-status.json", status) }
      finally { state.busy = false }
    }
  }
  return { tick, stop: async () => { state.stopped = true; while (state.busy) await new Promise(resolve => setTimeout(resolve, 20)) } }
}

const key = Symbol.for("openscience.discovery.runtime.v1")
const globals = globalThis as typeof globalThis & { [key]?: Map<string, { generation: string; stop: () => Promise<void> }> }
const running = globals[key] ??= new Map()
export async function start(directory: string, project: string, generation = import.meta.url, authorize?: () => Promise<void>, researchTick?: () => Promise<void>) {
  const identity = path.resolve(directory) + ":" + project
  const previous = running.get(identity)
  if (previous?.generation === generation) return previous
  if (previous) await previous.stop()
  const service = runtime(directory, project, authorize, researchTick)
  const timer = setInterval(() => void service.tick().catch(() => undefined), 15000)
  timer.unref()
  const handle = { generation, stop: async () => { clearInterval(timer); await service.stop(); if (running.get(identity) === handle) running.delete(identity) } }
  running.set(identity, handle)
  await service.tick()
  return handle
}
