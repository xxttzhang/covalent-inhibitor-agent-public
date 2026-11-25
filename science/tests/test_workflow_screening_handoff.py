"""Real backend handoff over historical receptor + explicit software fixtures."""
import hashlib
import time
from uuid import uuid4

import pytest

from covscreen.mcp.server import dispatch
from covscreen.native_approval import decode, signed
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.workflow_store import load
from test_library_workflow import call, state_file
from test_receptor_handoff import bound
from test_workflow_screening_plan import prepared, REVIEW


def request(current, action="compile_screening", operation="open", reason=None, **updates):
    args = {"action": action, "workflow_id": current["workflow"]["workflow_id"],
        "expected_revision": current["workflow"]["revision"]}
    if reason is not None:
        args["reason"] = reason
    text = "Software handoff fixture; do not submit scientific computation"
    payload = {"kind": "library-screening-host-request-v1", "arguments": args, "operation": operation,
        "scope": {"project": "fixture", "directory": "/tmp/fixture-host", "sessionID": "fixture-session", "ledger_id": "f" * 32},
        "source": {"text": text, "origin": {"user_message_id": "fixture-user", "assistant_message_id": "fixture-assistant",
            "part_ids": ["fixture-text"], "request_sha256": hashlib.sha256(text.encode()).hexdigest()}},
        "nonce": uuid4().hex, "expires": time.time() + 300, **updates}
    return {**args, "_workflow_handoff": signed(payload)}


def test_authenticated_handoff_locks_plan_recovers_and_commits_only_the_same_draft(bound):
    root, current = prepared(bound)
    planned = call(root, current, RESEARCH, "prepare_screening_plan", scientific_review=REVIEW, max_points=1)
    with pytest.raises(ValueError, match="authenticated host"):
        call(root, planned, RESEARCH, "compile_screening")
    for updates in ({"kind": "unrelated-native-permission"}, {"expires": time.time() - 1}):
        with pytest.raises(ValueError, match="purpose, scope or expiry"):
            dispatch(root, RESEARCH, request(planned, **updates))
    args = request(planned)
    opened = dispatch(root, RESEARCH, args)
    verified = decode(opened["_host_handoff"])
    assert verified["request_sha256"] == hashlib.sha256(args["_workflow_handoff"].encode()).hexdigest()
    assert "_host_handoff" not in verified["public_result"]
    assert opened["workflow"]["stage"] == "SCREENING_HANDOFF"
    assert opened["execution"]["scope"] == "screening_draft_preparation"
    assert opened["execution"]["state"] == "not_submitted"
    assert "reconsider" not in opened["workflow"]["tools"][ENTRY]["actions"]
    assert opened["workflow"]["tools"][RESEARCH]["actions"] == ["read_screening_plan", "recover_screening", "release_screening"]
    with pytest.raises(ValueError, match="unavailable"):
        call(root, opened, RESEARCH, "prepare_screening_plan", scientific_review=REVIEW, max_points=2)
    foreign = {**verified["scope"], "sessionID": "other-session"}
    with pytest.raises(ValueError, match="another host"):
        dispatch(root, RESEARCH, request(opened, "recover_screening", scope=foreign, source=None))
    recovered = dispatch(root, RESEARCH, request(opened, "recover_screening", source=None))
    restored = decode(recovered["_host_handoff"])
    assert restored["intent"] == verified["intent"] and restored["source"] == verified["source"]
    assert recovered["workflow"]["revision"] == opened["workflow"]["revision"]
    intent = verified["intent"]
    receipt = {"task_id": "screen-" + "a" * 24, "reference": {
        "schema": "library-workflow-execution-reference-v1", "workflow_id": intent["workflow_id"],
        "revision": intent["revision"], "operation_id": intent["operation_id"],
        "intent_sha256": "b" * 64, "plan_sha256": "c" * 64}, "state": "DRAFT", "preflight_verified": True}
    with pytest.raises(ValueError, match="unapproved native draft"):
        dispatch(root, RESEARCH, request(recovered, "recover_screening", "commit", source=None,
            receipt={**receipt, "state": "RUNNING"}))
    committed = dispatch(root, RESEARCH, request(recovered, "recover_screening", "commit", source=None, receipt=receipt))
    assert committed["workflow"]["stage"] == "SCREENING_EXECUTION_DRAFT"
    assert committed["execution"]["task_ids"] == [receipt["task_id"]]
    duplicate = dispatch(root, RESEARCH, request(committed, "recover_screening", "commit", source=None, receipt=receipt))
    assert duplicate["workflow"] == committed["workflow"]
    with pytest.raises(ValueError, match="different native task"):
        dispatch(root, RESEARCH, request(committed, "recover_screening", "commit", source=None,
            receipt={**receipt, "task_id": "screen-" + "f" * 24}))
    state = load(state_file(root, committed))
    assert state["screening_handoff"]["receipt"] == receipt
    assert not committed["result"]["calculation_submitted"]
    assert len(list((root / "state/jobs").glob("*/spec.json"))) == 1  # historical fixture only

    # Recovery must remain available after source loss; only a new approval
    # requires current scientific inputs. This never claims fresh remote checks.
    from covscreen_library_first import screening_plan
    observed = {"schema": "library-workflow-ledger-observation-v1", "reference": receipt["reference"],
        "task_id": receipt["task_id"], "task_state": "CANCELLED", "approval_recorded": False,
        "execution_state": "not_submitted", "ledger_sequence": 3, "observed_at": 1000,
        "observation_scope": "persisted_executor_ledger", "assessment": "not_evaluated", "remote_artifacts_rehashed": False}
    (screening_plan.folder(root, state["screening_plan"]["id"]) / "screening-plan.json").unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        dispatch(root, RESEARCH, request(committed, "approve_screening", source=None))
    stopped = dispatch(root, RESEARCH, request(committed, "screening_status", "observe", source=None, observation=observed))
    assert stopped["workflow"]["stage"] == "SCREENING_STOPPED"
    assert stopped["execution"]["state"] == "not_submitted"
    assert stopped["assessment"]["state"] != "passed"
    for changed in ({"ledger_sequence": 2}, {"execution_state": "running"}, {"task_id": "screen-" + "e" * 24},
                    {"assessment": "passed"}, {"approval_recorded": True}):
        with pytest.raises(ValueError):
            dispatch(root, RESEARCH, request(stopped, "screening_status", "observe", source=None, observation={**observed, **changed}))
    operation = {k: v for k, v in receipt["reference"].items() if k != "plan_sha256"}
    closure = {"schema": "library-workflow-operation-closure-v1", "operation": operation,
        "task_id": receipt["task_id"], "state": "CANCELLED_UNAPPROVED", "reason": "User declined this fixture draft", "compute_submitted": False}
    released = dispatch(root, RESEARCH, request(stopped, "recover_screening", "release", source=None, closure=closure))
    assert released["workflow"]["stage"] == "SCREENING_PLAN"
    saved = load(state_file(root, released))
    assert saved["screening_handoff"] is None
    assert saved["screening_handoff_history"][-1]["closure"] == closure
    assert saved["screening_plan"] is None
    assert saved["screening_inputs"] == state["screening_inputs"]
    assert "reconsider" in released["workflow"]["tools"][ENTRY]["actions"]


