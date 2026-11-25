import json
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from covscreen.ligand_workflow import ENTRY, EVIDENCE, DESIGN, directory, navigate, step
from covscreen.ligand_parent_evidence import branch, seal
from covscreen.target_hypothesis import folder, digest
from test_ligand_parent_evidence import dataset


@pytest.fixture
def case(tmp_path):
    evidence, comparison = dataset()
    identifier = "hyp-" + "a" * 24
    out = folder(tmp_path, identifier)
    out.mkdir(parents=True)
    seal(identifier, out, evidence)
    hypothesis = {"route": "ligand-first", "rationale": "Synthetic contract fixture, not scientific validation",
        "claims": [{"statement": "Fixture parent comparison", "evidence_ids": ["PMID:1"], "interpretation": "inference"}],
        "parent_ligand": "ParentAlpha", "parent_binding_mode": "noncovalent", "parent_evidence_ids": ["PMID:1"],
        "warhead_class": ["acrylamide"], "uncertainties": ["No derivatization feasibility established"],
        "parent_comparison": comparison}
    result = navigate(tmp_path, "start", request_id="case-one", target="TEST", site="C1", taxon=9606,
                      evidence_id=identifier)
    return tmp_path, result, hypothesis


def act(root, state, tool, action, **arguments):
    return step(root, tool, workflow_id=state["workflow"]["workflow_id"],
                expected_revision=state["workflow"]["revision"], action=action, **arguments)


def test_only_entry_before_evidence_and_start_is_idempotent(tmp_path):
    args = dict(request_id="original", target="EGFR", site="C797", taxon=9606)
    result = navigate(tmp_path, "start", **args)
    assert result["workflow"]["stage"] == "INTAKE"
    assert result["workflow"]["tools"][EVIDENCE]["actions"] == ["collect"]
    assert DESIGN not in result["workflow"]["tools"]
    assert navigate(tmp_path, "start", **args) == result
    with pytest.raises(ValueError, match="different scientific"):
        navigate(tmp_path, "start", **{**args, "site": "C800"})


def test_pair_deferral_disables_repeat_prepare_and_survives_reload(case, monkeypatch):
    from covscreen import ligand_design
    root, state, hypothesis = case
    ready = act(root, state, EVIDENCE, 'finalize', hypothesis=hypothesis)
    assessment = {'status': 'EVIDENCE_INSUFFICIENT',
                  'reasons': ['cys_connection_requires_review'], 'execution_ready': False}
    monkeypatch.setattr(ligand_design, 'prepare', lambda *a, **kw: {
        'state': 'NEEDS_PARENT_PAIR_REVIEW', 'parent_cys_assessment': assessment,
        'compute_submitted': False})
    deferred = act(root, ready, DESIGN, 'prepare')
    assert deferred['workflow']['stage'] == 'NEEDS_PARENT_PAIR_REVIEW'
    assert DESIGN not in deferred['workflow']['tools']
    assert 'export_parent_report' in deferred['workflow']['tools'][EVIDENCE]['actions']
    with pytest.raises(ValueError, match='unavailable'):
        act(root, deferred, DESIGN, 'prepare')
    reloaded = navigate(root, 'status', workflow_id=deferred['workflow']['workflow_id'])
    assert reloaded['parent_pair_review'] == assessment
    reopened = navigate(root, 'reconsider', workflow_id=deferred['workflow']['workflow_id'],
        expected_revision=deferred['workflow']['revision'], reason='Review an alternative unoccupied parent site')
    assert reopened['parent_pair_review'] is None
    assert reopened['branches'][-1]['parent_pair_review'] == assessment


@pytest.mark.parametrize('argument', ['pdb_id', 'molecule_id', 'query'])
def test_supplement_error_identifies_valid_fields_without_mutating(case, argument):
    root, state, _ = case
    before = (directory(root, state['workflow']['workflow_id']) / 'state.json').read_bytes()
    with pytest.raises(ValueError, match='Allowed action fields: source_ids'):
        act(root, state, EVIDENCE, 'supplement', **{argument: 'invalid-field'})
    assert (directory(root, state['workflow']['workflow_id']) / 'state.json').read_bytes() == before


