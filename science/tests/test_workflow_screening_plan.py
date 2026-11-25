"""Real workflow/receptor chain, synthetic CSV and proposal; no compute."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.workflow_protocol import begin_operation
from covscreen.workflow_store import load
from test_library_workflow import call, state_file
from test_receptor_handoff import bound
from test_workflow_screening_inputs import registration
from covscreen_library_first import screening_inputs, screening_plan

REVIEW = {
    "construct_and_components": "Historical isolated-chain software fixture; removed ligands and waters limit interpretation of the pocket.",
    "reaction_scope": "Exploratory acrylamide Michael Addition hypothesis only; structural geometry does not validate cysteine reactivity.",
    "prefix_scope": "The first 20,000 file records are a deterministic pilot, not random sampling or whole-library coverage.",
}


def prepared(bound, valid=True):
    root, initial = bound
    source, _ = registration(root)
    if valid:
        source.write_text("SMILES,InChI_Key,Source,CompoundType\n" + "C,test-only,software-fixture,synthetic\n" * 20001)
    reviewed = call(root, initial, RESEARCH, "review_receptor")
    return root, call(root, reviewed, RESEARCH, "prepare_screening_inputs", profile=screening_inputs.PROFILE)


def test_plan_commits_review_and_operation_restores_exact_intent_and_archives_changes(bound):
    root, current = prepared(bound)
    before = sorted((root / "state/jobs").iterdir())
    planned = call(root, current, RESEARCH, "prepare_screening_plan", scientific_review=REVIEW, max_points=0.25)
    assert planned["workflow"]["stage"] == "SCREENING_PLAN_READY"
    assert planned["assessment"]["state"] == "not_evaluated"
    assert any(item["scope"] == "workflow_screening_handoff" for item in planned["blockers"])
    result = planned["result"]
    assert result["budget_status"] == "proposed_not_user_approved"
    assert result["screening_authorized"] is False and result["calculation_submitted"] is False
    assert str(root) not in json.dumps(planned)
    state = load(state_file(root, planned))
    intent = screening_plan.intent(root, state)
    assert intent["revision"] == planned["workflow"]["revision"]
    assert intent["scientific_review"] == REVIEW
    assert intent["screening_inputs"]["artifact_id"] == current["result"]["artifact_id"]
    assert intent["research_inputs"] == screening_inputs.load_record(root, state)["research_inputs"]
    assert len(intent["operation_id"]) == 32
    assert call(root, planned, RESEARCH, "read_screening_plan")["result"] == result
    repeated = call(root, planned, RESEARCH, "prepare_screening_plan", scientific_review=REVIEW, max_points=0.25)
    assert repeated["result"] == result
    assert screening_plan.intent(root, load(state_file(root, repeated))) == intent
    assert load(state_file(root, repeated))["pending"] is None
    changed = call(root, repeated, RESEARCH, "prepare_screening_plan", scientific_review=REVIEW, max_points=0.5)
    assert changed["result"]["operation_id"] != result["operation_id"]
    assert changed["branches"][-1]["screening_plan"]["id"] == result["artifact_id"]
    state = load(state_file(root, changed))
    other = deepcopy(state)
    other["workflow_id"] = "library-flow-" + "f" * 24
    with pytest.raises(ValueError, match="another workflow|differs"):
        screening_plan.intent(root, other)
    manifest = screening_plan.folder(root, state["screening_plan"]["id"]) / "screening-plan.json"
    saved = manifest.read_bytes()
    manifest.write_bytes(saved + b" ")
    with pytest.raises(ValueError, match="checksum mismatch"):
        call(root, changed, RESEARCH, "read_screening_plan")
    manifest.write_bytes(saved)
    revised = call(root, changed, ENTRY, "reconsider", return_to="structure", reason="Select another construct after reviewing pocket limitations")
    assert load(state_file(root, revised))["screening_plan"] is None
    assert revised["branches"][-1]["screening_plan"]["id"] == changed["result"]["artifact_id"]
    assert sorted((root / "state/jobs").iterdir()) == before


def test_plan_rejects_parser_incompatible_library_without_publishing_an_intent(bound):
    root, current = prepared(bound, valid=False)
    with pytest.raises(ValueError, match="missing columns"):
        call(root, current, RESEARCH, "prepare_screening_plan", scientific_review=REVIEW, max_points=1)
    state = load(state_file(root, current))
    assert state["screening_plan"] is None and state["pending"] is None
    assert not (root / "state/screening_plans").exists()


def test_uncommitted_local_plan_is_not_adopted_and_new_inputs_invalidate_committed_plan(bound):
    root, current = prepared(bound)
    folder = state_file(root, current)
    state = load(folder)
    begin_operation(folder, state, RESEARCH, "prepare_screening_plan")
    uncommitted = screening_plan.freeze(root, state, REVIEW, 1)
    # Emulate a process exit after immutable file commit, before workflow commit.
    restored = call(root, current, ENTRY, "reconcile")
    assert restored["result"]["partial_artifacts_adopted"] is False
    assert load(folder)["screening_plan"] is None
    planned = call(root, restored, RESEARCH, "prepare_screening_plan", scientific_review=REVIEW, max_points=1)
    assert planned["result"]["operation_id"] != uncommitted["operation_id"]
    source = root / "synthetic-source.csv"
    source.write_text(source.read_text() + "CC,test-extra,software-fixture,synthetic\n")
    updated = call(root, planned, RESEARCH, "prepare_screening_inputs", profile=screening_inputs.PROFILE)
    assert updated["workflow"]["stage"] == "SCREENING_PLAN"
    assert load(folder)["screening_plan"] is None
    assert updated["branches"][-1]["screening_plan"]["id"] == planned["result"]["artifact_id"]


@pytest.mark.parametrize("review,budget", [({}, 1), (dict(REVIEW, prefix_scope="yes"), 1),
    (dict(REVIEW, approved=True), 1), (REVIEW, True), (REVIEW, float("nan")), (REVIEW, float("inf")), (REVIEW, -1)])
def test_scientific_proposal_requires_specific_rationales_and_finite_budget(review, budget):
    with pytest.raises(ValueError):
        screening_plan.proposal(review, budget)
