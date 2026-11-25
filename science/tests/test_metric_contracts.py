from pathlib import Path

from covscreen.metric_contracts import metric_contracts, workflow_metric_context
from covscreen.tool_api import capabilities_tool
from covscreen_ligand_first.ligand_first_geometry import PoseGeometryEvaluation, LigandFirstGeometryPolicy


def test_capability_exposes_scoped_contracts_without_mutating_policies(tmp_path):
    before = LigandFirstGeometryPolicy().model_dump()
    contracts = capabilities_tool(tmp_path)["metric_contracts"]
    assert not contracts["thresholds_validated"]
    covdock = next(group for group in contracts['groups'] if group['producer'] == 'covdock_gate')
    assert 'v3 only r_strain_Local_Strain_Energy' in covdock['metrics'][0]['definition']
    assert set(covdock['evaluation_versions']) == {'covdock-lo-pose-gate-v2.1', 'covdock-lo-pose-gate-v3.0-provisional'}
    geometry = next(group for group in contracts["groups"] if group["producer"] == "ligand_first_pose_geometry")
    assert {metric["field"] for metric in geometry["metrics"]} <= set(PoseGeometryEvaluation.model_fields)
    workspace = Path(__file__).resolve().parents[2]
    for group in contracts["groups"]:
        assert (workspace / group["implementation"]).is_file()
        fields = [metric["field"] for metric in group["metrics"]]
        assert len(fields) == len(set(fields))
    contracts["groups"][0]["metrics"].clear()
    assert metric_contracts()["groups"][0]["metrics"]
    assert LigandFirstGeometryPolicy().model_dump() == before
    assert list(tmp_path.iterdir()) == []


def test_current_stage_selects_explanation_without_claiming_results(tmp_path):
    from covscreen.mcp.server import dispatch
    first = dispatch(tmp_path, "covalent_library_workflow", {
        "action": "start", "request_id": "metric-context-test", "target": "EGFR", "taxon": 9606})
    assert first["metric_context"]["groups"] == []
    assert first["metric_context"]["role"] == "interpretation_only_not_a_gate_result"
    chemical = workflow_metric_context("library-first", "SCREENING_PLAN_READY")
    assert [group["producer"] for group in chemical["groups"]] == ["covindb_descriptor_filter"]
    geometry = workflow_metric_context("ligand-first", "POSE_EXECUTION_REVIEW")
    assert [group["producer"] for group in geometry["groups"]] == ["ligand_first_pose_geometry"]
    refinement = workflow_metric_context("library-first", "REFINEMENT_CANDIDATE_REVIEW")
    assert [group["producer"] for group in refinement["groups"]] == ["covdock_gate"]
    covdock = workflow_metric_context('ligand-first', 'COVDOCK_EXECUTION_REVIEW')
    assert covdock['groups'] == refinement['groups']
    assert covdock['thresholds_validated'] is False
    assert covdock['role'] == 'interpretation_only_not_a_gate_result'
    assert "threshold" not in chemical
    assert covdock['result_state_semantics'] == refinement['result_state_semantics']
    covdock['result_state_semantics'].clear()
    assert metric_contracts()['result_state_semantics']


def test_all_route_contexts_share_selection_and_measurement_semantics():
    semantics = metric_contracts()['result_state_semantics']
    assert 'not permission to read or review' in semantics['review_eligible']
    assert 'does not require gate A' in semantics['review_eligible']
    assert 'never a numerical zero' in semantics['missing_measurements']
    for route in ('library-first', 'ligand-first', 'de-novo'):
        context = workflow_metric_context(route, 'REFINEMENT_CANDIDATE_REVIEW')
        assert context['result_state_semantics'] == semantics
