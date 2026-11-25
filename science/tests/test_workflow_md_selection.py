"""Actual signed workflow decisions over explicit software publication fixtures."""
from copy import deepcopy
import pytest

from covscreen.workflow_store import load, save
from covscreen.library_workflow import RESEARCH
from covscreen_library_first.workflow import response
from covscreen_library_first.md_selection import TABLE, STRUCTURE
from test_workflow_refinement_handoff import research, exchange
from test_workflow_refinement_results import fixture, publication, report


def ready(research, verdict="BOTH_STRUCTURAL_QC_PASSED"):
    value, observed = fixture(research, verdict)
    state = load(research[1])
    state["preparation"] = {"run_id": "software-preparation", "state": "COMPLETED", "manifest_sha256": "a" * 64}
    state["receptor_context"] = {"id": "receptor-context-" + "b" * 32, "sha256": "c" * 64}
    save(research[1], state)
    value["delivery"] = publication(value)
    value["delivery"]["files"].append({"name": STRUCTURE, "artifact_id": "art_structure", "version_id": "ver_structure",
        "project_id": "fixture", "sha256": "7" * 64, "bytes": 100})
    report(research, value, observed)
    candidate = value["report"]["candidates"][0]
    args = {"compound_id": candidate["compound_id"], "input_sdf_index": candidate["input_sdf_index"],
        "reason": "Evaluate this explicitly reviewed structural candidate with subsequent MD preparation"}
    return value, observed, args


def choose(research, value, args):
    return exchange(research, "select_md_candidate", "md_candidate_select", values=args,
        payload={"refinement_report": value})


def test_choice_binds_native_files_and_receptor_then_releases_into_existing_branch_history(research):
    value, observed, args = ready(research)
    opened = exchange(research, "select_md_candidate", "open", values=args)
    assert "select_md_candidate" in opened["workflow"]["tools"][RESEARCH]["actions"]
    # Host refreshes evidence before its separate choice commit.
    fresh = {**value, "checked_at": 1200}
    exchange(research, "select_md_candidate", "refinement_report", values=args,
        payload={"refinement_report": fresh, "refinement_observation": observed})
    before = load(research[1])
    chosen = choose(research, fresh, args)
    current = load(research[1])
    choice = current["md_candidate"]
    assert choice["files"]["candidate_table"]["name"] == TABLE
    assert choice["files"]["covdock_structure"] == fresh["delivery"]["files"][1]
    assert choice["parents"]["prepared_receptor_manifest_sha256"] == before["preparation"]["manifest_sha256"]
    assert choice["native_workflow_digest"] == fresh["reference"]["digest"]
    assert chosen["md_candidate"]["state"] == "selected_asset_handoff_pending"
    assert chosen["md_candidate"]["md_ready"] is False
    assert "files" not in chosen["md_candidate"] and "parents" not in chosen["md_candidate"]
    assert "select_md_candidate" not in chosen["workflow"]["tools"][RESEARCH]["actions"]
    assert "release_md_candidate" in chosen["workflow"]["tools"][RESEARCH]["actions"]
    assert current["refinement_handoff"] == before["refinement_handoff"]
    assert current["screening_handoff"] == before["screening_handoff"]
    assert choose(research, fresh, args)["workflow"]["revision"] == current["revision"]
    with pytest.raises(ValueError, match="already bound"):
        choose(research, fresh, {**args, "reason": "Another rationale must not silently overwrite the original research decision"})
    assert load(research[1]) == current
    released = exchange(research, "release_md_candidate", "md_candidate_release", values={"reason": "Reconsider the candidate after reviewing the structural tradeoffs"})
    assert released["md_candidate"] is None
    assert released["branches"][-1]["md_candidate"] == choice
    assert load(research[1])["refinement_handoff"] == current["refinement_handoff"]
    assert choose(research, fresh, args)["md_candidate"]["id"] != choice["id"]


def test_changed_evidence_is_retained_before_selection_refusal_and_existing_choice_recovers(research):
    value, observed, args = ready(research)
    chosen = choose(research, value, args)
    frozen = deepcopy(load(research[1])["md_candidate"])
    degraded = {**value, "checked_at": 1200, "delivery": {**value["delivery"], "verification": "evidence_rejected", "files": []}}
    current = report(research, degraded, observed)
    assert current["md_candidate"]["state"] == "selected_evidence_reverification_required"
    with pytest.raises(ValueError, match="freshly verified"):
        choose(research, value, args)
    assert load(research[1])["refinement_handoff"]["report_result"] == degraded
    assert load(research[1])["md_candidate"] == frozen
    restored = report(research, {**value, "checked_at": 1300}, observed)
    assert restored["md_candidate"] == chosen["md_candidate"]
    unavailable = {**value, "checked_at": 1400, "verification": "verification_unavailable", "report": None, "delivery": None}
    assert report(research, unavailable, observed)["md_candidate"]["state"] == "selected_evidence_reverification_required"
    assert load(research[1])["md_candidate"] == frozen


@pytest.mark.parametrize("verdict", ["STRUCTURAL_QC_DISAGREEMENT", "BOTH_STRUCTURAL_QC_REJECTED", "NOT_FULLY_EVALUATED"])
def test_zero_eligible_candidates_never_gain_a_selection_action(research, verdict):
    value, observed, args = ready(research, verdict)
    state = load(research[1])
    assert "select_md_candidate" not in response(state)["workflow"]["tools"][RESEARCH]["actions"]
    with pytest.raises(ValueError): choose(research, value, args)
    assert load(research[1]) == state


@pytest.mark.parametrize("case", ["compound", "index", "reason", "boolean_index", "raw_path", "stale_report", "missing_table", "missing_structure", "preparation", "context"])
def test_invalid_choice_does_not_mutate_backend_state(research, case):
    value, observed, args = ready(research)
    state = load(research[1])
    if case == "compound": args["compound_id"] = "unknown"
    if case == "index": args["input_sdf_index"] += 1
    if case == "reason": args["reason"] = "short"
    if case == "boolean_index": args["input_sdf_index"] = True
    if case == "raw_path": args["candidate_results_path"] = "/untrusted/table.csv"
    if case == "stale_report": value["checked_at"] -= 1
    if case == "missing_table": state["refinement_handoff"]["report_result"]["delivery"]["files"] = value["delivery"]["files"][1:]
    if case == "missing_structure": state["refinement_handoff"]["report_result"]["delivery"]["files"] = value["delivery"]["files"][:1]
    if case == "preparation": state["preparation"]["state"] = "RUNNING"
    if case == "context": state["receptor_context"] = None
    save(research[1], state)
    with pytest.raises(ValueError): choose(research, value, args)
    assert load(research[1]) == state


def test_parent_change_invalidates_choice_and_downstream_inputs_prevent_release(research):
    value, observed, args = ready(research)
    choose(research, value, args)
    state = load(research[1])
    state["receptor_context"]["sha256"] = "f" * 64
    state["md_input"] = {"artifact_id": "candidate-pose-input-" + "d" * 32, "manifest_sha256": "e" * 64,
        "fixture": "A later frozen input must remain bound"}
    save(research[1], state)
    current = response(state)
    assert current["md_candidate"]["state"] == "selected_evidence_reverification_required"
    assert "release_md_candidate" not in current["workflow"]["tools"][RESEARCH]["actions"]
    with pytest.raises(ValueError, match="already bind"):
        exchange(research, "release_md_candidate", "md_candidate_release", values={"reason": "Cannot silently release downstream inputs"})
    assert load(research[1]) == state
