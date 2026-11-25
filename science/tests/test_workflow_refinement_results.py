"""Signed report admission: software fixtures, no real scientific validation."""
from copy import deepcopy
import pytest
from covscreen.workflow_store import load
from covscreen_library_first.workflow import RESEARCH
from test_workflow_refinement_handoff import research, exchange
from test_workflow_refinement_execution import prepared, sync


def fixture(research, verdict="BOTH_STRUCTURAL_QC_PASSED"):
    observed = prepared(research)
    observed["execution_state"] = "completed"
    for stage in observed["stages"]:
        stage.update(state="SUCCEEDED", submission_reserved=True, job_observed=True, result_record_verified=True)
    sync(research, observed)
    state = load(research[1])
    selection = state["screening_handoff"]["selection"]
    engine = {"status": "SUCCESS", "score": 1.0, "quality": {"status": "PASSED", "reason": None}, "measurements": None}
    candidate = {**selection["candidates"][0], "status": verdict, "af3": deepcopy(engine), "covdock": deepcopy(engine)}
    if verdict in {"STRUCTURAL_QC_DISAGREEMENT", "BOTH_STRUCTURAL_QC_REJECTED"}:
        candidate["covdock"]["quality"] = {"status": "REJECTED", "reason": "fixture_rejected"}
    if verdict == "BOTH_STRUCTURAL_QC_REJECTED":
        candidate["af3"]["quality"] = {"status": "REJECTED", "reason": "fixture_rejected"}
    if verdict == "NOT_FULLY_EVALUATED":
        candidate["af3"].update(status="FAILED", score=None, quality={"status": "NOT_EVALUATED", "reason": None})
    report = {"report_digest": "1" * 64, "quality_policy_digest": "d" * 64,
        "input_identity_sha256": selection["identity_sha256"], "candidate_count": 1, "candidates": [candidate],
        "counts": {s: int(s == verdict) for s in ("BOTH_STRUCTURAL_QC_PASSED", "STRUCTURAL_QC_DISAGREEMENT", "BOTH_STRUCTURAL_QC_REJECTED", "NOT_FULLY_EVALUATED")},
        "stages": [{"engine": s["engine"], "task_id": s["task_id"], "plan_digest": "2" * 64,
            "outcome_digest": "3" * 64, "output_manifest_sha256": "4" * 64,
            "success_count": int(candidate[s["engine"]]["status"] == "SUCCESS"),
            "failed_count": int(candidate[s["engine"]]["status"] == "FAILED")} for s in observed["stages"]],
        "hits_confirmed": False, "affinity_validated": False, "scope": "bounded_structural_qc_not_affinity"}
    return {"schema": "library-workflow-refinement-result-v1", "reference": observed["reference"],
        "task_id": observed["task_id"], "checked_at": 1100, "ledger_sequence": observed["ledger_sequence"],
        "verification": "verified", "report": report, "compute_submitted": False,
        "scientific_assessment": "not_evaluated"}, observed


def report(research, value, observed):
    return exchange(research, "refinement_report", "refinement_report",
        payload={"refinement_report": value, "refinement_observation": observed})