def test_vs_migration_preserves_cancelled_approved_history_and_inputs_without_reusing_permission(bound):
    root, current = prepared(bound)
    planned = call(root, current, RESEARCH, "prepare_screening_plan", scientific_review=REVIEW, max_points=1)
    opened = dispatch(root, RESEARCH, request(planned))
    intent = decode(opened["_host_handoff"])["intent"]
    reference = {"schema": "library-workflow-execution-reference-v1", "workflow_id": intent["workflow_id"],
        "revision": intent["revision"], "operation_id": intent["operation_id"], "intent_sha256": "b" * 64, "plan_sha256": "c" * 64}
    receipt = {"task_id": "screen-" + "a" * 24, "reference": reference, "state": "DRAFT", "preflight_verified": True}
    committed = dispatch(root, RESEARCH, request(opened, "recover_screening", "commit", source=None, receipt=receipt))
    observed = {"schema": "library-workflow-ledger-observation-v1", "reference": reference,
        "task_id": receipt["task_id"], "task_state": "CANCELLED", "approval_recorded": True,
        "execution_state": "not_submitted", "ledger_sequence": 7, "observed_at": 1000,
        "observation_scope": "persisted_executor_ledger", "assessment": "not_evaluated", "remote_artifacts_rehashed": False}
    stopped = dispatch(root, RESEARCH, request(committed, "screening_status", "observe", source=None, observation=observed))
    before = load(state_file(root, stopped))
    actions = stopped["workflow"]["tools"][RESEARCH]["actions"]
    assert "migrate_screening_to_vs" in actions and "release_screening" not in actions
    reason = "Retain this cancelled research and replace the suspended engine with CovDock VS"
    with pytest.raises(ValueError, match="authenticated host"):
        call(root, stopped, RESEARCH, "migrate_screening_to_vs", reason=reason)
    operation = {k: v for k, v in reference.items() if k != "plan_sha256"}
    closure = {"schema": "library-workflow-operation-closure-v1", "operation": operation, "task_id": receipt["task_id"],
        "state": "CANCELLED_UNSUBMITTED_FOR_VS", "reason": reason, "compute_submitted": False}
    for invalid in ({"state": "CANCELLED_UNAPPROVED"}, {"task_id": "screen-" + "d" * 24},
                    {"reason": "A different migration reason"}, {"compute_submitted": True},
                    {"operation": {**operation, "operation_id": "e" * 32}}):
        with pytest.raises(ValueError):
            dispatch(root, RESEARCH, request(stopped, "migrate_screening_to_vs", "release", reason=reason,
                source=None, closure={**closure, **invalid}))
    with pytest.raises(ValueError, match="purpose"):
        dispatch(root, RESEARCH, request(stopped, "release_screening", "release", reason=reason, source=None, closure=closure))
    assert load(state_file(root, stopped)) == before
    # Host sealed the SQLite operation but lost its response: normal recovery
    # must complete exactly that VS migration, never return to the old planner.
    migrated = dispatch(root, RESEARCH, request(stopped, "recover_screening", "release", source=None, closure=closure))
    assert migrated["workflow"]["stage"] == "VS_SCREENING_PLAN"
    assert migrated["execution"] == {"scope": "library_vs_screening_planning", "state": "not_submitted", "task_ids": []}
    assert migrated["result"]["new_approval_required"]
    assert migrated["result"]["historical_approval_recorded"]
    assert not migrated["result"]["approval_recorded"]
    saved = load(state_file(root, migrated))
    assert saved["screening_handoff"] is None and saved["screening_plan"] is None
    assert saved["screening_handoff_history"][-1] == {**before["screening_handoff"], "closure": closure}
    for key in ("workflow_id", "target_identity", "site_selection", "receptor_input", "preparation", "screening_receptor", "screening_inputs"):
        assert saved[key] == before[key]
    assert "compile_screening" not in migrated["workflow"]["tools"][RESEARCH]["actions"]
    assert len(list((root / "state/jobs").glob("*/spec.json"))) == 1
