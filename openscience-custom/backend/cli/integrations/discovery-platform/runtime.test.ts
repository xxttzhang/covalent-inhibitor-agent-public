import { expect, test } from "bun:test"
import path from "node:path"
import { mkdtemp, rm, symlink, cp } from "node:fs/promises"
import { tmpdir } from "node:os"
import { discoveryProject, routeDefaults } from "@synsci/util/discovery-config"
import { parseResult, result } from "@synsci/util/discovery-result"
import { runtime, publish, start } from "./runtime"
import { installedRelease } from "../../src/web/release"
import { fingerprint } from "../md-analysis/worker"

const platform = path.resolve(import.meta.dir, "../../../..")
const study = discoveryProject.parse(await Bun.file(path.join(platform, ".discovery/project.json")).json())
const base = { schema: "discovery-result-v1", project: "project-reuse-test", target: "target-reuse-test", route: "de-novo", run: "run-test", collectedAt: new Date().toISOString(), evidenceUpdatedAt: new Date().toISOString(), observation: "local-evidence", current: "", notice: "Test fixture, no scientific result", stages: [], stats: [], candidates: [], analysis: { state: "waiting", expectedReplicas: 0, observedReplicas: 0, message: "待运行" }, charts: [], reports: [], sources: [] }
function config() { return discoveryProject.parse({ schema: "discovery-project-v1", project: base.project, target: { id: base.target, name: "测试靶点", label: "测试靶点" }, routes: Object.fromEntries(Object.entries(routeDefaults).map(([id, value]) => [id, { ...value, ...(id === "de-novo" ? { inputPath: ".discovery/inbox/de-novo.json", dataPath: "results/discovery/de-novo.json" } : {}) }])) }) }

test("all real routes publish one contract, preserving counts, partial failure and MD ownership", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "discovery-pinned-routes-"))
  try {
  const registered = await Bun.file(path.join(platform, ".discovery/runtime.json")).json()
  // Pin the partial snapshot this test asserts; never overwrite the user's live output.
  registered.ligandMD = { ...registered.ligandMD,
    source: "reports/ligand-md-20261010-v1/status-20261010T173443.json",
    sha256: "6d7227119060c0a8f642b4ab97e9842e7a1a1deefd41a03cd1e51e8233f1d340" }
  delete registered.ligandMD.chartsSource
  delete registered.ligandMD.chartsSha256
  await publish(directory, ".discovery/project.json", study)
  await publish(directory, ".discovery/runtime.json", registered)
  const existing = await fingerprint(registered.sourceRoot, registered.md)
  const packagePath = path.join(registered.md.outputPrefix, existing.signature)
  await cp(path.join(platform, packagePath), path.join(directory, packagePath), { recursive: true })
  await runtime(directory, study.project, async () => { throw Error("Acceptance must reuse verified analysis, not launch a new one") }).tick()
  for (const route of ["de-novo", "library-first", "ligand-first"] as const) {
    const data = parseResult(await Bun.file(path.join(directory, study.routes[route].dataPath!)).json(), { project: study.project, target: study.target!.id, route })
    expect(data.candidates.length).toBe(route === "de-novo" ? 7 : route === "library-first" ? 8 : 3)
    expect(data.charts.length).toBe(route === "de-novo" ? 15 : route === "library-first" ? 56 : 0)
    expect(data.candidates.every(c => c.structures.length > 0)).toBe(true)
    if (route === "de-novo") {
      for (const candidate of data.candidates) {
        expect(candidate.scientificEvidence?.candidateID).toBe(candidate.id)
        const measurements = candidate.scientificEvidence!.measurements
        expect(measurements.some(item => item.metricID === "ring_rmsd")).toBe(candidate.md.metrics.length > 0)
        expect(measurements.every(item => data.sources.some(source =>
          source.path === item.evidence.path && source.sha256 === item.evidence.sha256))).toBe(true)
      }
    }
    if (route === "ligand-first") expect(data.stages.find(s => s.id === "dock")?.state).toBe("completed")
    if (route === "library-first") { expect(data.analysis.observedReplicas).toBe(24); expect(data.candidates.every(c => c.md.state === "ready" && c.md.chartIds.length === 7)).toBe(true) }
    if (route === "ligand-first") {
      // The registered October 10 snapshot contains partial MD evidence.
      // Migration must preserve it without presenting it as a completed result.
      expect(data.current).toBe("md")
      expect(data.observation).toBe("scheduler")
      expect(data.analysis.observedReplicas).toBe(0)
      expect(data.analysis.expectedReplicas).toBe(9)
      expect(data.candidates.every(c => c.md.state === "running" && c.md.independentReplicas === 0
        && c.md.metrics.length === 0 && c.md.chartIds.length === 0)).toBe(true)
    }
  }
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test("unconfigured, pending and native-tool publication stay project scoped across independent cold starts", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "discovery-reuse-"))
  try {
    await runtime(directory, base.project).tick()
    expect(await Bun.file(path.join(directory, ".discovery/runtime-status.json")).exists()).toBe(false)
    await publish(directory, ".discovery/project.json", config())
    await runtime(directory, base.project).tick()
    expect(await Bun.file(path.join(directory, "results/discovery/de-novo.json")).exists()).toBe(false)
    await publish(directory, ".discovery/inbox/de-novo.json", base)
    const launch = async () => {
      const command = `import {runtime} from ${JSON.stringify(path.join(import.meta.dir, "runtime.ts"))}; await runtime(${JSON.stringify(directory)},${JSON.stringify(base.project)}).tick()`
      const child = Bun.spawn([process.execPath, "-e", command], { stdout: "pipe", stderr: "pipe" })
      const error = new Response(child.stderr).text()
      expect(await child.exited, await error).toBe(0)
      return result.parse(await Bun.file(path.join(directory, "results/discovery/de-novo.json")).json())
    }
    const first = await launch()
    const second = await launch()
    expect(first.project).toBe(base.project)
    expect(second.run).toBe(first.run)
    expect(second.evidenceUpdatedAt).toBe(first.evidenceUpdatedAt)
    expect(second.candidates).toEqual([])
    expect(second.collectedAt >= first.collectedAt).toBe(true)
    await publish(directory, ".discovery/inbox/de-novo.json", { ...base, project: study.project })
    await runtime(directory, base.project).tick()
    expect((await Bun.file(path.join(directory, "results/discovery/de-novo.json")).json()).schema).toBe("discovery-result-error-v1")
  } finally { await rm(directory, { recursive: true, force: true }) }
})

