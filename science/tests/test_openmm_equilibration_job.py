import hashlib
import json
from pathlib import Path
import sys

import pytest

from covscreen.jobs import JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.native_approval import needs_approval
from covscreen.tool_api import library_first_equilibrate_openmm_system_tool




def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_job(job, spec, result):
    job.mkdir(parents=True, exist_ok=True)
    (job/"spec.json").write_text(spec.model_dump_json(indent=2)+"\n")
    (job/"result.json").write_text(result.model_dump_json(indent=2)+"\n")


def completed_result(run_id, engine, job):
    return RunResult(run_id=run_id, state="COMPLETED", engine=engine,
        route="library-first", project_id="candidate_001", submitted_at=utc_now(),
        started_at=utc_now(), finished_at=utc_now(), return_code=0, runtime_seconds=1,
        message="completed", stdout_path=str(job/"stdout.log"), stderr_path=str(job/"stderr.log"))


def project(tmp_path):
    config = tmp_path/"configs/local.inspire.yaml"; config.parent.mkdir()
    config.write_text("openmm_equilibration:\n  enabled: true\n"
        f"  python: {sys.executable}\n"
        f"  pythonpath: {sys.prefix}\n"
        "  platform: CPU\n  platform_properties:\n    Threads: '1'\n"
        "runtime:\n  worker_backend: local\n")
    parameter_run = "parameterization-parent-000001"
    parameter_folder = tmp_path/"state/jobs"/parameter_run/"artifacts/adduct-parameterization"
    parameter_folder.mkdir(parents=True)
    parameter = parameter_folder/"parameterization-audit.json"
    parameter.write_text(json.dumps({"status": "COVALENT_PARAMETERIZATION_AUDITED",
        "system_id": "candidate_001", "total_atoms": 2})+"\n")
    preparation_run = "openmm-preparation-parent-000001"
    preparation_job = tmp_path/"state/jobs"/preparation_run
    minimization = preparation_job/"artifacts/openmm-system-preparation/minimization"
    minimization.mkdir(parents=True)
    system = minimization/"system.xml"; system.write_text("system\n")
    minimization_manifest = minimization/"minimization.json"
    minimization_manifest.write_text(json.dumps({"status":
        "BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING", "system_id": "candidate_001",
        "artifacts": {"system.xml": sha(system)}})+"\n")
    preparation_spec = JobSpec(run_id=preparation_run, request_id="prep-request-000001",
        project_id="candidate_001", route="library-first",
        engine="library-first-openmm-system-preparation", command=["preparer"],
        working_directory=str(preparation_job), output_directory=str(minimization.parent),
        parent_run_id=parameter_run, resource_budget=ResourceBudget(max_runtime_minutes=30,
            cpu_count=2, memory_gb=16, max_candidates=1))
    write_job(preparation_job, preparation_spec,
        completed_result(preparation_run, "library-first-openmm-system-preparation", preparation_job))
    short_run = "openmm-short-qc-parent-000001"
    short_job = tmp_path/"state/jobs"/short_run
    short_folder = short_job/"artifacts/openmm-short-qc"; short_folder.mkdir(parents=True)
    positions = short_folder/"final-positions-nm.npy"; positions.write_bytes(b"positions\n")
    velocities = short_folder/"final-velocities-nm-ps.npy"; velocities.write_bytes(b"velocities\n")
    short_manifest = short_folder/"short-qc.json"
    short_manifest.write_text(json.dumps({"schema": "openmm-short-qc-artifact-v1",
        "status": "UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING",
        "system_id": "candidate_001", "atoms": 2, "short_qc_completed": True,
        "integration_stability_passed": True, "equilibration_completed": False,
        "parents": {"parameter_audit_sha256": sha(parameter),
            "minimization_manifest_sha256": sha(minimization_manifest)},
        "artifacts": {"final-positions-nm.npy": sha(positions),
            "final-velocities-nm-ps.npy": sha(velocities)}})+"\n")
    short_spec = JobSpec(run_id=short_run, request_id="short-request-000001",
        project_id="candidate_001", route="library-first",
        engine="library-first-openmm-short-qc", command=["short-qc"],
        working_directory=str(short_job), output_directory=str(short_folder),
        parent_run_id=preparation_run, resource_budget=ResourceBudget(max_runtime_minutes=30,
            cpu_count=2, memory_gb=16, max_candidates=1))
    write_job(short_job, short_spec,
        completed_result(short_run, "library-first-openmm-short-qc", short_job))
    return short_run, short_manifest


