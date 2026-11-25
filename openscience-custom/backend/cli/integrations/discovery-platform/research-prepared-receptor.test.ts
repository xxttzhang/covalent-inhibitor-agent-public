import { expect, test } from "bun:test"
import path from "node:path"
import { createHash } from "node:crypto"
import { tmpdir } from "node:os"
import { copyFile, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises"
import { researchContract } from "./research-loop-contract"
import { readPreparedReceptorEvidence, verifyPreparedReceptorEvidence } from "./research-prepared-receptor"

const root = path.resolve(import.meta.dir, "../../../../..")
const hash = (value: string | Buffer) => createHash("sha256").update(value).digest("hex")
async function actual() {
  const manifest = JSON.parse(await readFile(path.join(import.meta.dir, "research-scientific-manifest-v2.json"), "utf8"))
  const history = JSON.parse(await readFile(path.join(root, "reports/research-loop-20261013/historical-replay.json"), "utf8"))
  return { root, value: JSON.parse(await readFile(path.join(root, manifest.target.mapping_path), "utf8")),
    contract: researchContract.parse({ ...history.contract, target: { ...history.contract.target,
      receptor_sha256: manifest.target.receptor_sha256, mapping_sha256: manifest.target.mapping_sha256 } }),
    geometry: manifest.geometry as Record<string, number>, evaluator: manifest.evaluator,
    receptor_path: manifest.target.receptor_path, required_checks: manifest.required_receptor_checks as string[],
    core_sha256: manifest.prepared_receptor_semantics.candidate_chemistry_and_geometry_core_sha256 as string }
}

test("real failed preparation is readable evidence but cannot authorize a new scientific task", async () => {
  const input = await actual()
  const evidence = await readPreparedReceptorEvidence(input)
  expect(evidence.admissible).toBe(false)
  expect(evidence.scientific_passed).toBe(false)
  expect(evidence.audit_input_binding_complete).toBe(false)
  expect(evidence.limitations.join(" ")).toContain("Read-only historical evidence")
  expect(evidence.audit.checks.filter(item => item.status === "fail").map(item => item.id)).toEqual([
    "standard_amino_acid_stereochemistry", "intramolecular_distance_angles", "intramolecular_distance_internal"])
  expect(evidence.audit.runtime.preparation_plus_audit_cpu_core_hours).toBeGreaterThan(0)
  await expect(verifyPreparedReceptorEvidence(input)).rejects.toThrow("audit must pass every required check")
})

test("read-only evidence rejects a false summary instead of passing it to the decision model", async () => {
  const input = await actual()
  input.value.scientific_status = "pass"
  await expect(readPreparedReceptorEvidence(input)).rejects.toThrow("summary contradicts")
})

test("prepared evidence retains threshold and complete-check registry constraints", async () => {
  const input = await actual()
  await expect(readPreparedReceptorEvidence({ ...input, required_checks: input.required_checks.slice(1) }))
    .rejects.toThrow("missing or unsupported mandatory checks")
  await expect(readPreparedReceptorEvidence({ ...input, geometry: { ...input.geometry, internal_clash_lower_tolerance: 0.9 } }))
    .rejects.toThrow("numerical geometry thresholds")
})

test("prepared evidence cannot substitute receptor bytes or chemistry evaluator identities", async () => {
  const input = await actual()
  const contract = structuredClone(input.contract)
  contract.target.receptor_sha256 = "1".repeat(64)
  await expect(readPreparedReceptorEvidence({ ...input, contract })).rejects.toThrow("frozen science contract")
  await expect(readPreparedReceptorEvidence({ ...input, core_sha256: "2".repeat(64) })).rejects.toThrow("frozen science contract")
})

test("nested proof files are rehashed and constrained to the project", async () => {
  const input = await actual()
  input.value.references.independent_audit.sha256 = "3".repeat(64)
  await expect(readPreparedReceptorEvidence(input)).rejects.toThrow("evidence bytes changed")
  const outside = await actual()
  outside.value.references.source_receptor.path = "../unbound-structure.pdb"
  await expect(readPreparedReceptorEvidence(outside)).rejects.toThrow("must be contained")
})

/** Synthetic test copies preserve the original report tree unchanged. They
 * exercise provenance admission only and are never scientific pass evidence. */
async function synthetic() {
  const input = await actual()
  const directory = await realpath(await mkdtemp(path.join(tmpdir(), "SYNTHETIC-receptor-binding-")))
  const refs = input.value.references as Record<string, { path: string; sha256: string }>
  const receipt = JSON.parse(await readFile(path.join(root, refs.preparation_receipt!.path), "utf8")) as {
    artifacts: Record<string, string>; [key: string]: unknown }
  const policy = JSON.parse(await readFile(path.join(root, refs.independent_audit_policy!.path), "utf8")) as {
    initial_audit: { path: string } }
  const names = new Set([...Object.values(refs).map(item => item.path), policy.initial_audit.path,
    ...Object.keys(receipt.artifacts).map(name => path.join(path.dirname(refs.preparation_receipt!.path), name))])
  await Promise.all([...names].map(async name => {
    const target = path.join(directory, name)
    await mkdir(path.dirname(target), { recursive: true })
    await copyFile(path.join(root, name), target)
  }))
  const auditpath = path.join(directory, refs.independent_audit!.path)
  const audit = JSON.parse(await readFile(auditpath, "utf8")) as {
    status: string; checks: { status: string; measurements: Record<string, unknown> }[]; [key: string]: unknown }
  audit.synthetic_test_only = true
  audit.status = "pass"
  audit.checks = audit.checks.map(item => ({ ...item, status: "pass", measurements: { synthetic_test_only: true } }))
  input.value.scientific_status = "pass"
  const seal = async () => {
    const contents = JSON.stringify(audit)
    await writeFile(auditpath, contents)
    refs.independent_audit!.sha256 = hash(contents)
    input.contract.target.mapping_sha256 = hash(JSON.stringify(input.value))
  }
  await seal()
  return { input: { ...input, root: directory }, refs, receipt, audit, seal,
    async [Symbol.asyncDispose]() { await rm(directory, { recursive: true, force: true }) } }
}

test("a synthetic 2.1 pass remains readable but lacks native audited-input binding", async () => {
  await using data = await synthetic()
  const result = await readPreparedReceptorEvidence(data.input)
  expect(result.scientific_passed).toBe(true)
  expect(result.admissible).toBe(false)
  expect(result.audit_input_binding_complete).toBe(false)
  await expect(verifyPreparedReceptorEvidence(data.input)).rejects.toThrow("unsupported audited-input binding")
})

test("re-sealing changed topology and receipt cannot reuse a synthetic old passing audit for native admission", async () => {
  await using data = await synthetic()
  const receiptpath = path.join(data.input.root, data.refs.preparation_receipt!.path)
  const topology = path.join(path.dirname(receiptpath), "topology.json")
  const value = JSON.parse(await readFile(topology, "utf8")) as { bonds: unknown[]; [key: string]: unknown }
  value.synthetic_test_only = true
  value.bonds = value.bonds.slice(1)
  const content = JSON.stringify(value)
  await writeFile(topology, content)
  data.receipt.artifacts["topology.json"] = hash(content)
  data.receipt.synthetic_test_only = true
  const receipt = JSON.stringify(data.receipt)
  await writeFile(receiptpath, receipt)
  data.refs.preparation_receipt!.sha256 = hash(receipt)
  data.input.contract.target.mapping_sha256 = hash(JSON.stringify(data.input.value))
  const result = await readPreparedReceptorEvidence(data.input)
  expect(result.scientific_passed).toBe(true)
  expect(result.admissible).toBe(false)
  expect(result.audit_input_binding_complete).toBe(false)
  expect(result.limitations.join(" ")).toContain("does not establish that these were the audited inputs")
  await expect(verifyPreparedReceptorEvidence(data.input)).rejects.toThrow("unsupported audited-input binding")
})

for (const matching of [false, true]) test(`a manually appended 2.1 receipt hash cannot manufacture auditor support: matching=${matching}`, async () => {
  await using data = await synthetic()
  data.audit.preparation_receipt_sha256 = matching ? data.refs.preparation_receipt!.sha256 : "0".repeat(64)
  await data.seal()
  await expect(readPreparedReceptorEvidence(data.input)).rejects.toThrow(matching
    ? "does not support an added preparation receipt hash" : "receipt hash differs")
  await expect(verifyPreparedReceptorEvidence(data.input)).rejects.toThrow(matching
    ? "does not support an added preparation receipt hash" : "receipt hash differs")
})
