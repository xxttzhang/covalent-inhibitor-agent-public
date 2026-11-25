import json

import pytest

import covscreen.tool_api as api
from covscreen.jobs import sha256_file
from covscreen.tool_api import library_first_next_md_action_tool
from test_md_adduct_admission_job import setup as candidate_pose_setup
from test_md_candidate_delivery_job import completed_job
from test_md_candidate_selection_tool import configured as selection_setup


def test_delivery_next_action_validates_and_returns_selection_call(tmp_path):
    values=selection_setup(tmp_path)
    result=library_first_next_md_action_tool(tmp_path,{"stage":"candidate_delivery",
        **{key:value for key,value in values.items() if key!="action"}})
    assert result["next_tool"]=="covalent_library_first_select_md_candidate"
    assert result["next_arguments"]["action"]=="select"
    assert result["validated_plan"]["status"]=="PLANNED"
    assert result["calculation_submitted"] is False


def test_candidate_pose_next_action_compiles_native_admission_only(tmp_path):
    values=candidate_pose_setup(tmp_path)
    result=library_first_next_md_action_tool(tmp_path,{"stage":"candidate_pose",
        "candidate_pose_artifact_id":values["candidate_pose_artifact_id"],
        "manifest_sha256":values["manifest_sha256"]})
    assert result["next_tool"]=="covalent_library_first_admit_md_candidate"
    assert result["next_arguments"]["action"]=="plan"
    assert result["execution_action"]=="start_requires_native_approval"
    assert result["validated_plan"]["spec"]["engine"]=="library-first-md-adduct-admission"


def test_production_next_action_requires_all_three_replicas(tmp_path):
    with pytest.raises(ValueError,match="exactly three"):
        library_first_next_md_action_tool(tmp_path,{"stage":"production_replicas",
            "production_replicas":[{"run_id":"one","manifest_sha256":"a"*64}]})


def test_report_terminal_state_requires_exact_completed_report(tmp_path):
    run="md-report-parent-000001"; folder=tmp_path/"state/jobs"/run/"artifacts/md-report"
    completed_job(tmp_path,run,"library-first-md-report",folder)
    report=folder/"candidate-md-assessment.json"
    report.write_text(json.dumps({"classification":"STRUCTURALLY_SUPPORTIVE_FOR_EXPERT_REVIEW",
        "expert_review_required":True})+"\n")
    result=library_first_next_md_action_tool(tmp_path,{"stage":"md_report",
        "md_report_run_id":run,"md_report_manifest_sha256":sha256_file(report)})
    assert result["status"]=="MD_WORKFLOW_COMPLETE_EXPERT_REVIEW_REQUIRED"
    assert result["expert_review_required"] is True


@pytest.mark.parametrize(("stage","evidence","function_name","tool"),[
    ("parameterization",{"parameterization_run_id":"parameter-run",
        "parameterization_audit_sha256":"a"*64},"library_first_prepare_openmm_system_tool",
        "covalent_library_first_prepare_openmm_system"),
    ("system_preparation",{"system_preparation_run_id":"system-run",
        "preparation_manifest_sha256":"b"*64},"library_first_run_openmm_short_qc_tool",
        "covalent_library_first_run_openmm_short_qc"),
    ("short_qc",{"short_qc_run_id":"short-run","short_qc_manifest_sha256":"c"*64},
        "library_first_equilibrate_openmm_system_tool",
        "covalent_library_first_equilibrate_openmm_system"),
    ("structural_analysis",{"structural_analysis_run_id":"analysis-run",
        "structural_analysis_manifest_sha256":"d"*64},"library_first_build_md_report_tool",
        "covalent_library_first_build_md_report")])
def test_linear_stages_delegate_to_existing_plan_tools(tmp_path,monkeypatch,stage,evidence,
                                                       function_name,tool):
    monkeypatch.setattr(api,function_name,lambda root,args:{"status":"PLANNED","seen":args})
    result=library_first_next_md_action_tool(tmp_path,{"stage":stage,**evidence})
    assert result["next_tool"]==tool and result["next_arguments"]["action"]=="plan"
    assert result["execution_action"]=="start_requires_native_approval"


def test_equilibration_compiles_all_three_production_replicas(tmp_path,monkeypatch):
    monkeypatch.setattr(api,"library_first_run_openmm_production_tool",
        lambda root,args:{"status":"PLANNED","replica":args["replica"]})
    result=library_first_next_md_action_tool(tmp_path,{"stage":"equilibration",
        "equilibration_run_id":"equilibrium-run","equilibration_manifest_sha256":"e"*64})
    assert [call["validated_plan"]["replica"] for call in result["next_calls"]]==[1,2,3]
    assert all(call["arguments"]["action"]=="plan" for call in result["next_calls"])


def test_three_audits_delegate_to_one_structural_analysis_plan(tmp_path,monkeypatch):
    audits=[{"run_id":f"audit-{i}","manifest_sha256":str(i)*64} for i in (1,2,3)]
    monkeypatch.setattr(api,"library_first_analyze_openmm_replicas_tool",
        lambda root,args:{"status":"PLANNED","count":len(args["trajectory_audits"])})
    result=library_first_next_md_action_tool(tmp_path,{"stage":"trajectory_audits",
        "trajectory_audits":audits})
    assert result["next_tool"]=="covalent_library_first_analyze_openmm_replicas"
    assert result["validated_plan"]["count"]==3
