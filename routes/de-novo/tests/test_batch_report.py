import pytest

from covscreen_de_novo.batch_report import ranking


def candidate(name, score, physical=True):
    return {'compound_id': name, 'covdock_score': score, 'input_sdf_index': 1,
        'engine_status': 'SUCCESS', 'gate': {'physical_validity': 'PASS' if physical else 'FAIL',
            'physical_evidence_complete': True}}


def test_ranking_keeps_unmeasured_and_failed_candidates_without_combining_scores():
    lo = {'candidates': [candidate('unmeasured', -10), candidate('qualified', -7),
        candidate('no-pose', -12), candidate('failed', -20, False)]}
    pre = {'states': [
        {'candidate_id': 'qualified', 'control': False, 'identity_verified_count': 2, 'eligible_pose_count': 1},
        {'candidate_id': 'no-pose', 'control': False, 'identity_verified_count': 2, 'eligible_pose_count': 0},
        {'candidate_id': 'reference', 'control': True, 'eligible_pose_count': 1}]}
    result = ranking(lo, pre)
    assert [row['candidate_id'] for row in result['candidates']] == ['qualified', 'unmeasured', 'no-pose', 'failed']
    assert result['candidates'][1]['prereaction_state'] == 'NOT_EVALUATED'
    assert result['candidate_count'] == 4
    assert result['controls'] == [pre['states'][2]]
    assert result['score_combination'] == 'NONE'
    assert result['scientific_acceptance'] is False


def test_report_rejects_foreign_evidence_and_nonfinite_scores():
    with pytest.raises(ValueError, match='foreign'):
        ranking({'candidates': [candidate('a', -7)]}, {'states': [{'candidate_id': 'b', 'control': False}]})
    with pytest.raises(ValueError, match='Nonfinite'):
        ranking({'candidates': [candidate('a', float('nan'))]}, {'states': []})
