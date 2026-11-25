import { expect, test } from "bun:test"
import { candidateStructures, verifyStructure } from "./candidate-structure"

test("requires immutable explicit references, including exact CovDock pose", () => {
  const row = { compound_id: "test", af3_artifact_id: "af3", af3_artifact_version: "v1", af3_artifact_sha256: "a".repeat(64),
    covdock_artifact_id: "cov", covdock_artifact_version: "v2", covdock_artifact_sha256: "b".repeat(64), covdock_structure_pose: "16" }
  expect(candidateStructures(row).map((v) => [v.method, v.pose])).toEqual([["af3", undefined], ["covdock", 16]])
  for (const pose of ["", "-1", "16.0", "NaN"]) expect(candidateStructures({ ...row, covdock_structure_pose: pose })).toHaveLength(1)
  expect(candidateStructures({ model_cif: "/arbitrary/path.cif", pose_file: "2d.sdf" })).toEqual([])
  expect(candidateStructures({ ...row, af3_artifact_sha256: "bad", covdock_structure_pose: "" })).toEqual([])
})

test("rejects changed structure bytes", async () => {
  const bytes = new TextEncoder().encode("abc")
  await verifyStructure(bytes, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
  await expect(verifyStructure(bytes, "0".repeat(64))).rejects.toThrow("校验失败")
})