def test_provisional_report_retains_unknown_and_rejects_contradictory_axes(research):
    from covscreen.refinement_quality import STRICT_POLICY, validate_policy
    from covscreen_library_first.refinement_results import verify
    value, observed = fixture(research)
    state = load(research[1])
    policy = validate_policy(STRICT_POLICY)
    state["refinement_handoff"]["receipt"]["proposal"]["quality_policy_digest"] = policy
    value["report"]["quality_policy_digest"] = policy
    row = value["report"]["candidates"][0]
    passed = {"version": "refinement-independent-axes-v1", "physical_validity": "PASS",
        "evidence_complete": True, "review_eligible": True, "missing_reasons": [],
        "failure_reasons": [], "calibration_status": "provisional"}
    row["af3"]["quality"]["assessment"] = deepcopy(passed)
    row["af3"]["measurements"] = {"engine": "af3", "mpae": 1., "iptm": .7, "ligand_plddt_mean": 75., "has_clash": False}
    row["covdock"]["quality"].update(status="REJECTED", reason="ligand_strain_missing",
        assessment={**passed, "physical_validity": "UNKNOWN", "evidence_complete": False,
                    "review_eligible": False, "missing_reasons": ["ligand_strain_missing"]})
    row["covdock"]["measurements"] = {"engine": "covdock", "covalent_link_present": True,
        "geometry_inspection_status": "SUCCESS", "target_atom_match_count": 1., "external_target_bond_count": 1.,
        "ligand_heavy_atom_count": 5., "covalent_bond_length_angstrom": 1.8,
        "minimum_nonbonded_heavy_atom_distance_angstrom": 2.5, "severe_clash": False,
        "strain_kcal_mol": None, "strain_provenance": {"source_field": "r_strain_Local_Strain_Energy",
            "unit": "kcal/mol", "definition": "vendor_local_strain_unmodified", "status": "MISSING"}}
    row["status"] = "NOT_FULLY_EVALUATED"
    value["report"]["counts"].update(BOTH_STRUCTURAL_QC_PASSED=0, NOT_FULLY_EVALUATED=1)
    verify(state, value, observed)
    for key, item in (("physical_validity", "FAIL"), ("evidence_complete", True), ("review_eligible", True)):
        changed = deepcopy(value)
        changed["report"]["candidates"][0]["covdock"]["quality"]["assessment"][key] = item
        with pytest.raises(ValueError, match="axes contradict"):
            verify(state, changed, observed)
    changed = deepcopy(value)
    changed["report"]["candidates"][0]["covdock"]["measurements"]["strain_provenance"]["status"] = "VALID"
    with pytest.raises(ValueError, match="Strain provenance"):
        verify(state, changed, observed)
    changed = deepcopy(value)
    changed["report"]["candidates"][0]["covdock"]["quality"].pop("assessment")
    with pytest.raises(ValueError, match="axes differ"):
        verify(state, changed, observed)

def publication(value):
    return {"schema": "refinement-publication-observation-v1", "workflow_id": value["reference"]["workflow_id"],
        "workflow_digest": value["reference"]["digest"], "task_id": value["task_id"], "checked_at": value["checked_at"],
        "state": "COMPLETE", "verification": "verified", "report_digest": value["report"]["report_digest"],
        "inventory_digest": "9" * 64, "files": [{"name": "report/candidate-results.csv", "artifact_id": "art_fixture",
            "version_id": "ver_fixture", "project_id": "fixture", "sha256": "8" * 64, "bytes": 100}],
        "compute_submitted": False, "platform_files_published": False, "md_ready": False}


def test_publication_observation_recovers_exact_versions_without_claiming_md_readiness(research):
    value, observed = fixture(research)
    value["delivery"] = publication(value)
    result = report(research, value, observed)
    assert result["refinement_result"]["delivery"] == value["delivery"]
    assert result["workflow"]["stage"] == "REFINEMENT_CANDIDATE_REVIEW"
    witness = load(research[1])["refinement_handoff"]["delivery_witness"]
    assert "checked_at" not in witness
    for verification in ("evidence_rejected", "verification_unavailable", "not_ready"):
        degraded = deepcopy(value)
        degraded["delivery"].update(verification=verification, files=[])
        assert report(research, degraded, observed)["refinement_result"]["delivery"]["files"] == []
        assert load(research[1])["refinement_handoff"]["delivery_witness"] == witness
    assert report(research, value, observed)["refinement_result"]["delivery"] == value["delivery"]


