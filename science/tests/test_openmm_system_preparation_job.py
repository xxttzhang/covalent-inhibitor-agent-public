import hashlib
import json
from pathlib import Path
import os
import sys

import pytest

from covscreen.jobs import JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.native_approval import needs_approval
from covscreen.tool_api import library_first_prepare_openmm_system_tool


WORKSPACE = Path(__file__).resolve().parents[2]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project(tmp_path: Path) -> tuple[str, Path]:
    config = tmp_path / "configs/local.inspire.yaml"
    config.parent.mkdir()
    config.write_text(
        "openmm_system_preparation:\n"
        "  enabled: true\n"
        f"  python: {sys.executable}\n"
        f"  pythonpath: {os.environ.get('COVSCREEN_OPENMM_TEST_DEPS', sys.prefix)}\n"
        "  intermol_source: bundled\n"
        "runtime:\n  worker_backend: local\n"
    )
    run_id = "parameterization-parent-000001"
    job = tmp_path / "state/jobs" / run_id
    artifact = job / "artifacts/adduct-parameterization"
    artifact.mkdir(parents=True)
    source = artifact / "parameterized.cms"
    source.write_bytes(b"immutable-cms-fixture\n")
    mapping = artifact / "atom-mapping.json"
    mapping.write_text(json.dumps({"system_id": "candidate_001", "atoms": []}) + "\n")
    audit = artifact / "parameterization-audit.json"
    audit_record = {
        "schema": "covalent-parameterization-audit-v1",
        "system_id": "candidate_001",
        "status": "COVALENT_PARAMETERIZATION_AUDITED",
        "parameters_audited": True,
        "simulation_started": False,
        "parameterized_cms_sha256": sha(source),
        "atom_mapping_sha256": sha(mapping),
    }
    audit.write_text(json.dumps(audit_record) + "\n")
    (artifact / "finished.json").write_text(json.dumps(audit_record) + "\n")
    spec = JobSpec(
        run_id=run_id,
        request_id="parameterization-request-000001",
        project_id="candidate_001",
        route="library-first",
        engine="library-first-adduct-parameterization",
        command=["registered-parameterizer"],
        working_directory=str(job),
        output_directory=str(artifact),
        expected_output_paths=[str(artifact)],
        resource_budget=ResourceBudget(
            max_runtime_minutes=30, cpu_count=2, memory_gb=8, max_candidates=1
        ),
    )
    result = RunResult(
        run_id=run_id,
        state="COMPLETED",
        engine="library-first-adduct-parameterization",
        route="library-first",
        project_id="candidate_001",
        submitted_at=utc_now(),
        started_at=utc_now(),
        finished_at=utc_now(),
        return_code=0,
        runtime_seconds=1,
        message="completed",
        stdout_path=str(job / "stdout.log"),
        stderr_path=str(job / "stderr.log"),
    )
    (job / "spec.json").write_text(spec.model_dump_json(indent=2) + "\n")
    (job / "result.json").write_text(result.model_dump_json(indent=2) + "\n")
    return run_id, audit


def arguments(run_id: str, audit: Path) -> dict:
    return {
        "action": "plan",
        "parameterization_run_id": run_id,
        "parameterization_audit_sha256": sha(audit),
        "max_minimization_iterations": 25,
    }


def test_plan_replays_parameterization_and_freezes_registered_runtime(tmp_path):
    run_id, audit = project(tmp_path)
    result = library_first_prepare_openmm_system_tool(tmp_path, arguments(run_id, audit))
    spec = result["spec"]
    assert result["status"] == "PLANNED"
    assert result["calculation_submitted"] is False
    assert result["short_qc_completed"] is False
    assert result["md_system_ready"] is False
    assert spec["engine"] == "library-first-openmm-system-preparation"
    assert spec["parent_run_id"] == run_id
    assert spec["parameters"]["system_id"] == "candidate_001"
    assert spec["parameters"]["max_minimization_iterations"] == 25
    assert not (tmp_path / "vendor").exists()
    vendor = Path(spec["command"][spec["command"].index("--intermol-source") + 1])
    assert vendor == WORKSPACE / "science/vendor/InterMol"
    assert set(spec["parameters"]["runtime_versions"]) == {"openmm", "numpy", "parmed"}
    assert len(spec["input_sha256"]) == 6
    assert spec["provenance"]["algorithm_version"] == "library-first-openmm-system-preparation-v1"
    assert spec["provenance"]["short_qc_completed"] is False
    assert not Path(spec["expected_output_paths"][0]).exists()


def test_plan_rejects_tampered_parameterization_parent(tmp_path):
    run_id, audit = project(tmp_path)
    (audit.parent / "parameterized.cms").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="required state"):
        library_first_prepare_openmm_system_tool(tmp_path, arguments(run_id, audit))


def test_plan_rejects_inconsistent_completion_receipt(tmp_path):
    run_id, audit = project(tmp_path)
    (audit.parent / "finished.json").write_text(json.dumps({"status": "PASS"}) + "\n")
    with pytest.raises(ValueError, match="required state"):
        library_first_prepare_openmm_system_tool(tmp_path, arguments(run_id, audit))


def test_plan_rejects_gpu_or_multiple_candidates(tmp_path):
    run_id, audit = project(tmp_path)
    values = arguments(run_id, audit)
    values["resource_budget"] = {
        "max_runtime_minutes": 30,
        "gpu_count": 1,
        "cpu_count": 2,
        "memory_gb": 16,
        "max_candidates": 2,
    }
    with pytest.raises(ValueError, match="CPU-only"):
        library_first_prepare_openmm_system_tool(tmp_path, values)


def test_only_start_requires_native_approval():
    name = "covalent_library_first_prepare_openmm_system"
    assert needs_approval(name, {"action": "start"}) is True
    assert needs_approval(name, {"action": "plan"}) is False
    assert needs_approval(name, {"action": "status"}) is False
