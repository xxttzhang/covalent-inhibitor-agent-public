import { expect, test } from "bun:test"
import { cp, mkdir, mkdtemp, rm } from "node:fs/promises"
import { tmpdir } from "node:os"
import path from "node:path"
import { gunzipSync } from "node:zlib"
import { root, snapshot } from "./monitor"
import { maestroGeometry } from "../../../../frontend/workspace/src/artifacts/maestro-preview"

test("library keeps eight identities, three A and five B1, with MD pending", async () => {
  const data = await snapshot("library-first", root, binding)
  expect(data.candidates).toHaveLength(8)
  expect(data.candidates.filter(c => c.featured)).toHaveLength(3)
  expect(data.candidates.filter(c => c.badge === "Gate B1")).toHaveLength(5)
  expect(data.current).toBe("md")
  expect(data.mdReason).toContain("23")
  const file = path.join(root, "openscience-custom/inspire-results/screen-9b56c32831224a76a3848d43/refinements/refineflow-76378344dae197d0022d4549/covdock/2dfd05e435fc6ded069d93d727f59b6c62fbc486e66bb1e15a05fcf8a0701fd5-library_first_core_subset_covdock_lo-out.maegz")
  const text = gunzipSync(await Bun.file(file).arrayBuffer()).toString()
  for (const candidate of data.candidates) {
    const pose = maestroGeometry(text, Number(candidate.row.covdock_structure_pose))
    expect(pose.compound).toBe(candidate.row.compound_id)
    expect(pose.inputIndex).toBe(candidate.row.input_sdf_index)
  }
})

test("ligand displays verified current AF3 models and only the recovered 066 pose", async () => {
  const data = await snapshot("ligand-first", root, binding)
  expect(data.candidates).toHaveLength(3)
  expect(data.stages.find(s => s.id === "dock")?.state).toBe("blocked")
  expect(data.candidates.filter(c => c.row.covdock_artifact_id)).toHaveLength(1)
  expect(data.candidates.filter(c => c.row.af3_artifact_id)).toHaveLength(3)
  expect(data.candidates.every(c => c.row.actual_sample_count === "1")).toBe(true)
  const candidate = data.candidates.find(c => c.featured)!
  const text = gunzipSync(await Bun.file(path.join(root, "reports/ligand-covdock-serial-retry-clean-20261008/retained-066-capsule/covdock_lo/library_first_af3_top_covdock_lo-out.maegz")).arrayBuffer()).toString()
  const pose = maestroGeometry(text, Number(candidate.row.covdock_structure_pose))
  expect(pose.compound).toBe(candidate.row.compound_id)
  expect(pose.inputIndex).toBe(candidate.row.input_sdf_index)
  expect(candidate.row.covdock_gate_detail).toBe("B2")
  expect(data.stages.find(s => s.id === "md")?.state).toBe("pending")
})

test("rejects changed evidence and swapped structure identities", async () => {
  const data = await snapshot("ligand-first", root, binding)
  const temp = await mkdtemp(path.join(tmpdir(), "refinement-monitor-"))
  try {
    for (const source of data.sources) {
      await mkdir(path.dirname(path.join(temp, source.path)), { recursive: true })
      await cp(path.join(root, source.path), path.join(temp, source.path))
    }
    expect((await snapshot("ligand-first", temp, binding)).candidates).toHaveLength(3)
    const file = path.join(temp, "reports/three-route-workbench-20261008/structures.json")
    const registration = await Bun.file(file).json()
    registration.structures[0].artifact.current.sha256 = "0".repeat(64)
    await Bun.write(file, JSON.stringify(registration))
    await expect(snapshot("ligand-first", temp, binding)).rejects.toThrow("AF3 structure registration mismatch")
    await cp(path.join(root, "reports/three-route-workbench-20261008/structures.json"), file)
    const csv = path.join(temp, "reports/ligand-af3-recovery-20261008-v2/verified-evidence/artifacts/stage4_top_candidates.csv")
    await Bun.write(csv, (await Bun.file(csv).text()) + " ")
    await expect(snapshot("ligand-first", temp, binding)).rejects.toThrow("Evidence hash mismatch")
  } finally { await rm(temp, { recursive: true, force: true }) }
})

const binding = await Bun.file(path.resolve(import.meta.dir, "../../../../.discovery/runtime.json")).json()
