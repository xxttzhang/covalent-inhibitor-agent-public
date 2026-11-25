import { expect, test } from "bun:test"
import path from "node:path"
import { readFile } from "node:fs/promises"
import { researchContract } from "./research-loop-contract"
import { readPreparedReceptorEvidence, verifyPreparedReceptorEvidence } from "./research-prepared-receptor"
import { preparedReceptorV4 } from "./research-prepared-receptor-v4"

const root = path.resolve(import.meta.dir, "../../../../..")
async function actual() {
  const manifest = JSON.parse(await readFile(path.join(root, preparedReceptorV4.manifest.path), "utf8"))
  const history = JSON.parse(await readFile(path.join(root, "reports/research-loop-20261013/historical-replay.json"), "utf8"))
  return { root, value: JSON.parse(await readFile(path.join(root, preparedReceptorV4.binding.path), "utf8")),
    contract: researchContract.parse({ ...history.contract, target: { ...history.contract.target,
      receptor_sha256: manifest.target.receptor_sha256, mapping_sha256: preparedReceptorV4.binding.sha256 },
      quality: { ...history.contract.quality, manifest_sha256: preparedReceptorV4.manifest.sha256 } }),
    geometry: manifest.geometry as Record<string, number>, evaluator: manifest.evaluator,
    receptor_path: manifest.target.receptor_path, required_checks: manifest.required_receptor_checks as string[],
    core_sha256: manifest.prepared_receptor_semantics.candidate_chemistry_and_geometry_core_sha256 as string }
}

test("actual v4 audit binds all parent artifacts and admits scientific input without executing a task", async () => {
  const value = await verifyPreparedReceptorEvidence(await actual())
  expect(value.admissible).toBe(true)
  expect(value.scientific_passed).toBe(true)
  expect(value.audit_input_binding_complete).toBe(true)
  expect(value.audit.checks).toHaveLength(11)
  expect(value.audit.checks.every(item => item.status === "pass")).toBe(true)
  expect(value.audit.runtime.preparation_plus_audit_cpu_core_hours).toBeLessThan(0.15)
  expect(value.limitations.join(" ")).toContain("not experimentally resolved")
})

test("model-resealed passing proof cannot replace the registered actual v4 audit", async () => {
  const input = await actual()
  input.value.references.independent_audit.sha256 = "a".repeat(64)
  await expect(readPreparedReceptorEvidence(input)).rejects.toThrow("not the registered actual audited receptor")
})

test("actual v4 readiness does not authorize another target, criterion or evaluator", async () => {
  const input = await actual()
  await expect(readPreparedReceptorEvidence({ ...input, geometry: { ...input.geometry, internal_clash_lower_tolerance: 0.9 } }))
    .rejects.toThrow("registered manifest/evaluator/target")
  await expect(readPreparedReceptorEvidence({ ...input, evaluator: { ...input.evaluator, sha256: "a".repeat(64) } }))
    .rejects.toThrow("registered manifest/evaluator/target")
  await expect(readPreparedReceptorEvidence({ ...input, required_checks: input.required_checks.slice(1) })).rejects.toThrow("required checks changed")
  const contract = structuredClone(input.contract)
  contract.target.receptor_sha256 = "a".repeat(64)
  await expect(readPreparedReceptorEvidence({ ...input, contract })).rejects.toThrow("registered manifest/evaluator/target")
  contract.target.receptor_sha256 = input.contract.target.receptor_sha256
  contract.quality.manifest_sha256 = "a".repeat(64)
  await expect(readPreparedReceptorEvidence({ ...input, contract })).rejects.toThrow("registered manifest/evaluator/target")
})