def test_verified_parent_opens_design_and_reselection_retains_old_proposal(case):
    root, state, hypothesis = case
    first = act(root, state, EVIDENCE, "finalize", hypothesis=hypothesis)
    assert first["workflow"]["stage"] == "DESIGN_READY"
    assert first["workflow"]["tools"][DESIGN]["actions"] == ["prepare"]
    assert "finalize" not in first["workflow"]["tools"][EVIDENCE]["actions"]
    assert "supplement" in first["workflow"]["tools"][EVIDENCE]["actions"]
    original = first["proposal"]
    path = folder(root, first["evidence_id"]) / original["proposal_id"] / "hypothesis.json"
    before = digest(path)
    reopened = navigate(root, "reconsider", workflow_id=first["workflow"]["workflow_id"],
        expected_revision=first["workflow"]["revision"], reason="Recompare the other parent after reviewing scientific uncertainty")
    assert DESIGN not in reopened["workflow"]["tools"]
    assert reopened["branches"][0]["proposal"] == original
    assert digest(path) == before
    history = navigate(root, "inspect_branch", workflow_id=reopened["workflow"]["workflow_id"],
        expected_revision=reopened["workflow"]["revision"], branch_index=0)
    assert history["result"]["historical_branch"] and not history["result"]["active_handoff_restored"]
    assert history["workflow"]["stage"] == "PARENT_REVIEW"
    hypothesis["parent_ligand"] = "ParentBeta"
    hypothesis["parent_evidence_ids"] = ["PMID:2"]
    hypothesis["parent_comparison"]["selected_molecule_id"] = "ChEMBL:CHEMBL2"
    for row in hypothesis["parent_comparison"]["candidates"]:
        row["disposition"] = "priority_for_geometry" if row["molecule_id"] == "ChEMBL:CHEMBL2" else "backup"
    second = act(root, reopened, EVIDENCE, "finalize", hypothesis=hypothesis)
    assert second["workflow"]["stage"] == "DESIGN_READY"
    assert second["proposal"]["proposal_id"] != original["proposal_id"]
    assert digest(path) == before


@pytest.mark.parametrize("omitted", [
    ["parent_binding_mode", "parent_evidence_ids"],
    ["parent_ligand", "parent_binding_mode", "parent_evidence_ids"],
])
def test_missing_parent_fields_are_actionable_and_can_be_completed(case, omitted):
    root, state, hypothesis = case
    incomplete = {key: value for key, value in hypothesis.items() if key not in omitted}
    blocked = act(root, state, EVIDENCE, "finalize", hypothesis=incomplete)
    assert blocked["result"]["state"] == "NEEDS_INPUTS"
    assert "parent_evidence_ids" in blocked["result"]["missing"]
    assert "hypothesis.parent_binding_mode" in blocked["result"]["next_action"]
    assert blocked["result"].get("design_handoff") is None
    assert blocked["workflow"]["stage"] == "PARENT_REVIEW"
    assert DESIGN not in blocked["workflow"]["tools"]
    assert "finalize" in blocked["workflow"]["tools"][EVIDENCE]["actions"]
    report = Path(blocked["result"]["artifacts"]["hypothesis"])
    before = digest(report)
    ready = act(root, blocked, EVIDENCE, "finalize", hypothesis=hypothesis)
    assert ready["workflow"]["stage"] == "DESIGN_READY"
    assert digest(report) == before


def test_scientific_rejection_keeps_evidence_tools_and_never_opens_design(case):
    root, state, hypothesis = case
    hypothesis["parent_comparison"]["candidates"][0]["binding_evidence"][0]["quote"] = "Invented quotation that is absent from the saved source"
    rejected = act(root, state, EVIDENCE, "finalize", hypothesis=hypothesis)
    assert rejected["workflow"]["stage"] == "PARENT_REVIEW"
    assert DESIGN not in rejected["workflow"]["tools"]
    assert "search_parent_literature" in rejected["workflow"]["tools"][EVIDENCE]["actions"]


def test_external_supplement_invalidates_design_without_erasing_history(case):
    root, state, hypothesis = case
    ready = act(root, state, EVIDENCE, "finalize", hypothesis=hypothesis)
    identifier, out, data = branch(root, ready["evidence_id"])
    data["supplemental_sources"]["PMID:3"] = {"kind": "literature", "title": "Additional synthetic fixture"}
    seal(identifier, out, data)
    current = navigate(root, "status", workflow_id=ready["workflow"]["workflow_id"])
    assert current["evidence_id"] == identifier
    assert current["workflow"]["stage"] == "PARENT_REVIEW"
    assert current["proposal"] is None and current["branches"]
    with pytest.raises(ValueError, match="STALE_WORKFLOW"):
        act(root, ready, DESIGN, "prepare")


