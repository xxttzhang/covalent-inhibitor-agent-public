"""Check the existing short-QC worker's registered receipts without running MD."""
import json
import math

from covscreen.jobs import sha256_file
from .md_preparation_evidence import registered_output


def short_qc_receipt(root, record, manager, spec, result, parent):
    if result.state != "COMPLETED" or result.return_code != 0:
        raise ValueError("Short QC has not completed successfully")
    folder = manager.registry.job_dir(spec.run_id) / "artifacts/openmm-short-qc"
    manifest = registered_output(root, folder, spec, result, "short-qc.json")
    checksum = sha256_file(manifest)
    prior = record.get("short_qc")
    if prior and prior["manifest_sha256"] != checksum:
        raise ValueError("Previously verified short QC changed")
    value = json.loads(manifest.read_text())
    if not isinstance(value, dict):
        raise ValueError("Short-QC receipt must be an object")
    # Caller revalidates the preparation and its parameterization/admission chain first.
    preparation = json.loads((manager.registry.job_dir(parent["run_id"])
        / "artifacts/openmm-system-preparation/openmm-preparation.json").read_text())
    parents = {"parameter_audit_sha256": preparation["parents"]["parameter_audit_sha256"], **preparation["stages"]}
    protocol = {"ensemble": "NVT", "unrestrained": True, "platform": "CPU",
        "cpu_threads": spec.resource_budget.cpu_count,
        **{key: spec.parameters[key] for key in ("steps", "report_interval", "velocity_seed")},
        "timestep_fs": 1.0, "duration_ps": spec.parameters["steps"] / 1000,
        "temperature_k": 300.0, "friction_per_ps": 1.0, "constraint_tolerance": 1e-6}
    if (value.get("schema") != "openmm-short-qc-artifact-v1"
            or value.get("status") != "UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING"
            or value.get("system_id") != parent["candidate_id"] or value.get("atoms") != preparation["atoms"]
            or value.get("parents") != parents or value.get("protocol") != protocol
            or value.get("short_qc_completed") is not True or value.get("integration_stability_passed") is not True
            or any(value.get(key) is not False for key in ("equilibration_completed", "production_started", "md_system_ready", "campaign_ready"))):
        raise ValueError("Short-QC receipt differs from the approved protocol or preparation chain")
    names = {"final-positions-nm.npy", "final-velocities-nm-ps.npy", "frames.jsonl"}
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != names:
        raise ValueError("Short-QC artifact inventory is incomplete")
    for name in names:
        if not isinstance(artifacts[name], str):
            raise ValueError("Short-QC artifact checksum is missing")
        registered_output(root, folder, spec, result, name, artifacts[name])
    replay = value.get("state_replay", {})
    if not isinstance(replay, dict):
        raise ValueError("Short-QC replay must be an object")
    for key in ("energy_difference_kj_mol", "relative_force_rms_difference"):
        number = replay.get(key)
        if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
            raise ValueError("Short-QC state replay metrics are missing or nonfinite")
    if replay["relative_force_rms_difference"] > 1e-5:
        raise ValueError("Short-QC state replay contradicts the worker gate")
    interval = protocol["report_interval"]
    expected = protocol["steps"] // interval
    rows = []
    with (folder / "frames.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Short-QC frame must be an object")
            step = (len(rows) + 1) * interval
            if len(rows) >= expected or row.get("step") != step or row.get("time_ps") != step / 1000:
                raise ValueError("Short-QC frames differ from the approved reporting schedule")
            for key in ("potential_energy_kj_mol", "kinetic_energy_kj_mol", "temperature_k",
                        "covalent_distance_angstrom", "max_relative_constraint_error"):
                number = row.get(key)
                if type(number) not in (int, float) or not math.isfinite(number):
                    raise ValueError("Short-QC frame metrics are missing or nonfinite")
            if (not 1.4 < row["covalent_distance_angstrom"] < 2.4
                    or not 0 <= row["max_relative_constraint_error"] < 1e-4
                    or not 1 < row["temperature_k"] < 2000):
                raise ValueError("Short-QC frame contradicts the worker stability gate")
            rows.append(row)
    if (len(rows) != expected or value.get("frames") != expected
            or value.get("first_frame") != rows[0] or value.get("last_frame") != rows[-1]):
        raise ValueError("Short-QC frame summary is incomplete or inconsistent")
    return {"run_id": spec.run_id, "manifest_sha256": checksum, "candidate_id": parent["candidate_id"],
        "short_qc_completed": True, "integration_stability_passed": True, "duration_ps": protocol["duration_ps"],
        "equilibration_completed": False, "production_started": False, "md_system_ready": False}
