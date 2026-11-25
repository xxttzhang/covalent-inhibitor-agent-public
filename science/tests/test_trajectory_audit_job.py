import hashlib
import json
from pathlib import Path

import pytest

from covscreen.jobs import JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.native_approval import needs_approval
from covscreen.tool_api import library_first_audit_openmm_trajectory_tool
from test_openmm_production_job import project as production_parents


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def project(tmp_path):
    equilibrium_run, _ = production_parents(tmp_path)
    production_run = "openmm-production-parent-000001"
    job = tmp_path / "state/jobs" / production_run
    folder = job / "artifacts/openmm-production"
    folder.mkdir(parents=True)
    trajectory = folder / "trajectory.dcd"
    frames = folder / "frames.jsonl"
    trajectory.write_bytes(b"immutable-dcd\n")
    frames.write_text('{"frame":1,"time_ps":10.0}\n')
    parameter = (tmp_path / "state/jobs/parameterization-parent-000001"
        / "artifacts/adduct-parameterization/parameterization-audit.json")
    parameter_data = json.loads(parameter.read_text())
    manifest = folder / "production.json"
    manifest.write_text(json.dumps({
        "schema": "openmm-production-replica-artifact-v1",
        "status": "PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING",
        "system_id": parameter_data["system_id"],
        "replica": 1,
        "production_completed": True,
        "trajectory_audited": False,
        "parents": {"parameter_audit_sha256": sha(parameter)},
        "artifacts": {"trajectory.dcd": sha(trajectory), "frames.jsonl": sha(frames)},
    }) + "\n")
    spec = JobSpec(run_id=production_run, request_id="production-request-000001",
        project_id=parameter_data["system_id"], route="library-first",
        engine="library-first-openmm-production", command=["production"],
        working_directory=str(job), output_directory=str(folder),
        parent_run_id=equilibrium_run,
        resource_budget=ResourceBudget(max_runtime_minutes=1440, cpu_count=4,
            memory_gb=32, max_candidates=1))
    result = RunResult(run_id=production_run, state="COMPLETED",
        engine="library-first-openmm-production", route="library-first",
        project_id=parameter_data["system_id"], submitted_at=utc_now(),
        started_at=utc_now(), finished_at=utc_now(), return_code=0,
        runtime_seconds=1, message="completed", stdout_path=str(job / "stdout.log"),
        stderr_path=str(job / "stderr.log"))
    (job / "spec.json").write_text(spec.model_dump_json())
    (job / "result.json").write_text(result.model_dump_json())
    return production_run, manifest


def args(run, manifest):
    return {"action": "plan", "production_run_id": run,
        "production_manifest_sha256": sha(manifest),
        "resource_budget": {"max_runtime_minutes": 120, "gpu_count": 0,
            "cpu_count": 4, "memory_gb": 16, "max_candidates": 1}}


def test_plan_freezes_completed_production_and_audit_scope(tmp_path):
    run, manifest = project(tmp_path)
    response = library_first_audit_openmm_trajectory_tool(tmp_path, args(run, manifest))
    spec = response["spec"]
    assert response["status"] == "PLANNED"
    assert spec["engine"] == "library-first-trajectory-audit"
    assert spec["parent_run_id"] == run
    assert spec["parameters"]["replica"] == 1
    assert spec["provenance"]["structural_analysis_completed"] is False
    assert len(spec["input_sha256"]) == 6
    assert not Path(spec["expected_output_paths"][0]).exists()


def test_plan_rejects_tampered_production_trajectory(tmp_path):
    run, manifest = project(tmp_path)
    (manifest.parent / "trajectory.dcd").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="trajectory-audit admission"):
        library_first_audit_openmm_trajectory_tool(tmp_path, args(run, manifest))


def test_only_start_requires_approval():
    name = "covalent_library_first_audit_openmm_trajectory"
    assert needs_approval(name, {"action": "start"}) is True
    assert needs_approval(name, {"action": "plan"}) is False
