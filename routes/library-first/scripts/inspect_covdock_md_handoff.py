#!/usr/bin/env python3
"""Extract and independently re-read one frozen CovDock adduct pose.

Run only through the registered Schrodinger launcher.  The script consumes a
candidate-pose-input artifact; it never accepts an arbitrary MAEGZ path.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path

import _bootstrap
from covscreen_library_first.md_candidate_pose import load_candidate_pose_input


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atom_record(atom):
    return (atom.index, atom.atomic_number, atom.chain.strip(), str(atom.resnum),
            atom.inscode.strip(), atom.pdbres.strip(), atom.pdbname.strip(),
            atom.formal_charge, tuple(round(float(value), 6) for value in atom.xyz))


def graph(structure):
    atoms = tuple(atom_record(atom) for atom in structure.atom)
    bonds = tuple(sorted((min(bond.atom1.index, bond.atom2.index),
                          max(bond.atom1.index, bond.atom2.index),
                          float(bond.order)) for bond in structure.bond))
    return atoms, bonds


def property_value(structure, names):
    for name in names:
        if name in structure.property:
            return structure.property[name]
    return None


def run(root, artifact_id, manifest_sha256, output):
    from schrodinger.structure import StructureReader

    loaded = load_candidate_pose_input(root, artifact_id, manifest_sha256)
    manifest, directory = loaded["manifest"], loaded["directory"]
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Native handoff output must be a new directory")
    source = directory / "covdock-source.maegz"
    selected = None
    observed_count = 0
    historical_audit = None
    if manifest.get('schema') == 'historical-candidate-pose-input-v1':
        from covscreen.md_historical_native import inspect
        selected, observed_count, historical_audit = inspect(loaded, StructureReader)
    else:
        for index, structure in enumerate(StructureReader(str(source))):
            observed_count += 1
            if index == manifest["pose_index"]:
                selected = structure
    if selected is None:
        raise ValueError("Selected CovDock pose index is absent")
    compound = property_value(selected, ("s_sd_COMPOUND_ID", "s_sd_COMPOUND\\_ID"))
    input_index = property_value(selected, ("i_sd_INPUT_SDF_INDEX", "i_sd_INPUT\\_SDF\\_INDEX"))
    if (compound is not None and str(compound) != manifest["compound_id"]
            or input_index is not None and int(input_index) != manifest["input_sdf_index"]):
        raise ValueError("Native CovDock pose identity differs from frozen selection")
    target = manifest["target"]
    sulfurs = [atom for atom in selected.atom
        if atom.chain.strip() == target["author_chain"]
        and str(atom.resnum) == str(target["author_residue"])
        and atom.pdbname.strip() == "SG" and atom.atomic_number == 16]
    if len(sulfurs) != 1:
        raise ValueError("Expected one target cysteine sulfur")
    sulfur = sulfurs[0]
    target_residue = (sulfur.chain.strip(), str(sulfur.resnum), sulfur.inscode.strip())
    partners = [atom for atom in sulfur.bonded_atoms
        if (atom.chain.strip(), str(atom.resnum), atom.inscode.strip()) != target_residue]
    if len(partners) != 1 or partners[0].atomic_number != 6:
        raise ValueError("Expected one intermolecular cysteine S-C bond")
    electrophile = partners[0]
    if any(atom.atomic_number == 1 for atom in sulfur.bonded_atoms):
        raise ValueError("Covalently bound sulfur retains a hydrogen")
    distance = math.dist(tuple(float(value) for value in sulfur.xyz),
                         tuple(float(value) for value in electrophile.xyz))
    if not 1.5 <= distance <= 2.2:
        raise ValueError("Cysteine S-C bond length is outside the structural QC range")
    ligand_residue = (electrophile.chain.strip(), str(electrophile.resnum),
                      electrophile.inscode.strip())
    if ligand_residue == target_residue or ligand_residue[0] == target["author_chain"]:
        raise ValueError("Ligand residue is not separated from the target protein chain")
    ligand_heavy = [atom.index for atom in selected.atom
        if (atom.chain.strip(), str(atom.resnum), atom.inscode.strip()) == ligand_residue
        and atom.atomic_number > 1]
    if len(ligand_heavy) < 5 or electrophile.index not in ligand_heavy:
        raise ValueError("Ligand heavy-atom selection is incomplete")
    output.mkdir(parents=True, exist_ok=False)
    adduct = output / "adduct.mae"
    selected.write(str(adduct))
    reread = list(StructureReader(str(adduct)))
    if len(reread) != 1 or graph(reread[0]) != graph(selected):
        raise ValueError("Independent native graph re-read differs from selected pose")
    adduct_sha = sha(adduct)
    chemistry = {key: manifest["chemistry"][key] for key in
        ("warhead_class", "reaction_type", "covalent_mode", "product_state")}
    chemistry["electrophile_atom_name"] = electrophile.pdbname.strip()
    candidate = {"schema": "candidate-md-handoff-v1",
        "candidate_id": manifest["candidate_id"], "compound_id": manifest["compound_id"],
        "status": "structural_quality_passed", "engine": "CovDock-LO",
        "target": manifest["target"], "chemistry": chemistry,
        "pose": {"format": "mae", "source_sha256": adduct_sha,
                 "pose_index": manifest["pose_index"]},
        "parents": {"prepared_receptor_sha256": manifest["parents"]["prepared_receptor_sha256"],
                    "receptor_context_sha256": manifest["parents"]["receptor_context_sha256"]},
        "warnings": []}
    candidate_path = output / "candidate-evidence.json"
    candidate_path.write_text(json.dumps(candidate, indent=2, allow_nan=False) + "\n")
    candidate_sha = sha(candidate_path)
    qc = {"schema": "covalent-adduct-graph-qc-v1", "system_id": manifest["candidate_id"],
        "status": "LOOP_GRAPH_QC_PASSED_PENDING_RELAXATION",
        "simulation_state": "covalent_adduct", "heavy_graph_preserved": True, "warnings": [],
        "loop_qc_input_sha256": adduct_sha,
        "covalent_atom_indices_1based": [electrophile.index, sulfur.index],
        "ligand_heavy_atom_indices_1based": sorted(ligand_heavy),
        "target_atom": {"chain": target["author_chain"], "residue": target["author_residue"],
                        "atom_name": "SG", "atom_index_1based": sulfur.index},
        "ligand_residue": {"chain": ligand_residue[0], "residue": ligand_residue[1],
            "resname": electrophile.pdbres.strip(),
            "electrophile_atom_name": electrophile.pdbname.strip()},
        "candidate_evidence_sha256": candidate_sha,
        "prepared_receptor_sha256": manifest["parents"]["prepared_receptor_sha256"],
        "receptor_context_sha256": manifest["parents"]["receptor_context_sha256"],
        "independent_graph_audit": True}
    qc_path = output / "graph-qc.json"
    qc_path.write_text(json.dumps(qc, indent=2, allow_nan=False) + "\n")
    result = {"schema": "candidate-md-handoff-artifact-v1",
        "status": "native_graph_qc_passed_admission_pending",
        "candidate_id": manifest["candidate_id"], "compound_id": manifest["compound_id"],
        "pose_index": manifest["pose_index"], "source_pose_count": observed_count,
        "covalent_distance_angstrom": distance,
        "parents": {"candidate_pose_manifest_sha256": manifest_sha256,
                    **manifest["parents"]},
        "artifacts": {"adduct.mae": adduct_sha,
            "candidate-evidence.json": candidate_sha, "graph-qc.json": sha(qc_path)},
        "native_graph_qc_completed": True, "parameterization_completed": False,
        "md_system_ready": False, "campaign_ready": False,
        "calculation_submitted": False}
    if historical_audit is not None:
        audit_path = output / 'historical-structure-audit.json'
        historical_audit['candidate_pose_manifest_sha256'] = manifest_sha256
        historical_audit['adduct_sha256'] = adduct_sha
        audit_path.write_text(json.dumps(historical_audit, indent=2, allow_nan=False) + '\n')
        result['historical_structure_audit_sha256'] = sha(audit_path)
    (output / "candidate-md-handoff.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.artifact_id, args.manifest_sha256, args.output),
                     ensure_ascii=False, allow_nan=False))
