"""Admit one candidate-specific covalent adduct handoff for native parameters.

This module binds independently produced structural evidence to the exact
adduct bytes and receptor parents.  It does not build an adduct, inspect a
vendor-native graph, assign force-field parameters or claim MD readiness.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil


SUPPORTED_REACTIONS = {
    "acrylamide": "Michael Addition",
    "vinyl_sulfonamide": "Michael Addition",
    "vinyl_sulfone": "Michael Addition",
    "propiolamide": "Michael Addition",
    "chloroacetamide": "Nucleophilic Substitution",
}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _regular_json(path, expected_sha256, label):
    path = Path(path)
    if (path.is_symlink() or not path.is_file()
            or not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256)
            or sha(path) != expected_sha256):
        raise ValueError(label + " checksum mismatch")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(label + " must be a JSON object")
    return value


def _candidate(value, source_sha256, prepared_sha256, context_sha256):
    required = {"schema", "candidate_id", "compound_id", "status", "engine", "target",
                "chemistry", "pose", "parents", "warnings"}
    if set(value) != required or value.get("schema") != "candidate-md-handoff-v1":
        raise ValueError("Unsupported candidate MD handoff schema")
    if (not isinstance(value["candidate_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", value["candidate_id"])
            or not isinstance(value["compound_id"], str) or not value["compound_id"].strip()):
        raise ValueError("Candidate identity is invalid")
    if (value["status"] != "structural_quality_passed"
            or value["engine"] not in {"CovDock-LO", "AlphaFold3"}
            or value["warnings"] != []):
        raise ValueError("Candidate structural evidence has not passed without warnings")
    target = value["target"]
    if (not isinstance(target, dict) or set(target) != {"entry_id", "uniprot", "canonical_cys",
            "author_chain", "author_residue", "atom_name"}
            or target["atom_name"] != "SG" or type(target["canonical_cys"]) is not int
            or target["canonical_cys"] < 1):
        raise ValueError("Candidate target identity is invalid")
    chemistry = value["chemistry"]
    if (not isinstance(chemistry, dict) or set(chemistry) != {"warhead_class", "reaction_type",
            "covalent_mode", "electrophile_atom_name", "product_state"}):
        raise ValueError("Candidate chemistry contract is invalid")
    warhead = chemistry["warhead_class"]
    if (warhead not in SUPPORTED_REACTIONS
            or chemistry["reaction_type"] != SUPPORTED_REACTIONS[warhead]
            or chemistry["covalent_mode"] != "Cys-S-C"
            or chemistry["product_state"] != "covalent_adduct"
            or not isinstance(chemistry["electrophile_atom_name"], str)
            or not chemistry["electrophile_atom_name"].strip()):
        raise ValueError("Unsupported or inconsistent candidate reaction chemistry")
    pose = value["pose"]
    if (not isinstance(pose, dict) or set(pose) != {"format", "source_sha256", "pose_index"}
            or pose["format"] != "mae" or pose["source_sha256"] != source_sha256
            or type(pose["pose_index"]) is not int or pose["pose_index"] < 0):
        raise ValueError("Candidate pose does not bind the exact MAE source")
    if value["parents"] != {"prepared_receptor_sha256": prepared_sha256,
                            "receptor_context_sha256": context_sha256}:
        raise ValueError("Candidate receptor parents differ from the admitted inputs")
    return value


def _parents(prepared, context, prepared_sha256, context_sha256):
    receptor = _regular_json(prepared, prepared_sha256, "Prepared receptor manifest")
    site = _regular_json(context, context_sha256, "Receptor context manifest")
    if receptor.get('schema') == 'historical-adduct-receptor-source-v1':
        from .md_historical_pose import receptor_identity
        identity = receptor_identity(receptor, site, prepared_sha256)
        return receptor, site, identity
    if (receptor.get("schema") != "prepared-receptor-artifact-v1"
            or receptor.get("status") != "prepared_isolated_protein_scope"
            or receptor.get("receptor_prepared") is not True
            or receptor.get("campaign_ready") is not False):
        raise ValueError("Prepared receptor does not have the scoped admitted state")
    if (site.get("schema") != "receptor-site-context-artifact-v1"
            or site.get("status") != "descriptive_site_context_completed_review_pending"
            or site.get("parents", {}).get("prepared_receptor_sha256") != prepared_sha256
            or site.get("campaign_ready") is not False):
        raise ValueError("Receptor context does not bind the prepared receptor")
    identity = site.get("identity", {})
    expected = {"entry_id": receptor.get("entry_id"), "uniprot": receptor.get("uniprot"),
        "canonical_cys": receptor.get("canonical_cys"), "author_chain": receptor.get("author_chain"),
        "author_residue": receptor.get("author_residue")}
    if any(identity.get(key) != value for key, value in expected.items()):
        raise ValueError("Receptor context identity differs from prepared receptor")
    return receptor, site, expected


def _graph_qc(value, source_sha256, candidate_sha256, prepared_sha256, context_sha256,
              candidate):
    required = {"schema", "system_id", "status", "simulation_state", "heavy_graph_preserved",
        "warnings", "loop_qc_input_sha256", "covalent_atom_indices_1based",
        "ligand_heavy_atom_indices_1based", "target_atom", "ligand_residue",
        "candidate_evidence_sha256", "prepared_receptor_sha256", "receptor_context_sha256",
        "independent_graph_audit"}
    if set(value) != required or value.get("schema") != "covalent-adduct-graph-qc-v1":
        raise ValueError("Unsupported covalent-adduct graph QC schema")
    if (value["system_id"] != candidate["candidate_id"]
            or value["status"] != "LOOP_GRAPH_QC_PASSED_PENDING_RELAXATION"
            or value["simulation_state"] != "covalent_adduct"
            or value["heavy_graph_preserved"] is not True or value["warnings"] != []
            or value["loop_qc_input_sha256"] != source_sha256
            or value["candidate_evidence_sha256"] != candidate_sha256
            or value["prepared_receptor_sha256"] != prepared_sha256
            or value["receptor_context_sha256"] != context_sha256
            or value["independent_graph_audit"] is not True):
        raise ValueError("Covalent-adduct graph QC did not pass or bind every parent")
    pair, ligand = value["covalent_atom_indices_1based"], value["ligand_heavy_atom_indices_1based"]
    for label, indices in (("covalent pair", pair), ("ligand selection", ligand)):
        if (not isinstance(indices, list) or not indices
                or any(type(index) is not int or index < 1 for index in indices)
                or len(indices) != len(set(indices))):
            raise ValueError("Invalid " + label)
    if len(pair) != 2 or len(ligand) < 5 or pair[0] not in ligand or pair[1] in ligand:
        raise ValueError("Expected ordered ligand-carbon / protein-sulfur indices")
    target = value["target_atom"]
    expected_target = candidate["target"]
    if target != {"chain": expected_target["author_chain"],
                  "residue": expected_target["author_residue"], "atom_name": "SG",
                  "atom_index_1based": pair[1]}:
        raise ValueError("Graph QC target atom differs from candidate target")
    ligand_residue = value["ligand_residue"]
    if (not isinstance(ligand_residue, dict)
            or set(ligand_residue) != {"chain", "residue", "resname", "electrophile_atom_name"}
            or ligand_residue["chain"] == target["chain"]
            or ligand_residue["electrophile_atom_name"]
            != candidate["chemistry"]["electrophile_atom_name"]):
        raise ValueError("Graph QC ligand identity differs from candidate chemistry")
    return value


def admit_covalent_adduct_input(source, source_sha256, graph_qc, graph_qc_sha256,
                                candidate_evidence, candidate_evidence_sha256,
                                prepared_receptor, prepared_receptor_sha256,
                                receptor_context, receptor_context_sha256, output):
    """Copy a fully bound handoff into one immutable parameterization input."""
    source, graph_qc, candidate_evidence, prepared_receptor, receptor_context, output = map(
        Path, (source, graph_qc, candidate_evidence, prepared_receptor, receptor_context, output))
    if output.exists() or output.is_symlink():
        raise ValueError("Covalent-adduct input output must be a new directory")
    if (source.is_symlink() or not source.is_file() or source.suffix.lower() != ".mae"
            or sha(source) != source_sha256):
        raise ValueError("Adduct source must be exact regular MAE bytes")
    _, _, identity = _parents(prepared_receptor, receptor_context,
                              prepared_receptor_sha256, receptor_context_sha256)
    candidate_raw = _regular_json(candidate_evidence, candidate_evidence_sha256,
                                  "Candidate evidence")
    candidate = _candidate(candidate_raw, source_sha256, prepared_receptor_sha256,
                           receptor_context_sha256)
    if any(candidate["target"][key] != value for key, value in identity.items()):
        raise ValueError("Candidate target differs from receptor parents")
    qc_raw = _regular_json(graph_qc, graph_qc_sha256, "Covalent-adduct graph QC")
    qc = _graph_qc(qc_raw, source_sha256, candidate_evidence_sha256,
                   prepared_receptor_sha256, receptor_context_sha256, candidate)
    output.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source, output / "adduct.mae")
    shutil.copyfile(graph_qc, output / "graph-qc.json")
    shutil.copyfile(candidate_evidence, output / "candidate-evidence.json")
    shutil.copyfile(prepared_receptor, output / "prepared-receptor.json")
    shutil.copyfile(receptor_context, output / "receptor-context.json")
    artifacts = {path.name: sha(path) for path in output.iterdir() if path.is_file()}
    manifest = {"schema": "covalent-adduct-input-artifact-v1",
        "status": "adduct_input_admitted_native_parameterization_pending",
        "system_id": qc["system_id"], "candidate_id": candidate["candidate_id"],
        "compound_id": candidate["compound_id"], "target": candidate["target"],
        "chemistry": candidate["chemistry"],
        "parents": {"candidate_evidence_sha256": candidate_evidence_sha256,
            "prepared_receptor_sha256": prepared_receptor_sha256,
            "receptor_context_sha256": receptor_context_sha256,
            "graph_qc_sha256": graph_qc_sha256},
        "artifacts": artifacts,
        "native_graph_revalidation_pending": True,
        "parameterization_completed": False, "md_system_ready": False,
        "campaign_ready": False, "calculation_submitted": False,
        "remaining_checks": ["vendor-native graph revalidation and OPLS assignment",
            "solvated-system minimization and CPU/GPU energy-force agreement",
            "unrestrained equilibration and trajectory audit"]}
    path = output / "adduct-input.json"
    path.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    return manifest


def load_covalent_adduct_directory(directory, manifest_sha256):
    """Replay one admitted directory, independent of its registry location."""
    directory = Path(directory).resolve()
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Covalent-adduct input artifact is unavailable")
    path = directory / "adduct-input.json"
    manifest = _regular_json(path, manifest_sha256, "Covalent-adduct input manifest")
    if (manifest.get("schema") != "covalent-adduct-input-artifact-v1"
            or manifest.get("status") != "adduct_input_admitted_native_parameterization_pending"
            or manifest.get("native_graph_revalidation_pending") is not True
            or manifest.get("parameterization_completed") is not False
            or manifest.get("md_system_ready") is not False):
        raise ValueError("Covalent-adduct input has an unsupported state")
    expected = manifest.get("artifacts")
    if not isinstance(expected, dict) or set(expected) != {"adduct.mae", "graph-qc.json",
            "candidate-evidence.json", "prepared-receptor.json", "receptor-context.json"}:
        raise ValueError("Covalent-adduct input inventory is incomplete")
    for name, digest in expected.items():
        candidate = directory / name
        if (candidate.is_symlink() or not candidate.is_file() or sha(candidate) != digest):
            raise ValueError("Covalent-adduct input artifact checksum mismatch: " + name)
    parents = manifest["parents"]
    receptor, _, identity = _parents(directory / "prepared-receptor.json",
        directory / "receptor-context.json", parents["prepared_receptor_sha256"],
        parents["receptor_context_sha256"])
    candidate = _candidate(json.loads((directory / "candidate-evidence.json").read_text()),
        expected["adduct.mae"], parents["prepared_receptor_sha256"],
        parents["receptor_context_sha256"])
    if sha(directory / "candidate-evidence.json") != parents["candidate_evidence_sha256"]:
        raise ValueError("Candidate evidence parent checksum mismatch")
    qc = _graph_qc(json.loads((directory / "graph-qc.json").read_text()), expected["adduct.mae"],
        parents["candidate_evidence_sha256"], parents["prepared_receptor_sha256"],
        parents["receptor_context_sha256"], candidate)
    if sha(directory / "graph-qc.json") != parents["graph_qc_sha256"]:
        raise ValueError("Graph QC parent checksum mismatch")
    if (manifest["system_id"] != qc["system_id"] or manifest["candidate_id"] != candidate["candidate_id"]
            or manifest["target"] != candidate["target"] or manifest["chemistry"] != candidate["chemistry"]
            or any(candidate["target"][key] != value for key, value in identity.items())
            or receptor["uniprot"] != manifest["target"]["uniprot"]):
        raise ValueError("Covalent-adduct manifest differs from recomputed evidence")
    return {"directory": directory, "manifest": manifest, "candidate": candidate,
            "graph_qc": qc, "evidence_files": [path, *[directory / name for name in sorted(expected)]]}


def load_covalent_adduct_input(root, artifact_id, manifest_sha256):
    """Reload a state-registered admitted artifact without trusting copied paths."""
    root = Path(root).resolve()
    if (not isinstance(artifact_id, str)
            or not re.fullmatch(r"covalent-adduct-input-[a-f0-9]{32}", artifact_id)):
        raise ValueError("Use a covalent-adduct-input artifact ID")
    directory = (root / "state/covalent_adduct_input" / artifact_id[22:]).resolve()
    directory.relative_to(root)
    return load_covalent_adduct_directory(directory, manifest_sha256)
