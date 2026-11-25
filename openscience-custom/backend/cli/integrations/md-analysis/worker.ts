import type { Binding } from "../discovery-platform/binding"
import path from "node:path"
import { mkdir, rename } from "node:fs/promises"

export const root = path.resolve(import.meta.dir, "../../../../..")
const digest = (bytes: string | Uint8Array) => new Bun.CryptoHasher("sha256").update(bytes).digest("hex")
type Chart = { system: string; kind: string; title: string; caption: string; file: string; sha256: string }
type Package = { schema: string; charts: Chart[]; files: Record<string, string> }

export async function fingerprint(base: string, settings: Binding["md"]) {
  const directory = path.join(base, settings.source)
  const files = (await Array.fromAsync(new Bun.Glob("**/*.{json,csv}").scan({ cwd: directory, onlyFiles: true }))).sort()
  const hashes = await Promise.all([...files.map(file => path.join(settings.source, file)), ...settings.scripts.map(file => "ops/" + file)]
    .map(async file => [file, digest(new Uint8Array(await Bun.file(path.join(base, file)).arrayBuffer()))]))
  const count = files.filter(file => file.endsWith("/analysis-v1/metrics-manifest.json")).length
  return { signature: digest(JSON.stringify(hashes)), count }
}

export async function verify(directory: string, chartCount: number) {
  const manifest = await Bun.file(path.join(directory, "manifest.json")).json() as Package
  if (manifest.schema !== "md-analysis-package-v1" || manifest.charts.length !== chartCount) throw Error("Invalid analysis package")
  for (const [file, hash] of Object.entries(manifest.files)) {
    if (path.basename(file) !== file || digest(new Uint8Array(await Bun.file(path.join(directory, file)).arrayBuffer())) !== hash)
      throw Error("Analysis output integrity failed: " + file)
  }
  return manifest
}

/** This adapter accepts only the registered OPLS batch. Future engines need their own input binding. */
export function collector(base: string, projectRoot: string, project: string, settings: Binding["md"], authorize?: () => Promise<void>) {
  const folder = path.join(projectRoot, settings.outputPrefix)
  const stateFile = path.join(projectRoot, settings.statePath)
  const state = { busy: false, signature: "", phase: "waiting", count: 0, error: "", package: undefined as Package | undefined,
    finishedAt: "", startedAt: "", failed: "" }
  const publish = async () => {
    await mkdir(folder, { recursive: true })
    await mkdir(path.dirname(stateFile), { recursive: true })
    const directory = `${settings.outputPrefix}/${state.signature}`
    const value = { schema: "md-analysis-state-v1", project, collectedAt: new Date().toISOString(),
      signature: state.signature, phase: state.phase, count: state.count, expected: settings.expected, error: state.error,
      startedAt: state.startedAt, finishedAt: state.finishedAt,
      charts: state.phase === "ready" ? state.package?.charts.map(chart => ({ ...chart, path: directory + "/" + chart.file })) : [],
      bundle: state.phase === "ready" ? { path: directory + "/analysis-bundle.zip", sha256: state.package?.files["analysis-bundle.zip"] } : null,
      scope: "registered-opls-replicas", rmsf: "missing", review: "automatic-report-needs-review" }
    await Bun.write(stateFile + ".tmp", JSON.stringify(value))
    await rename(stateFile + ".tmp", stateFile)
  }
  return async () => {
    if (state.busy) return
    state.busy = true
    try {
      const current = await fingerprint(base, settings)
      state.count = current.count
      if (current.signature !== state.signature) {
        state.signature = current.signature
        state.error = ""
        state.package = undefined
        state.finishedAt = ""
      }
      if (state.count !== settings.expected) { state.phase = "waiting"; return }
      if (state.failed === state.signature) { state.phase = "failed"; return }
      const output = path.join(folder, state.signature)
      if (await Bun.file(path.join(output, "manifest.json")).exists()) {
        state.package = await verify(output, settings.chartCount)
        state.phase = "ready"
        state.finishedAt ||= (await Bun.file(path.join(output, "manifest.json")).stat()).mtime.toISOString()
        return
      }
      state.startedAt = new Date().toISOString()
      state.phase = "validation"
      await publish()
      const partial = output + ".partial-" + Date.now()
      await authorize?.()
      const process = Bun.spawn([settings.python,
        path.join(base, "ops/export_md_workbench.py"), "--root", path.join(base, settings.source), "--output", partial],
      { stdout: "pipe", stderr: "pipe", env: { ...Bun.env, PYTHONUNBUFFERED: "1" } })
      const timeout = setTimeout(() => process.kill(), 180_000)
      const errors = new Response(process.stderr).text()
      const reader = process.stdout.pipeThrough(new TextDecoderStream()).getReader()
      const buffer = { text: "" }
      while (true) {
        const next = await reader.read()
        if (next.done) break
        const chunk = next.value
        buffer.text += chunk
        const lines = buffer.text.split("\n")
        buffer.text = lines.pop() ?? ""
        for (const line of lines.filter(Boolean)) {
          const progress = JSON.parse(line) as { phase: string }
          if (["validation", "analysis", "rendering", "report"].includes(progress.phase)) {
            state.phase = progress.phase
            await publish()
          }
        }
      }
      const code = await process.exited
      clearTimeout(timeout)
      const error = await errors
      if (code !== 0) throw Error("Analysis failed: " + error.slice(-1200))
      if ((await fingerprint(base, settings)).signature !== state.signature) throw Error("Input changed during analysis")
      state.package = await verify(partial, settings.chartCount)
      await rename(partial, output)
      state.finishedAt = new Date().toISOString()
      state.phase = "ready"
    } catch (error) {
      state.phase = "failed"
      state.failed = state.signature
      state.error = error instanceof Error ? error.message : "Analysis failed"
    } finally {
      await publish().finally(() => { state.busy = false })
    }
  }
}