test("rejects swapped route, target, duplicate candidate and orphan chart identities", () => {
  const scope = { project: base.project, target: base.target, route: base.route }
  expect(() => parseResult({ ...base, target: study.target!.id }, scope)).toThrow("不匹配")
  expect(() => parseResult({ ...base, route: "library-first" }, scope)).toThrow("不匹配")
  const candidate = { id: "test-only", label: "test", smiles: "CCO", featured: false, badge: "test", tone: "gray", note: "test", metrics: [], structures: [], md: { state: "pending", summary: "test", independentReplicas: 0, metrics: [], chartIds: [] } }
  expect(() => result.parse({ ...base, candidates: [candidate, candidate] })).toThrow("Duplicate")
  expect(() => result.parse({ ...base, candidates: [{ ...candidate, md: { ...candidate.md, chartIds: ["foreign"] } }] })).toThrow("binding")
})

test("installed release cold load verifies immutable files; tampering and symlink output escape fail", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "discovery-release-"))
  const outside = await mkdtemp(path.join(tmpdir(), "discovery-outside-"))
  try {
    const html = "<main>release fixture</main>"
    const sha = new Bun.CryptoHasher("sha256").update(html).digest("hex")
    await Bun.write(path.join(directory, "release/index.html"), html)
    await publish(directory, "active.json", { version: "a".repeat(64), directory: "release", files: { "/index.html": sha } })
    expect((await installedRelease(directory))?.index).toBe(path.join(directory, "release/index.html"))
    await Bun.write(path.join(directory, "release/index.html"), html + "tamper")
    await expect(installedRelease(directory)).rejects.toThrow("integrity")
    await symlink(outside, path.join(directory, "outside"))
    await expect(publish(directory, "outside/result.json", {})).rejects.toThrow("escapes")
    await expect(publish(directory, "../result.json", {})).rejects.toThrow()
  } finally { await rm(directory, { recursive: true, force: true }); await rm(outside, { recursive: true, force: true }) }
})

test("a new released observer replaces the previous generation and duplicates share one observer", async () => {
  const directory = await mkdtemp(path.join(tmpdir(), "discovery-upgrade-"))
  try {
    const first = await start(directory, base.project, "release-one")
    expect(await start(directory, base.project, "release-one")).toBe(first)
    const second = await start(directory, base.project, "release-two")
    expect(second).not.toBe(first)
    await first.stop()
    expect(await start(directory, base.project, "release-two")).toBe(second)
    await second.stop()
  } finally { await rm(directory, { recursive: true, force: true }) }
})
