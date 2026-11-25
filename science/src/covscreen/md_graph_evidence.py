"""Shared native covalent-graph receipt validation for discovery workflows."""
import json

from .jobs import sha256_file
from .md_adduct_input import load_covalent_adduct_directory
from .md_candidate_pose import load_candidate_pose_input


def graph_admission_receipt(root, stage_record, manager, spec, result, pose):
    """Replay graph evidence; caller must verify approval and current selection.

    ``pose`` carries an artifact_id and manifest_sha256, independent of route
    state layout. This check does not grant parameterization or MD readiness.
    """
    if (spec.engine != 'library-first-md-adduct-admission' or spec.route != 'library-first'
            or any(getattr(result, key) != getattr(spec, key)
                   for key in ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('Native admission job identity differs from its registered specification')
    if (spec.parameters.get('candidate_pose_artifact_id') != pose['artifact_id']
            or spec.parameters.get('manifest_sha256') != pose['manifest_sha256']):
        raise ValueError('Native admission job pose differs from the selected frozen input')
    if result.state != "COMPLETED" or result.return_code != 0:
        raise ValueError("Native admission has not completed successfully")
    folder = manager.registry.job_dir(spec.run_id) / "artifacts/md-adduct-admission"
    if any(path.is_symlink() for path in (folder, folder.parent, folder.parent.parent, folder / "adduct-input")):
        raise ValueError("Native admission output cannot be a symbolic link")
    path = folder / "md-adduct-admission.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("Native admission receipt is missing")
    value = json.loads(path.read_text())
    expected = stage_record.get("admission")
    digest = sha256_file(path)
    if expected and expected["manifest_sha256"] != digest:
        raise ValueError("Previously verified native admission output changed")
    if (value.get("schema") != "library-first-md-adduct-admission-v1"
            or value.get("status") != "ADDUCT_ADMITTED_PARAMETERIZATION_PENDING"
            or value.get("native_graph_qc_completed") is not True
            or value.get("parameterization_completed") is not False or value.get("md_system_ready") is not False
            or value.get("parents", {}).get("candidate_pose_manifest_sha256") != pose["manifest_sha256"]):
        raise ValueError("Native admission output does not bind the frozen candidate")
    checksum = value.get("artifacts", {}).get("adduct-input/adduct-input.json")
    loaded = load_covalent_adduct_directory(folder / "adduct-input", checksum)
    original = load_candidate_pose_input(root, pose["artifact_id"], pose["manifest_sha256"])["manifest"]
    admitted = loaded["manifest"]
    native = folder / "native-handoff/candidate-md-handoff.json"
    if (native.parent.is_symlink() or native.is_symlink() or not native.is_file()
            or sha256_file(native) != value.get("parents", {}).get("native_handoff_manifest_sha256")):
        raise ValueError("Native graph handoff witness changed or is missing")
    handoff = json.loads(native.read_text())
    if (handoff.get("schema") != "candidate-md-handoff-artifact-v1"
            or handoff.get("status") != "native_graph_qc_passed_admission_pending"
            or handoff.get("native_graph_qc_completed") is not True
            or handoff.get("parents", {}).get("candidate_pose_manifest_sha256") != pose["manifest_sha256"]
            or handoff.get("candidate_id") != original["candidate_id"]
            or handoff.get("compound_id") != original["compound_id"] or handoff.get("pose_index") != original["pose_index"]):
        raise ValueError("Native graph witness differs from the selected pose")
    if handoff.get("artifacts") != {key: admitted["artifacts"][key]
                                   for key in ("adduct.mae", "graph-qc.json", "candidate-evidence.json")}:
        raise ValueError("Native graph witness differs from admitted graph bytes")
    records = {item.path: item for item in result.artifacts}
    if len(records) != len(result.artifacts):
        raise ValueError('Native admission has duplicate registered artifact paths')
    extra_files = []
    if original.get('schema') == 'historical-candidate-pose-input-v1':
        audit_path = native.parent / 'historical-structure-audit.json'
        if audit_path.is_symlink() or not audit_path.is_file() or sha256_file(audit_path) != handoff.get('historical_structure_audit_sha256'):
            raise ValueError('Historical native structure audit is missing or changed')
        audit = json.loads(audit_path.read_text())
        if (audit.get('schema') != 'historical-adduct-native-audit-v1'
                or audit.get('candidate_id') != original['candidate_id']
                or audit.get('compound_id') != original['compound_id']
                or audit.get('candidate_pose_manifest_sha256') != pose['manifest_sha256']
                or audit.get('source_manifest_sha256') != original['parents']['historical_source_sha256']
                or audit.get('target_manifest_sha256') != original['parents']['target_identity_sha256']
                or audit.get('adduct_sha256') != admitted['artifacts']['adduct.mae']
                or audit.get('canonical_site') != original['target']['canonical_cys']
                or audit.get('author_chain') != original['target']['author_chain']
                or audit.get('author_residue') != original['target']['author_residue']
                or any(audit.get(k) is not True for k in ('original_pose_identity_verified',
                    'original_heavy_graph_preserved', 'cleanup_retained_graph_verified',
                    'native_coordinate_mapping_verified', 'peptide_connectivity_verified', 'new_loop_clash_check_passed'))
                or any(audit.get(k) is not False for k in ('parameterization_completed', 'md_system_ready'))):
            raise ValueError('Historical native audit does not bind the selected candidate and source')
        extra_files.append(audit_path)
    for file in [path, native, *loaded["evidence_files"], *extra_files]:
        record = records.get(str(file.relative_to(root)))
        if (not record or record.run_id != spec.run_id or record.sha256 != sha256_file(file)
                or record.size_bytes != file.stat().st_size):
            raise ValueError("Native admission output differs from registered worker artifacts")
    if (admitted["candidate_id"] != original["candidate_id"] or value.get("candidate_id") != original["candidate_id"]
            or value.get("system_id") != admitted["system_id"] or admitted["target"] != original["target"]
            or loaded["candidate"]["compound_id"] != original["compound_id"]
            or loaded["candidate"]["pose"]["pose_index"] != original["pose_index"]
            or any(admitted["chemistry"][key] != original["chemistry"][key]
                   for key in ("warhead_class", "reaction_type", "covalent_mode", "product_state"))
            or any(admitted["parents"][key] != original["parents"][key]
                   for key in ("prepared_receptor_sha256", "receptor_context_sha256"))):
        raise ValueError("Admitted native graph belongs to different candidate or receptor parents")
    return {"run_id": spec.run_id, "manifest_sha256": digest, "adduct_input_manifest_sha256": checksum,
        "candidate_id": admitted["candidate_id"], "native_graph_qc_completed": True,
        "parameterization_completed": False, "md_system_ready": False}

