import hashlib
import json
from pathlib import Path

from covscreen.tool_api import library_first_select_md_candidate_tool
from covscreen_library_first.md_candidate_delivery import freeze_md_candidate_delivery
from test_md_candidate_delivery_job import completed_job, project


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def configured(tmp_path):
    pareto_run,pareto_manifest,table,_=project(tmp_path)
    covdock=tmp_path/"state/jobs/covdock-parent-000001/artifacts/covdock_lo_registered_manifest.json"
    delivery_run="delivery-parent-000001"
    folder=tmp_path/"state/jobs"/delivery_run/"artifacts/md-candidate-delivery"
    completed_job(tmp_path,delivery_run,"library-first-md-candidate-delivery",folder,
        parent=pareto_run)
    folder.rmdir()
    freeze_md_candidate_delivery(table,sha(table),covdock,sha(covdock),delivery_run,folder)
    delivery=folder/"candidate-results.json"

    preparation_run="preparation-parent-000001"
    prep_root=tmp_path/"state/jobs"/preparation_run/"artifacts/receptor-preparation"
    completed_job(tmp_path,preparation_run,"library-first-receptor-preparation",prep_root)
    prepared_dir=prep_root/"prepared"; prepared_dir.mkdir()
    prepared=prepared_dir/"prepared-receptor.json"
    prepared.write_text(json.dumps({"schema":"prepared-receptor-artifact-v1",
        "status":"prepared_isolated_protein_scope","entry_id":"4Z3V","uniprot":"Q06187",
        "canonical_cys":481,"author_chain":"A","author_residue":"481",
        "receptor_prepared":True,"campaign_ready":False})+"\n")
    context_id="c"*32; context_dir=tmp_path/"state/receptor_site_context"/context_id
    context_dir.mkdir(parents=True); context=context_dir/"site-context.json"
    context.write_text(json.dumps({"schema":"receptor-site-context-artifact-v1",
        "status":"descriptive_site_context_completed_review_pending",
        "identity":{"entry_id":"4Z3V","uniprot":"Q06187","canonical_cys":481,
            "author_chain":"A","author_residue":"481"},
        "parents":{"prepared_receptor_sha256":sha(prepared)},"campaign_ready":False})+"\n")
    return {"action":"plan","delivery_run_id":delivery_run,
        "delivery_manifest_sha256":sha(delivery),"compound_id":"CMP-1","input_sdf_index":7,
        "preparation_run_id":preparation_run,
        "prepared_receptor_manifest_sha256":sha(prepared),
        "receptor_context_artifact_id":"receptor-context-"+context_id,
        "receptor_context_manifest_sha256":sha(context)}


def test_plan_validates_selection_without_registering_artifact(tmp_path):
    arguments=configured(tmp_path)
    result=library_first_select_md_candidate_tool(tmp_path,arguments)
    assert result["status"]=="PLANNED" and result["native_graph_qc_completed"] is False
    assert not (tmp_path/"state/candidate_pose_input").exists()


def test_select_registers_idempotent_hash_bound_candidate_pose(tmp_path):
    arguments=configured(tmp_path); arguments["action"]="select"
    first=library_first_select_md_candidate_tool(tmp_path,arguments)
    second=library_first_select_md_candidate_tool(tmp_path,arguments)
    assert first==second
    assert first["artifact_id"].startswith("candidate-pose-input-")
    assert len(first["manifest_sha256"])==64