def test_workflow_guard_rejects_skips_identity_override_and_cross_route(case):
    root, state, hypothesis = case
    with pytest.raises(ValueError, match="unavailable"):
        act(root, state, DESIGN, "generate")
    with pytest.raises(ValueError, match="unexpected evidence"):
        act(root, state, EVIDENCE, "read", evidence_id="hyp-" + "b" * 24)
    hypothesis["route"] = "library-first"
    with pytest.raises(ValueError, match="only accepts Ligand-first"):
        act(root, state, EVIDENCE, "finalize", hypothesis=hypothesis)


def test_invalid_scientific_input_can_be_corrected_without_restarting(case):
    root, state, hypothesis = case
    hypothesis["claims"][0]["evidence_ids"] = ["PMID:999"]
    with pytest.raises(ValueError, match="Unknown evidence"):
        act(root, state, EVIDENCE, "finalize", hypothesis=hypothesis)
    current = navigate(root, "status", workflow_id=state["workflow"]["workflow_id"])
    assert current["pending"] is None
    assert current["workflow"]["stage"] == "PARENT_REVIEW"
    hypothesis["claims"][0]["evidence_ids"] = ["PMID:1"]
    assert act(root, current, EVIDENCE, "finalize", hypothesis=hypothesis)["workflow"]["stage"] == "DESIGN_READY"


def test_parallel_revisions_have_one_winner_and_workflows_are_isolated(case):
    root, state, hypothesis = case
    def run():
        try:
            return act(root, state, EVIDENCE, "finalize", hypothesis=hypothesis)["workflow"]["stage"]
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: run(), range(2)))
    assert outcomes.count("DESIGN_READY") == 1
    assert any("STALE_WORKFLOW" in outcome for outcome in outcomes)
    other = navigate(root, "start", request_id="another", target="TEST", site="C1", taxon=9606,
                     evidence_id=state["evidence_id"])
    assert other["workflow"]["stage"] == "PARENT_REVIEW"
    assert other["workflow"]["workflow_id"] != state["workflow"]["workflow_id"]


def test_observational_reads_do_not_invalidate_an_unchanged_parent_comparison(case):
    root, state, hypothesis = case
    with ThreadPoolExecutor(max_workers=2) as pool:
        reads = list(pool.map(lambda action: act(root, state, EVIDENCE, action), ["read", "read_parent_evidence"]))
    assert all(read["workflow"]["revision"] == state["workflow"]["revision"] for read in reads)
    ready = act(root, state, EVIDENCE, "finalize", hypothesis=hypothesis)
    assert ready["workflow"]["stage"] == "DESIGN_READY"


def test_pending_collection_is_not_replayed(case):
    root, current, _ = case
    path = directory(root, current["workflow"]["workflow_id"]) / "state.json"
    state = json.loads(path.read_text())
    state["pending"] = {"tool": EVIDENCE, "action": "collect", "operation_id": "interrupted-test"}
    path.write_text(json.dumps(state))
    observed = navigate(root, "status", workflow_id=state["workflow_id"])
    assert set(observed["workflow"]["tools"]) == {ENTRY}
    assert observed["workflow"]["tools"][ENTRY]["actions"] == ["status"]
    with pytest.raises(ValueError, match="reconciliation"):
        navigate(root, "reconsider", workflow_id=state["workflow_id"], expected_revision=state["revision"], reason="Do not replay this uncertain launch")


@pytest.fixture
def target_artifact(tmp_path):
    source = Path(__file__).parent / "fixtures/target_identity"
    out = tmp_path / "state/target_identity" / ("a" * 32)
    out.mkdir(parents=True)
    for source_name, name in [("egfr-manifest.json", "target_identity.json"),
                              ("egfr.json", "uniprot.json"), ("P00533.fasta", "P00533.fasta")]:
        shutil.copyfile(source / source_name, out / name)
    return {"target_artifact_id": "target-" + "a" * 32,
            "target_manifest_sha256": digest(out / "target_identity.json")}


