// TEST ONLY. These generated reports exercise admission mechanics; they are
// deliberately labelled synthetic and are not evidence about any molecule.
import { createHash } from "node:crypto"
import { writeFile } from "node:fs/promises"
import path from "node:path"
import manifest from "./research-scientific-manifest.json"
import type { ResearchContract } from "./research-loop-contract"
import type { ResearchExecutionBinding } from "./research-execution"

const hash = (value: string) => createHash("sha256").update(value).digest("hex")
export async function writeSyntheticResearchProofs(contract: ResearchContract, binding: ResearchExecutionBinding) {
  const audit = {
    schema: "drug-design-receptor-audit-v1", evaluator: "egfr-library-scientific-evaluator-v1",
    evaluator_sha256: manifest.evaluator.sha256, structure_sha256: contract.target.receptor_sha256,
    entry_id: "1M17", uniprot: "P00533", uniprot_residue: 797, auth_asym_id: "A", author_residue_id: "773",
    reactive_atom: "SG", taxon: 9606, mapping_method: "frozen_PDB_DBREF_and_SEQADV_plus_coordinate_audit",
    mapping_status: "pass", status: "pass",
    checks: ["receptor_bytes", "dbref_mapping", "construct_record", "unique_reactive_atom", "protein_coordinates"].map(id => ({
      id, status: "pass", detail: "SYNTHETIC infrastructure fixture; no scientific audit performed",
      measurements: id === "receptor_bytes" ? { sha256: contract.target.receptor_sha256 } : {},
    })), construct_scope: "SYNTHETIC fixture; no scientific claim", new_compute: false,
  }
  const subset = {
    schema: "drug-design-frozen-library-prefix-v1", freezer_sha256: hash("SYNTHETIC freezer"),
    operation: "data-freeze-only-no-molecule-screening", header: ["SYNTHETIC_fixture"],
    selection: { duplicates_preserved: true, method: "file_order_prefix", records: 20000, source_record_index_base: 0, unique_row_hashes: 20000 },
    source: { path: binding.library, sha256: contract.inputs.library_sha256, size_bytes: 2000000 },
    subset: { encoding: "utf-8", filename: "library-prefix.csv", line_ending: "LF",
      semantics: "SYNTHETIC fixture; no library data", sha256: hash("SYNTHETIC subset"), size_bytes: 1000000 },
    records: Array.from({ length: 20000 }, (_, index) => ({ source_record_index: index, row_sha256: hash(`SYNTHETIC row ${index}`) })),
  }
  const quality = structuredClone(manifest)
  quality.scope.dataset_note = "SYNTHETIC infrastructure fixture; no scientific acceptance"
  quality.target.construct = "SYNTHETIC fixture; no scientific claim"
  quality.target.receptor_sha256 = contract.target.receptor_sha256!
  quality.recipe.sha256 = contract.inputs.science_config_sha256!
  quality.scope.dataset_source_sha256 = contract.inputs.library_sha256!
  quality.scope.dataset_subset_sha256 = subset.subset.sha256
  quality.scope.dataset_manifest_path = path.relative(contract.directory, binding.subset_manifest)
  contract.quality.required_checks = ["target_construct_and_residue_mapping", "warhead_and_reaction_compatibility",
    "pose_geometry", "registered_objective_acceptance"]
  contract.target.mapping_sha256 = hash(JSON.stringify(audit))
  contract.inputs.subset_sha256 = hash(JSON.stringify(subset))
  quality.scope.dataset_manifest_sha256 = contract.inputs.subset_sha256
  contract.quality.manifest_sha256 = hash(JSON.stringify(quality))
  await writeFile(binding.mapping, JSON.stringify(audit))
  await writeFile(binding.subset_manifest, JSON.stringify(subset))
  await writeFile(binding.quality_manifest, JSON.stringify(quality))
  return { audit, subset, quality }
}
