"""Authenticated backend planning exchange; software evidence, no compute."""
from copy import deepcopy
import hashlib
import time
from uuid import uuid4

import pytest
from covscreen.library_workflow import navigate, RESEARCH
from covscreen.mcp.server import dispatch
from covscreen.native_approval import signed, decode
from covscreen.workflow_store import load, save
from covscreen_library_first.workflow import directory
from test_workflow_screening_results import fixture as result_fixture


@pytest.fixture
def research(tmp_path, monkeypatch):
    key = tmp_path / "test-only.key"
    key.write_text("fixture-refinement-key-not-for-production-123456")
    key.chmod(0o600)
    monkeypatch.setenv("COVALENT_APPROVAL_KEY_FILE", str(key))
    initial = navigate(tmp_path, action="start", request_id="refinement-fixture", target="EGFR", taxon=9606)
    folder = directory(tmp_path, initial["workflow"]["workflow_id"])
    state = load(folder)
    result, observed, _ = result_fixture()
    result["reference"]["workflow_id"] = state["workflow_id"]
    source = {"text": "Software request for native refinement planning only", "origin": {
        "user_message_id": "fixture-user", "assistant_message_id": "fixture-assistant", "part_ids": ["fixture-part"],
        "request_sha256": hashlib.sha256(b"Software request for native refinement planning only").hexdigest()}}
    scope = {"project": "fixture", "directory": "/tmp/fixture-refinement", "sessionID": "fixture", "ledger_id": "a" * 32}
    plan = {"id": "screening-plan-" + "a" * 32, "sha256": "a" * 64}
    state.update(stage="CANDIDATE_REVIEW", screening_plan=plan, screening_handoff={"scope": scope,
        "source": source, "intent": {k: result["reference"][k] for k in ("workflow_id", "revision", "operation_id")},
        "screening_plan": plan, "receipt": None, "observation": {**observed, "execution_state": "completed", "ledger_sequence": 10,
            "observed_at": 1000, "observation_scope": "persisted_executor_ledger", "remote_artifacts_rehashed": False},
        "selection": result["selection"], "screening_result": result})
    save(folder, state)
    return tmp_path, folder, source, scope


def exchange(research, action="plan_refinement", operation="open", *, values=None, payload=None):
    root, folder, source, scope = research
    state = load(folder)
    args = {"action": action, "workflow_id": state["workflow_id"], "expected_revision": state["revision"],
        **({"refinement_review": "Compare the exact reviewed subset with independent structural methods", "max_points": 1} if action == "plan_refinement" else {}),
        **(values or {})}
    request = {"kind": "library-screening-host-request-v1", "arguments": args, "operation": operation,
        "scope": scope, "source": source if action == "plan_refinement" else None, "nonce": uuid4().hex,
        "expires": time.time() + 300, **(payload or {})}
    return dispatch(root, RESEARCH, {**args, "_workflow_handoff": signed(request)})


def receipt(research):
    current = load(research[1])["refinement_handoff"]
    return {"schema": "library-workflow-refinement-planning-v1", "planning_scope": current["planning_scope"],
        "task_id": current["task_id"], "checked_at": 1001,
        "proposal": {"digest": "c" * 64, "state": "PENDING_RUNTIME_CONFIRMATION", "proposed_points": 0.5,
            "selected_count": 1, "quality_policy_digest": "d" * 64, "blockers": [], "stages": [
                {"engine": "af3", "minutes": 120, "candidate_count": 1, "proposed_points": 0.5},
                {"engine": "covdock", "minutes": 240, "candidate_count": 1, "proposed_points": 0.0}]},
        "draft": {"workflow_id": "refineflow-" + "a" * 24, "digest": "b" * 64, "proposal_digest": "c" * 64,
            "state": "COMPILED_NOT_EXECUTABLE", "approve_before": 100000, "blockers": []},
        "compute_submitted": False, "approval_recorded": False, "scientific_assessment": "not_evaluated"}


