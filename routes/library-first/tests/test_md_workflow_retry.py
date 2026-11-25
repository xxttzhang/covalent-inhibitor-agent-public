"""Native workflow retry contracts; science and job submission are labelled fixtures.

Real MCP dispatch, signatures, registry identities and workflow persistence are
used. submit only registers a queued job; no scientific worker is started.
"""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "science/tests"))
from test_workflow_md_execution import research, frozen, call, approval, register, completed_output
from test_workflow_md_parameterization import admitted
from test_workflow_md_system_preparation import ready as parameterized
from test_workflow_md_short_qc import ready as prepared
from test_workflow_md_equilibration import ready as qc_passed

from covscreen.execution import JobManager
from covscreen.jobs import utc_now
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.mcp.registry import TOOLS
from covscreen.mcp.server import dispatch
from covscreen.native_approval import needs_approval
from covscreen.workflow_store import load
from covscreen_library_first import md_execution

REASON = "The recorded software failure was reviewed; prepare a new independently approved attempt."


def start(fixture, monkeypatch, stage="admission"):
    if stage == "admission":
        frozen(fixture)
        monkeypatch.setattr(JobManager, "submit", register)
    else:
        {"parameterization": admitted, "system_preparation": parameterized,
         "short_qc": prepared, "equilibration": qc_passed}[stage](fixture, monkeypatch)
    call(fixture, "plan_md_" + stage)
    args = approval(fixture, "start_md_" + stage)
    run = dispatch(fixture[0], RESEARCH, args)["md_execution"]["run_id"]
    return run, args


def terminal(fixture, run, state="FAILED", stage="admission"):
    registry = JobManager(fixture[0]).registry
    result = registry.load_result(run)
    result.state, result.return_code = state, 1 if state == "FAILED" else -15
    result.finished_at = utc_now()
    result.failure_reason = "software_fixture_" + state.lower()
    result.message = "Explicit terminal software fixture; no engine was run."
    registry.write_result(result)
    return call(fixture, "md_" + stage + "_status")


def inventory(fixture):
    registry = JobManager(fixture[0]).registry
    return sorted(path.name for path in registry.root.iterdir())


def reopened(fixture):
    return dispatch(fixture[0], ENTRY, {"action": "status", "workflow_id": load(fixture[1])["workflow_id"]})


@pytest.mark.parametrize("stage,state", [
    ("admission", "FAILED"), ("admission", "TIMED_OUT"), ("admission", "CANCELLED"),
    ("parameterization", "FAILED"), ("system_preparation", "TIMED_OUT"),
    ("short_qc", "CANCELLED"), ("equilibration", "FAILED"),
])
def test_terminal_retry_archives_original_and_requires_new_exact_approval(research, monkeypatch, stage, state):
    run, old_approval = start(research, monkeypatch, stage)
    terminal(research, run, state, stage)
    old = deepcopy(load(research[1])["md_execution"])
    registry = JobManager(research[0]).registry
    original = {p.name: p.read_bytes() for p in registry.job_dir(run).iterdir() if p.is_file()}
    before = inventory(research)
    result = call(research, "retry_md_stage", reason=REASON)
    current = load(research[1])
    replacement = current["md_execution"]
    assert current["branches"][-1]["md_execution"] == old
    assert current["branches"][-1]["reason"] == REASON
    assert replacement["plan"]["input"] == old["plan"]["input"]
    assert replacement["plan"]["arguments"] == old["plan"]["arguments"]
    assert replacement.get("parent") == old.get("parent")
    assert replacement["plan"]["request_id"] != old["plan"]["request_id"]
    assert replacement["plan"]["spec"]["attempt"] == old["plan"]["spec"]["attempt"] + 1 == 2
    assert not any(key in replacement for key in ("run_id", "approved_scope", "cancellation_scope", "cancellation_requested"))
    assert current["pending"] is None and inventory(research) == before
    for response in (result, reopened(research)):
        assert response["execution"]["state"] == "not_submitted"
        assert response["md_execution"]["attempt"] == 2
        previous = response["md_execution"]["previous_attempts"]
        assert [(p["run_id"], p["job_state"], p["attempt"]) for p in previous] == [(run, state, 1)]
        assert response["workflow"]["stage"] == "MD_" + stage.upper() + "_PLAN_READY"
        assert "retry_md_stage" not in response["workflow"]["tools"][RESEARCH]["actions"]
    with pytest.raises(ValueError):
        call(research, "start_md_" + stage, human_approved=True)
    with pytest.raises(ValueError):
        dispatch(research[0], RESEARCH, old_approval)
    assert inventory(research) == before
    new_run = dispatch(research[0], RESEARCH, approval(research, "start_md_" + stage))["md_execution"]["run_id"]
    assert new_run != run and registry.load_spec(new_run).attempt == 2
    assert len(inventory(research)) == len(before) + 1
    assert {p.name: p.read_bytes() for p in registry.job_dir(run).iterdir() if p.is_file()} == original


