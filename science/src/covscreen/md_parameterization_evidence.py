"""Verify parameterization receipts against a freshly validated admission parent."""
import json

from covscreen.jobs import sha256_file
from covscreen.md_parameter_contract import parameter_contract


def parameterization_receipt(root, stage_record, manager, spec, result, parent):
    if result.state != "COMPLETED" or result.return_code != 0:
        raise ValueError("Parameterization has not completed successfully")
    folder = manager.registry.job_dir(spec.run_id) / "artifacts/adduct-parameterization"
    paths = {name: folder / name for name in ("parameterization-audit.json", "finished.json", "parameterized.cms", "atom-mapping.json", "input.mae")}
    if any(path.is_symlink() for path in (folder, folder.parent, folder.parent.parent)):
        raise ValueError("Parameterization output directory cannot be a symbolic link")
    records = {item.path: item for item in result.artifacts}
    for path in paths.values():
        record = records.get(str(path.relative_to(root)))
        if (path.is_symlink() or not path.is_file() or not record or record.run_id != spec.run_id
                or sha256_file(path) != record.sha256 or path.stat().st_size != record.size_bytes):
            raise ValueError("Parameterization output differs from registered worker artifacts")
    checksum = sha256_file(paths["parameterization-audit.json"])
    prior = stage_record.get("parameterization")
    if prior and prior["manifest_sha256"] != checksum:
        raise ValueError("Previously verified parameterization output changed")
    audit = parameter_contract(json.loads(paths["parameterization-audit.json"].read_text()), sha256_file(paths["parameterized.cms"]))
    finished = json.loads(paths["finished.json"].read_text())
    original = parent
    parent = manager.registry.job_dir(original["run_id"]) / "artifacts/md-adduct-admission/adduct-input"
    if (audit.get("system_id") != original["candidate_id"] or audit.get("solute_graph_preserved") is not True
            or audit.get("source_input_sha256") != sha256_file(parent / "adduct.mae")
            or audit.get("source_qc_sha256") != sha256_file(parent / "graph-qc.json")
            or audit.get("native_input_sha256") != sha256_file(paths["input.mae"])
            or audit.get("atom_mapping_sha256") != sha256_file(paths["atom-mapping.json"])
            or any(finished.get(key) != value for key, value in audit.items())):
        raise ValueError("Parameterization evidence differs from the frozen admission or completion receipt")
    return {"run_id": spec.run_id, "manifest_sha256": checksum, "candidate_id": original["candidate_id"],
        "parameterized_cms_sha256": audit["parameterized_cms_sha256"], "atom_mapping_sha256": audit["atom_mapping_sha256"],
        "parameterization_completed": True, "md_system_ready": False, "simulation_started": False}