def test_lock_precedes_planning_and_recovery_keeps_identity_without_parent_mutation(research):
    original = deepcopy(load(research[1])["screening_handoff"])
    opened = exchange(research)
    assert opened["workflow"]["stage"] == "REFINEMENT_HANDOFF"
    proof = decode(opened["_host_handoff"])
    bound = proof["refinement_handoff"]
    assert bound == load(research[1])["refinement_handoff"]
    assert opened["execution"] == {"scope": "refinement_draft_preparation", "state": "not_submitted", "task_ids": []}
    assert opened["result"]["approval_recorded"] is False
    assert "plan_refinement" not in opened["workflow"]["tools"][RESEARCH]["actions"]
    recovered = exchange(research, "recover_refinement")
    assert decode(recovered["_host_handoff"])["refinement_handoff"] == bound
    assert recovered["workflow"]["revision"] == opened["workflow"]["revision"]
    for action in ("screening_status", "select_screening_candidates", "release_screening"):
        with pytest.raises(ValueError):
            exchange(research, action, "observe")
    with pytest.raises(ValueError, match="another host"):
        exchange(research, "recover_refinement", payload={"scope": {**research[3], "sessionID": "other"}})
    with pytest.raises(ValueError, match="review and budget"):
        exchange(research, operation="refinement_plan", values={"max_points": 2}, payload={"refinement_result": receipt(research)})
    planned = receipt(research)
    committed = exchange(research, "recover_refinement", "refinement_plan", payload={"refinement_result": planned})
    assert committed["workflow"]["stage"] == "REFINEMENT_PLAN_READY"
    assert committed["refinement_plan"]["draft"] == planned["draft"]
    assert "host_scope" not in committed["refinement_plan"] and "source" not in committed["refinement_plan"]
    assert load(research[1])["screening_handoff"] == original
    duplicate = exchange(research, "recover_refinement", "refinement_plan", payload={"refinement_result": planned})
    assert duplicate["workflow"]["revision"] == committed["workflow"]["revision"]
    unavailable = {**planned, "checked_at": 1002, "proposal": None, "draft": None}
    degraded = exchange(research, "recover_refinement", "refinement_plan", payload={"refinement_result": unavailable})
    assert degraded["workflow"]["stage"] == "REFINEMENT_PLAN_BLOCKED"
    assert load(research[1])["refinement_handoff"]["draft_identity"]["digest"] == planned["draft"]["digest"]
    substituted = deepcopy(planned)
    substituted["checked_at"] = 1003
    substituted["draft"]["workflow_id"] = "refineflow-" + "f" * 24
    with pytest.raises(ValueError, match="replace"):
        exchange(research, "recover_refinement", "refinement_plan", payload={"refinement_result": substituted})
    assert not (research[0] / "state/jobs").exists()


def test_release_archives_only_the_sealed_operation_and_allows_explicit_replan(research):
    exchange(research)
    state = load(research[1])
    current = state["refinement_handoff"]
    closure = {"schema": "library-refinement-planning-closure-v1", "planning_scope": current["planning_scope"],
        "task_id": current["task_id"], "state": "NO_EXECUTABLE_WORKFLOW", "compute_submitted": False,
        "reason": "Explicitly revise this scientific proposal after failed planning"}
    with pytest.raises(ValueError, match="sealed"):
        exchange(research, "recover_refinement", "refinement_release", payload={"refinement_closure": {**closure, "compute_submitted": True}})
    assert load(research[1]) == state
    released = exchange(research, "recover_refinement", "refinement_release", payload={"refinement_closure": closure})
    assert released["workflow"]["stage"] == "CANDIDATE_REVIEW"
    assert released["refinement_plan"] is None
    archived = load(research[1])["refinement_handoff_history"]
    assert archived == [{**current, "closure": closure}]
    with pytest.raises(ValueError, match="durably opened"):
        exchange(research, "recover_refinement", "refinement_plan", payload={"refinement_result": {}})
    replanned = exchange(research, values={"max_points": 2})
    new = decode(replanned["_host_handoff"])["refinement_handoff"]
    assert new["planning_scope"]["operation_id"] != current["planning_scope"]["operation_id"]
    assert new["planning_scope"]["selection"] == current["planning_scope"]["selection"]
    assert new["planning_scope"]["max_points"] == 2


@pytest.mark.parametrize("case", ["count", "budget", "order", "sum", "expired", "plan_digest"])
def test_native_proposal_denominators_budget_and_expiry_are_preserved(research, case):
    exchange(research)
    value = receipt(research)
    if case == "count":
        value["proposal"]["stages"][0]["candidate_count"] = 2
    if case == "budget":
        value["proposal"]["stages"][0]["proposed_points"] = 2.0
        value["proposal"]["proposed_points"] = 2.0
    if case == "order":
        value["proposal"]["stages"].reverse()
    if case == "sum":
        value["proposal"]["proposed_points"] = 0.1
    if case == "expired":
        value["draft"]["approve_before"] = 1000
    if case == "plan_digest":
        value["draft"]["proposal_digest"] = "f" * 64
    before = load(research[1])
    with pytest.raises(ValueError):
        exchange(research, "recover_refinement", "refinement_plan", payload={"refinement_result": value})
    assert load(research[1]) == before


@pytest.mark.parametrize("change", [
    {"compute_submitted": True}, {"approval_recorded": True}, {"scientific_assessment": "passed"},
    {"planning_scope": {}}, {"task_id": "screen-" + "0" * 24}, {"proposal": None}, {"command": "untrusted"},
])
def test_invalid_signed_receipt_does_not_change_state(research, change):
    exchange(research)
    before = load(research[1])
    with pytest.raises(ValueError):
        exchange(research, "recover_refinement", "refinement_plan", payload={"refinement_result": {**receipt(research), **change}})
    assert load(research[1]) == before


@pytest.mark.parametrize("case", ["zero", "missing", "foreign", "budget", "review", "unopened"])
def test_invalid_preconditions_never_lock_or_plan(research, case):
    state = load(research[1])
    if case == "zero":
        state["screening_handoff"]["screening_result"]["selection"]["selected_count"] = 0
    if case == "missing":
        state["screening_handoff"]["screening_result"]["verification"] = "selection_unavailable"
    if case == "foreign":
        state["screening_handoff"]["screening_result"]["task_id"] = "screen-" + "0" * 24
    save(research[1], state)
    with pytest.raises(ValueError):
        exchange(research, operation="refinement_plan" if case == "unopened" else "open",
            values={"max_points": -1} if case == "budget" else {"refinement_review": "vague"} if case == "review" else None)
    assert load(research[1]) == state