def test_retry_is_published_as_planning_and_cannot_accept_identity_or_authorization(research, monkeypatch):
    run, _ = start(research, monkeypatch)
    terminal(research, run)
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == RESEARCH)
    assert "retry_md_stage" in schema["properties"]["action"]["enum"]
    assert not needs_approval(RESEARCH, {"action": "retry_md_stage"})
    for extra in ({"run_id": "substitution"}, {"attempt": 4}, {"human_approved": True}, {"resource_budget": {}}):
        with pytest.raises(ValueError):
            call(research, "retry_md_stage", reason=REASON, **extra)
    with pytest.raises(ValueError, match="concrete reason"):
        call(research, "retry_md_stage", reason="retry")


def test_unsubmitted_replanning_and_release_do_not_reset_or_consume_attempt_number(research, monkeypatch):
    run, _ = start(research, monkeypatch)
    terminal(research, run)
    call(research, "retry_md_stage", reason=REASON)
    call(research, "plan_md_admission", native_timeout_seconds=600)
    assert load(research[1])["md_execution"]["plan"]["spec"]["attempt"] == 2
    call(research, "release_md_stage", reason="Reconsider the new draft without deleting the previous failed attempt.")
    call(research, "plan_md_admission")
    assert load(research[1])["md_execution"]["plan"]["spec"]["attempt"] == 2
    second = dispatch(research[0], RESEARCH, approval(research))["md_execution"]["run_id"]
    terminal(research, second, "TIMED_OUT")
    third = call(research, "retry_md_stage", reason=REASON)
    assert third["md_execution"]["attempt"] == 3
    assert [p["run_id"] for p in third["md_execution"]["previous_attempts"]] == [run, second]


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "COMPLETED"])
def test_fresh_terminal_check_rejects_stale_failed_workflow(research, monkeypatch, state):
    run, _ = start(research, monkeypatch)
    terminal(research, run)
    registry = JobManager(research[0]).registry
    result = registry.load_result(run)
    result.state = state
    registry.write_result(result)
    before = inventory(research)
    with pytest.raises(ValueError, match="freshly verified"):
        call(research, "retry_md_stage", reason=REASON)
    current = load(research[1])
    assert current["md_execution"]["run_id"] == run
    assert current["md_execution"]["job_state"] == state
    assert not current["branches"] and inventory(research) == before
    assert "retry_md_stage" not in reopened(research)["workflow"]["tools"][RESEARCH]["actions"]


