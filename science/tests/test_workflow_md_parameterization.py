"""Workflow and cancellation contracts; native science outputs are software fixtures."""
import json
from copy import deepcopy

import pytest
from covscreen.execution import JobManager, execute_persisted_job
from covscreen.jobs import sha256_file
from covscreen.mcp.server import dispatch
from covscreen.library_workflow import RESEARCH
from covscreen.workflow_store import load
from covscreen import tool_api
from test_workflow_md_execution import research, frozen, call, approval, register, completed_output


def admitted(fixture, monkeypatch):
    frozen(fixture)
    call(fixture, "plan_md_admission")
    monkeypatch.setattr(JobManager, "submit", register)
    result = dispatch(fixture[0], RESEARCH, approval(fixture))
    folder = completed_output(fixture, result["md_execution"]["run_id"])
    assert call(fixture, "md_admission_status")["md_execution"]["verification"] == "verified"
    return folder


def parameterized(fixture, run, *, parent=None):
    manager = JobManager(fixture[0])
    state = load(fixture[1])
    if parent is None:
        parent = state["md_execution"]["plan"]["input"]
    source = manager.registry.job_dir(parent["run_id"]) / "artifacts/md-adduct-admission/adduct-input"
    output = manager.registry.job_dir(run) / "artifacts/adduct-parameterization"
    output.mkdir(parents=True)
    (output / "parameterized.cms").write_bytes(b"EXPLICIT SOFTWARE CMS; NO NATIVE FORCE-FIELD CLAIM\n")
    (output / "atom-mapping.json").write_text("[]")
    (output / "input.mae").write_bytes((source / "adduct.mae").read_bytes())
    audit = {"system_id": parent["candidate_id"], "status": "COVALENT_PARAMETERIZATION_AUDITED",
        "forcefield": "OPLS_2005", "water": "TIP3P", "simulation_state": "covalent_adduct",
        "parameters_audited": True, "simulation_started": False, "total_atoms": 10, "total_charge_e": 0,
        "parameterized_cms_sha256": sha256_file(output / "parameterized.cms"),
        "atom_mapping_sha256": sha256_file(output / "atom-mapping.json"), "solute_graph_preserved": True,
        "source_input_sha256": sha256_file(source / "adduct.mae"), "source_qc_sha256": sha256_file(source / "graph-qc.json"),
        "native_input_sha256": sha256_file(output / "input.mae")}
    (output / "parameterization-audit.json").write_text(json.dumps(audit))
    (output / "finished.json").write_text(json.dumps({**audit, "finished_epoch": 1}))
    result = manager.registry.load_result(run)
    result.state = "COMPLETED"; result.return_code = 0
    result.artifacts = [manager.registry.register_file(run, file, "other") for file in output.iterdir()]
    manager.registry.write_result(result)
    return output


def test_parameterization_reuses_exact_admission_and_has_independent_approval(research, monkeypatch):
    admitted(research, monkeypatch)
    parent = deepcopy(load(research[1])["md_execution"])
    plan = call(research, "plan_md_parameterization")
    assert plan["workflow"]["stage"] == "MD_PARAMETERIZATION_PLAN_READY"
    assert plan["execution"]["scope"] == "registered_parameterization"
    assert "adduct_admission_run_id" not in plan["workflow"]["tools"][RESEARCH]["fields"]
    state = load(research[1])
    assert state["md_execution"]["parent"] == parent
    assert state["md_execution"]["plan"]["spec"]["parent_run_id"] == parent["run_id"]
    assert state["md_execution"]["plan"]["spec"]["engine"] == "library-first-adduct-parameterization"
    with pytest.raises(ValueError, match="receipt required"):
        call(research, "start_md_parameterization", human_approved=True)
    started = dispatch(research[0], RESEARCH, approval(research, "start_md_parameterization"))
    run = started["md_execution"]["run_id"]
    parameterized(research, run)
    final = call(research, "md_parameterization_status")
    assert final["md_execution"]["verification"] == "verified"
    assert final["md_execution"]["parameterization_completed"] is True
    assert final["md_execution"]["md_system_ready"] is False
    assert final["assessment"]["state"] == "not_evaluated"
    assert load(research[1])["md_execution"]["parent"] == parent


