import { expect, test } from "bun:test"
import { cp, mkdir, mkdtemp, rm, symlink } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { collector, fingerprint, root, verify } from "./worker"

test("published real report contains every replica and a verifiable source bundle", async () => {
  const current = await fingerprint(root, binding.md)
  expect(current.count).toBe(12)
  const folder = path.join(root, "openscience-custom/results/discovery/md-analysis", current.signature)
  const manifest = await verify(folder, binding.md.chartCount)
  expect(manifest.charts.filter(c => c.system === "denovo_007")).toHaveLength(5)
  const summary = await Bun.file(path.join(folder, "sampling-summary.json")).json()
  expect(summary.replicas).toHaveLength(12)
  expect(summary.systems.denovo_007.equilibrium_proven).toBe(false)
  const before = (await Bun.file(path.join(folder, "manifest.json")).stat()).mtimeMs
  await collector(root, path.join(root, "openscience-custom"), binding.project, binding.md)()
  expect((await Bun.file(path.join(folder, "manifest.json")).stat()).mtimeMs).toBe(before)
})

test("incomplete inputs wait; corrupted complete inputs fail without publishing plots", async () => {
  const temp = await mkdtemp(path.join(tmpdir(), "md-analysis-"))
  try {
    await cp(path.join(root, source), path.join(temp, source), { recursive: true })
    await mkdir(path.join(temp, "ops"))
    for (const name of ["export_md_workbench.py", "summarize_md_replicas_20261003.py", "replica_statistics_20261003.py", "audit_openmm_pilot_20261002.py"])
      await cp(path.join(root, "ops", name), path.join(temp, "ops", name))
    await mkdir(path.join(temp, ".tooling"))
    await symlink(path.join(root, ".tooling/md-validation-20261003"), path.join(temp, ".tooling/md-validation-20261003"))
    const missing = path.join(temp, source, "runs/denovo_001-r1/analysis-v1/metrics-manifest.json")
    const original = await Bun.file(missing).text()
    await rm(missing)
    const collect = collector(temp, path.join(temp, "openscience-custom"), binding.project, binding.md)
    await collect()
    const status = path.join(temp, "openscience-custom/" + binding.md.statePath)
    expect((await Bun.file(status).json()).phase).toBe("waiting")
    expect((await Bun.file(status).json()).count).toBe(11)
    await Bun.write(missing, original)
    const csv = path.join(temp, source, "runs/denovo_001-r1/analysis-v1/trajectory-measurements.csv")
    await Bun.write(csv, (await Bun.file(csv).text()) + "\n")
    await collect()
    const result = await Bun.file(status).json()
    expect(result.phase).toBe("failed")
    expect(result.charts).toEqual([])
    expect(result.bundle).toBeNull()
    const started = result.startedAt
    await collect()
    expect((await Bun.file(status).json()).startedAt).toBe(started)
  } finally { await rm(temp, { recursive: true, force: true }) }
}, 30000)

const binding = await Bun.file(path.resolve(import.meta.dir, "../../../../.discovery/runtime.json")).json()
const source = binding.md.source
