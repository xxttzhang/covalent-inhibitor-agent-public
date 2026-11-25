import hashlib
import json
from pathlib import Path

import pytest

from covscreen.jobs import JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.native_approval import needs_approval
from covscreen.tool_api import library_first_build_md_report_tool
from test_structural_analysis_job import project as analysis_parents


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def project(tmp_path):
    analysis_parents(tmp_path)
    run="structural-analysis-parent-000001"; job=tmp_path/"state/jobs"/run
    folder=job/"artifacts/structural-analysis"; folder.mkdir(parents=True)
    frames=folder/"frame-metrics.csv"; frames.write_text("replica,frame\n1,1\n")
    manifest=folder/"structural-analysis.json"
    manifest.write_text(json.dumps({"schema":"openmm-three-replica-structural-analysis-v1",
        "status":"THREE_REPLICA_STRUCTURAL_ANALYSIS_COMPLETE_REPORT_PENDING",
        "system_id":"candidate_001","replicas":[1,2,3],"screening_decision_made":False,
        "artifacts":{"frame-metrics.csv":sha(frames)}})+"\n")
    spec=JobSpec(run_id=run,request_id="analysis-request-000001",project_id="candidate_001",
        route="library-first",engine="library-first-structural-analysis",command=["analysis"],
        working_directory=str(job),output_directory=str(folder),
        resource_budget=ResourceBudget(max_runtime_minutes=240,cpu_count=8,memory_gb=32,max_candidates=1))
    result=RunResult(run_id=run,state="COMPLETED",engine=spec.engine,route=spec.route,
        project_id=spec.project_id,submitted_at=utc_now(),started_at=utc_now(),finished_at=utc_now(),
        return_code=0,runtime_seconds=1,message="completed",stdout_path=str(job/"stdout.log"),stderr_path=str(job/"stderr.log"))
    (job/"spec.json").write_text(spec.model_dump_json()); (job/"result.json").write_text(result.model_dump_json())
    return run,manifest


def args(run,manifest): return {"action":"plan","structural_analysis_run_id":run,
    "structural_analysis_manifest_sha256":sha(manifest),"resource_budget":{
        "max_runtime_minutes":30,"gpu_count":0,"cpu_count":2,"memory_gb":4,"max_candidates":1}}


def test_plan_freezes_analysis_and_policy(tmp_path):
    run,manifest=project(tmp_path); result=library_first_build_md_report_tool(tmp_path,args(run,manifest))
    assert result["status"]=="PLANNED" and result["expert_review_required"] is True
    assert result["spec"]["engine"]=="library-first-md-report"
    assert result["spec"]["parameters"]["policy_id"]=="library-first-covalent-md-structural-triage-v1"
    assert result["spec"]["provenance"]["affinity_claimed"] is False


def test_plan_rejects_tampered_frame_metrics(tmp_path):
    run,manifest=project(tmp_path); (manifest.parent/"frame-metrics.csv").write_text("tampered\n")
    with pytest.raises(ValueError,match="MD-report admission"):
        library_first_build_md_report_tool(tmp_path,args(run,manifest))


def test_start_requires_native_approval():
    name="covalent_library_first_build_md_report"
    assert needs_approval(name,{"action":"start"}) is True
    assert needs_approval(name,{"action":"plan"}) is False
