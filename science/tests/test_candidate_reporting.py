import json
import pytest
from covscreen.tool_api import get_job_candidates_tool
from test_result_verification import fixture, refresh


def setup(root):
    registry, result, output = fixture(root)
    result.candidate_table_path = str((output / 'stage2_candidates.parquet').relative_to(root))
    registry.write_result(result)
    return registry, result, output


def test_historical_report_never_invents_current_formula(tmp_path):
    _, result, _ = setup(tmp_path)
    report = get_job_candidates_tool(tmp_path, result.run_id)
    assert report['scientific_result_verified']
    explanation = report['scoring_interpretation']
    assert explanation['contract_status'] == 'not_recorded_in_this_historical_output'
    assert explanation['score_contract'] is None
    assert explanation['activity_ranking_validated'] is False
    assert explanation['cross_channel_scores_comparable'] is False
    assert 'A' not in report['gate_semantics']
    assert 'PASS' in report['gate_semantics']
    assert 'separate alert fields' in report['gate_semantics']['risk_annotations']
    assert '独立警报字段' in report['authoritative_report_markdown']


def test_recorded_contract_is_returned_from_verified_artifact(tmp_path):
    registry, result, output = setup(tmp_path)
    path = output / 'stage2_selection_manifest.json'
    manifest = json.loads(path.read_text())
    contract = {'version': 'test-score-contract', 'exploitation': 'fixture formula; not a scientific result'}
    manifest['selection']['score_contract'] = contract
    manifest['algorithm_version'] = 'test-score-contract'
    path.write_text(json.dumps(manifest))
    refresh(registry, result, output)
    report = get_job_candidates_tool(tmp_path, result.run_id)
    assert report['scoring_interpretation']['score_contract'] == contract
    assert report['scoring_interpretation']['source_manifest_sha256']
    path.write_text('{}')
    rejected = get_job_candidates_tool(tmp_path, result.run_id)
    assert rejected['scientific_result_verified'] is False
    assert rejected['candidates'] == []


def test_candidate_path_cannot_redirect_to_unregistered_table(tmp_path):
    registry, result, output = setup(tmp_path)
    path = tmp_path / 'unregistered.csv'
    path.write_text('compound_id,score\nother,999\n')
    result.candidate_table_path = path.name
    registry.write_result(result)
    with pytest.raises(ValueError, match='registered artifact'):
        get_job_candidates_tool(tmp_path, result.run_id)


@pytest.mark.parametrize('limit', [True, 0, -1, 2001, 1.5])
def test_invalid_display_limits_are_rejected(tmp_path, limit):
    with pytest.raises(ValueError, match='limit'):
        get_job_candidates_tool(tmp_path, 'unused-run', limit=limit)