@pytest.mark.parametrize("state", [None, "NOT_STARTED", "WAITING", "ACTIVE", "PAUSED", "COMPLETE"])
def test_delivery_recovery_is_stage_scoped_and_commits_only_report_observations(research, state):
    value, observed = fixture(research)
    with pytest.raises(ValueError, match="unavailable"):
        exchange(research, "resume_refinement_delivery", "open")
    value["delivery"] = publication(value) if state else None
    if state and state != "COMPLETE":
        value["delivery"].update(state=state, verification="not_ready", files=[])
    result = report(research, value, observed)
    actions = result["workflow"]["tools"][RESEARCH]["actions"]
    if state in {None, "COMPLETE"}:
        assert "resume_refinement_delivery" not in actions
        with pytest.raises(ValueError, match="unavailable"):
            exchange(research, "resume_refinement_delivery", "open")
        return
    assert "resume_refinement_delivery" in actions
    before = load(research[1])
    exchange(research, "resume_refinement_delivery", "open")
    assert load(research[1]) == before
    with pytest.raises(ValueError, match="purpose mismatch"):
        exchange(research, "resume_refinement_delivery", "refinement_observe", payload={"refinement_observation": observed})
    assert load(research[1]) == before
    completed = {**value, "delivery": publication(value)}
    saved = exchange(research, "resume_refinement_delivery", "refinement_report",
        payload={"refinement_report": completed, "refinement_observation": observed})
    assert saved["refinement_result"]["delivery"]["verification"] == "verified"
    assert "resume_refinement_delivery" not in saved["workflow"]["tools"][RESEARCH]["actions"]
    assert saved["refinement_result"]["compute_submitted"] is False
    assert load(research[1])["refinement_handoff"]["planning_scope"] == before["refinement_handoff"]["planning_scope"]


@pytest.mark.parametrize("case", ["workflow", "digest", "parent", "time", "report", "project", "duplicate", "path",
    "unverified", "incomplete", "empty", "inventory", "md_ready", "compute", "publish", "bytes", "witness", "no_report"])
def test_publication_rejects_unbound_or_changed_versions_before_backend_commit(research, case):
    value, observed = fixture(research)
    value["delivery"] = publication(value)
    if case == "witness": report(research, value, observed)
    bad = deepcopy(value)
    delivery = bad["delivery"]
    if case == "workflow": delivery["workflow_id"] = "another"
    if case == "digest": delivery["workflow_digest"] = "f" * 64
    if case == "parent": delivery["task_id"] = "screen-" + "0" * 24
    if case == "time": delivery["checked_at"] += 1
    if case == "report": delivery["report_digest"] = "f" * 64
    if case == "project": delivery["files"][0]["project_id"] = "another"
    if case == "duplicate": delivery["files"].append(deepcopy(delivery["files"][0]))
    if case == "path": delivery["files"][0]["local_path"] = "/private/file"
    if case == "unverified": delivery["verification"] = "evidence_rejected"
    if case == "incomplete": delivery["state"] = "WAITING"
    if case == "empty": delivery["files"] = []
    if case == "inventory": delivery["inventory_digest"] = None
    if case == "md_ready": delivery["md_ready"] = True
    if case == "compute": delivery["compute_submitted"] = True
    if case == "publish": delivery["platform_files_published"] = True
    if case == "bytes": delivery["files"][0]["bytes"] = 1024 * 1024 * 1024
    if case == "witness": delivery["files"][0]["version_id"] = "ver_changed"
    if case == "no_report": bad.update(report=None, verification="verification_unavailable")
    before = load(research[1])
    with pytest.raises(ValueError): report(research, bad, observed)
    assert load(research[1]) == before


