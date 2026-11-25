import { describe, expect, test } from "bun:test"
import path from "node:path"
import { mkdtemp, mkdir, cp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import { snapshot } from "./monitor"
import { workbench, stages, stale } from "../../../../frontend/workspace/src/discovery/model"

const root = path.resolve(import.meta.dir, "../../../../..")
const presentation = (await Bun.file(path.join(root, "openscience-custom/.discovery/project.json")).json()).routes["de-novo"]
describe("De novo evidence workbench", () => {
  test("reads the real candidate identities, verified MD denominator and actual failed pose", async () => {
    const data = workbench.parse(await snapshot(root, binding))
    expect(data.project).toBe(study.project)
    expect(data.generation.slots).toBe(8)
    expect(data.generation.passed).toBe(7)
    expect(data.docking.poses).toBe(70)
    expect(data.docking.passed).toBe(69)
    expect(data.candidates.filter(c => c.md).map(c => c.row.compound_id).sort()).toEqual(["denovo_001", "denovo_004", "denovo_007"])
    expect(data.md.replicas).toBe(12)
    expect(data.candidates.find(c => c.row.compound_id === "denovo_001")?.md?.rmsd.replica_estimates).toHaveLength(3)
    expect(data.md.control.equilibrium_proven).toBe(false)
    expect(data.amber.productionStarted).toBe(false)
    expect(stages(data, presentation).find(s => s.id === "md")?.status).toBe("计算完成")
    expect(stages(data, presentation).find(s => s.id === "amber")?.status).toBe("参数准备")
    expect(data.reports.some(r => r.name === "REPEATED_MD_REPORT_PLATFORM.md")).toBe(true)
  })
  test("refuses modified MD evidence and missing generation status instead of inventing success", async () => {
    const original = await snapshot(root, binding)
    const temp = await mkdtemp(path.join(tmpdir(), "denovo-workbench-"))
    try {
      for (const item of original.sources) {
        const destination = path.join(temp, item.path)
        await mkdir(path.dirname(destination), { recursive: true })
        await cp(path.join(root, item.path), destination)
      }
      expect((await snapshot(temp, binding)).md.replicas).toBe(12)
      const filename = path.join(temp, "reports/md-replicas-final-20261008-v1/sampling-summary.json")
      await Bun.write(filename, (await Bun.file(filename).text()) + " ")
      await expect(snapshot(temp, binding)).rejects.toThrow("MD summary hash mismatch")
      await rm(path.join(temp, `.cache/denovo/tasks/${study.task}/status.json`))
      await expect(snapshot(temp, binding)).rejects.toThrow()
    } finally { await rm(temp, { recursive: true, force: true }) }
  })
  test("marks an expired observation stale independently of the last successful scientific result", () => {
    const now = Date.now()
    expect(stale(new Date(now - 61000).toISOString(), now)).toBe(true)
    expect(stale(new Date(now - 10000).toISOString(), now)).toBe(false)
    expect(stale("bad timestamp", now)).toBe(true)
  })
})

const binding = await Bun.file(path.resolve(import.meta.dir, "../../../../.discovery/runtime.json")).json()
const study = binding.study
