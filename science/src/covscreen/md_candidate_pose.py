"""Freeze one screened CovDock pose for candidate-specific MD handoff.

The public boundary is a registered artifact ID plus a manifest checksum.  Raw
paths are accepted only by the local artifact producer and are copied into the
new immutable directory.  Native graph inspection is deliberately a later
step because Maestro structures require the registered Schrodinger runtime.
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
import shutil

from covscreen.md_adduct_input import SUPPORTED_REACTIONS
from covscreen.pose_identity import exact_pose_index


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _regular(path, digest, label):
    path = Path(path)
    if (path.is_symlink() or not path.is_file()
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or sha(path) != digest):
        raise ValueError(label + " checksum mismatch")
    return path


def _json(path, digest, label):
    value = json.loads(_regular(path, digest, label).read_text())
    if not isinstance(value, dict):
        raise ValueError(label + " must be a JSON object")
    return value


def _receptor_parents(prepared_path, prepared_sha256, context_path, context_sha256):
    prepared = _json(prepared_path, prepared_sha256, "Prepared receptor manifest")
    context = _json(context_path, context_sha256, "Receptor context manifest")
    if (prepared.get("schema") != "prepared-receptor-artifact-v1"
            or prepared.get("status") != "prepared_isolated_protein_scope"
            or prepared.get("receptor_prepared") is not True
            or prepared.get("campaign_ready") is not False):
        raise ValueError("Prepared receptor state is unsupported")
    identity = {key: prepared.get(key) for key in
        ("entry_id", "uniprot", "canonical_cys", "author_chain", "author_residue")}
    if (context.get("schema") != "receptor-site-context-artifact-v1"
            or context.get("status") != "descriptive_site_context_completed_review_pending"
            or context.get("parents", {}).get("prepared_receptor_sha256") != prepared_sha256
            or context.get("campaign_ready") is not False
            or any(context.get("identity", {}).get(key) != value for key, value in identity.items())):
        raise ValueError("Receptor context does not bind the prepared receptor")
    return identity


def _selected_row(table, compound_id, input_sdf_index):
    with Path(table).open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or len(rows) > 10000:
        raise ValueError("Candidate result table has an unsupported size")
    identities = [(row.get("compound_id"), row.get("input_sdf_index")) for row in rows]
    if len(set(identities)) != len(identities):
        raise ValueError("Candidate result table contains duplicate identities")
    matches = [row for row in rows if row.get("compound_id") == compound_id
               and row.get("input_sdf_index") == str(input_sdf_index)]
    if len(matches) != 1:
        raise ValueError("Expected exactly one selected candidate row")
    return matches[0]


def _delivered_pose(row, delivery, identity):
    """Use identical scientific admission checks when freezing and replaying."""
    if (row.get("covdock_status") != "SUCCESS"
            or row.get("af3_status") != "SUCCESS"
            or row.get("physical_validity", "").upper() not in {"PASS", "WARN"}):
        raise ValueError("Selected candidate lacks admitted structural evidence")
    target = delivery.get("target_context", {})
    warhead = row.get("warhead_class")
    reaction = SUPPORTED_REACTIONS.get(warhead)
    if (target.get("attachment_residue") != f'{identity["author_chain"]}:{identity["author_residue"]}'
            or target.get("nucleophilic_atom") != "SG"
            or target.get("reaction_type") != reaction or reaction is None
            or row.get("nucleophilic_atom") != f'{identity["author_chain"]}:CYS:{identity["author_residue"]}:SG'):
        raise ValueError("Candidate chemistry or target differs from receptor parents")
    if not row.get("standardized_smiles"):
        raise ValueError("Candidate chemistry is absent")
    return warhead, reaction, exact_pose_index(row.get("covdock_structure_pose"))


def freeze_candidate_pose(candidate_results, candidate_results_sha256, covdock_source,
                          covdock_source_sha256, compound_id, input_sdf_index,
                          prepared_receptor, prepared_receptor_sha256, receptor_context,
                          receptor_context_sha256, output, *, native_target_context=None):
    """Copy and bind one quality-passed CovDock pose without inspecting MAE."""
    candidate_results = _regular(candidate_results, candidate_results_sha256,
                                 "Candidate result table")
    covdock_source = _regular(covdock_source, covdock_source_sha256,
                              "CovDock structure source")
    if covdock_source.name.lower().endswith(".maegz") is False:
        raise ValueError("CovDock source must be a MAEGZ file")
    if (not isinstance(compound_id, str) or not compound_id.strip()
            or type(input_sdf_index) is not int or input_sdf_index < 0):
        raise ValueError("Candidate identity is invalid")
    identity = _receptor_parents(prepared_receptor, prepared_receptor_sha256,
                                 receptor_context, receptor_context_sha256)
    row = _selected_row(candidate_results, compound_id, input_sdf_index)
    if (row.get("route") != "library-first"
            or row.get("result_scope") != "candidate-results-v1"
            or row.get("online_structural_status") != "BOTH_STRUCTURAL_QC_PASSED"):
        raise ValueError("Candidate has not passed the formal structural-quality gate")
    if row.get("covdock_artifact_sha256") != covdock_source_sha256:
        raise ValueError("Candidate row does not bind the exact CovDock source")
    if (not row.get("covdock_artifact_id") or not row.get("covdock_artifact_version")
            or not re.fullmatch(r"\d+", row.get("covdock_structure_pose", ""))):
        raise ValueError("Candidate row lacks a delivered CovDock artifact or pose")
    pose_index = exact_pose_index(row["covdock_structure_pose"])
    warhead = row.get("warhead_class")
    if warhead not in SUPPORTED_REACTIONS or not row.get("standardized_smiles"):
        raise ValueError("Candidate chemistry is absent or unsupported")
    try:
        recorded = json.loads(row["covdock_target_context"]) if row.get("covdock_target_context") else None
    except json.JSONDecodeError as error:
        raise ValueError("CovDock target context is invalid") from error
    target_context = native_target_context if native_target_context is not None else recorded
    if native_target_context is not None and (not isinstance(native_target_context, dict)
        or native_target_context.get("native_workflow_id") != row.get("task_id")
        or native_target_context.get("artifact_id") != row.get("covdock_artifact_id")
        or native_target_context.get("version_id") != row.get("covdock_artifact_version")):
        raise ValueError("Native target context does not bind the selected table and structure")
    if native_target_context is not None and recorded is not None and (
        not isinstance(recorded, dict) or any(recorded.get(key) != native_target_context.get(key)
            for key in ("artifact_sha256", "parameters"))):
        raise ValueError("Candidate table target context conflicts with the bound native plan")
    parameters = target_context.get("parameters", {}) if isinstance(target_context, dict) else {}
    expected_reaction = SUPPORTED_REACTIONS[warhead]
    if (parameters.get("chain") != identity["author_chain"]
            or str(parameters.get("residue_number")) != str(identity["author_residue"])
            or parameters.get("residue") != "CYS"
            or parameters.get("reaction_type") != expected_reaction
            or not isinstance(target_context, dict) or target_context.get("artifact_sha256") != covdock_source_sha256):
        raise ValueError("CovDock target context differs from receptor or chemistry parents")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Candidate pose output must be a new directory")
    output.mkdir(parents=True, exist_ok=False)
    copies = {"candidate-results.csv": candidate_results,
              "covdock-source.maegz": covdock_source,
              "prepared-receptor.json": Path(prepared_receptor),
              "receptor-context.json": Path(receptor_context)}
    for name, source in copies.items():
        shutil.copyfile(source, output / name)
    artifacts = {name: sha(output / name) for name in sorted(copies)}
    candidate_id = "library-%06d-%s" % (input_sdf_index, covdock_source_sha256[:12])
    manifest = {"schema": "candidate-pose-input-artifact-v1",
        "status": "pose_selected_native_graph_qc_pending", "candidate_id": candidate_id,
        "compound_id": compound_id, "input_sdf_index": input_sdf_index,
        "engine": "CovDock-LO", "pose_index": pose_index,
        "target": {**identity, "atom_name": "SG"},
        "chemistry": {"standardized_smiles": row["standardized_smiles"],
            "warhead_class": warhead, "reaction_type": expected_reaction,
            "covalent_mode": "Cys-S-C", "product_state": "covalent_adduct"},
        "delivered_artifact": {"artifact_id": row["covdock_artifact_id"],
            "version_id": row["covdock_artifact_version"], "sha256": covdock_source_sha256},
        "parents": {"candidate_results_sha256": candidate_results_sha256,
            "prepared_receptor_sha256": prepared_receptor_sha256,
            "receptor_context_sha256": receptor_context_sha256},
        "artifacts": artifacts, "native_graph_qc_completed": False,
        "parameterization_completed": False, "md_system_ready": False,
        "campaign_ready": False, "calculation_submitted": False}
    if native_target_context is not None:
        manifest["native_target_context"] = native_target_context
    path = output / "candidate-pose-input.json"
    path.write_text(json.dumps(manifest, indent=2, allow_nan=False) + "\n")
    return manifest


def freeze_delivered_candidate_pose(candidate_delivery, candidate_delivery_sha256,
                                    compound_id, input_sdf_index, prepared_receptor,
                                    prepared_receptor_sha256, receptor_context,
                                    receptor_context_sha256, output, *, expected_route=None, expected_task_id=None):
    """Select one candidate from a frozen delivery without accepting source paths."""
    delivery_path = _regular(candidate_delivery, candidate_delivery_sha256,
                             "Candidate delivery manifest")
    delivery = json.loads(delivery_path.read_text())
    shared = delivery.get("schema") == "shared-md-candidate-delivery-v1"
    source_bundle = None
    if not shared and (expected_route is not None or expected_task_id is not None):
        if expected_route != "library-first" or expected_task_id != delivery.get("task_id"):
            raise ValueError("Legacy Library delivery cannot replace another bound source")
    if shared:
        from .md_candidate_delivery import load_shared_candidate_delivery
        source_bundle = load_shared_candidate_delivery(delivery_path, candidate_delivery_sha256,
            expected_route=expected_route, expected_task_id=expected_task_id)
        if delivery.get('source_profile') == 'current-study-refinement-reviews-v1' and (
                delivery['parents']['prepared_receptor'] != prepared_receptor_sha256
                or delivery['parents']['receptor_context'] != receptor_context_sha256):
            raise ValueError('Current-study MD pose must retain its reviewed receptor parents')
    if not shared and (delivery.get("schema") != "library-first-md-candidate-delivery-v1"
            or delivery.get("status") != "candidate_delivery_frozen_md_selection_pending"
            or delivery.get("route") != "library-first"
            or delivery.get("candidate_selected_for_md") is not False
            or delivery.get("native_graph_qc_completed") is not False):
        raise ValueError("Candidate delivery has an unsupported state")
    if (not isinstance(compound_id, str) or not compound_id.strip()
            or type(input_sdf_index) is not int or input_sdf_index < 0):
        raise ValueError("Candidate identity is invalid")
    candidates = delivery.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("Candidate delivery is empty")
    matches = [item for item in candidates if item.get("compound_id") == compound_id
               and item.get("input_sdf_index") == str(input_sdf_index)]
    if len(matches) != 1:
        raise ValueError("Expected exactly one candidate in the frozen delivery")
    row = matches[0]
    identity = _receptor_parents(prepared_receptor, prepared_receptor_sha256,
                                 receptor_context, receptor_context_sha256)
    warhead, expected_reaction, pose_index = _delivered_pose(row, delivery, identity)
    assets = delivery.get("assets")
    asset_key = row.get("covdock_asset")
    record = assets.get(asset_key) if isinstance(assets, dict) else None
    if not isinstance(record, dict) or set(record) != {"filename", "sha256", "size"}:
        raise ValueError("Selected candidate lacks a frozen CovDock asset")
    delivery_dir = delivery_path.parent.resolve()
    source = (delivery_dir / record["filename"]).resolve()
    source.relative_to(delivery_dir)
    source = _regular(source, record["sha256"], "Frozen CovDock asset")
    if not source.name.lower().endswith(".maegz"):
        raise ValueError("Frozen CovDock source must be a MAEGZ file")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Candidate pose output must be a new directory")
    output.mkdir(parents=True, exist_ok=False)
    copies = {"candidate-delivery.json": delivery_path,
              "covdock-source.maegz": source,
              "prepared-receptor.json": Path(prepared_receptor),
              "receptor-context.json": Path(receptor_context)}
    for name, parent in copies.items():
        shutil.copyfile(parent, output / name)
    artifacts = {name: sha(output / name) for name in sorted(copies)}
    candidate_id = (expected_route if shared else "library") + "-%06d-%s" % (input_sdf_index, record["sha256"][:12])
    if source_bundle:
        for evidence in source_bundle["evidence_files"]:
            relative = evidence.relative_to(delivery_path.parent)
            target_file = output / "source-delivery" / relative
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(evidence, target_file)
    manifest = {"schema":"candidate-pose-input-artifact-v3" if shared else "candidate-pose-input-artifact-v2",
        "status":"pose_selected_native_graph_qc_pending","candidate_id":candidate_id,
        "compound_id":compound_id,"input_sdf_index":input_sdf_index,
        "engine":"CovDock-LO","pose_index":pose_index,
        "target":{**identity,"atom_name":"SG"},
        "chemistry":{"standardized_smiles":row["standardized_smiles"],
            "warhead_class":warhead,"reaction_type":expected_reaction,
            "covalent_mode":"Cys-S-C","product_state":"covalent_adduct"},
        "delivered_artifact":{"delivery_task_id":delivery.get("task_id"),
            "asset_key":asset_key,"sha256":record["sha256"]},
        "parents":{"candidate_delivery_sha256":candidate_delivery_sha256,
            "prepared_receptor_sha256":prepared_receptor_sha256,
            "receptor_context_sha256":receptor_context_sha256},
        "artifacts":artifacts,"native_graph_qc_completed":False,
        "parameterization_completed":False,"md_system_ready":False,
        "campaign_ready":False,"calculation_submitted":False}
    if shared:
        manifest["origin"] = {"route": expected_route, "task_id": expected_task_id,
            "delivery_sha256": candidate_delivery_sha256}
    (output/"candidate-pose-input.json").write_text(
        json.dumps(manifest,indent=2,allow_nan=False)+"\n")
    return manifest


def load_candidate_pose_input(root, artifact_id, manifest_sha256):
    """Replay a registered candidate pose artifact and every copied parent."""
    root = Path(root).resolve()
    if (not isinstance(artifact_id, str)
            or not re.fullmatch(r"candidate-pose-input-[a-f0-9]{32}", artifact_id)):
        raise ValueError("Use a candidate-pose-input artifact ID")
    directory = (root / "state/candidate_pose_input" / artifact_id[21:]).resolve()
    directory.relative_to(root)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Candidate pose input artifact is unavailable")
    path = directory / "candidate-pose-input.json"
    manifest = _json(path, manifest_sha256, "Candidate pose input manifest")
    if manifest.get('schema') == 'historical-candidate-pose-input-v1':
        from .md_historical_pose import load
        return load(directory, manifest_sha256)
    if (manifest.get("schema") not in {"candidate-pose-input-artifact-v1",
                                       "candidate-pose-input-artifact-v2", "candidate-pose-input-artifact-v3"}
            or manifest.get("status") != "pose_selected_native_graph_qc_pending"
            or manifest.get("native_graph_qc_completed") is not False
            or manifest.get("parameterization_completed") is not False
            or manifest.get("md_system_ready") is not False
            or manifest.get("campaign_ready") is not False
            or manifest.get("calculation_submitted") is not False
            or type(manifest.get("pose_index")) is not int
            or type(manifest.get("input_sdf_index")) is not int
            or manifest["input_sdf_index"] < 0):
        raise ValueError("Candidate pose input has an unsupported state")
    exact_pose_index(manifest["pose_index"])
    expected = manifest.get("artifacts")
    shared = manifest.get("schema") == "candidate-pose-input-artifact-v3"
    delivery_v2 = shared or manifest.get("schema") == "candidate-pose-input-artifact-v2"
    selection_name = "candidate-delivery.json" if delivery_v2 else "candidate-results.csv"
    names = {selection_name, "covdock-source.maegz", "prepared-receptor.json",
             "receptor-context.json"}
    if not isinstance(expected, dict) or set(expected) != names:
        raise ValueError("Candidate pose input inventory is incomplete")
    for name, digest in expected.items():
        _regular(directory / name, digest, "Candidate pose artifact " + name)
    parents = manifest.get("parents", {})
    selection_parent = ("candidate_delivery_sha256" if delivery_v2
                        else "candidate_results_sha256")
    if (expected[selection_name] != parents.get(selection_parent)
            or expected["prepared-receptor.json"] != parents.get("prepared_receptor_sha256")
            or expected["receptor-context.json"] != parents.get("receptor_context_sha256")
            or expected["covdock-source.maegz"] != manifest.get("delivered_artifact", {}).get("sha256")):
        raise ValueError("Candidate pose copied artifacts differ from parent hashes")
    identity = _receptor_parents(directory / "prepared-receptor.json",
        parents["prepared_receptor_sha256"], directory / "receptor-context.json",
        parents["receptor_context_sha256"])
    native = manifest.get("native_target_context")
    if native is not None:
        parameters = native.get("parameters", {}) if isinstance(native, dict) else {}
        if (not isinstance(native, dict) or native.get("artifact_sha256") != expected["covdock-source.maegz"]
            or native.get("artifact_id") != manifest["delivered_artifact"].get("artifact_id")
            or native.get("version_id") != manifest["delivered_artifact"].get("version_id")
            or parameters.get("chain") != identity["author_chain"]
            or str(parameters.get("residue_number")) != str(identity["author_residue"])
            or parameters.get("residue") != "CYS"
            or parameters.get("reaction_type") != manifest["chemistry"]["reaction_type"]):
            raise ValueError("Frozen native target context differs from receptor or source")
    source_evidence = []
    if delivery_v2:
        delivery = json.loads((directory / selection_name).read_text())
        if shared:
            from .md_candidate_delivery import load_shared_candidate_delivery
            origin = manifest.get("origin", {})
            if (set(origin) != {"route", "task_id", "delivery_sha256"}
                    or origin["delivery_sha256"] != parents["candidate_delivery_sha256"]):
                raise ValueError("Shared pose origin differs from its delivery")
            bundle = load_shared_candidate_delivery(directory / "source-delivery/candidate-results.json",
                origin["delivery_sha256"], expected_route=origin["route"], expected_task_id=origin["task_id"])
            if bundle["manifest"] != delivery:
                raise ValueError("Shared pose delivery copies disagree")
            if delivery.get('source_profile') == 'current-study-refinement-reviews-v1' and (
                    delivery['parents']['prepared_receptor'] != parents['prepared_receptor_sha256']
                    or delivery['parents']['receptor_context'] != parents['receptor_context_sha256']):
                raise ValueError('Current-study pose receptor parents differ from the reviewed delivery')
            source_evidence = bundle["evidence_files"]
        matched = [item for item in delivery.get("candidates", [])
            if item.get("compound_id") == manifest["compound_id"]
            and item.get("input_sdf_index") == str(manifest["input_sdf_index"])]
        row, = matched
        _delivered_pose(row, delivery, identity)
        source_matches = (delivery.get("assets", {}).get(row.get("covdock_asset"), {})
                          .get("sha256") == expected["covdock-source.maegz"])
    else:
        row = _selected_row(directory / selection_name, manifest["compound_id"],
                            manifest["input_sdf_index"])
        source_matches = row.get("covdock_artifact_sha256") == expected["covdock-source.maegz"]
    if native is not None and (native.get("native_workflow_id") != row.get("task_id")
        or native.get("artifact_id") != row.get("covdock_artifact_id")
        or native.get("version_id") != row.get("covdock_artifact_version")):
        raise ValueError("Frozen native target context differs from the selected table")
    if (not source_matches
            or exact_pose_index(row.get("covdock_structure_pose")) != manifest["pose_index"]
            or any(manifest["target"].get(key) != value for key, value in identity.items())
            or row.get("warhead_class") != manifest["chemistry"]["warhead_class"]):
        raise ValueError("Candidate pose manifest differs from replayed evidence")
    if shared:
        expected_chemistry = {"standardized_smiles": row["standardized_smiles"],
            "warhead_class": row["warhead_class"], "reaction_type": SUPPORTED_REACTIONS.get(row["warhead_class"]),
            "covalent_mode": "Cys-S-C", "product_state": "covalent_adduct"}
        context = delivery["target_context"]
        if (manifest["chemistry"] != expected_chemistry or manifest["engine"] != "CovDock-LO"
                or manifest["target"] != {**identity, "atom_name": "SG"}
                or manifest["candidate_id"] != origin["route"] + "-%06d-%s" % (manifest["input_sdf_index"], expected["covdock-source.maegz"][:12])
                or manifest["delivered_artifact"]["delivery_task_id"] != origin["task_id"]
                or manifest["delivered_artifact"]["asset_key"] != row["covdock_asset"]
                or row.get("af3_status") != "SUCCESS" or row.get("covdock_status") != "SUCCESS"
                or row.get("physical_validity", "").upper() not in {"PASS", "WARN"}
                or context["attachment_residue"] != f'{identity["author_chain"]}:{identity["author_residue"]}'
                or context["reaction_type"] != expected_chemistry["reaction_type"]):
            raise ValueError("Shared selected pose differs from its scientific source")
    return {"directory": directory, "manifest": manifest, "row": row,
            "evidence_files": [path, *[directory / name for name in sorted(names)], *source_evidence]}