@pytest.mark.parametrize("corruption", ["input", "parent", "spec", "result_identity"])
def test_revalidation_failure_preserves_original_task_and_never_archives(research, monkeypatch, corruption):
    stage = "parameterization" if corruption == "parent" else "admission"
    run, _ = start(research, monkeypatch, stage)
    terminal(research, run, stage=stage)
    current = load(research[1])
    registry = JobManager(research[0]).registry
    if corruption == "input":
        path = research[0] / "state/candidate_pose_input" / current["md_input"]["artifact_id"][21:] / "covdock-source.maegz"
        path.write_bytes(b"changed original input")
    elif corruption == "parent":
        path = registry.job_dir(current["md_execution"]["parent"]["run_id"]) / "artifacts/md-adduct-admission/adduct-input/adduct.mae"
        path.write_bytes(b"changed upstream evidence")
    elif corruption == "spec":
        spec = registry.load_spec(run)
        spec.request_id = "different-request"
        (registry.job_dir(run) / "spec.json").write_text(spec.model_dump_json())
    else:
        result = registry.load_result(run)
        result.attempt += 1
        registry.write_result(result)
    before = inventory(research)
    with pytest.raises(ValueError):
        call(research, "retry_md_stage", reason=REASON)
    saved = load(research[1])
    assert saved["branches"] == current["branches"] and saved["pending"] is None
    assert saved["md_execution"]["run_id"] == run
    assert saved["md_execution"]["verification"] == "evidence_rejected"
    assert saved["md_execution"]["retry_plan_error"]
    assert inventory(research) == before


def test_compile_failure_retains_original_and_can_be_planned_after_repair(research, monkeypatch):
    run, _ = start(research, monkeypatch)
    terminal(research, run)
    original = load(research[1])["md_execution"]
    build = JobManager.build_spec
    with monkeypatch.context() as patch:
        patch.setattr(JobManager, "build_spec", lambda *_: (_ for _ in ()).throw(ValueError("runtime unavailable")))
        with pytest.raises(ValueError, match="runtime unavailable"):
            call(research, "retry_md_stage", reason=REASON)
    current = load(research[1])
    assert current["md_execution"]["run_id"] == run
    assert current["md_execution"]["plan"] == original["plan"]
    assert not current["branches"]
    assert reopened(research)["md_execution"]["retry_plan_error"] == "ValueError: runtime unavailable"
    assert call(research, "retry_md_stage", reason=REASON)["md_execution"]["attempt"] == 2


def test_lost_new_start_reply_reconciles_one_new_attempt_without_replay(research, monkeypatch):
    run, _ = start(research, monkeypatch)
    terminal(research, run)
    call(research, "retry_md_stage", reason=REASON)
    before = inventory(research)
    calls = []
    def dropped(manager, request):
        calls.append(request.request_id)
        register(manager, request)
        raise ConnectionError("new attempt reply lost")
    monkeypatch.setattr(JobManager, "submit", dropped)
    args = approval(research)
    with pytest.raises(ConnectionError):
        dispatch(research[0], RESEARCH, args)
    assert reopened(research)["pending"] and RESEARCH not in reopened(research)["workflow"]["tools"]
    with pytest.raises(ValueError):
        call(research, "retry_md_stage", reason=REASON)
    result = call(research, "reconcile")
    assert result["pending"] is None and result["md_execution"]["attempt"] == 2
    assert result["md_execution"]["run_id"] != run
    assert len(inventory(research)) == len(before) + 1 and len(calls) == 1
    with pytest.raises(ValueError):
        dispatch(research[0], RESEARCH, args)
    assert len(calls) == 1


def test_cancel_request_without_confirmed_terminal_cannot_enable_retry(research, monkeypatch):
    run, _ = start(research, monkeypatch)
    response = dispatch(research[0], RESEARCH, approval(research, "cancel_md_stage"))
    assert response["md_execution"]["cancellation_requested"] is True
    assert response["md_execution"]["cancellation_confirmed"] is False
    assert "retry_md_stage" not in response["workflow"]["tools"][RESEARCH]["actions"]
    with pytest.raises(ValueError):
        call(research, "retry_md_stage", reason=REASON)


def test_changed_runtime_after_retry_plan_requires_explicit_replanning(research, monkeypatch):
    run, _ = start(research, monkeypatch)
    terminal(research, run)
    call(research, "retry_md_stage", reason=REASON)
    original = JobManager.build_spec
    def changed(manager, request):
        spec = original(manager, request)
        spec.command.append("--runtime-drift")
        return spec
    monkeypatch.setattr(JobManager, "build_spec", changed)
    before = inventory(research)
    with pytest.raises(ValueError, match="Frozen MD stage plan changed"):
        approval(research)
    assert inventory(research) == before and load(research[1])["pending"] is None