@pytest.mark.parametrize("verdict", ["BOTH_STRUCTURAL_QC_PASSED", "STRUCTURAL_QC_DISAGREEMENT", "BOTH_STRUCTURAL_QC_REJECTED", "NOT_FULLY_EVALUATED"])
def test_report_retains_all_verdicts_including_zero_passes_and_recovers_same_witness(research, verdict):
    value, observed = fixture(research, verdict)
    result = report(research, value, observed)
    assert result["workflow"]["stage"] == "REFINEMENT_CANDIDATE_REVIEW"
    assert result["refinement_result"]["report"] == value["report"]
    assert "reference" not in result["refinement_result"]
    assert result["assessment"]["state"] == "not_evaluated"
    assert result["refinement_result"]["report"]["counts"]["BOTH_STRUCTURAL_QC_PASSED"] == int(verdict == "BOTH_STRUCTURAL_QC_PASSED")
    assert "refinement_report" in result["workflow"]["tools"][RESEARCH]["actions"]
    assert report(research, value, observed)["workflow"] == result["workflow"]
    recovered = sync(research, {**observed, "observed_at": 1200, "ledger_sequence": 21}, "recover_refinement")
    assert recovered["workflow"]["stage"] == "REFINEMENT_CANDIDATE_REVIEW"
    unavailable = {**value, "ledger_sequence": 21, "checked_at": 1300, "verification": "verification_unavailable", "report": None}
    degraded = report(research, unavailable, {**observed, "observed_at": 1200, "ledger_sequence": 21})
    assert degraded["workflow"]["stage"] == "REFINEMENT_REVIEW"
    assert degraded["refinement_result"]["report"] is None
    assert load(research[1])["refinement_handoff"]["report_witness"] == value["report"]
    restored = report(research, {**value, "ledger_sequence": 21, "checked_at": 1400},
        {**observed, "observed_at": 1200, "ledger_sequence": 21})
    assert restored["workflow"]["stage"] == "REFINEMENT_CANDIDATE_REVIEW"


@pytest.mark.parametrize("case", ["workflow", "parent", "sequence", "time", "candidate", "policy", "identity", "stage", "count", "coverage", "verdict", "score", "quality", "measurement", "path", "affinity", "witness", "unverified"])
def test_report_rejects_wrong_or_stale_evidence_without_partial_backend_commit(research, case):
    value, observed = fixture(research)
    # First-time malformed receipts must fail before the new ledger observation saves.
    if case in {"time", "witness"}:
        report(research, value, observed)
    bad = deepcopy(value)
    if case == "workflow": bad["reference"]["digest"] = "f" * 64
    if case == "parent": bad["task_id"] = "screen-" + "0" * 24
    if case == "sequence": bad["ledger_sequence"] += 1
    if case == "time": bad["checked_at"] = 1099
    if case == "candidate": bad["report"]["candidates"][0]["compound_id"] = "another"
    if case == "policy": bad["report"]["quality_policy_digest"] = "f" * 64
    if case == "identity": bad["report"]["input_identity_sha256"] = "f" * 64
    if case == "stage": bad["report"]["stages"].reverse()
    if case == "count": bad["report"]["counts"]["BOTH_STRUCTURAL_QC_PASSED"] = 0
    if case == "coverage": bad["report"]["stages"][0]["failed_count"] = 1
    if case == "verdict": bad["report"]["candidates"][0]["covdock"]["quality"] = {"status": "REJECTED", "reason": "fixture"}
    if case == "score": bad["report"]["candidates"][0]["af3"]["score"] = None
    if case == "quality": bad["report"]["candidates"][0]["af3"]["quality"]["reason"] = "invalid_pass"
    if case == "measurement": bad["report"]["candidates"][0]["covdock"]["measurements"] = {"engine": "af3", "mpae": 1.0, "iptm": 0.8, "ligand_plddt_mean": 80.0, "has_clash": False}
    if case == "path": bad["report"]["stages"][0]["remote_path"] = "/not-public"
    if case == "affinity": bad["report"]["affinity_validated"] = True
    if case == "witness": bad["report"]["report_digest"] = "f" * 64
    if case == "unverified": bad["verification"] = "evidence_rejected"
    before = load(research[1])
    with pytest.raises(ValueError):
        report(research, bad, observed)
    assert load(research[1]) == before
