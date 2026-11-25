"""Read-only distances and preparation choices; never edits molecular inputs."""
import csv
import importlib.util
import json
from pathlib import Path

import numpy as np
from rdkit import Chem

SOURCE = Path(__file__).with_name("research-scientific-evaluator.py")
SPEC = importlib.util.spec_from_file_location("scientific_evaluator", SOURCE)
science = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(science)


def point_label(atom):
    return {key: atom[key] for key in ("serial", "chain", "number", "residue", "name", "altloc", "occupancy")}


def minimum(atoms, targets):
    distances = np.linalg.norm(np.array([atom["xyz"] for atom in atoms])[:, None, :] - np.array([atom["xyz"] for atom in targets])[None, :, :], axis=2)
    first, second = np.unravel_index(distances.argmin(), distances.shape)
    return {"distance_angstrom": float(distances[first, second]), "problem_atom": point_label(atoms[first]), "reference_atom": {key: value for key, value in targets[second].items() if key != "xyz"}}


def run():
    manifest = science.load_manifest()
    pdb = science.WORKSPACE / manifest["target"]["receptor_path"]
    audit, atoms = science.receptor_audit(pdb, manifest)
    sg = [atom for atom in atoms if atom["chain"] == "A" and atom["number"] == 773 and atom["name"] == "SG"]
    aq4 = [{"serial": int(line[6:11]), "name": line[12:16].strip(), "residue": "AQ4", "chain": line[21], "number": int(line[22:26]), "xyz": [float(line[i:i + 8]) for i in (30, 38, 46)]}
           for line in pdb.read_text().splitlines() if line.startswith("HETATM") and line[17:20] == "AQ4" and line[76:78].strip() not in {"H", "D"}]
    historical = science.WORKSPACE / "reports/layout-migration-20261012/gpu-final"
    rows = science.csv_rows(historical / "carsidock/scores.csv")
    poses = list(Chem.SDMolSupplier(str(historical / "carsidock/carsidock.sdf"), sanitize=False, removeHs=False))
    assert len(sg) == 1 and len(aq4) == 29 and len(rows) == 5
    selected = []
    for row in rows:
        pose = poses[int(row["selected_pose_record_index"])]
        assert pose.GetProp("_Name") == row["pose_variant_id"]
        recovered = science.recover_parent(pose, Chem.MolFromSmiles(row["standardized_smiles"]))
        assert len(recovered) == 1
        sulfur = recovered[0][0]
        selected.append({"compound_id": row["compound_id"], "pose_variant_id": row["pose_variant_id"], "record_index": int(row["selected_pose_record_index"]), "sulfur_placeholder_excluded": sulfur,
                         "atoms": [{"atom_index": index, "element": pose.GetAtomWithIdx(index).GetSymbol(), "xyz": xyz.tolist()}
                                   for index, xyz in enumerate(pose.GetConformer().GetPositions()) if index != sulfur and pose.GetAtomWithIdx(index).GetAtomicNum() != 1]})
    groups = []
    numbers = sorted({atom["number"] for atom in atoms if atom["altloc"] or atom["occupancy"] == 0})
    for number in numbers:
        residue = [atom for atom in atoms if atom["chain"] == "A" and atom["number"] == number]
        bad = [atom for atom in residue if atom["altloc"] or atom["occupancy"] == 0]
        zero = [atom for atom in residue if atom["occupancy"] == 0]
        alternate = [atom for atom in residue if atom["altloc"]]
        zero_names = sorted({atom["name"] for atom in zero})
        backbone = sorted({"N", "CA", "C", "O"}.intersection(zero_names))
        groups.append({"chain": "A", "author_residue": number, "canonical_residue_from_DBREF": number + 24, "residue": residue[0]["residue"],
                       "zero_occupancy_count": len(zero), "zero_occupancy_names": zero_names, "alternate_atom_count": len(alternate),
                       "alternate_conformers": [{"label": label, "atoms": [point_label(atom) for atom in alternate if atom["altloc"] == label],
                                                  "to_SG": minimum([atom for atom in alternate if atom["altloc"] == label], sg),
                                                  "to_AQ4": minimum([atom for atom in alternate if atom["altloc"] == label], aq4)} for label in sorted({atom["altloc"] for atom in alternate})],
                       "zero_deletion_effect": {"whole_residue_deleted": bool(zero) and len(zero) == len(residue), "backbone_atoms_lost": backbone,
                                                "sidechain_atoms_lost": sorted(set(zero_names) - {"N", "CA", "C", "O"}),
                                                "interpretation": "Creates a backbone gap; new explicit modeling or justified construct definition required" if backbone else "Creates incomplete sidechain; rebuild or explicitly justify a different construct/model" if zero else "Deleting all alternate atoms would remove the entire modeled residue; choose complete conformer(s), not all altloc records"},
                       "distances": {"to_SG": minimum(bad, sg), "to_AQ4": minimum(bad, aq4),
                                     "to_historical_selected_poses": [{"compound_id": item["compound_id"], "pose_variant_id": item["pose_variant_id"], **minimum(bad, item["atoms"])} for item in selected]}})
    assert sum(item["zero_occupancy_count"] for item in groups) == 37 and sum(item["alternate_atom_count"] for item in groups) == 28
    missing = [line for line in pdb.read_text().splitlines() if line.startswith("REMARK 465") and len(line) > 26 and line[15:18].strip() in {"GLY", "SER", "HIS", "MET", "ALA", "LEU", "PRO", "THR", "ASP", "ASN", "PHE", "TYR", "ARG", "GLN"}]
    prepared = []
    candidates = [
        ("reports/denovo-covdock-20260929/receptor_prepared.mae", "4G5J", "Maestro top-level s_pdb_PDB_ID value 4G5J; prepared audit uses CYS797 with a different coordinate frame; not an atom-mapped preparation of 1M17"),
        ("routes/de-novo/registry/egfr_c797_v1/assets/protein_A.pdb", "4G5J-associated route asset", "Route warhead_atom_map.json identifies 4G5J; no verified atom mapping to the current 1M17 input"),
        ("data/processed/egfr28_pre_covalent_prep/wt_common_receptor_9msr_repair/repaired_common_receptor_9MSR.pdb", "9MSR-associated artifact", "Different named source receptor; no 1M17 mapping/construct equivalence verified in this audit"),
        ("science/results/target-hypotheses/hyp-de61c391941f47fd8f788900/pdbs/1M17.pdb", "1M17", "Local saved source structure, not presumed prepared"),
        ("science/results/target-hypotheses/hyp-faa768ef0680439395625495/pdbs/1M17.pdb", "1M17", "Local saved source structure, not presumed prepared"),
        ("science/results/EGFR-mini/covdock_lo_grid_seed/EGFR_covdock_lo_grid_seed_receptor_prepared.mae", "unknown", "Referenced by Library config; not found locally. Remote existence/content was not queried in this read-only local task"),
    ]
    for filename, source, note in candidates:
        path = science.WORKSPACE / filename
        digest = science.sha(path) if path.is_file() else None
        prepared.append({"path": filename, "local_exists": path.is_file(), "sha256": digest, "source": source,
                         "same_bytes_as_1M17_source": digest == science.sha(pdb) if digest else None,
                         "reuse_status": "not_admitted_without_new_provenance_mapping_and_contract", "evidence": note})
    evidence = [pdb, SOURCE, science.DEFAULT_MANIFEST, historical / "carsidock/scores.csv", historical / "carsidock/carsidock.sdf",
                science.WORKSPACE / "reports/denovo-covdock-20260929/prepared_receptor_audit.json",
                science.WORKSPACE / "routes/de-novo/registry/egfr_c797_v1/assets/warhead_atom_map.json"]
    return {"schema": "drug-design-receptor-preparation-plan-v1", "purpose": "diagnostic_only_not_acceptance", "origin": "local_read_only_artifact_analysis",
            "script_sha256": science.sha(__file__), "new_remote_compute": False, "new_molecular_coordinates": False, "pdb_modified": False,
            "source_receptor_sha256": science.sha(pdb), "source_receptor_status": audit["status"], "source_mapping_status": audit["mapping_status"],
            "distance_definition": "Euclidean minimum between problematic heavy-atom coordinates and the named reference heavy atoms, in angstrom, same deposited coordinate frame; zero-occupancy coordinates are not experimentally supported positions. No score/cutoff or acceptance inference.",
            "references": {"target": "A:CYS773:SG", "crystal_ligand": "AQ4 A999, 29 heavy atoms", "historical_poses": [{k: value for k, value in item.items() if k != "atoms"} for item in selected]},
            "residues": groups, "original_missing_residue_records": missing, "existing_prepared_candidates": prepared,
            "conservative_plan": ["Keep original 1M17 and v1 failed audit immutable; create a versioned preparation product plus per-atom identity/change ledger.",
                                  "Do not merely delete all 37 zero-occupancy atoms: GLU725 loses its full backbone and six other residues lose sidechain atoms; missing coordinates need an explicit modeling/construct policy.",
                                  "For CYS751 and ASP831, A and B have equal 0.5 occupancy. Keep each complete conformer as an explicit alternative or preregister a deterministic conformer choice with uncertainty; do not infer A is more probable from occupancy.",
                                  "Before modeling, decide how to handle existing missing 965-976 loop and unresolved termini. Rebuilding versus an explicitly truncated construct changes the input hypothesis and must be documented.",
                                  "Re-audit construct/sequence mapping, full residue atom completeness, termini/chain connectivity, target SG, protonation/tautomer assumptions, and unchanged/modeled coordinates; bind new receptor/mapping/manifest hashes before any new pilot.",
                                  "Existing 4G5J/9MSR artifacts may support a separately declared study after source/mutation/atom mapping validation; do not silently substitute them for this 1M17 contract."],
            "unresolved_choices": ["No electron-density comparison or occupancy-based winner establishes altloc A versus B.", "No local model validation establishes rebuilt zero-occupancy/backbone/loop coordinates.", "No justified truncation boundaries, capping, protonation pH/tautomers or treatment of structural waters are frozen for a new product.", "Distances to the five historical selected poses are retrospective diagnostics, not a basis to optimize receptor choice for favorable scores."],
            "evidence": [{"path": str(path.relative_to(science.WORKSPACE)), "sha256": science.sha(path)} for path in evidence]}


if __name__ == "__main__":
    result = run()
    path = science.WORKSPACE / "reports/research-loop-20261013/receptor-preparation-plan-v1.json"
    with path.open("x") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(path), "sha256": science.sha(path), "residue_count": len(result["residues"]), "purpose": result["purpose"]}))