def arguments(run_id, manifest):
    return {"action": "plan", "short_qc_run_id": run_id,
        "short_qc_manifest_sha256": sha(manifest), "integrator_seed": 11,
        "barostat_seed": 12, "resource_budget": {"max_runtime_minutes": 720,
            "gpu_count": 0, "cpu_count": 4, "memory_gb": 32, "max_candidates": 1}}


def test_plan_freezes_short_qc_and_registered_runtime(tmp_path):
    run_id, manifest = project(tmp_path)
    result = library_first_equilibrate_openmm_system_tool(tmp_path, arguments(run_id, manifest))
    spec = result["spec"]
    assert result["status"] == "PLANNED"
    assert result["production_started"] is False
    assert spec["engine"] == "library-first-openmm-equilibration"
    assert spec["parent_run_id"] == run_id
    assert spec["parameters"]["duration_ps"] == 500
    assert spec["parameters"]["platform"] == "CPU"
    assert spec["parameters"]["runtime_versions"]["openmm"] == "8.6.1"
    assert len(spec["input_sha256"]) == 8
    assert spec["provenance"]["equilibrium_convergence_claimed"] is False
    assert not Path(spec["expected_output_paths"][0]).exists()


def test_plan_rejects_tampered_short_qc_parent(tmp_path):
    run_id, manifest = project(tmp_path)
    (manifest.parent/"final-positions-nm.npy").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="required state"):
        library_first_equilibrate_openmm_system_tool(tmp_path, arguments(run_id, manifest))


def test_plan_rejects_gpu_budget_that_differs_from_runtime(tmp_path):
    run_id, manifest = project(tmp_path)
    values = arguments(run_id, manifest); values["resource_budget"]["gpu_count"] = 1
    with pytest.raises(ValueError, match="does not match"):
        library_first_equilibrate_openmm_system_tool(tmp_path, values)


def test_only_start_requires_native_approval():
    name = "covalent_library_first_equilibrate_openmm_system"
    assert needs_approval(name, {"action": "start"}) is True
    assert needs_approval(name, {"action": "plan"}) is False
    assert needs_approval(name, {"action": "status"}) is False


def test_gpu_plan_freezes_full_quota_and_defers_cuda_context(tmp_path):
    import yaml
    from covscreen.execution import JobManager
    from covscreen.tool_api import _openmm_equilibration_request
    run_id, manifest = project(tmp_path)
    config = tmp_path/'configs/local.inspire.yaml'
    value = yaml.safe_load(config.read_text())
    value['openmm_equilibration'].update(platform='CUDA',
        platform_properties={'Precision':'mixed','DeviceIndex':'0','UseCpuPme':'false','DeterministicForces':'true'},
        scheduler_profile={'workspace':'分布式训练空间','project':'test','group':'test-H200',
                           'quota':'1,20,200','image':'test:cuda'})
    config.write_text(yaml.safe_dump(value))
    request = _openmm_equilibration_request(tmp_path, {'short_qc_run_id':run_id,
        'short_qc_manifest_sha256':sha(manifest)}, execution_mode='plan')
    manager = JobManager(tmp_path)
    result = manager.build_spec(request)
    assert manager.worker_backend(request) == 'external-openmm'
    assert (result.resource_budget.cpu_count,result.resource_budget.memory_gb,result.resource_budget.gpu_count)==(20,200,1)
    assert result.resource_budget.max_runtime_minutes==120
    assert result.parameters['cpu_threads']==4
    assert result.parameters['runtime_versions']['platform']=='CPU'
    assert result.parameters['platform']=='CUDA' and result.parameters['platform_validation_required']
    assert result.parameters['scheduler_profile']==value['openmm_equilibration']['scheduler_profile']
    # Generic submission cannot bypass the original Ligand-first study's approval.
    with pytest.raises(ValueError,match='boundary'):
        manager._prepare_submission(request)
