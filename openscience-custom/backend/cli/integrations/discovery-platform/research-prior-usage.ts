import path from "node:path"
import { readFile } from "node:fs/promises"
import { emptyResources, researchDigest, type ResearchContract } from "./research-loop-contract"
import type { ResearchLoopStore } from "./research-loop-store"
import { preparedReceptorV4, readPreparedReceptorV4 } from "./research-prepared-receptor-v4"

/** Host-only carry-forward of already measured scientific preparation. This
 * reads the registered actual evidence; there is no caller-supplied amount or
 * model-authored receipt. It performs no chemistry, model call or remote work. */
export async function prepareResearchPriorUsage(contract: ResearchContract) {
  if (contract.mode !== "native-compute" || contract.quality.manifest_sha256 !== preparedReceptorV4.manifest.sha256) return null
  const root = contract.directory
  const manifest = JSON.parse(await readFile(path.join(root, preparedReceptorV4.manifest.path), "utf8"))
  const value = JSON.parse(await readFile(path.join(root, preparedReceptorV4.binding.path), "utf8"))
  // This rehashes the code-pinned manifest, binding, actual audit, receipts and
  // all parent artifacts, and cross-checks measured seconds and cumulative sum.
  const proof = await readPreparedReceptorV4({ root, contract, value, geometry: manifest.geometry,
    evaluator: manifest.evaluator, receptor_path: manifest.target.receptor_path, required_checks: manifest.required_receptor_checks,
    core_sha256: manifest.prepared_receptor_semantics.candidate_chemistry_and_geometry_core_sha256 })
  if (!proof.admissible) throw Error("Prior preparation must belong to the admitted receptor")
  const key = `prior-scientific-usage:${proof.audit_sha256}`
  const resources = { ...emptyResources(), cpu_core_hours: proof.audit.runtime.cumulative_cpu_core_hours }
  const receipt = { schema: "drug-design-prior-scientific-usage-v1", contract_digest: researchDigest(contract),
    scope: "measured-local-receptor-preparation-audits-and-diagnostics", platform_bill: false, new_computation: false,
    binding: preparedReceptorV4.binding, manifest: preparedReceptorV4.manifest, audit: proof.references.independent_audit,
    previous_cpu_core_hours: proof.audit.runtime.previous_preparation_and_audits_cpu_core_hours,
    v4_cpu_core_hours: proof.audit.runtime.preparation_plus_audit_cpu_core_hours, resources }
  const reservation = { key, kind: "evaluation", resources, round: 0, candidate_slots: 0,
    plan_digest: researchDigest(receipt), source_release: contract.inputs.source_release }
  const verify = (store: ResearchLoopStore, id: string) => {
    if (store.get(id).digest !== receipt.contract_digest) throw Error("Prior scientific usage belongs to another contract")
    const evidence = store.evidence(id, "host").find(item => item.key === key)
    const row = store.reservations(id).find(item => item.key === key)
    if (!evidence || researchDigest(evidence.payload) !== researchDigest(receipt) || !row
      || researchDigest(row.payload) !== researchDigest(reservation) || row.state !== "SETTLED" || row.receipt !== key
      || researchDigest(row.actual) !== researchDigest(resources) || row.task !== null)
      throw Error("Bound prior scientific CPU usage has not been carried into the research ledger")
  }
  return {
    // A detached view cannot change the captured authoritative receipt.
    receipt: structuredClone(receipt),
    verify,
    record(store: ResearchLoopStore, id: string) {
      return store.db.transaction(() => {
        const loop = store.get(id)
        if (loop.digest !== receipt.contract_digest) throw Error("Prior scientific usage belongs to another contract")
        if (store.reservations(id).some(item => item.key === key) || store.evidence(id).some(item => item.key === key)) {
          verify(store, id)
          return store.get(id)
        }
        if (loop.state !== "READY" || store.reservations(id).length || store.evidence(id, "decision").length)
          throw Error("Prior scientific usage must be carried before any model or computation")
        store.record(id, key, receipt)
        store.reserve(id, store.get(id).revision, reservation)
        store.settle(id, key, resources, key)
        verify(store, id)
        return store.get(id)
      }).immediate()
    },
  }
}
