import { expect, test } from "bun:test"
import { mkdir, mkdtemp, copyFile, rm } from "node:fs/promises"
import path from "node:path"
import os from "node:os"
import { createStudy, importHistorical, studyOverview } from "./study-store"
import { discoveryProject } from "@synsci/util/discovery-config"
import { studyRoutes } from "@synsci/util/discovery-study"
import { studyReport } from "@synsci/util/discovery-study-report"

test("real three-route snapshots enter the official study ingress without rewriting candidates", async () => {
  const platform = path.resolve(import.meta.dir, "../../../..")
  const config = discoveryProject.parse(await Bun.file(path.join(platform, ".discovery/project.json")).json())
  const root = await mkdtemp(path.join(os.tmpdir(), "study-history-acceptance-"))
  try {
    await mkdir(path.join(root, ".discovery"))
    await copyFile(path.join(platform, ".discovery/project.json"), path.join(root, ".discovery/project.json"))
    for (const route of studyRoutes) {
      const source = config.routes[route].dataPath!
      await mkdir(path.dirname(path.join(root, source)), { recursive: true })
      await copyFile(path.join(platform, source), path.join(root, source))
    }
    const plan = await createStudy(root, config.project, {
      title: "Real historical ingress acceptance — no scientific calculation",
      target: { id: config.target!.id, gene: "EGFR", taxon: 9606, uniprot: "P00533",
        site: { ...config.target!.site, numbering: "canonical" }, receptor: { reference: "PDB:1M17" } },
      hypothesis: { state: "pending", rationale: "Engineering acceptance only. 1M17 is a prospective plan input, not a claim that historical routes used the same receptor. Original route metadata is retained.", evidence: [] },
      routes: Object.fromEntries(studyRoutes.map(route => [route, { selected: true, mode: "reuse", inputs: {} }])),
    })
    for (const route of studyRoutes) await importHistorical(root, config.project, plan.id, route)
    const overview = await studyOverview(root, config.project, plan.id)
    expect(overview.newComputeSubmitted).toBe(false)
    expect(overview.candidates).toHaveLength(18)
    expect(overview.pool.rdkitVersion).toBeTruthy()
    expect(overview.pool.sourceCount).toBe(18)
    expect(overview.pool.groups.flatMap(group => group.sources).length).toBe(18)
    expect(studyReport(overview)).toContain("导入身份已核验并冻结")
    expect(studyReport(overview)).not.toContain("旧导入回执未记录")
    for (const route of overview.routes) {
      const original = await Bun.file(path.join(root, config.routes[route.route].dataPath!)).json()
      expect(route.state).toBe("historical-imported")
      expect(route.imported!.data.candidates).toEqual(original.candidates)
      expect(route.imported!.data.stages).toEqual(original.stages)
      expect(route.imported!.receipt.source.run).toBe(original.run)
      expect(route.imported!.receipt.source.evidenceUpdatedAt).toBe(original.evidenceUpdatedAt)
    }
  } finally { await rm(root, { recursive: true, force: true }) }
})
