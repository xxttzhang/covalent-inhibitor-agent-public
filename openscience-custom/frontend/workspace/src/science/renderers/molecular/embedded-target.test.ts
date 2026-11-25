import { expect, test } from "bun:test"
import { embeddedTarget, targetMarker } from "./embedded-target"

const body = "ATOM      1  SG  CYS A 797      50.463   7.017 -17.184  1.00 20.00           S  \nHETATM    2  C5  LIG Z   1      50.463  10.317 -17.184  1.00  0.00           C  \nEND\n"
const hash = (text: string) => new Bun.CryptoHasher("sha256").update(text).digest("hex")
const record = { task_id: "denovo-" + "a".repeat(24), plan_digest: "b".repeat(64), structure_sha256: hash(body),
  source_receptor_sha256: "c".repeat(64), target: "EGFR", chain: "A", residue: "CYS", residue_number: 797,
  insertion_code: "", atom: "SG", reaction_type: "Michael Addition", ligand: { chain: "Z", residue: "LIG", residue_number: 1, atom: "C5" } }
const file = (value = record, content = body) => targetMarker + JSON.stringify(value) + "\n" + content

test("binds declared residue and exact reaction atoms even at a pre-reaction distance", async () => {
  const result = await embeddedTarget(file())
  expect(result?.context.parameters?.residue_number).toBe(797)
  expect(result?.context.parameters?.receptor_sha256).toBe(hash(file()))
  expect(result?.atom).toBe("SG")
  expect(result?.ligand.atom).toBe("C5")
})
test("never guesses C797 in an unannotated structure", async () => {
  expect(await embeddedTarget(body)).toBeUndefined()
})
test("rejects changed coordinates, missing atoms, wrong chains and duplicate metadata", async () => {
  await expect(embeddedTarget(file(record, body.replace('50.463', '51.463')))).rejects.toThrow("校验")
  await expect(embeddedTarget(file({ ...record, chain: 'B' }))).rejects.toThrow("缺失")
  await expect(embeddedTarget(file({ ...record, ligand: { ...record.ligand, atom: 'C9' } }))).rejects.toThrow("缺失")
  await expect(embeddedTarget(targetMarker + JSON.stringify(record) + '\n' + file())).rejects.toThrow("多个")
})
test("does not bake EGFR numbering into the viewer", async () => {
  const changed = body.replace('CYS A 797', 'CYS B 123')
  const result = await embeddedTarget(file({ ...record, target: 'TEST', chain: 'B', residue_number: 123, structure_sha256: hash(changed) }, changed))
  expect(result?.context.parameters?.chain).toBe('B')
  expect(result?.context.parameters?.residue_number).toBe(123)
})
