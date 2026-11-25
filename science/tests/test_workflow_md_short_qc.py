"""Actual workflow contracts; molecular states and metric rows are software fixtures."""
from copy import deepcopy
import json

import pytest
from covscreen.execution import JobManager, execute_persisted_job
from covscreen.jobs import sha256_file
from covscreen.mcp.server import dispatch
from covscreen.library_workflow import RESEARCH
from covscreen.workflow_store import load
from test_workflow_md_execution import research, call, approval, register
from test_workflow_md_system_preparation import ready as parameterized, start as start_preparation, prepared


def ready(fixture, monkeypatch):
    parameterized(fixture, monkeypatch)
    run = start_preparation(fixture)
    output = prepared(fixture, run)
    assert call(fixture, "md_system_preparation_status")["md_execution"]["verification"] == "verified"
    return output


def start(fixture):
    call(fixture, "plan_md_short_qc", steps=20, report_interval=10, velocity_seed=7)
    return dispatch(fixture[0], RESEARCH, approval(fixture, "start_md_short_qc"))["md_execution"]["run_id"]


def register_output(fixture, run, folder):
    manager = JobManager(fixture[0])
    result = manager.registry.load_result(run)
    result.state = "COMPLETED"
    result.return_code = 0
    result.artifacts = [manager.registry.register_file(run, path, "other") for path in folder.iterdir()]
    manager.registry.write_result(result)


