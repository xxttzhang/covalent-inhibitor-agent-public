import { test, expect } from "bun:test"
import { createRequire } from "node:module"
import path from "node:path"
import { wasmIdentity } from "@synsci/util/discovery-wasm-identity"

test("installed WASM RDKit preserves identity distinctions without a Python subprocess", async () => {
  const require = createRequire(path.resolve(import.meta.dir, "../../../../frontend/workspace/package.json"))
  const init = require("@rdkit/rdkit")
  const rd = await init({ wasmBinary: await Bun.file(require.resolve("@rdkit/rdkit/dist/RDKit_minimal.wasm")).arrayBuffer() })
  const smiles = ["CCO", "OCC", "[CH3:1][CH2:2][OH:3]", "C[C@H](O)F", "C[C@@H](O)F", "CC(O)F",
    "[13CH3]CO", "CC[O-]", "CCO.[Na+]", "OC=C", "CC=O", "F/C=C/F", "F/C=C\\F", "FC=CF",
    "invalid", "*CC", "CCO |atomProp:0.foo.bar|", "C=CC(=O)N"]
  const values = smiles.map(text => wasmIdentity(rd, text))
  expect(rd.version()).toBeTruthy()
  for (const i of [1, 2]) expect(values[i].canonical).toBe(values[0].canonical)
  for (const i of [6, 7, 8]) expect(values[i].canonical).not.toBe(values[0].canonical)
  expect(values[3].canonical).not.toBe(values[4].canonical)
  expect(values[9].canonical).not.toBe(values[10].canonical)
  expect(values[11].canonical).not.toBe(values[12].canonical)
  for (const i of [5, 13, 15]) expect(values[i].state).toBe("unresolved")
  for (const i of [14, 16]) expect(values[i].state).toBe("invalid")
  expect(values[17].state).toBe("validated")
})
