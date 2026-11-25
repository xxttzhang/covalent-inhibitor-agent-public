"""Bind registered NPT receipts; protocol completion and admission are distinct."""
import json
import math

from covscreen.jobs import sha256_file
from .md_preparation_evidence import registered_output
from .md_equilibration_checks import equilibration_checks


def equilibration_receipt(root, record, manager, spec, result, parent):
    if result.state != "COMPLETED" or result.return_code != 0:
        raise ValueError("Equilibration has not completed successfully")
    folder = manager.registry.job_dir(spec.run_id) / "artifacts/openmm-equilibration"
    manifest = registered_output(root, folder, spec, result, "equilibration.json")
    checksum = sha256_file(manifest)
    prior = record.get("equilibration")
    if prior and prior["manifest_sha256"] != checksum:
        raise ValueError("Previously verified equilibration changed")
    value = json.loads(manifest.read_text())
    if not isinstance(value, dict):
        raise ValueError("Equilibration receipt must be an object")
    short = json.loads((manager.registry.job_dir(parent["run_id"]) / "artifacts/openmm-short-qc/short-qc.json").read_text())
    parents = {key: short["parents"][key] for key in ("parameter_audit_sha256", "minimization_manifest_sha256")}
    parents["short_qc_manifest_sha256"] = parent["manifest_sha256"]
    protocol = {"ensemble": "NPT", "unrestrained": True, "platform": spec.parameters["platform"],
        "platform_properties": spec.parameters["platform_properties"], "duration_ps": 500.0,
        "steps": 250000, "report_interval_ps": 10.0, "timestep_fs": 2.0, "temperature_k": 300.0,
        "pressure_bar": 1.01325, "friction_per_ps": 1.0, "barostat_interval": 25,
        **{key: spec.parameters[key] for key in ("integrator_seed", "barostat_seed")}}
    if (value.get("schema") != "openmm-equilibration-artifact-v1"
            or value.get("system_id") != parent["candidate_id"] or value.get("atoms") != short["atoms"]
            or value.get("parents") != parents or value.get("protocol") != protocol
            or value.get("equilibration_protocol_completed") is not True
            or value.get("admission_duration_met") is not True
            or any(value.get(key) is not False for key in ("equilibrium_convergence_claimed", "production_started", "md_system_ready", "campaign_ready"))):
        raise ValueError("Equilibration receipt differs from the approved protocol or parent chain")
    names = {"final-positions-nm.npy", "final-velocities-nm-ps.npy", "final-box-vectors-nm.npy",
        "final-state.xml", "checkpoint.chk", "frames.jsonl"}
    if spec.parameters['platform'] == 'CUDA':
        names |= {'platform-validation.json', 'validation-cpu-forces.npy', 'validation-cuda-forces.npy'}
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != names:
        raise ValueError("Equilibration artifact inventory is incomplete")
    for name in names:
        if not isinstance(artifacts[name], str):
            raise ValueError("Equilibration artifact checksum is missing")
        registered_output(root, folder, spec, result, name, artifacts[name])
    if spec.parameters['platform'] == 'CUDA':
        from .md_platform_validation import readback
        if value.get('platform_validation_passed') is not True:
            raise ValueError('CUDA equilibration lacks a passing numerical comparison')
        readback(folder, parent['candidate_id'], {**parents,
            'positions_sha256': short['artifacts']['final-positions-nm.npy']},
            spec.parameters['platform_properties'], spec.parameters['cpu_threads'], short['atoms'])
    rows = []
    with (folder / "frames.jsonl").open() as stream:
        for line in stream:
            row = json.loads(line)
            if (not isinstance(row, dict) or len(rows) >= 50
                    or row.get("step") != (len(rows) + 1) * 5000
                    or row.get("time_ps") != (len(rows) + 1) * 10):
                raise ValueError("Equilibration frames differ from the fixed reporting schedule")
            for key in ("potential_energy_kj_mol", "kinetic_energy_kj_mol", "temperature_k", "volume_nm3",
                        "density_kg_m3", "covalent_distance_angstrom", "max_relative_constraint_error"):
                number = row.get(key)
                if type(number) not in (int, float) or not math.isfinite(number):
                    raise ValueError("Equilibration frame metrics are missing or nonfinite")
            if (row["volume_nm3"] <= 0 or not 1 < row["temperature_k"] < 2000
                    or not 100 < row["density_kg_m3"] < 2000
                    or not 1.4 < row["covalent_distance_angstrom"] < 2.4
                    or not 0 <= row["max_relative_constraint_error"] < 1e-4):
                raise ValueError("Equilibration frame contradicts the worker stability gate")
            rows.append(row)
    if (len(rows) != 50 or value.get("frames") != 50 or value.get("final_window_frames") != 10
            or value.get("last_frame") != rows[-1]):
        raise ValueError("Equilibration frame summary is incomplete")
    checks = value.get("checks")
    expected = {"temperature_window", "density_window", "volume_stability", "constraint_stability", "covalent_geometry"}
    if not isinstance(checks, dict) or set(checks) != expected or any(type(item) is not bool for item in checks.values()):
        raise ValueError("Equilibration admission checks are incomplete")
    if checks != equilibration_checks(rows[-10:]):
        raise ValueError("Equilibration admission checks contradict the registered frames")
    admitted = all(checks.values())
    status = "NPT_EQUILIBRATION_COMPLETED_PRODUCTION_REVIEW_PENDING" if admitted else "EQUILIBRATION_DIAGNOSTIC_COMPLETE_NOT_ADMITTED"
    if value.get("production_review_ready") is not admitted or value.get("status") != status:
        raise ValueError("Equilibration admission flags contradict its checks")
    return {"run_id": spec.run_id, "manifest_sha256": checksum, "candidate_id": parent["candidate_id"],
        "equilibration_protocol_completed": True, "production_review_ready": admitted,
        "assessment": "review_required" if admitted else "rejected", "checks": checks,
        "equilibrium_convergence_claimed": False, "production_started": False, "md_system_ready": False}
