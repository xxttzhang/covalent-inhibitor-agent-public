import { expect, test } from "bun:test"
import { candidatePool, chemicalIdentities } from "./chemical-identity"

test("sandbox-enabled chemistry groups identities without unrestricted Python", async () => {
  const records = ["a", "b"].map(recordID => ({ recordID, candidate: { smiles: "CCO" } }))
  const pool = await candidatePool(records, false)
  expect(pool.rdkitVersion).toBeTruthy()
  expect(pool.groups).toHaveLength(1)
  expect(pool.groups[0].sources).toEqual(records)
})

test("real RDKit identity preserves chemical distinctions", async () => {
  const inputs = ["CCO", "OCC", "[CH3:1][CH2:2][OH:3]", "C[C@H](O)F", "C[C@@H](O)F",
    "CC(O)F", "CC(O)F", "[13CH3]CO", "CC[O-]", "CCO.[Na+]", "OC=C", "CC=O",
    "F/C=C/F", "F/C=C\\F", "FC=CF", "not-a-smiles", "*CC", "CCO |atomProp:0.foo.bar|"]
  const result = await chemicalIdentities(inputs)
  expect(result.version).toBeTruthy() // fail, not skip, when the configured chemistry runtime is absent
  const values = result.identities
  expect(values[0].canonical).toBe(values[1].canonical)
  expect(values[0].canonical).toBe(values[2].canonical)
  expect(values[3].canonical).not.toBe(values[4].canonical)
  expect(values[5].state).toBe("unresolved")
  for (const index of [7, 8, 9]) expect(values[index].canonical).not.toBe(values[0].canonical)
  expect(values[10].canonical).not.toBe(values[11].canonical)
  expect(values[12].canonical).not.toBe(values[13].canonical)
  expect(values[14].state).toBe("unresolved")
  expect(values[15].state).toBe("invalid")
  expect(values[16].state).toBe("unresolved")
  expect(values[17].state).toBe("invalid")
})

test("pool retains all source-specific metrics and never ranks or merges uncertain molecules", async () => {
  const records = ["CCO", "OCC", "CC(O)F", "CC(O)F", "invalid", "invalid"].map((smiles, index) => ({
    recordID: String(index), route: index % 2 ? "ligand-first" : "library-first",
    candidate: { smiles, metrics: [{ method: index % 2 ? "AF3" : "CovDock", value: index === 1 ? null : index }] },
  }))
  const pool = await candidatePool(records)
  expect(pool.rdkitVersion).toBeTruthy()
  expect(pool.sourceCount).toBe(6)
  expect(pool.groups).toHaveLength(5)
  expect(pool.groups[0].sources).toEqual(records.slice(0, 2))
  expect(pool.groups[0].sources[1].candidate.metrics[0].value).toBeNull()
  expect(pool.ranked).toBe(false)
})
