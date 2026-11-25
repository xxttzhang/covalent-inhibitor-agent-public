import { test, expect } from "bun:test"
import path from "node:path"
import os from "node:os"
import { mkdtemp, rm } from "node:fs/promises"
import { binding } from "./binding"
import { discoveryProject } from "@synsci/util/discovery-config"
import { snapshot } from "../refinement-monitor/monitor"
import { adapt } from "./adapter"
import { ligandMD } from "./ligand-md"

async function inputs() {
  const root = path.resolve(import.meta.dir, "../../../..")
  const config = discoveryProject.parse(await Bun.file(path.join(root, ".discovery/project.json")).json())
  const settings = binding.parse(await Bun.file(path.join(root, ".discovery/runtime.json")).json())
  const raw = await snapshot("ligand-first", settings.sourceRoot, settings)
  const packet = adapt(raw, config, "ligand-first")
  return { root, settings, raw, packet }
}

test("real partial snapshot does not turn manifest references into verified analyses", async () => {
  const { settings, raw, packet } = await inputs()
  const pinned = { ...settings.ligandMD!, source: "reports/ligand-md-20261010-v1/status-20261010T173443.json", sha256: "6d7227119060c0a8f642b4ab97e9842e7a1a1deefd41a03cd1e51e8233f1d340" }
  const updated = await ligandMD(packet, raw, settings.sourceRoot, pinned)
  expect(updated.current).toBe("md")
  expect((updated.legacy!.data as typeof raw).current).toBe("md")
  expect(updated.stages.find(s => s.id === "md")?.state).toBe("running")
  expect(updated.analysis.observedReplicas).toBe(0)
  expect(updated.analysis.expectedReplicas).toBe(9)
  expect(updated.stats.find(s => s.label === "生产完成的重复")?.value).toBe("3")
  expect(updated.candidates.every(c => c.md.summary.includes("清单待同步校验"))).toBe(true)
  expect(updated.candidates.every(c => c.md.state === "running")).toBe(true)
  expect(updated.candidates.map(c => c.structures)).toEqual(packet.candidates.map(c => c.structures))
  expect(updated.candidates.every(c => c.scientificEvidence?.candidateID === c.id
    && c.scientificEvidence.measurements.every(item => item.scope === "candidate"))).toBe(true)
  await expect(ligandMD(packet, raw, settings.sourceRoot, { ...pinned, sha256: "0".repeat(64) })).rejects.toThrow("hash mismatch")
  await expect(ligandMD({ ...packet, run: "other" }, raw, settings.sourceRoot, pinned)).rejects.toThrow("scope mismatch")
})

// Contract fixtures exercise the actual file reader and hash checks; they are
// temporary test evidence, never published as a scientific run.
async function fixture(failed = false) {
  const input = await inputs()
  const hash = (text: string) => new Bun.CryptoHasher("sha256").update(text).digest("hex")
  const systems = Object.values(input.settings.ligandMD!.candidates)
  const scientific = Object.fromEntries(systems.map(system => {
    const stopped = failed && system === "ligand_058"
    const replicas = Object.fromEntries(Array.from({ length: stopped ? 2 : 3 }, (_, index) => {
      const replica = index + 1
      const identity = { candidate: system, replica, frames: 1000, production_ps: 10000, input_manifest_sha256: hash(`${system}:${replica}:input`), initial_velocity_file_sha256: hash(`${system}:${replica}:velocity`), CPU_smoke_only: false, convergence_established: false, affinity_or_kinact_proven: false }
      const audit = JSON.stringify({ ...identity, status: "FULL_REPLICA_TRAJECTORY_AUDITED" })
      const manifest = JSON.stringify({ ...identity, status: "REPLICA_METRICS_BOUND", files: { "audit.json": hash(audit) } })
      return [String(replica), {
        "finished.json": { candidate: system, replica, status: "REPLICA_PRODUCTION_COMPLETE_REQUIRES_AUDIT", production_ps: 10000, production_frames: 1000, smoke_only: false, manifest_sha256: identity.input_manifest_sha256, initial_velocity_file_sha256: identity.initial_velocity_file_sha256 },
        "short-qc.json": { status: "PASS_SHORT_PREP_GEOMETRY" }, "energy-force-check.json": { passed: true },
        analysis_manifest_text: stopped && replica === 2 ? undefined : manifest,
        analysis_audit_text: stopped && replica === 2 ? undefined : audit,
        file_verification: stopped && replica === 2 ? undefined : { candidate: system, replica, passed: true, metrics_means: { whole_ligand_RMSD_A: 1 + replica / 10, largest_ring_system_RMSD_A: .5 + replica / 10, ligand_heavy_COM_displacement_A: .4 + replica / 10, initial_contact_retention_excluding_tether: .8 + replica / 100 }, input_manifest_sha256: identity.input_manifest_sha256, analysis_manifest_sha256: hash(manifest), initial_velocity_file_sha256: identity.initial_velocity_file_sha256 },
        analysis_log_tail: stopped && replica === 2 ? "assert np.allclose(xyz, positions_nm, atol=3e-6, rtol=0)\nAssertionError" : undefined,
      }]
    }))
    return [system, { "status.json": { system_id: system, state: stopped ? "NEEDS_REVIEW" : "THREE_REPLICAS_AUDITED_ANALYSIS_COMPLETE", analysis_complete: !stopped, active_replica: stopped ? 2 : 3,
      replicas: Object.entries(replicas).filter(([, output]) => output.analysis_manifest_text).map(([number, output]) => ({ replica: Number(number), production_ps: 10000, frames: 1000, analysis_manifest_sha256: hash(output.analysis_manifest_text!) })) }, replicas }]
  }))
  const data = { observed_epoch: 1791959943, scheduler: { unresolved: [], items: systems.map(system => ({ name: `ligand-md-${system.split("_")[1]}-20261010-v1`, status: failed && system === "ligand_058" ? "FAILED" : "SUCCEEDED", project: input.settings.ligandMD!.inspireProject })) }, scientific_outputs: scientific }
  const directory = await mkdtemp(path.join(os.tmpdir(), "ligand-md-contract-"))
  const dockingBytes = await Bun.file(path.join(input.settings.sourceRoot, input.settings.ligandMD!.dockingSource)).text()
  await Bun.write(path.join(directory, "docking.json"), dockingBytes)
  const read = async () => {
    const bytes = JSON.stringify(data)
    await Bun.write(path.join(directory, "snapshot.json"), bytes)
    return ligandMD(input.packet, input.raw, directory, { ...input.settings.ligandMD!, source: "snapshot.json", sha256: hash(bytes), dockingSource: "docking.json", dockingSha256: hash(dockingBytes), chartsSource: undefined, chartsSha256: undefined })
  }
  return { ...input, data, read, cleanup: () => rm(directory, { recursive: true, force: true }) }
}

