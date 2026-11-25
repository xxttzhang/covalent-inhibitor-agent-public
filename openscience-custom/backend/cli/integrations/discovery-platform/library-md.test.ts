import { expect, test } from "bun:test"
import path from "node:path"
import { binding } from "./binding"
import { discoveryProject } from "@synsci/util/discovery-config"
import { snapshot } from "../refinement-monitor/monitor"
import { adapt } from "./adapter"
import { libraryMD } from "./library-md"

const root = path.resolve(import.meta.dir, "../../../..")
const config = discoveryProject.parse(await Bun.file(path.join(root, ".discovery/project.json")).json())
const settings = binding.parse(await Bun.file(path.join(root, ".discovery/runtime.json")).json())
test("Library MD links actual immutable figures and replica metrics, rejects corrupted evidence and identity changes", async () => {
  const raw = await snapshot("library-first", settings.sourceRoot, settings)
  const packet = adapt(raw, config, "library-first")
  const complete = await libraryMD(packet, raw, settings.sourceRoot, settings.libraryMD!)
  expect(complete.analysis.observedReplicas).toBe(24)
  expect(complete.stages.find(s => s.id === "md")?.state).toBe("completed")
  const first = complete.candidates.find(c => c.md.chartIds.includes("library_017:rmsd"))!
  expect(first.md.metrics[0].value).toBeCloseTo(2.93760033397592)
  expect(first.md.metrics[2].value).toBeCloseTo(72.82121212121213)
  expect(first.md.metrics[2].uncertainty?.independentN).toBe(3)
  expect(first.scientificEvidence?.candidateID).toBe(first.id)
  const measurements = first.scientificEvidence!.measurements.filter(item => item.scope === "md")
  expect(measurements).toHaveLength(3)
  for (const item of measurements) {
    expect(item.stage).toBe("md")
    expect(item.inputs[0].sha256).toBe(settings.libraryMD!.pins["review-v1/replica-review-evidence.json"])
    expect(item.evidence.path).toEndWith("review-v1/system-summary.csv")
    expect(item.unit).toBe(first.md.metrics.find(metric => metric.id === item.metricID)!.unit)
  }
  expect(complete.reports.some(r => r.asset.name === "batch-summary.png")).toBe(true)
  expect(complete.reports.some(r => r.asset.name === "analysis-bundle.zip")).toBe(true)
  await expect(libraryMD(packet, raw, settings.sourceRoot, { ...settings.libraryMD!, pins: { ...settings.libraryMD!.pins, "review-v1/verification.json": "0".repeat(64) } })).rejects.toThrow("hash mismatch")
  const changed = structuredClone(raw)
  changed.candidates[0].row.input_sdf_index = "999"
  await expect(libraryMD(packet, changed, settings.sourceRoot, settings.libraryMD!)).rejects.toThrow("identity mismatch")
})
