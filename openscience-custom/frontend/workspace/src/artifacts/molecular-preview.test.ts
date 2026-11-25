import { expect, test } from "bun:test"
import { molecularPreview } from "./molecular-preview"

test("immutable structure filenames select an explicit format for extensionless raw URLs", () => {
  expect(molecularPreview("sha-model.CIF", 237653)).toEqual({ kind: "protein-structure", format: "mmcif" })
  expect(molecularPreview("model.pdb", 500)).toEqual({ kind: "protein-structure", format: "pdb" })
  expect(molecularPreview("poses.sdf", 500)).toEqual({ kind: "chem-3d", format: "sdf" })
  expect(molecularPreview("poses.maegz", 500)).toBeUndefined()
  expect(molecularPreview("model.cif", 8 * 1024 * 1024 + 1)).toBeUndefined()
  expect(molecularPreview("model.cif", -1)).toBeUndefined()
})