def completed(fixture, run, *, parent=None):
    manager = JobManager(fixture[0])
    spec = manager.registry.load_spec(run)
    parent = parent or load(fixture[1])["md_execution"]["plan"]["input"]
    preparation = json.loads((manager.registry.job_dir(parent["run_id"])
        / "artifacts/openmm-system-preparation/openmm-preparation.json").read_text())
    folder = manager.registry.job_dir(run) / "artifacts/openmm-short-qc"
    folder.mkdir(parents=True)
    rows = [{"step": step, "time_ps": step / 1000, "potential_energy_kj_mol": -2,
        "kinetic_energy_kj_mol": 1, "temperature_k": 300, "covalent_distance_angstrom": 1.8,
        "max_relative_constraint_error": 0.000001} for step in range(spec.parameters["report_interval"], spec.parameters["steps"] + 1, spec.parameters["report_interval"])]
    (folder / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    for name in ("final-positions-nm.npy", "final-velocities-nm-ps.npy"):
        (folder / name).write_bytes(b"EXPLICIT SOFTWARE STATE FIXTURE; NOT A SCIENTIFIC RESULT")
    value = {"schema": "openmm-short-qc-artifact-v1", "status": "UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING",
        "system_id": parent["candidate_id"], "atoms": preparation["atoms"],
        "parents": {"parameter_audit_sha256": preparation["parents"]["parameter_audit_sha256"], **preparation["stages"]},
        "protocol": {"ensemble": "NVT", "unrestrained": True, "platform": "CPU", "cpu_threads": spec.resource_budget.cpu_count,
            **{key: spec.parameters[key] for key in ("steps", "report_interval", "velocity_seed")},
            "timestep_fs": 1.0, "duration_ps": spec.parameters["steps"] / 1000, "temperature_k": 300.0,
            "friction_per_ps": 1.0, "constraint_tolerance": 1e-6},
        "state_replay": {"energy_difference_kj_mol": 0, "relative_force_rms_difference": 0},
        "frames": len(rows), "first_frame": rows[0], "last_frame": rows[-1],
        "short_qc_completed": True, "integration_stability_passed": True, "equilibration_completed": False,
        "production_started": False, "md_system_ready": False, "campaign_ready": False,
        "artifacts": {name: sha256_file(folder / name) for name in ("frames.jsonl", "final-positions-nm.npy", "final-velocities-nm-ps.npy")}}
    (folder / "short-qc.json").write_text(json.dumps(value))
    register_output(fixture, run, folder)
    return folder


def test_short_qc_uses_existing_engine_and_bound_protocol_with_separate_approval(research, monkeypatch):
    ready(research, monkeypatch)
    parent = deepcopy(load(research[1])["md_execution"])
    plan = call(research, "plan_md_short_qc", steps=20, report_interval=10, velocity_seed=7)
    assert plan["workflow"]["stage"] == "MD_SHORT_QC_PLAN_READY"
    fields = plan["workflow"]["tools"][RESEARCH]["fields"]
    assert all(name in fields for name in ("steps", "report_interval", "velocity_seed"))
    assert all(name not in fields for name in ("native_timeout_seconds", "max_minimization_iterations", "system_preparation_run_id"))
    spec = load(research[1])["md_execution"]["plan"]["spec"]
    assert spec["engine"] == "library-first-openmm-short-qc"
    assert spec["parent_run_id"] == parent["run_id"]
    with pytest.raises(ValueError, match="receipt required"):
        call(research, "start_md_short_qc", human_approved=True)
    run = dispatch(research[0], RESEARCH, approval(research, "start_md_short_qc"))["md_execution"]["run_id"]
    completed(research, run)
    result = call(research, "md_short_qc_status")
    assert result["md_execution"]["verification"] == "verified"
    assert result["md_execution"]["short_qc_completed"] is True
    assert result["md_execution"]["short_qc"]["equilibration_completed"] is False
    assert result["md_execution"]["md_system_ready"] is False
    assert result["assessment"]["state"] == "not_evaluated"
    assert load(research[1])["md_execution"]["parent"] == parent


@pytest.mark.parametrize("changed", ["short-qc.json", "frames.jsonl", "final-positions-nm.npy", "final-velocities-nm-ps.npy", "parent"])
def test_changed_qc_or_preparation_bytes_revoke_current_receipt(research, monkeypatch, changed):
    preparation = ready(research, monkeypatch)
    run = start(research)
    output = completed(research, run)
    assert call(research, "md_short_qc_status")["md_execution"]["verification"] == "verified"
    witness = deepcopy(load(research[1])["md_execution"]["short_qc"])
    path = preparation / "minimization/system.xml" if changed == "parent" else output / changed
    path.write_text("changed evidence")
    if changed == "parent":
        with pytest.raises(ValueError):
            call(research, "md_short_qc_status")
    else:
        assert call(research, "md_short_qc_status")["md_execution"]["short_qc"] is None
    assert load(research[1])["md_execution"]["verification"] == "evidence_rejected"
    assert load(research[1])["md_execution"]["short_qc"] == witness


@pytest.mark.parametrize("changed", ["protocol", "parent", "readiness", "replay", "frame_count", "frame_schedule", "frame_metric", "frame_shape", "manifest_shape"])
def test_registered_inconsistent_qc_evidence_cannot_pass(research, monkeypatch, changed):
    ready(research, monkeypatch)
    run = start(research)
    folder = completed(research, run)
    manifest = folder / "short-qc.json"
    value = json.loads(manifest.read_text())
    if changed == "protocol":
        value["protocol"]["velocity_seed"] = 9
    elif changed == "parent":
        value["parents"]["parameter_audit_sha256"] = "0" * 64
    elif changed == "readiness":
        value["equilibration_completed"] = True
    elif changed == "replay":
        value["state_replay"]["relative_force_rms_difference"] = 0.5
    elif changed == "frame_count":
        value["frames"] += 1
    elif changed == "manifest_shape":
        value = []
    else:
        rows = [json.loads(line) for line in (folder / "frames.jsonl").read_text().splitlines()]
        if changed == "frame_schedule":
            rows[0]["step"] = 1
        elif changed == "frame_metric":
            rows[0]["temperature_k"] = float("nan")
        else:
            rows[0] = []
        (folder / "frames.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        value["artifacts"]["frames.jsonl"] = sha256_file(folder / "frames.jsonl")
        value["first_frame"] = rows[0]
    manifest.write_text(json.dumps(value))
    register_output(research, run, folder)
    result = call(research, "md_short_qc_status")["md_execution"]
    assert result["verification"] == "evidence_rejected"
    assert result["short_qc"] is None


def test_qc_plan_release_restores_preparation_and_bad_schedule_is_rejected(research, monkeypatch):
    ready(research, monkeypatch)
    parent = deepcopy(load(research[1])["md_execution"])
    with pytest.raises(ValueError):
        call(research, "plan_md_short_qc", steps=20, report_interval=7)
    assert load(research[1])["md_execution"] == parent
    call(research, "plan_md_short_qc")
    result = call(research, "release_md_stage", reason="Review the integration sampling schedule before requesting execution.")
    assert result["workflow"]["stage"] == "MD_SYSTEM_PREPARATION_REVIEW"
    assert load(research[1])["md_execution"] == parent


def test_qc_lost_start_reply_recovers_and_cancels_original_run(research, monkeypatch):
    ready(research, monkeypatch)
    call(research, "plan_md_short_qc")
    def interrupted(manager, request):
        register(manager, request)
        raise ConnectionError("Lost short-QC reply")
    monkeypatch.setattr(JobManager, "submit", interrupted)
    with pytest.raises(ConnectionError):
        dispatch(research[0], RESEARCH, approval(research, "start_md_short_qc"))
    restored = call(research, "reconcile")
    assert restored["pending"] is None
    run = restored["md_execution"]["run_id"]
    result = dispatch(research[0], RESEARCH, approval(research, "cancel_md_stage"))
    assert result["md_execution"]["cancellation_requested"] is True
    assert result["md_execution"]["cancellation_confirmed"] is False
    execute_persisted_job(research[0], run)
    result = call(research, "md_short_qc_status")
    assert result["md_execution"]["run_id"] == run
    assert result["md_execution"]["cancellation_confirmed"] is True
