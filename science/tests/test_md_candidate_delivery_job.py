import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from covscreen.jobs import JobSpec, ResourceBudget, RunResult, utc_now
from covscreen.native_approval import needs_approval
from covscreen.tool_api import library_first_deliver_md_candidates_tool


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def completed_job(root, run, engine, output, *, parent=None):
    job=root/"state/jobs"/run; output.mkdir(parents=True,exist_ok=True)
    spec=JobSpec(run_id=run,request_id=run+"-request",project_id="btk-c481",
        route="library-first",engine=engine,command=[engine],working_directory=str(job),
        output_directory=str(output),parent_run_id=parent,
        resource_budget=ResourceBudget(max_runtime_minutes=30,cpu_count=2,
            memory_gb=8,max_candidates=100))
    result=RunResult(run_id=run,state="COMPLETED",engine=engine,route="library-first",
        project_id=spec.project_id,submitted_at=utc_now(),started_at=utc_now(),
        finished_at=utc_now(),return_code=0,runtime_seconds=1,message="completed",
        stdout_path=str(job/"stdout.log"),stderr_path=str(job/"stderr.log"))
    (job/"spec.json").write_text(spec.model_dump_json())
    (job/"result.json").write_text(result.model_dump_json())
    return job


def project(tmp_path):
    covdock_run="covdock-parent-000001"; covdock_job=tmp_path/"state/jobs"/covdock_run
    covdock_artifacts=covdock_job/"artifacts"
    completed_job(tmp_path,covdock_run,"schrodinger-covdock-lo",covdock_artifacts)
    pose=covdock_artifacts/"poses.maegz"; pose.write_bytes(b"poses\n")
    af3=covdock_artifacts/"model.cif"; af3.write_text("model\n")
    covdock_manifest=covdock_artifacts/"covdock_lo_registered_manifest.json"
    covdock_manifest.write_text(json.dumps({"status":"completed",
        "gate_quality_gates":{"identity":True},"pose_files":[str(pose)],
        "attachment_residue":"A:481","nucleophilic_atom":"SG",
        "reaction_type":"Michael Addition","input_receptor_sha256":"a"*64})+"\n")

    pareto_run="pareto-parent-000001"; pareto_job=tmp_path/"state/jobs"/pareto_run
    pareto_artifacts=pareto_job/"artifacts"
    completed_job(tmp_path,pareto_run,"library-first-pareto",pareto_artifacts,
        parent=covdock_run)
    table=pareto_artifacts/"stage6_final_candidates.parquet"
    pd.DataFrame([{"compound_id":"CMP-1","input_sdf_index":7,
        "standardized_smiles":"C=CC(=O)NC","target_id":"BTK","site_id":"A:CYS:481",
        "parent_id":"library","warhead_class":"acrylamide","electrophilic_atom":"C1",
        "nucleophilic_atom":"A:CYS:481:SG","af3_success":True,"af3_mpae":1.2,
        "model_cif":str(af3),"covdock_success":True,"covdock_score":-7.0,
        "covdock_pose_file":str(pose),"covdock_pose_index":3,"physical_validity":"PASS",
        "covdock_gate_detail":"A","cross_method_consensus":"HIGH"}]).to_parquet(table,index=False)
    pareto_manifest=pareto_artifacts/"stage6_pareto_manifest.json"
    pareto_manifest.write_text(json.dumps({"status":"completed",
        "quality_gates":{"baseline_objectives_available":True},
        "outputs":{table.name:{"path":str(table),"sha256":sha(table)}}})+"\n")
    return pareto_run,pareto_manifest,table,pose


def args(run,manifest): return {"action":"plan","pareto_run_id":run,
    "pareto_manifest_sha256":sha(manifest),"resource_budget":{
        "max_runtime_minutes":30,"gpu_count":0,"cpu_count":2,"memory_gb":8,
        "max_candidates":10}}


def test_plan_freezes_pareto_covdock_and_all_structure_assets(tmp_path):
    run,manifest,table,pose=project(tmp_path)
    result=library_first_deliver_md_candidates_tool(tmp_path,args(run,manifest))
    spec=result["spec"]
    assert result["status"]=="PLANNED" and result["candidate_selected_for_md"] is False
    assert spec["engine"]=="library-first-md-candidate-delivery"
    assert spec["parameters"]["candidate_count"]==1
    assert spec["parameters"]["raw_paths_exposed"] is False
    assert spec["provenance"]["selection_not_made"] is True
    frozen=set(spec["input_sha256"])
    assert str(table.relative_to(tmp_path)) in frozen
    assert str(pose.relative_to(tmp_path)) in frozen


def test_plan_rejects_tampered_pareto_table(tmp_path):
    run,manifest,table,_=project(tmp_path)
    frame=pd.read_parquet(table); frame.loc[0,"compound_id"]="TAMPERED"
    frame.to_parquet(table,index=False)
    with pytest.raises(ValueError,match="Pareto result has not passed"):
        library_first_deliver_md_candidates_tool(tmp_path,args(run,manifest))


def test_plan_rejects_unregistered_covdock_pose(tmp_path):
    run,manifest,_,_=project(tmp_path)
    covdock=tmp_path/"state/jobs/covdock-parent-000001/artifacts/covdock_lo_registered_manifest.json"
    value=json.loads(covdock.read_text()); value["pose_files"]=[]
    covdock.write_text(json.dumps(value))
    with pytest.raises(ValueError,match="absent"):
        library_first_deliver_md_candidates_tool(tmp_path,args(run,manifest))


def test_start_requires_native_approval():
    name="covalent_library_first_deliver_md_candidates"
    assert needs_approval(name,{"action":"start"}) is True
    assert needs_approval(name,{"action":"plan"}) is False
