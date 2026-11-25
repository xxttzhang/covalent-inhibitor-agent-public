"""Hash-bound, descriptive context audit around a prepared target cysteine.

The audit compares the prepared isolated protein with the preserved deposited
coordinates.  It measures geometry and solvent exposure; it does not infer
cysteine reactivity, ligandability, biological-assembly suitability or campaign
readiness.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path

from .pdb_coordinates import read_pdb_coordinates


SITE_CONTEXT_POLICY_V1 = {
    "schema": "receptor-site-context-policy-v1",
    "sasa_algorithm": "ShrakeRupley",
    "probe_radius_angstrom": 1.4,
    "surface_points_per_atom": 960,
    "protein_shell_radius_angstrom": 6.0,
    "deposited_environment_radius_angstrom": 8.0,
    "contact_reporting_radius_angstrom": 4.5,
    "thresholds_are_acceptance_criteria": False,
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _load_prepared(directory, expected_sha256, acquisition_sha256, *, review_root=None):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Prepared receptor must be a regular directory")
    manifest_path = directory / "prepared-receptor.json"
    if (manifest_path.is_symlink() or not manifest_path.is_file()
            or _sha(manifest_path) != expected_sha256):
        raise ValueError("Prepared receptor manifest checksum mismatch")
    manifest = json.loads(manifest_path.read_text())
    restrained = manifest.get('schema') == 'prepared-receptor-artifact-v2'
    if (manifest.get("schema") not in {"prepared-receptor-artifact-v1", "prepared-receptor-artifact-v2"}
            or manifest.get("status") != "prepared_isolated_protein_scope"
            or manifest.get("receptor_prepared") is not True
            or manifest.get("campaign_ready") is not False
            or manifest.get("parents", {}).get("target_acquisition_sha256") != acquisition_sha256):
        raise ValueError("Prepared receptor is not a matching scoped artifact")
    for name, digest in manifest.get("artifacts", {}).items():
        path = directory / name
        if (Path(name).name != name or path.is_symlink() or not path.is_file()
                or _sha(path) != digest):
            raise ValueError("Prepared receptor artifact checksum mismatch: " + str(name))
    if restrained:
        from .receptor_environment_review import load
        if review_root is None:
            raise ValueError('Restrained site context requires the bound review root')
        motion = manifest['motion_review']
        review = load(Path(review_root), motion['reference'], motion['bound_input'],
            motion['inventory'], scope='restrained-receptor')
        witnesses = [Path(review_root) / name for name, digest in motion['inventory'].items()
            if digest == motion['bound_input'].get('evaluation_sha256')]
        if len(witnesses) != 1:
            raise ValueError('Restrained site context requires one reviewed evaluation')
        evaluation = json.loads(witnesses[0].read_text())
        if (review['decision'] != 'suitable_for_exploratory_receptor_preparation'
                or evaluation.get('parents', {}).get('acquisition_sha256') != acquisition_sha256
                or evaluation.get('result', {}).get('quality') != json.loads((directory / 'identity-quality.json').read_text())
                or evaluation.get('result', {}).get('geometry') != json.loads((directory / 'geometry-quality.json').read_text())
                or manifest.get('coordinate_scope') != 'position_restrained_computational_proposal'):
            raise ValueError('Restrained site context review differs from the prepared measurements')
    return manifest


def _prepared_measurements(prepared, manifest, policy):
    from Bio import __version__ as biopython_version
    from Bio.PDB import PDBParser, ShrakeRupley

    mapping = json.loads((prepared / "atom-mapping.json").read_text())
    target_atoms = [atom for atom in mapping.get("atoms", [])
                    if atom.get("author_residue") == manifest["author_residue"]
                    and atom.get("resname") == "CYS"]
    sulfur, = [atom for atom in target_atoms if atom.get("name") == "SG"]
    target_position = sulfur["entity_position"]
    if sulfur.get("original_serial") is None:
        raise ValueError("Target cysteine sulfur must retain an observed coordinate")
    raw = (prepared / "prepared-receptor.pdb").read_text()
    structure = PDBParser(QUIET=True).get_structure("prepared", io.StringIO(raw))
    models = list(structure.get_models())
    if len(models) != 1:
        raise ValueError("Prepared receptor must contain exactly one model")
    import re
    matched = re.fullmatch(r"(-?\d+)([A-Za-z]?)", manifest["author_residue"])
    if matched is None:
        raise ValueError("Prepared target author residue identifier is invalid")
    author_number, insertion = int(matched.group(1)), matched.group(2) or " "
    residues = [residue for residue in models[0].get_residues()
                if residue.id == (" ", author_number, insertion)]
    target_residue, = residues
    sg = target_residue["SG"]
    ShrakeRupley(probe_radius=policy["probe_radius_angstrom"],
                 n_points=policy["surface_points_per_atom"]).compute(structure, level="A")
    residue_sasa = sum(float(atom.sasa) for atom in target_residue.get_atoms())
    sg_xyz = tuple(float(value) for value in sg.coord)
    shell = {}
    for atom in mapping["atoms"]:
        if atom["element"] in {"H", "D"} or atom["entity_position"] == target_position:
            continue
        distance = math.dist(sg_xyz, atom["xyz_angstrom"])
        if distance > policy["protein_shell_radius_angstrom"]:
            continue
        key = (atom["entity_position"], atom["author_residue"], atom["resname"])
        row = shell.setdefault(key, {"entity_position": atom["entity_position"],
            "author_residue": atom["author_residue"], "resname": atom["resname"],
            "minimum_sg_distance_angstrom": distance, "closest_atom": atom["name"],
            "contains_modeled_atom": False})
        if distance < row["minimum_sg_distance_angstrom"]:
            row.update(minimum_sg_distance_angstrom=distance, closest_atom=atom["name"])
        # Provenance follows the deposited atom identity even after relaxation.
        # A moved observed atom is not a newly reconstructed atom.
        row["contains_modeled_atom"] |= atom.get("original_serial") is None
    return {
        "target_entity_position": target_position,
        "target_author_residue": manifest["author_residue"],
        "sg_sasa_angstrom_squared": float(sg.sasa),
        "cysteine_residue_sasa_angstrom_squared": residue_sasa,
        "protein_shell": sorted(shell.values(), key=lambda row: (
            row["minimum_sg_distance_angstrom"], row["entity_position"])),
        "runtime": {"biopython": biopython_version},
    }


def _deposited_environment(acquisition, acquired, policy):
    plan = acquired["acquisition"]["plan"]
    mapping, = acquired["acquisition"]["mappings"]
    selection = acquired["selection"]
    _, _, atoms, model = read_pdb_coordinates(
        acquisition / "structure.pdb", plan["entry"], selection.get("model"))
    chain = mapping["auth_asym_id"]
    residue = mapping["author_residue_id"]
    site = [atom for atom in atoms if atom["chain"] == chain and atom["residue"] == residue
            and atom["resname"] == "CYS" and atom["name"] == "SG"]
    selected_altloc = selection.get("altloc")
    if selected_altloc is not None:
        site = [atom for atom in site if not atom["altloc"] or atom["altloc"] == selected_altloc]
    if len(site) != 1:
        raise ValueError("Deposited target sulfur selection is ambiguous")
    sg = site[0]
    groups = {}
    for atom in atoms:
        if atom["serial"] == sg["serial"] or atom["element"] in {"H", "D"}:
            continue
        distance = math.dist(sg["xyz"], atom["xyz"])
        if distance > policy["deposited_environment_radius_angstrom"]:
            continue
        if atom["record"] == "ATOM" and atom["chain"] == chain:
            continue
        category = ("water" if atom["resname"] in {"HOH", "WAT", "DOD"}
                    else "other_polymer_chain" if atom["record"] == "ATOM"
                    else "nonwater_heterogen")
        key = (category, atom["record"], atom["resname"], atom["chain"], atom["residue"], atom["altloc"])
        row = groups.setdefault(key, {"category": category, "record": atom["record"],
            "resname": atom["resname"], "chain": atom["chain"], "residue": atom["residue"],
            "altloc": atom["altloc"] or None, "heavy_atom_count": 0,
            "minimum_sg_distance_angstrom": distance, "closest_atom": atom["name"]})
        row["heavy_atom_count"] += 1
        if distance < row["minimum_sg_distance_angstrom"]:
            row.update(minimum_sg_distance_angstrom=distance, closest_atom=atom["name"])
    rows = sorted(groups.values(), key=lambda row: (
        row["minimum_sg_distance_angstrom"], row["category"], row["chain"], row["residue"]))
    return {"model": model, "target_sg_occupancy": sg["occupancy"],
        "components_within_context_radius": rows,
        "components_within_contact_radius": [row for row in rows
            if row["minimum_sg_distance_angstrom"] <= policy["contact_reporting_radius_angstrom"]]}


def assess_receptor_site_context(acquisition, acquisition_sha256, acquired,
                                 prepared, prepared_sha256, output,
                                 *, policy=None, review_root=None):
    """Create one immutable context artifact from revalidated parent artifacts."""
    acquisition, prepared, output = map(Path, (acquisition, prepared, output))
    if output.exists() or output.is_symlink():
        raise ValueError("Site-context output must be a new directory")
    selected = SITE_CONTEXT_POLICY_V1 if policy is None else policy
    if selected != SITE_CONTEXT_POLICY_V1:
        raise ValueError("Only the registered site-context policy is supported")
    manifest = _load_prepared(prepared, prepared_sha256, acquisition_sha256, review_root=review_root)
    plan = acquired["acquisition"]["plan"]
    if (manifest["entry_id"] != plan["entry"] or manifest["uniprot"] != plan["uniprot"]
            or manifest["canonical_cys"] != plan["cys"]):
        raise ValueError("Prepared receptor identity differs from acquisition")
    prepared_result = _prepared_measurements(prepared, manifest, selected)
    deposited_result = _deposited_environment(acquisition, acquired, selected)
    result = {"schema": "receptor-site-context-artifact-v1",
        "status": "descriptive_site_context_completed_review_pending",
        "identity": {"entry_id": manifest["entry_id"], "uniprot": manifest["uniprot"],
                     "canonical_cys": manifest["canonical_cys"],
                     "author_chain": manifest["author_chain"],
                     "author_residue": manifest["author_residue"]},
        "parents": {"target_acquisition_sha256": acquisition_sha256,
                    "prepared_receptor_sha256": prepared_sha256},
        "policy": selected,
        "policy_sha256": hashlib.sha256(json.dumps(selected, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(),
        "prepared_isolated_protein": prepared_result,
        "deposited_environment": deposited_result,
        "interpretation_limits": [
            "SASA and distance measurements do not establish cysteine reactivity or ligandability.",
            "The deposited environment may reflect crystal packing and is not a biological-assembly decision.",
            "Component proximity does not decide whether a ligand, ion, cofactor or water should be retained.",
        ],
        "binding_site_suitability_validated": False,
        "reactivity_validated": False,
        "campaign_ready": False,
        "calculation_submitted": False,
        "remaining_checks": ["biological assembly and component-retention decision",
            "supported reaction class and cysteine-reactivity evidence",
            "candidate-specific noncovalent pose and covalent geometry"],
    }
    if manifest['schema'] == 'prepared-receptor-artifact-v2':
        result.update(schema='receptor-site-context-artifact-v2',
            coordinate_scope=manifest['coordinate_scope'], motion_review=manifest['motion_review'])
    output.mkdir(parents=True, exist_ok=False)
    path = output / "site-context.json"
    path.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
