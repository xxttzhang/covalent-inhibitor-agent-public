import { test, expect } from "bun:test"
import { checkWarheads } from "./warhead-check"

test("real RDKit distinguishes four classes and excludes cyanoacrylamides", async () => {
  const result = await checkWarheads(["C=CC(=O)NC", "CN(C)C#N", "CC#N", "C=CC(=O)OC", "C=C(C#N)C(=O)NC", "C=C(C#N)C(=O)OC", "N#Cc1ccccc1", "invalid", "*CC#N"], false)
  expect(result.rows.map(row => row.matches.map(match => match.warhead))).toEqual([
    ["acrylamide"], ["cyanamide"], ["nitrile"], ["methyl-acrylate"], [], [], ["nitrile"], [], []])
  expect(result.rows[7]!.state).toBe("invalid")
  expect(result.rows[8]!.state).toBe("invalid")
  expect(result.establishes_reactivity).toBe(false)
})
test("multiple reactive motifs are reported and indices point into the input graph", async () => {
  const result = await checkWarheads(["C=CC(=O)NCCNC(=O)C=C", "CN(C)C#N"], false)
  expect(result.rows[0]!.matches).toHaveLength(2)
  expect(result.rows[1]!.matches[0]!.electrophile_index).toBe(3)
})