test("nine verified analyses complete the standard and legacy MD stages", async () => {
  const test = await fixture()
  try {
    const updated = await test.read()
    expect(updated.analysis.state).toBe("ready")
    expect(updated.analysis.observedReplicas).toBe(9)
    expect(updated.stages.find(s => s.id === "md")?.state).toBe("completed")
    expect((updated.legacy!.data as typeof test.raw).stages.find(s => s.id === "md")?.state).toBe("done")
    expect(updated.candidates.every(c => c.md.state === "ready" && c.md.independentReplicas === 3)).toBe(true)
    expect(updated.candidates.every(c => c.md.metrics.length === 4 && c.md.metrics.every(metric => metric.uncertainty?.independentN === 3))).toBe(true)
    for (const candidate of updated.candidates) {
      const evidence = candidate.scientificEvidence!
      expect(evidence.smiles).toBe(candidate.smiles)
      expect(evidence.measurements.filter(item => item.scope === "md")).toHaveLength(4)
      for (const item of evidence.measurements.filter(item => item.scope === "md")) {
        expect(item.stage).toBe("md")
        expect(item.evidence.path).toBe("snapshot.json")
        expect(item.evidence.sha256).toMatch(/^[a-f0-9]{64}$/)
        expect(item.definition).toBe(candidate.md.metrics.find(metric => metric.id === item.metricID)!.definition)
      }
    }
    expect(updated.candidates.every(c => c.badge === "对接已完成 · Gate B" && c.featured)).toBe(true)
    expect(updated.stages.find(s => s.id === "dock")?.state).toBe("completed")
    expect(updated.stages.find(s => s.id === "review")?.state).toBe("completed")
    expect(updated.notice).toContain("90 ns")
    expect(updated.stats.some(s => s.note.includes("并非全部完成"))).toBe(false)
    expect(updated.candidates.map(c => c.structures)).toEqual(test.packet.candidates.map(c => c.structures))
  } finally { await test.cleanup() }
})

test("current audited snapshot publishes seven hash-bound charts per candidate despite the partial batch failure", async () => {
  const { settings, raw, packet } = await inputs()
  const updated = await ligandMD(packet, raw, settings.sourceRoot, settings.ligandMD!)
  expect(updated.analysis.state).toBe("failed")
  expect(updated.charts).toHaveLength(21)
  expect(updated.candidates.every(candidate => candidate.md.chartIds.length === 7)).toBe(true)
  expect(updated.candidates.find(candidate => candidate.id.endsWith("058"))?.md.independentReplicas).toBe(1)
  expect(updated.charts.every(chart => chart.asset.storage === "project")).toBe(true)
})

test("failed analysis retains eight productions, seven analyses, and two completed candidates", async () => {
  const test = await fixture(true)
  try {
    const updated = await test.read()
    expect(updated.analysis.state).toBe("failed")
    expect(updated.analysis.observedReplicas).toBe(7)
    expect(updated.stats.find(s => s.label === "生产完成的重复")?.value).toBe("8")
    expect(updated.stages.find(s => s.id === "md")?.state).toBe("blocked")
    expect(updated.candidates.filter(c => c.md.state === "ready")).toHaveLength(2)
    const failed = updated.candidates.find(c => c.id.endsWith("058"))!
    expect(failed.md.state).toBe("failed")
    expect(failed.md.metrics).toHaveLength(4)
    expect(failed.md.metrics.every(metric => metric.uncertainty === undefined)).toBe(true)
    expect(failed.md.summary).toContain("第 2 重复轨迹坐标一致性审计失败")
    expect(failed.md.summary).toContain("第 3 重复未启动")
  } finally { await test.cleanup() }
})

test("scheduler success cannot replace analysis bytes and tampered analyses are rejected", async () => {
  const test = await fixture()
  try {
    const output = test.data.scientific_outputs.ligand_058.replicas["3"]
    const bytes = output.analysis_manifest_text!
    output.analysis_manifest_text = undefined
    const partial = await test.read()
    expect(partial.analysis.state).toBe("waiting")
    expect(partial.analysis.observedReplicas).toBe(8)
    expect(partial.candidates.find(c => c.id.endsWith("058"))?.md.state).toBe("pending")
    output.analysis_manifest_text = bytes + " "
    await expect(test.read()).rejects.toThrow("manifest hash mismatch")
    output.analysis_manifest_text = bytes
    output.analysis_audit_text += " "
    await expect(test.read()).rejects.toThrow("audit hash mismatch")
  } finally { await test.cleanup() }
})
