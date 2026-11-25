import { expect, test } from "bun:test"
import { maestroGeometry, maestroOffsets, maestroTokens } from "./maestro-preview"

const source = `f_m_ct {
i_sd_INPUT\\_SDF\\_INDEX
s_sd_COMPOUND\\_ID
:::
17 "candidate 17"
m_depend[1] {
i_m_depend_dependency
:::
1 10
:::
}
m_atom[2] {
r_m_x_coord
r_m_y_coord
r_m_z_coord
i_m_atomic_number
i_m_residue_number
s_m_pdb_atom_name
s_m_pdb_residue_name
s_m_chain_name
i_cdock_ligand_atom
:::
1 1.123456 -2.5 3 16 797 " SG " CYS A 0
2 2.987654 -2 3.25 6 900 " C1 " UNK " " 1
:::
}
}
`

test("nested dependency table is not CT metadata; exact coordinates and identity survive", () => {
  const result = maestroGeometry(source, 0)
  expect(result.inputIndex).toBe("17")
  expect(result.compound).toBe("candidate 17")
  expect(result.atoms[0]).toMatchObject({ xyz: [1.123456, -2.5, 3], element: "S", residue: 797, ligand: false })
  expect(result.atoms[1]).toMatchObject({ xyz: [2.987654, -2, 3.25], element: "C", ligand: true })
  expect(result.cif).toContain("2.987654 -2 3.25")
  expect(result.cif).toContain("_entity.type")
  expect(result.cif).toContain("LC2_900 non-polymer")
  expect(maestroOffsets(source + source)).toHaveLength(2)
  expect(maestroGeometry(source + source, 1).inputIndex).toBe("17")
})

test("invalid pose, truncated atom rows and invalid coordinates fail closed", () => {
  expect(() => maestroGeometry(source, -1)).toThrow()
  expect(() => maestroGeometry(source, 1)).toThrow()
  expect(() => maestroGeometry(source.replace("m_atom[2]", "m_atom[3]"), 0)).toThrow("incomplete")
  expect(() => maestroGeometry(source.replace("1.123456", "NaN"), 0)).toThrow("Invalid")
  expect(() => maestroTokens('"unfinished')).toThrow()
  expect(maestroTokens('# comment\n"a b" c\\_d')).toEqual(["a b", "c_d"])
})
