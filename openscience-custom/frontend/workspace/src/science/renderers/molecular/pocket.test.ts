import { expect, test } from "bun:test"
import { pocketLigands, pocketNeighborhood, pocketTarget, type PocketResidue } from "./pocket"
import type { TargetContext } from "@synsci/util/screening-target"

const residue = (key: string, x: number, polymer = true, component = "CYS"): PocketResidue => ({
  key, label: key, chain: "A", number: 773, component, polymer,
  atoms: [0, 1, 2].map((i) => ({ unit: 1, index: i, name: i === 0 ? "SG" : "C", element: i === 0 ? "S" : "C", xyz: [x, i, 0] })),
})
test("keeps separate ligand instances and excludes water and ions", () => {
  const ion = { ...residue("ion", 0, false, "ZN"), atoms: residue("ion", 0).atoms.slice(0, 1) }
  expect(pocketLigands([residue("protein", 0), residue("water", 0, false, "HOH"), ion,
    residue("lig1", 0, false, "UNK"), residue("lig2", 0, false, "UNK")]).map((r) => r.key)).toEqual(["lig1", "lig2"])
})
test("5 Angstrom heavy-atom neighborhood includes whole residues and preserves numbering", () => {
  const ligand = residue("lig", 0, false, "UNK")
  const result = pocketNeighborhood([ligand, residue("near", 1.8), residue("edge", 5), residue("far", 5.01)], ligand, 5)
  expect(result.neighbors.map((r) => r.key)).toEqual(["near", "edge"])
  expect(result.reactive.map((r) => r.key)).toEqual(["near"])
  expect(result.reactive[0].number).toBe(773)
  expect(result.neighbors[0].atoms).toHaveLength(3)
})
test("does not pick a unique target when geometry is ambiguous; validates radius", () => {
  const ligand = residue("lig", 0, false, "UNK")
  expect(pocketNeighborhood([residue("a", 1.8), residue("b", -1.8)], ligand, 5).reactive).toHaveLength(2)
  for (const radius of [NaN, -1, 11]) expect(() => pocketNeighborhood([], ligand, radius)).toThrow()
})

test("task targets require exact file, chain, insertion code and residue; supports non-CYS", () => {
  const ser = { ...residue("ser", 2, true, "SER"), number: 195, chain: "B", insertionCode: "A" }
  const context: TargetContext = { task_id: "task", plan_digest: "a".repeat(64), artifact_sha256: "b".repeat(64),
    target: "TEST", site: "S195A", parameters: { target: "TEST", receptor: "prepared.cif", chain: "B", residue: "SER",
      residue_number: 195, insertion_code: "A", numbering: "structure", reaction_type: "Acylation", receptor_sha256: "b".repeat(64) } }
  expect(pocketTarget([ser], [], context).target).toEqual(ser)
  expect(pocketTarget([ser, { ...ser, key: "copy" }], [], context).target).toBeUndefined()
  expect(pocketTarget([{ ...ser, chain: "A" }], [], context).target).toBeUndefined()
  expect(pocketTarget([{ ...ser, insertionCode: "" }], [], context).target).toBeUndefined()
  expect(pocketTarget([ser], [], { ...context, artifact_sha256: "c".repeat(64) }).target).toBeUndefined()
  expect(pocketTarget([ser], [], { ...context, parameters: { ...context.parameters!, numbering: "canonical" } }).target).toBeUndefined()
  // A CYS797 in an unrelated structure no longer becomes the default target.
  expect(pocketTarget([{ ...residue("wrong", 2), number: 797 }], []).target).toBeUndefined()
})