def test_unspecified_site_uses_verified_identity_before_exploratory_choice(tmp_path, target_artifact):
    arguments = dict(request_id="target-only", target="EGFR", taxon=9606)
    state = navigate(tmp_path, "start", **arguments)
    assert state["target"]["site"] is None
    assert state["workflow"]["stage"] == "TARGET_REVIEW"
    assert state["workflow"]["tools"][EVIDENCE]["actions"] == ["resolve_target"]
    assert "select_site" not in state["workflow"]["tools"][ENTRY]["actions"]
    with pytest.raises(ValueError, match="unavailable"):
        act(tmp_path, state, EVIDENCE, "collect")
    verified = act(tmp_path, state, EVIDENCE, "resolve_target", **target_artifact)
    assert verified["result"]["verified_identity"]["identity"]["unique_verified_target"] == "P00533"
    assert "select_site" in verified["workflow"]["tools"][ENTRY]["actions"]
    indexed = act(tmp_path, verified, EVIDENCE, "search_parent_index", limit=2)
    assert indexed["result"]["uniprot_id"] == "P00533"
    assert indexed["result"]["total_matches"] == 69
    assert indexed["workflow"]["revision"] == verified["workflow"]["revision"]
    assert indexed["target"]["site"] is None
    with pytest.raises(ValueError, match="unexpected evidence arguments"):
        act(tmp_path, verified, EVIDENCE, "search_parent_index", uniprot_id="P04626")
    selection = dict(workflow_id=verified["workflow"]["workflow_id"],
        expected_revision=verified["workflow"]["revision"], reason="Explore this canonical cysteine; binding and reactivity still require evidence")
    with pytest.raises(ValueError, match="not a cysteine"):
        navigate(tmp_path, "select_site", site="C1", **selection)
    selected = navigate(tmp_path, "select_site", site="C797", **selection)
    assert selected["workflow"]["stage"] == "INTAKE"
    assert selected["site_selection"]["interpretation"] == "exploratory_hypothesis"
    assert selected["site_selection"]["reactivity_validated"] is False
    assert selected["workflow"]["tools"][EVIDENCE]["actions"] == ["collect"]
    assert navigate(tmp_path, "start", **arguments) == selected
    with pytest.raises(ValueError, match="unavailable"):
        navigate(tmp_path, "revise_target", workflow_id=selected["workflow"]["workflow_id"],
            expected_revision=selected["workflow"]["revision"], target="BTK", taxon=9606, reason="Cannot rewrite a bound site")


def test_target_correction_is_explicit_and_cross_target_artifacts_fail(tmp_path, target_artifact):
    state = navigate(tmp_path, "start", request_id="typo", target="EFGR", taxon=9606)
    with pytest.raises(ValueError, match="another gene"):
        act(tmp_path, state, EVIDENCE, "resolve_target", **target_artifact)
    state = navigate(tmp_path, "status", workflow_id=state["workflow"]["workflow_id"])
    assert state["target_identity"] is None
    fixed = navigate(tmp_path, "revise_target", workflow_id=state["workflow"]["workflow_id"],
        expected_revision=state["workflow"]["revision"], target="EGFR", taxon=9606,
        reason="Explicit correction of the transposed gene symbol; verify the corrected identity")
    verified = act(tmp_path, fixed, EVIDENCE, "resolve_target", **target_artifact)
    assert verified["target"]["target"] == "EGFR"
    stored = json.loads((directory(tmp_path, state["workflow"]["workflow_id"]) / "state.json").read_text())
    assert stored["initial_target"]["target"] == "EFGR"
    assert any(event["operation"] == "revise_target" for event in stored["events"])


def test_site_selection_rechecks_source_bytes_and_reconsider_preserves_target_stage(tmp_path, target_artifact):
    state = navigate(tmp_path, "start", request_id="corruption", target="EGFR", taxon=9606)
    state = act(tmp_path, state, EVIDENCE, "resolve_target", **target_artifact)
    fasta = tmp_path / "state/target_identity" / ("a" * 32) / "P00533.fasta"
    fasta.write_text("corrupted")
    with pytest.raises(ValueError, match="FASTA differs"):
        navigate(tmp_path, "select_site", workflow_id=state["workflow"]["workflow_id"],
            expected_revision=state["workflow"]["revision"], site="C797", reason="Cannot use changed identity evidence")
    again = navigate(tmp_path, "reconsider", workflow_id=state["workflow"]["workflow_id"],
        expected_revision=state["workflow"]["revision"], reason="Still need an evidence supported site hypothesis")
    assert again["workflow"]["stage"] == "TARGET_REVIEW"
    assert "collect" not in again["workflow"]["tools"][EVIDENCE]["actions"]
