"""Persist real workflow diagnostics using signed software-only job fixtures.

The existing fixture registers jobs and synthetic evidence without starting a
scientific worker. These assertions exercise the public MCP and saved workflow.
"""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "science/tests"))
from test_workflow_md_execution import research, frozen, call, approval, register, completed_output

from covscreen.execution import JobManager
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.mcp.server import dispatch
from covscreen.workflow_store import load
from covscreen_library_first import md_execution


def started(fixture, monkeypatch):
    frozen(fixture)
    call(fixture, "plan_md_admission")
    monkeypatch.setattr(JobManager, "submit", register)
    return dispatch(fixture[0], RESEARCH, approval(fixture))["md_execution"]["run_id"]


def reopened(fixture):
    return dispatch(fixture[0], ENTRY, {"action": "status", "workflow_id": load(fixture[1])["workflow_id"]})


@pytest.mark.parametrize("state,code,reason", [
    ("FAILED", 1, "engine_nonzero_exit"),
    ("TIMED_OUT", -15, "approved_wall_time_exceeded"),
    ("CANCELLED", None, "cancelled_by_native_request"),
])
def test_terminal_failure_reason_survives_status_and_workflow_reopen(research, monkeypatch, state, code, reason):
    run = started(research, monkeypatch)
    manager = JobManager(research[0])
    result = manager.registry.load_result(run)
    result.state, result.return_code, result.failure_reason = state, code, reason
    result.message = "Explicit software terminal result; no scientific worker was started."
    manager.registry.write_result(result)
    before = sorted(p.name for p in manager.registry.root.iterdir())

    observed = call(research, "md_admission_status")
    for response in (observed, reopened(research)):
        md = response["md_execution"]
        assert md["run_id"] == run
        assert (md["job_state"], md["return_code"], md["failure_reason"], md["message"]) == (
            state, code, reason, result.message)
        assert md["evidence_error"] is None
        assert response["execution"]["state"] == "failed"
        blocker = next(item for item in response["blockers"] if item["code"] == "md_stage_failed")
        assert state in blocker["reason"] and reason in blocker["reason"]
        assert response["workflow"]["tools"][RESEARCH]["actions"] == ["md_admission_status", "retry_md_stage"]
        assert md["production_review_ready"] is False
    assert sorted(p.name for p in manager.registry.root.iterdir()) == before


def test_rejected_output_reports_error_and_clears_it_after_exact_bytes_restored(research, monkeypatch):
    run = started(research, monkeypatch)
    output = completed_output(research, run)
    assert call(research, "md_admission_status")["md_execution"]["verification"] == "verified"
    manifest = output / "md-adduct-admission.json"
    original = manifest.read_bytes()
    manifest.write_text("changed software receipt")

    rejected = call(research, "md_admission_status")
    error = rejected["md_execution"]["evidence_error"]
    assert error and error.startswith("JSONDecodeError:")
    for response in (rejected, reopened(research)):
        assert response["execution"]["state"] == "completed"
        assert response["md_execution"]["verification"] == "evidence_rejected"
        assert response["md_execution"]["evidence_error"] == error
        assert response["md_execution"]["admission"] is None
        assert any(b["code"] == "md_stage_evidence_rejected" and error in b["reason"] for b in response["blockers"])

    manifest.write_bytes(original)
    recovered = call(research, "md_admission_status")
    assert recovered["md_execution"]["verification"] == "verified"
    assert recovered["md_execution"]["evidence_error"] is None
    assert "error" not in load(research[1])["md_execution"]


def test_input_verification_error_survives_failed_status_call(research, monkeypatch):
    run = started(research, monkeypatch)
    asset = load(research[1])["md_input"]
    source = research[0] / "state/candidate_pose_input" / asset["artifact_id"][21:] / "covdock-source.maegz"
    source.write_bytes(b"changed frozen input")
    with pytest.raises(ValueError):
        call(research, "md_admission_status")
    response = reopened(research)
    assert response["md_execution"]["run_id"] == run
    assert response["md_execution"]["verification"] == "evidence_rejected"
    assert response["md_execution"]["evidence_error"]
    assert any(b["code"] == "md_stage_evidence_rejected" for b in response["blockers"])
    assert not any(a.startswith("plan_md_") or a.startswith("start_md_")
        for a in response["workflow"]["tools"][RESEARCH]["actions"])


@pytest.mark.parametrize("terminal", ["CANCELLED", "FAILED", "TIMED_OUT"])
def test_lost_cancellation_reply_reconciles_current_terminal_diagnostics(research, monkeypatch, terminal):
    run = started(research, monkeypatch)
    manager = JobManager(research[0])
    inventory = sorted(p.name for p in manager.registry.root.iterdir())
    original_cancel = md_execution.api.cancel_job_tool
    cancellations = []

    def dropped_reply(*args, **kwargs):
        original_cancel(*args, **kwargs)
        cancellations.append(run)
        # Authoritative terminal fixture appears before the lost API response;
        # the genuine cooperative cancellation record has already been saved.
        result = manager.registry.load_result(run)
        result.state, result.return_code = terminal, -15
        result.failure_reason = "software_confirmed_" + terminal.lower()
        result.message = "Terminal software fixture after native cancellation request; no engine started."
        manager.registry.write_result(result)
        raise ConnectionError("Cancellation reply lost")

    monkeypatch.setattr(md_execution.api, "cancel_job_tool", dropped_reply)
    with pytest.raises(ConnectionError, match="Cancellation reply lost"):
        dispatch(research[0], RESEARCH, approval(research, "cancel_md_stage"))
    assert load(research[1])["pending"]["action"] == "cancel_md_stage"

    recovered = call(research, "reconcile")
    result = manager.registry.load_result(run)
    for response in (recovered, reopened(research)):
        md = response["md_execution"]
        assert response["pending"] is None
        assert md["run_id"] == run and md["job_state"] == terminal
        assert md["cancellation_requested"] is True
        assert md["cancellation_confirmed"] is (terminal == "CANCELLED")
        assert (md["return_code"], md["failure_reason"], md["message"]) == (
            result.return_code, result.failure_reason, result.message)
        blocker = next(b for b in response["blockers"] if b["code"] == "md_stage_failed")
        assert result.failure_reason in blocker["reason"]
        assert response["workflow"]["tools"][RESEARCH]["actions"] == ["md_admission_status", "retry_md_stage"]
    assert recovered["result"]["automatic_resubmission_allowed"] is False
    assert cancellations == [run]
    assert sorted(p.name for p in manager.registry.root.iterdir()) == inventory
