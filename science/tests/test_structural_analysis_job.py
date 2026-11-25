import hashlib
import json
from pathlib import Path

from covscreen.jobs import JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.native_approval import needs_approval
from covscreen.tool_api import library_first_analyze_openmm_replicas_tool
from test_openmm_production_job import project as production_parents


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def completed(job, run_id, engine, parent, project_id):
    spec = JobSpec(run_id=run_id, request_id=run_id + "-request", project_id=project_id,
        route="library-first", engine=engine, command=[engine], working_directory=str(job),
        output_directory=str(job / "artifacts"), parent_run_id=parent,
        resource_budget=ResourceBudget(max_runtime_minutes=120, cpu_count=4,
            memory_gb=16, max_candidates=1))
    result = RunResult(run_id=run_id, state="COMPLETED", engine=engine,
        route="library-first", project_id=project_id, submitted_at=utc_now(),
        started_at=utc_now(), finished_at=utc_now(), return_code=0,
        runtime_seconds=1, message="completed", stdout_path=str(job / "stdout.log"),
        stderr_path=str(job / "stderr.log"))
    (job / "spec.json").write_text(spec.model_dump_json())
    (job / "result.json").write_text(result.model_dump_json())


def project(tmp_path):
    equilibrium_run, _ = production_parents(tmp_path)
    parameter_folder = (tmp_path / "state/jobs/parameterization-parent-000001"
        / "artifacts/adduct-parameterization")
    parameter = json.loads((parameter_folder / "parameterization-audit.json").read_text())
    mapping = parameter_folder / "atom-mapping.json"; mapping.write_text("[]\n")
    parameter_path = parameter_folder / "parameterization-audit.json"
    parameter["atom_mapping_sha256"] = sha(mapping); parameter_path.write_text(json.dumps(parameter)+"\n")
    prep = tmp_path / "state/jobs/openmm-preparation-parent-000001/artifacts/openmm-system-preparation/minimization"
    reference = prep / "minimized-positions-nm.npy"; reference.write_bytes(b"reference\n")
    minimization = prep / "minimization.json"; value=json.loads(minimization.read_text())
    value.setdefault("artifacts", {})["minimized-positions-nm.npy"] = sha(reference)
    minimization.write_text(json.dumps(value)+"\n")
    audits = []
    for replica in (1, 2, 3):
        production_run = f"openmm-production-parent-00000{replica}"
        production_job = tmp_path / "state/jobs" / production_run
        folder = production_job / "artifacts/openmm-production"; folder.mkdir(parents=True)
        trajectory = folder / "trajectory.dcd"; trajectory.write_bytes(f"dcd-{replica}\n".encode())
        manifest = folder / "production.json"
        manifest.write_text(json.dumps({"schema":"openmm-production-replica-artifact-v1",
            "status":"PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING",
            "system_id":parameter["system_id"],"replica":replica,
            "parents":{"equilibration_manifest_sha256":"a"*64,
                "parameter_audit_sha256":sha(parameter_path)},
            "artifacts":{"trajectory.dcd":sha(trajectory)}})+"\n")
        completed(production_job, production_run, "library-first-openmm-production",
            equilibrium_run, parameter["system_id"])
        audit_run = f"trajectory-audit-parent-00000{replica}"
        audit_job = tmp_path / "state/jobs" / audit_run
        audit_folder = audit_job / "artifacts/trajectory-audit"; audit_folder.mkdir(parents=True)
        audit = audit_folder / "trajectory-audit.json"
        audit.write_text(json.dumps({"status":"TRAJECTORY_INTEGRITY_VERIFIED_ANALYSIS_PENDING",
            "parents":{"production_manifest_sha256":sha(manifest)}})+"\n")
        completed(audit_job, audit_run, "library-first-trajectory-audit",
            production_run, parameter["system_id"])
        audits.append({"run_id":audit_run,"manifest_sha256":sha(audit)})
    return audits


def test_plan_requires_all_three_formal_replicas(tmp_path):
    audits = project(tmp_path)
    result = library_first_analyze_openmm_replicas_tool(tmp_path, {"action":"plan",
        "trajectory_audits":audits,"resource_budget":{"max_runtime_minutes":240,
        "gpu_count":0,"cpu_count":8,"memory_gb":32,"max_candidates":1}})
    assert result["status"] == "PLANNED"
    assert result["spec"]["engine"] == "library-first-structural-analysis"
    assert result["spec"]["parameters"]["replicas"] == [1,2,3]
    assert result["spec"]["provenance"]["screening_decision_made"] is False


def test_start_requires_native_approval():
    name="covalent_library_first_analyze_openmm_replicas"
    assert needs_approval(name,{"action":"start"}) is True
    assert needs_approval(name,{"action":"plan"}) is False
