"""Native ledger receipt admission, with no engine or scheduler invocation."""
from copy import deepcopy
import pytest
from covscreen.workflow_store import load
from covscreen_library_first.workflow import RESEARCH
from test_workflow_refinement_handoff import research, exchange, receipt


def prepared(research):
    exchange(research)
    planned = receipt(research)
    exchange(research, "recover_refinement", "refinement_plan", payload={"refinement_result": planned})
    return {"schema": "library-refinement-ledger-observation-v1", "reference": {
        "planning_scope": planned["planning_scope"], **{k: planned["draft"][k] for k in ("workflow_id", "digest")}},
        "task_id": planned["task_id"], "native_state": "APPROVED", "approval_recorded": True,
        "execution_state": "not_submitted", "stages": [{"task_id": "refine-" + c * 24, "engine": engine,
            "state": "APPROVED", "submission_reserved": False, "job_observed": False, "result_record_verified": False}
            for c, engine in (("a", "af3"), ("b", "covdock"))],
        "ledger_sequence": 20, "observed_at": 1000, "observation_scope": "persisted_executor_ledger",
        "remote_artifacts_rehashed": False, "scientific_assessment": "not_evaluated"}


def sync(research, value, action="refinement_status"):
    return exchange(research, action, "refinement_observe", payload={"refinement_observation": value})


def test_approved_refinement_recovers_exact_native_identity_and_never_releases_or_replans(research):
    value = prepared(research)
    approved = sync(research, value, "approve_refinement")
    assert approved["workflow"]["stage"] == "REFINEMENT_EXECUTION"
    assert approved["refinement_plan"]["approval_recorded"] is True
    assert approved["refinement_observation"]["remote_artifacts_rehashed"] is False
    assert "reference" not in approved["refinement_observation"]
    assert "approve_refinement" not in approved["workflow"]["tools"][RESEARCH]["actions"]
    assert "release_refinement" not in approved["workflow"]["tools"][RESEARCH]["actions"]
    assert sync(research, value, "recover_refinement")["workflow"] == approved["workflow"]
    for operation, payload in (("refinement_plan", {"refinement_result": receipt(research)}),
        ("refinement_release", {"refinement_closure": {}})):
        with pytest.raises(ValueError):
            exchange(research, "recover_refinement", operation, payload=payload)
    active = deepcopy(value)
    active.update(ledger_sequence=21, execution_state="running")
    active["stages"][0].update(state="RUNNING", submission_reserved=True, job_observed=True)
    assert sync(research, active)["execution"]["state"] == "running"
    requested = deepcopy(active)
    requested.update(ledger_sequence=22, execution_state="outcome_unknown")
    for stage in requested["stages"]:
        stage["state"] = "CANCEL_REQUESTED"
    assert sync(research, requested)["workflow"]["stage"] == "REFINEMENT_EXECUTION"
    stopped = deepcopy(requested)
    stopped.update(ledger_sequence=23, execution_state="failed")
    for stage in stopped["stages"]:
        stage["state"] = "CANCELLED"
    assert sync(research, stopped)["workflow"]["stage"] == "REFINEMENT_STOPPED"
    with pytest.raises(ValueError, match="terminal"):
        sync(research, {**active, "ledger_sequence": 24})


def test_completed_native_records_advance_to_review_without_scientific_acceptance(research):
    value = prepared(research)
    value.update(execution_state="completed")
    for stage in value["stages"]:
        stage.update(state="SUCCEEDED", submission_reserved=True, job_observed=True, result_record_verified=True)
    result = sync(research, value)
    assert result["workflow"]["stage"] == "REFINEMENT_REVIEW"
    assert result["assessment"]["state"] == "not_evaluated"
    assert result["execution"]["task_ids"] == [s["task_id"] for s in value["stages"]]
    assert result["refinement_plan"]["compute_submitted"] is True


def test_uncertain_dispatch_is_not_reported_as_a_confirmed_job(research):
    value = prepared(research)
    value.update(execution_state="outcome_unknown")
    value["stages"][0].update(state="DISPATCHING", submission_reserved=True)
    result = sync(research, value)
    assert result["refinement_plan"]["compute_submitted"] is None
    assert result["workflow"]["stage"] == "REFINEMENT_EXECUTION"
    assert result["refinement_observation"]["stages"][0]["job_observed"] is False


def test_cancelled_unapproved_confirmation_can_release_but_not_return_to_draft(research):
    value = prepared(research)
    value.update(native_state="DRAFT", approval_recorded=False)
    for stage in value["stages"]:
        stage["state"] = "DRAFT"
    assert sync(research, value)["workflow"]["stage"] == "REFINEMENT_APPROVAL_PENDING"
    cancelled = deepcopy(value)
    cancelled.update(native_state="CANCELLED", ledger_sequence=21)
    for stage in cancelled["stages"]:
        stage["state"] = "CANCELLED"
    result = sync(research, cancelled, "cancel_refinement")
    assert "release_refinement" in result["workflow"]["tools"][RESEARCH]["actions"]
    with pytest.raises(ValueError):
        sync(research, {**value, "ledger_sequence": 22})


@pytest.mark.parametrize("case", ["identity", "grant", "state", "stages", "unapproved_run", "result", "remote", "science", "stale", "rollback", "foreign_member"])
def test_invalid_ledger_observation_does_not_mutate_backend(research, case):
    value = prepared(research)
    sync(research, value)
    bad = deepcopy(value)
    bad["ledger_sequence"] += 1
    if case == "identity": bad["reference"]["digest"] = "f" * 64
    if case == "grant": bad["approval_recorded"] = False
    if case == "state": bad["execution_state"] = "completed"
    if case == "stages": bad["stages"].reverse()
    if case == "unapproved_run": bad["stages"][0]["state"] = "RUNNING"
    if case == "result": bad["stages"][0]["result_record_verified"] = True
    if case == "remote": bad["remote_artifacts_rehashed"] = True
    if case == "science": bad["scientific_assessment"] = "passed"
    if case == "stale": bad["ledger_sequence"] = 19
    if case == "rollback":
        bad.update(native_state="NOT_CREATED", approval_recorded=False)
        for stage in bad["stages"]: stage["state"] = "NOT_CREATED"
    if case == "foreign_member": bad["stages"][0]["task_id"] = "refine-" + "f" * 24
    before = load(research[1])
    with pytest.raises(ValueError): sync(research, bad)
    assert load(research[1]) == before
