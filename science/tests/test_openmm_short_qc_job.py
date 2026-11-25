import hashlib
import json
from pathlib import Path
import sys

import pytest

from covscreen.jobs import JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.native_approval import needs_approval
from covscreen.tool_api import library_first_run_openmm_short_qc_tool




def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def project(tmp_path):
    config = tmp_path / "configs/local.inspire.yaml"; config.parent.mkdir()
    config.write_text("openmm_system_preparation:\n  enabled: true\n"
        f"  python: {sys.executable}\n"
        f"  pythonpath: {sys.prefix}\n"
        "runtime:\n  worker_backend: local\n")
    parameter_run = "parameterization-parent-000001"
    parameter_folder = tmp_path / "state/jobs" / parameter_run / "artifacts/adduct-parameterization"
    parameter_folder.mkdir(parents=True)
    parameter = parameter_folder / "parameterization-audit.json"
    parameter.write_text(json.dumps({"status": "COVALENT_PARAMETERIZATION_AUDITED",
        "system_id": "candidate_001", "total_atoms": 2}) + "\n")
    run_id = "openmm-preparation-parent-000001"
    job = tmp_path / "state/jobs" / run_id
    folder = job / "artifacts/openmm-system-preparation"
    conversion = folder / "conversion"; conversion.mkdir(parents=True)
    conversion_manifest = conversion / "conversion.json"
    conversion_manifest.write_text(json.dumps({"system_id": "candidate_001"}) + "\n")
    forcefield = folder / "forcefield-audit"; forcefield.mkdir()
    forcefield_manifest = forcefield / "forcefield-audit.json"
    forcefield_manifest.write_text(json.dumps({"system_id": "candidate_001"}) + "\n")
    minimization = folder / "minimization"; minimization.mkdir()
    for name in ("system.xml", "minimized-positions-nm.npy", "cpu-reference-forces.npy"):
        (minimization / name).write_bytes((name + "\n").encode())
    minimization_manifest = minimization / "minimization.json"
    minimization_manifest.write_text(json.dumps({"system_id": "candidate_001"}) + "\n")
    preparation = folder / "openmm-preparation.json"
    preparation.write_text(json.dumps({"status": "OPENMM_SYSTEM_MINIMIZED_SHORT_QC_PENDING",
        "system_id": "candidate_001", "short_qc_completed": False, "simulation_started": False,
        "parents": {"parameter_audit_sha256": sha(parameter)},
        "stages": {"conversion_manifest_sha256": sha(conversion_manifest),
            "forcefield_audit_manifest_sha256": sha(forcefield_manifest),
            "minimization_manifest_sha256": sha(minimization_manifest)}}) + "\n")
    spec = JobSpec(run_id=run_id, request_id="openmm-preparation-request-000001",
        project_id="candidate_001", route="library-first",
        engine="library-first-openmm-system-preparation", command=["registered-preparer"],
        working_directory=str(job), output_directory=str(folder),
        expected_output_paths=[str(folder)], parent_run_id=parameter_run,
        resource_budget=ResourceBudget(max_runtime_minutes=30, cpu_count=2,
            memory_gb=16, max_candidates=1))
    result = RunResult(run_id=run_id, state="COMPLETED",
        engine="library-first-openmm-system-preparation", route="library-first",
        project_id="candidate_001", submitted_at=utc_now(), started_at=utc_now(),
        finished_at=utc_now(), return_code=0, runtime_seconds=1, message="completed",
        stdout_path=str(job / "stdout.log"), stderr_path=str(job / "stderr.log"))
    (job / "spec.json").write_text(spec.model_dump_json(indent=2) + "\n")
    (job / "result.json").write_text(result.model_dump_json(indent=2) + "\n")
    return run_id, preparation


def arguments(run_id, preparation):
    return {"action": "plan", "system_preparation_run_id": run_id,
        "preparation_manifest_sha256": sha(preparation), "steps": 1000,
        "report_interval": 100, "velocity_seed": 9}


def test_plan_freezes_completed_system_preparation(tmp_path):
    run_id, preparation = project(tmp_path)
    result = library_first_run_openmm_short_qc_tool(tmp_path, arguments(run_id, preparation))
    spec = result["spec"]
    assert result["status"] == "PLANNED"
    assert result["equilibration_completed"] is False
    assert result["md_system_ready"] is False
    assert spec["engine"] == "library-first-openmm-short-qc"
    assert spec["parent_run_id"] == run_id
    assert spec["parameters"]["system_id"] == "candidate_001"
    assert spec["parameters"]["runtime_versions"]["openmm"] == "8.6.1"
    assert len(spec["input_sha256"]) == 10
    assert spec["provenance"]["algorithm_version"] == "library-first-openmm-short-qc-v1"
    assert not Path(spec["expected_output_paths"][0]).exists()


def test_plan_rejects_tampered_preparation_parent(tmp_path):
    run_id, preparation = project(tmp_path)
    (preparation.parent / "conversion/conversion.json").write_text("{}\n")
    with pytest.raises(ValueError, match="required state"):
        library_first_run_openmm_short_qc_tool(tmp_path, arguments(run_id, preparation))


def test_plan_rejects_invalid_step_partition(tmp_path):
    run_id, preparation = project(tmp_path)
    values = arguments(run_id, preparation); values["steps"] = 101
    with pytest.raises(ValueError, match="divisible"):
        library_first_run_openmm_short_qc_tool(tmp_path, values)


def test_only_start_requires_native_approval():
    name = "covalent_library_first_run_openmm_short_qc"
    assert needs_approval(name, {"action": "start"}) is True
    assert needs_approval(name, {"action": "plan"}) is False
    assert needs_approval(name, {"action": "status"}) is False
