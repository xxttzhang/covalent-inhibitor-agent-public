"""Receipt admission only; native worker tests cover actual selection science."""
from copy import deepcopy
import pytest
from covscreen import library_workflow  # Activate the canonical route package.
from covscreen_library_first.screening_results import verify, phase, binding, SelectionPolicy


def fixture():
    reference = {"schema": "library-workflow-execution-reference-v1", "workflow_id": "library-flow-" + "a" * 24,
        "revision": 7, "operation_id": "b" * 32, "intent_sha256": "c" * 64, "plan_sha256": "d" * 64}
    policy = {"max_candidates": 1, "max_fraction": 0.25, "disagreement_threshold": 0.5,
        "top_fraction": 0.2, "uncertainty_count": 1, "consensus_count": 0, "diversity_count": 0}
    selected = {"selection_id": "a" * 64, "selection_manifest_digest": "b" * 64, "source_manifest_digest": "c" * 64,
        "input_sha256": "d" * 64, "identity_sha256": "e" * 64, "policy": policy,
        "reason": "Software fixture for explicit candidate review", "status": "SUBSET_PROPOSED", "input_count": 5,
        "eligible_count": 4, "excluded_count": 1, "selected_count": 1, "effective_cap": 1,
        "candidates": [{"input_sdf_index": 1, "compound_id": "fixture", "selection_channel": "rank_disagreement_review",
            "carsidock_rank_fraction": 0, "rtmscore_rank_fraction": 1, "rank_disagreement": 1}],
        "calibrated_uncertainty": False, "refinement_approval_required": True,
        "limitations": ["Not calibrated uncertainty or evidence of activity"]}
    report = {"schema": "library-workflow-screening-result-v1", "reference": reference, "task_id": "screen-" + "f" * 24,
        "checked_at": 1000, "verification": "verified", "report_verified_at": 1000,
        "stages": [{"phase": name, "manifest_digest": "c" * 64, "input_count": 5, "success_count": 4,
                    "failed_count": 1, "artifact_count": 1} for name in ("prepare", "carsidock", "rtmscore", "report")],
        "selection": selected, "compute_submitted": False, "scientific_assessment": "not_evaluated"}
    observed = {"task_state": "SUCCEEDED", "approval_recorded": True, "task_id": report["task_id"], "reference": reference}
    arguments = {"action": "select_screening_candidates", "selection_policy": policy, "reason": selected["reason"]}
    return report, observed, arguments


def test_bound_report_preserves_denominators_exact_selection_and_zero_results():
    report, observed, args = fixture()
    accepted = verify(report, observed, {}, args)
    handoff = {"screening_result": accepted, "selection": accepted["selection"], "report_witness": accepted["stages"]}
    assert phase(handoff, "SCREENING_REVIEW") == "CANDIDATE_REVIEW"
    assert verify(report, observed, handoff, {"action": "read_screening_candidates"}) == report
    assert binding(report["selection"])["policy"] == args["selection_policy"]
    empty = deepcopy(report)
    empty["selection"].update(status="NO_CANDIDATES_MEET_POLICY", candidates=[], selected_count=0)
    assert verify(empty, observed, handoff, args)["selection"]["excluded_count"] == 1
    assert phase({"screening_result": empty}, "SCREENING_REVIEW") == "SCREENING_SELECTION_EMPTY"
    unavailable = {**report, "verification": "selection_unavailable", "selection": None}
    assert verify(unavailable, observed, handoff, {"action": "read_screening_candidates"}) == unavailable
    assert phase({"screening_result": unavailable}, "SCREENING_REVIEW") == "SCREENING_REVIEW"


@pytest.mark.parametrize("change", [
    {"task_id": "screen-" + "0" * 24}, {"reference": {}}, {"checked_at": 999}, {"compute_submitted": True},
    {"scientific_assessment": "passed"}, {"verification": "selection_unavailable"}, {"report_verified_at": None},
    {"stages": []}, {"selection": None}, {"path": "/inspire/unbound/file"},
])
def test_bad_result_envelopes_cannot_replace_bound_evidence(change):
    report, observed, args = fixture()
    with pytest.raises(ValueError):
        verify({**report, **change}, observed, {"screening_result": report, "selection": report["selection"]}, args)


@pytest.mark.parametrize("change", [
    {"source_manifest_digest": "f" * 64}, {"input_count": 4}, {"eligible_count": 5}, {"selected_count": 0},
    {"status": "NO_CANDIDATES_MEET_POLICY"}, {"calibrated_uncertainty": True}, {"refinement_approval_required": False},
    {"reason": "Different unsupported scientific justification"}, {"path": "/inspire/forged"},
])
def test_bad_selection_cannot_advance_candidates(change):
    report, observed, args = fixture()
    report["selection"] = {**report["selection"], **change}
    with pytest.raises(ValueError):
        verify(report, observed, {}, args)


def test_report_witness_and_selected_policy_cannot_be_silently_replaced():
    report, observed, args = fixture()
    handoff = {"report_witness": deepcopy(report["stages"]), "selection": deepcopy(report["selection"])}
    report["stages"][0]["manifest_digest"] = "f" * 64
    with pytest.raises(ValueError, match="bound report"):
        verify(report, observed, handoff, args)
    report, observed, args = fixture()
    report["selection"]["selection_id"] = "9" * 64
    with pytest.raises(ValueError, match="exact bound subset"):
        verify(report, observed, handoff, {"action": "read_screening_candidates"})
    with pytest.raises(ValueError):
        SelectionPolicy.model_validate({**args["selection_policy"], "consensus_count": 1})


def test_missing_subset_permits_explicit_policy_revision_only_while_core_report_is_verified():
    from covscreen_library_first.workflow import ASSETS, view, RESEARCH
    report, _, _ = fixture()
    state = {**dict.fromkeys(ASSETS), "pending": None, "branches": [], "workflow_id": "library-flow-" + "a" * 24,
        "revision": 10, "stage": "SCREENING_REVIEW", "screening_handoff": {"selection": report["selection"],
            "screening_result": {**report, "verification": "selection_unavailable", "selection": None}}}
    assert "select_screening_candidates" in view(state)["tools"][RESEARCH]["actions"]
    state["screening_handoff"]["screening_result"].update(verification="verification_unavailable", report_verified_at=None, stages=[])
    assert "select_screening_candidates" not in view(state)["tools"][RESEARCH]["actions"]
    assert "read_screening_candidates" in view(state)["tools"][RESEARCH]["actions"]
