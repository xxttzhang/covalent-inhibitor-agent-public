"""Local candidate evaluation against an explicitly audited prepared 1M17 receptor.

No preparation, molecular generation, external computation, model or network calls.
Frozen v1 candidate chemistry/geometry is reused unchanged. Prepared-model provenance
is checked separately; occupancy zero is never relabelled experimental evidence.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import numpy as np
import yaml
from rdkit import Chem, rdBase

VERSION = "egfr-library-scientific-evaluator-v4.0"
WORKSPACE = Path(__file__).resolve().parents[5]
DEFAULT_MANIFEST = Path(__file__).with_name("research-scientific-manifest-v4.json")
FROZEN_CORE_SHA256 = "ba6829a12d96df43e6c3b53079c003dcb65bf88e13c94f2667db03f1687ae0b8"
RECEPTOR_CHECKS = ["unique_atom_identity", "finite_coordinates", "atom_provenance_and_original_coordinates", "full_deposited_sequence_and_heavy_atom_completeness", "polymer_connectivity", "reactive_site_identity_and_prereaction_hydrogen", "standard_amino_acid_stereochemistry", "intramolecular_distance_bonds", "intramolecular_distance_angles", "intramolecular_distance_internal", "aromatic_planarity"]
SCIENTIFIC_CHECKS = ["target_construct_and_residue_mapping", "warhead_and_reaction_compatibility", "pose_geometry", "registered_objective_acceptance"]
BINDING_REFS = {"source_receptor", "prepared_receptor", "source_mapping_audit", "preparation_policy", "preparation_script", "preparation_receipt", "atom_mapping", "independent_audit_policy", "independent_audit", "independent_auditor", "candidate_evaluator", "frozen_v1_evaluator", "frozen_v1_manifest"}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def workspace_path(relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ValueError("Expected repository-relative proof path")
    resolved = (WORKSPACE / relative).resolve()
    if not resolved.is_relative_to(WORKSPACE):
        raise ValueError("Proof path escapes repository")
    return resolved


def verified_ref(reference):
    if set(reference) != {"path", "sha256"}:
        raise ValueError("Invalid proof reference")
    path = workspace_path(reference["path"])
    if sha(path) != reference["sha256"]:
        raise ValueError("Proof bytes differ: " + reference["path"])
    return path


def require(condition, message):
    if not condition:
        raise ValueError(message)


_core_path = Path(__file__).with_name("research-scientific-evaluator.py")
require(sha(_core_path) == FROZEN_CORE_SHA256, "Frozen v1 scientific core changed")
_spec = importlib.util.spec_from_file_location("frozen_scientific_core", _core_path)
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
csv_rows, candidate_chemistry, geometry_checks, check, combined = core.csv_rows, core.candidate_chemistry, core.geometry_checks, core.check, core.combined


def load_manifest(filename=DEFAULT_MANIFEST):
    value = json.loads(Path(filename).read_text())
    require(value["schema"] == "drug-design-scientific-manifest-v4" and value["evaluator"]["version"] == VERSION, "Unknown scientific version")
    require(value["evaluator"]["sha256"] == sha(__file__) and verified_ref({key:value["evaluator"][key] for key in ("path", "sha256")}) == Path(__file__).resolve(), "Candidate evaluator binding changed")
    require(value["evaluator"]["packages"] == {"rdkit": rdBase.rdkitVersion, "numpy": np.__version__, "pyyaml": yaml.__version__}, "Scientific package versions changed")
    require(value["required_receptor_checks"] == RECEPTOR_CHECKS and value["required_scientific_checks"] == SCIENTIFIC_CHECKS, "Required scientific checks changed")
    binding = json.loads(verified_ref({"path":value["target"]["mapping_path"], "sha256":value["target"]["mapping_sha256"]}).read_text())
    require(binding["schema"] == "drug-design-prepared-receptor-binding-v4" and binding["method_revision"] == "4.0", "Unknown prepared receptor proof schema")
    require(set(binding["references"]) == BINDING_REFS, "Incomplete receptor proof references")
    paths = {name: verified_ref(ref) for name, ref in binding["references"].items()}
    require(sha(paths["frozen_v1_evaluator"]) == FROZEN_CORE_SHA256 and paths["candidate_evaluator"] == Path(__file__).resolve(), "Scientific core identity changed")
    frozen = json.loads(paths["frozen_v1_manifest"].read_text())
    for key in ("geometry", "recipe", "artifacts", "acceptance"):
        require(value[key] == frozen[key], "Frozen v1 candidate criterion changed: " + key)
    for key in ("max_candidates", "minimum_accepted_candidates", "source_rows", "selection", "dataset_subset_sha256", "dataset_source_sha256", "dataset_manifest_sha256", "dataset_manifest_path"):
        require(value["scope"][key] == frozen["scope"][key], "Frozen scientific scope changed: " + key)
    return value


def receptor_audit(pdb, manifest):
    target = manifest["target"]
    require(Path(pdb).resolve() == workspace_path(target["receptor_path"]) and sha(pdb) == target["receptor_sha256"], "Prepared receptor bytes/path changed")
    binding_path = verified_ref({"path":target["mapping_path"], "sha256":target["mapping_sha256"]})
    binding = json.loads(binding_path.read_text())
    require(binding["schema"] == "drug-design-prepared-receptor-binding-v4" and binding["method_revision"] == "4.0" and set(binding["references"]) == BINDING_REFS, "Incomplete receptor binding")
    refs = binding["references"]
    paths = {name: verified_ref(ref) for name, ref in refs.items()}
    require(paths["prepared_receptor"] == Path(pdb).resolve() and paths["candidate_evaluator"] == Path(__file__).resolve(), "Target/evaluator reference mismatch")
    frozen = json.loads(paths["frozen_v1_manifest"].read_text())
    source_audit = json.loads(paths["source_mapping_audit"].read_text())
    actual_source_audit, _ = core.receptor_audit(paths["source_receptor"], frozen)
    require(source_audit == actual_source_audit and source_audit["mapping_status"] == "pass", "Original DBREF/construct/site mapping is not independently verified")
    require(target["entry"] == source_audit["entry_id"] == "1M17" and target["uniprot"] == source_audit["uniprot"] == "P00533" and target["canonical_residue"] == source_audit["uniprot_residue"] == 797 and target["author_residue"] == int(source_audit["author_residue_id"]) == 773 and target["chain"] == "A" and target["atom"] == "SG", "Target mapping identity mismatch")
    policy = json.loads(paths["preparation_policy"].read_text())
    receipt = json.loads(paths["preparation_receipt"].read_text())
    mapping = json.loads(paths["atom_mapping"].read_text())
    audit_policy = json.loads(paths["independent_audit_policy"].read_text())
    audit = json.loads(paths["independent_audit"].read_text())
    source_hash, prepared_hash, policy_hash = refs["source_receptor"]["sha256"], refs["prepared_receptor"]["sha256"], refs["preparation_policy"]["sha256"]
    require(policy["schema"] == "drug-design-receptor-preparation-policy-v4" and policy["source"]["sha256"] == source_hash and policy["preparation_script_sha256"] == refs["preparation_script"]["sha256"], "Preparation policy identity mismatch")
    require(policy["v1_manifest"] == refs["frozen_v1_manifest"] and policy["geometry"] == manifest["geometry"], "Preparation policy criterion mismatch")
    require(receipt["schema"] == "drug-design-receptor-preparation-receipt-v4" and receipt["status"] == "prepared_pending_independent_audit" and receipt["source_sha256"] == source_hash and receipt["policy_sha256"] == policy_hash and receipt["script_sha256"] == refs["preparation_script"]["sha256"], "Preparation receipt identity mismatch")
    require(receipt["platform"] == "CPU" and receipt["threads"] == 1 and receipt["new_gpu_compute"] is False and receipt["modeled_coordinates_are_experimental"] is False, "Preparation scope changed")
    for filename, digest in receipt["artifacts"].items():
        path = (paths["preparation_receipt"].parent / filename).resolve()
        require(path.is_relative_to(paths["preparation_receipt"].parent) and sha(path) == digest, "Preparation receipt artifact changed")
    require(mapping["schema"] == "drug-design-prepared-receptor-atom-map-v4" and mapping["source_sha256"] == source_hash and mapping["prepared_sha256"] == prepared_hash and mapping["policy_sha256"] == policy_hash, "Atom provenance identity mismatch")
    require(audit_policy["schema"] == "drug-design-prepared-receptor-audit-policy-v4" and audit_policy["method_revision"] == "4.0" and audit_policy["preparation_policy"] == refs["preparation_policy"] and audit_policy["auditor"] == refs["independent_auditor"], "Independent audit policy mismatch")
    verified_ref(audit_policy["initial_audit"])
    require(audit_policy["geometry"] == manifest["geometry"] and audit_policy["required_checks"] == RECEPTOR_CHECKS and audit_policy["thresholds_changed_from_v1"] is False and audit_policy["coordinate_changes"] is False, "Audit scientific criteria changed")
    require(audit["schema"] == "drug-design-prepared-receptor-audit-v4" and audit["method_revision"] == "4.0", "Unknown independent audit")
    for field, expected in {"policy_sha256": policy_hash, "audit_policy_sha256": refs["independent_audit_policy"]["sha256"], "prepared_sha256": prepared_hash, "source_sha256": source_hash, "mapping_sha256": refs["atom_mapping"]["sha256"], "evaluator_sha256": refs["independent_auditor"]["sha256"]}.items():
        require(audit[field] == expected, "Independent audit identity mismatch: " + field)
    require(audit["frozen_v1_geometry"] == manifest["geometry"] and audit["thresholds_changed_from_v1"] is False and audit["historical_scores_used_to_select_conformer"] is False and audit["required_checks"] == RECEPTOR_CHECKS, "Audit threshold or provenance mismatch")
    require(audit["audit_input_binding_complete"] is True and audit["preparation_receipt_sha256"] == refs["preparation_receipt"]["sha256"], "Audit did not bind its actual preparation receipt")
    require(audit["preparation_artifacts"] == receipt["artifacts"] and audit["topology_sha256"] == receipt["artifacts"]["topology.json"], "Audit did not bind the actual preparation topology/artifacts")
    require(audit["initial_inputs"] == receipt["initial_inputs"] == policy["initial"], "Initial preparation lineage differs")
    initial = {name: verified_ref(ref) for name, ref in receipt["initial_inputs"].items()}
    for role, field in (("receipt", "initial_artifacts"), ("parent_v3_receipt", "parent_v3_artifacts")):
        parent = json.loads(initial[role].read_text())
        require(parent["artifacts"] == receipt[field] == audit[field], "Parent artifact manifest differs from audited lineage")
        for name, digest in parent["artifacts"].items():
            require(Path(name).name == name and sha(initial[role].parent/name) == digest, "Parent preparation artifact changed")
    final_stage = receipt["stages"][-2]
    require(final_stage["stage"] == "amber_unrestrained" and final_stage["artificial_chirality_restraint"] is False and final_stage["soft_nonbonded"] is False, "Final prepared model retains artificial forces")
    require(final_stage["tolerance_reached"] is True, "Final unconstrained minimization did not converge")
    checks = audit["checks"]
    require(len(checks) == len(RECEPTOR_CHECKS) and {item["id"] for item in checks} == set(RECEPTOR_CHECKS), "Independent required checks missing/duplicated")
    expected_status = "pass" if all(item["status"] == "pass" for item in checks) else "fail"
    require(audit["status"] == expected_status, "Independent status contradicts detailed checks")
    require(math.isfinite(audit["runtime"]["preparation_plus_audit_cpu_core_hours"]) and 0 <= audit["runtime"]["preparation_plus_audit_cpu_core_hours"] <= policy["compute"]["maximum_cpu_core_hours"] and audit["runtime"]["new_gpu_compute"] is False, "Preparation compute budget mismatch")
    atoms = []
    for line in Path(pdb).read_text().splitlines():
        if line.startswith("ATOM  "):
            atoms.append({"serial":int(line[6:11]), "name":line[12:16].strip(), "altloc":line[16].strip(), "residue":line[17:20].strip(), "chain":line[21], "number":int(line[22:26]), "insertion":line[26].strip(), "xyz":[float(line[i:i+8]) for i in (30,38,46)], "occupancy":float(line[54:60]), "element":line[76:78].strip()})
    return {"schema":"drug-design-prepared-receptor-proof-evaluation-v4", "status":expected_status, "mapping_status":"pass", "structure_sha256":prepared_hash, "source_structure_sha256":source_hash, "binding_sha256":sha(binding_path), "audit_sha256":refs["independent_audit"]["sha256"], "checks":checks, "coordinate_origin":"observed_and_explicitly_modeled", "modeled_coordinates_are_experimental":False}, atoms


def evaluate(directory, pdb, manifest, manifest_path=DEFAULT_MANIFEST):
    root = Path(directory)
    audit, atoms = receptor_audit(pdb, manifest)
    config_path = WORKSPACE / manifest["recipe"]["path"]
    if sha(config_path) != manifest["recipe"]["sha256"]:
        raise ValueError("Frozen scientific recipe changed")
    config = yaml.safe_load(config_path.read_text())
    paths = {key: root / filename for key, filename in manifest["artifacts"].items()}
    missing = [key for key, filename in paths.items() if not filename.is_file()]
    if missing:
        return {"schema": "drug-design-scientific-evaluation-v4", "status": "unknown", "outcome": "insufficient_evidence", "missing": missing, "receptor": audit, "candidates": []}
    rows = csv_rows(paths["candidates_csv"])
    docked = csv_rows(paths["docking_csv"])
    scored = csv_rows(paths["scoring_csv"])
    inputs = list(Chem.SDMolSupplier(str(paths["candidates_sdf"]), removeHs=False)) if paths["candidates_sdf"].stat().st_size else []
    poses = list(Chem.SDMolSupplier(str(paths["poses_sdf"]), removeHs=False, sanitize=False)) if paths["poses_sdf"].stat().st_size else []
    ids = [row["compound_id"] for row in rows]
    if len(rows) > manifest["scope"]["max_candidates"] or len(ids) != len(set(ids)) or len(inputs) != len(rows):
        raise ValueError("Candidate scope, SDF denominator or identity duplication error")
    for table in (docked, scored):
        found = [row["compound_id"] for row in table]
        if sorted(found) != sorted(ids) or len(found) != len(set(found)):
            raise ValueError("Engine output identity denominator mismatch")
    if sorted(int(row["input_sdf_index"]) for row in rows) != list(range(len(rows))):
        raise ValueError("Input SDF indices must form the complete unique denominator")
    results = []
    for row in rows:
        index = int(row["input_sdf_index"])
        if index < 0 or index >= len(inputs) or inputs[index] is None:
            raise ValueError("Invalid input SDF index")
        sdf = inputs[index]
        if not sdf.HasProp("COMPOUND_ID") or sdf.GetProp("COMPOUND_ID") != row["compound_id"] or not sdf.HasProp("INPUT_SDF_INDEX") or sdf.GetProp("INPUT_SDF_INDEX") != str(index):
            raise ValueError("Input SDF compound ID mismatch")
        checks = candidate_chemistry(row, sdf, config)
        dock = next(item for item in docked if item["compound_id"] == row["compound_id"])
        score = next(item for item in scored if item["compound_id"] == row["compound_id"])
        if any(item.get("standardized_smiles") != row["standardized_smiles"] or item.get("input_sdf_index") != row["input_sdf_index"] for item in (dock, score)):
            raise ValueError("Engine output parent identity or input index mismatch")
        successful = dock["engine_status"] == score["engine_status"] == "SUCCESS"
        numeric = all(item.get("raw_score", "").strip() and math.isfinite(float(item["raw_score"])) for item in (dock, score))
        checks.append(check("finite_engine_scores", "pass" if successful and numeric else "unknown", "Both engine outputs need a finite score; no score threshold is interpreted as affinity"))
        if successful:
            pose_index = int(dock["selected_pose_record_index"])
            if pose_index < 0 or pose_index >= len(poses) or poses[pose_index] is None:
                checks.append(check("pose_identity", "fail", "Selected pose record missing"))
            elif poses[pose_index].GetProp("_Name") != dock["pose_variant_id"]:
                checks.append(check("pose_identity", "fail", "Selected pose variant identity differs from score table"))
            elif audit["status"] != "pass":
                checks.append(check("pose_geometry", "unknown", "Receptor audit failed; geometric conclusion withheld"))
            else:
                checks.extend(geometry_checks(poses[pose_index], row, atoms, manifest["geometry"]))
        else:
            checks.append(check("pose_geometry", "unknown", "Computation did not produce a successful docked and scored candidate"))
        results.append({"compound_id": row["compound_id"], "input_sdf_index": index, "status": combined(checks), "checks": checks})
    counts = {state: sum(item["status"] == state for item in results) for state in ("pass", "fail", "unknown")}
    outcome = "insufficient_evidence" if audit["status"] != "pass" or counts["unknown"] else "objective_met" if counts["pass"] >= manifest["scope"]["minimum_accepted_candidates"] else "no_viable_candidate"
    return {"schema": "drug-design-scientific-evaluation-v4", "evaluator": VERSION, "evaluator_sha256": sha(__file__),
            "manifest_sha256": sha(manifest_path), "origin": "local_artifact_evaluation", "new_remote_compute": False, "native_execution_provenance_verified": False,
            "status": "unknown" if outcome == "insufficient_evidence" else "pass" if outcome == "objective_met" else "fail", "outcome": outcome,
            "receptor": audit, "candidate_count": len(results), "counts": counts, "candidates": results,
            "artifacts": {key: {"path": str(filename.resolve()), "sha256": sha(filename)} for key, filename in paths.items()},
            "accepted_candidate_ids": [item["compound_id"] for item in results if item["status"] == "pass"],
            "conclusion_scope": "computational-shortlist-only",
            "limitations": manifest["limitations"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--pdb", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    pdb = workspace_path(manifest["target"]["receptor_path"])
    if args.pdb and args.pdb.resolve() != pdb:
        raise ValueError("Explicit receptor differs from frozen manifest")
    result = evaluate(args.artifacts, pdb, manifest, args.manifest) if args.artifacts else receptor_audit(pdb, manifest)[0]
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output":str(args.output), "sha256":sha(args.output), "status":result["status"], "outcome":result.get("outcome"), "counts":result.get("counts")}))


if __name__ == "__main__":
    main()