@pytest.mark.parametrize("changed", ["parameterized.cms", "atom-mapping.json", "finished.json", "input.mae", "parameterization-audit.json", "parent"])
def test_parameterization_invalidates_acceptance_when_current_or_parent_bytes_change(research, monkeypatch, changed):
    upstream = admitted(research, monkeypatch)
    call(research, "plan_md_parameterization")
    started = dispatch(research[0], RESEARCH, approval(research, "start_md_parameterization"))
    output = parameterized(research, started["md_execution"]["run_id"])
    assert call(research, "md_parameterization_status")["md_execution"]["verification"] == "verified"
    witness = deepcopy(load(research[1])["md_execution"]["parameterization"])
    target = upstream / "adduct-input/adduct.mae" if changed == "parent" else output / changed
    target.write_text("changed software evidence")
    if changed == "parent":
        with pytest.raises(ValueError):
            call(research, "md_parameterization_status")
    else:
        assert call(research, "md_parameterization_status")["md_execution"]["parameterization"] is None
    record = load(research[1])["md_execution"]
    assert record["verification"] == "evidence_rejected"
    assert record["parameterization"] == witness


def test_planning_revalidates_parent_and_retains_rejection(research, monkeypatch):
    output = admitted(research, monkeypatch)
    (output / "adduct-input/adduct.mae").write_text("changed admission")
    with pytest.raises(ValueError):
        call(research, "plan_md_parameterization")
    assert load(research[1])["md_execution"]["verification"] == "evidence_rejected"
    assert load(research[1])["md_execution"]["stage"] == "admission"


def test_parameterization_lost_start_receipt_recovers_only_original_job(research, monkeypatch):
    admitted(research, monkeypatch)
    call(research, "plan_md_parameterization")
    def interrupted(manager, request):
        register(manager, request)
        raise ConnectionError("Lost parameterization reply")
    monkeypatch.setattr(JobManager, "submit", interrupted)
    with pytest.raises(ConnectionError):
        dispatch(research[0], RESEARCH, approval(research, "start_md_parameterization"))
    result = call(research, "reconcile")
    assert result["pending"] is None
    assert result["md_execution"]["stage"] == "parameterization"
    assert result["execution"]["state"] == "submitted"
    assert len(list((research[0] / "state/jobs").iterdir())) == 3


@pytest.mark.parametrize("stage", ["admission", "parameterization"])
def test_unsubmitted_plan_release_archives_and_restores_its_parent(research, monkeypatch, stage):
    if stage == "admission":
        frozen(research)
    else:
        admitted(research, monkeypatch)
    before = load(research[1])
    call(research, "plan_md_" + stage)
    planned = load(research[1])["md_execution"]
    result = call(research, "release_md_stage", reason="Revise the proposed resource budget before any execution approval")
    state = load(research[1])
    assert state["branches"][-1]["md_execution"] == planned
    assert state.get("md_execution") == before.get("md_execution")
    assert state["md_input"] == before["md_input"]
    assert result["workflow"]["stage"] == ("REFINEMENT_CANDIDATE_REVIEW" if stage == "admission" else "MD_ADMISSION_REVIEW")


@pytest.mark.parametrize("stage", ["admission", "parameterization"])
@pytest.mark.parametrize("lost", [False, True])
def test_cancel_uses_real_cooperative_worker_protocol_and_recovers_lost_reply(research, monkeypatch, stage, lost):
    if stage == "admission":
        frozen(research)
        monkeypatch.setattr(JobManager, "submit", register)
    else:
        admitted(research, monkeypatch)
    call(research, "plan_md_" + stage)
    started = dispatch(research[0], RESEARCH, approval(research, "start_md_" + stage))
    run = started["md_execution"]["run_id"]
    with pytest.raises(ValueError, match="receipt required"):
        call(research, "cancel_md_stage", human_approved=True)
    original = tool_api.cancel_job_tool
    if lost:
        def dropped(*args, **kwargs):
            original(*args, **kwargs)
            raise ConnectionError("Lost cancellation receipt")
        monkeypatch.setattr(tool_api, "cancel_job_tool", dropped)
        with pytest.raises(ConnectionError):
            dispatch(research[0], RESEARCH, approval(research, "cancel_md_stage"))
        result = call(research, "reconcile")
    else:
        result = dispatch(research[0], RESEARCH, approval(research, "cancel_md_stage"))
    assert result["md_execution"]["cancellation_requested"] is True
    assert result["md_execution"]["cancellation_confirmed"] is False
    assert result["execution"]["state"] == "submitted"
    # Actual worker consumes cancellation before starting any scientific engine.
    terminal = execute_persisted_job(research[0], run)
    assert terminal.state == "CANCELLED" and terminal.engine_pid is None
    observed = call(research, "md_" + stage + "_status")
    assert observed["md_execution"]["cancellation_confirmed"] is True
    assert observed["workflow"]["stage"] == "MD_" + stage.upper() + "_FAILED"
