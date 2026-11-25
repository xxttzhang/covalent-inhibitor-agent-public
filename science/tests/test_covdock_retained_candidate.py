import json
from pathlib import Path

import pandas as pd
import pytest

from covscreen.covdock_retained import validate_retained_candidate, selection_inspection
from covscreen.jobs import sha256_file


def fixture(tmp_path):
    parent = tmp_path / 'state/jobs/parent'
    parent.mkdir(parents=True)
    table = tmp_path / 'candidates.parquet'
    pd.DataFrame({'compound_id': ['a', 'b', 'c'], 'input_sdf_index': [2, 1, 0],
                  'standardized_smiles': ['CC', 'CCC', 'CCCC']}).to_parquet(table)
    receptor = tmp_path / 'receptor.mae'
    receptor.write_text('frozen receptor bytes')
    pose = parent / 'artifacts/poses.maegz'
    pose.parent.mkdir()
    pose.write_bytes(b'frozen pose bytes, not a structural fixture')
    spec = {'run_id': 'parent', 'engine': 'schrodinger-covdock-lo', 'route': 'ligand-first',
            'parameters': {'candidate_table': 'candidates.parquet', 'receptor': 'receptor.mae'},
            'input_sha256': {'candidates.parquet': sha256_file(table), 'receptor.mae': sha256_file(receptor)}}
    (parent / 'spec.json').write_text(json.dumps(spec))
    (parent / 'result.json').write_text(json.dumps({'run_id': 'parent', 'state': 'TIMED_OUT'}))
    title = 'a__pa__i2'
    audit = tmp_path / 'audit.json'
    audit.write_text(json.dumps({'kind': 'READ_ONLY_RETAINED_CANDIDATE_AUDIT',
        'parent_run_state': 'TIMED_OUT', 'new_docking_or_scoring_run': False,
        'retained_066_identity_hash_verified': True, 'sources': {str(pose): sha256_file(pose)},
        'retained_pose_count': 2,
        'prepared_inputs': [{'title': title, 'vendor_input_hash': 'input-hash', 'index': 0}],
        'rows': [{'title': 'protein - ' + title, 'pose_index': i, 'vendor_input_hash': 'input-hash',
                  'matching_input_indices': [0]} for i in range(2)]}))
    files = {'parent_spec': parent / 'spec.json', 'parent_result': parent / 'result.json',
             'candidate_table': table, 'receptor': receptor, 'pose_file': pose, 'readback_audit': audit}
    descriptor = tmp_path / 'descriptor.json'
    descriptor.write_text(json.dumps({'kind': 'covdock-retained-candidate-v1', 'parent_run_id': 'parent',
        'compound_id': 'a', 'input_sdf_index': 2, 'title': title, 'vendor_input_hash': 'input-hash',
        'pose_count': 2, 'files': {key: {'path': str(path.relative_to(tmp_path)), 'sha256': sha256_file(path)}
                                 for key, path in files.items()}}))
    return descriptor, files


def rebind(descriptor, key, path):
    data = json.loads(descriptor.read_text())
    data['files'][key]['sha256'] = sha256_file(path)
    descriptor.write_text(json.dumps(data))


def test_candidate_reparse_retains_original_denominator(tmp_path):
    descriptor, _ = fixture(tmp_path)
    result = validate_retained_candidate(tmp_path, descriptor)
    assert result['original_candidate_count'] == 3
    assert result['candidate']['input_sdf_index'].tolist() == [2]
    assert len(result['audit']['rows']) == 2


def test_changed_pose_is_rejected(tmp_path):
    descriptor, files = fixture(tmp_path)
    files['pose_file'].write_bytes(b'changed')
    with pytest.raises(ValueError, match='Retained source changed'):
        validate_retained_candidate(tmp_path, descriptor)


def test_descriptor_cannot_assign_audited_poses_to_another_original_candidate(tmp_path):
    descriptor, _ = fixture(tmp_path)
    data = json.loads(descriptor.read_text())
    data.update(compound_id='b', input_sdf_index=1)
    descriptor.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='title differs'):
        validate_retained_candidate(tmp_path, descriptor)


def test_nonterminal_parent_is_rejected_even_if_descriptor_rebound(tmp_path):
    descriptor, files = fixture(tmp_path)
    files['parent_result'].write_text(json.dumps({'run_id': 'parent', 'state': 'RUNNING'}))
    rebind(descriptor, 'parent_result', files['parent_result'])
    with pytest.raises(ValueError, match='interrupted'):
        validate_retained_candidate(tmp_path, descriptor)


def test_rebound_candidate_table_cannot_change_original_identity(tmp_path):
    descriptor, files = fixture(tmp_path)
    table = pd.read_parquet(files['candidate_table'])
    table.loc[0, 'standardized_smiles'] = 'N'
    table.to_parquet(files['candidate_table'])
    rebind(descriptor, 'candidate_table', files['candidate_table'])
    with pytest.raises(ValueError, match='scientific input differs'):
        validate_retained_candidate(tmp_path, descriptor)


@pytest.mark.parametrize('change', ['title', 'pose_index', 'vendor_input_hash'])
def test_foreign_or_reordered_pose_is_rejected(tmp_path, change):
    descriptor, files = fixture(tmp_path)
    audit = json.loads(files['readback_audit'].read_text())
    audit['rows'][1][change] = 0 if change == 'pose_index' else 'another-candidate'
    files['readback_audit'].write_text(json.dumps(audit))
    rebind(descriptor, 'readback_audit', files['readback_audit'])
    with pytest.raises(ValueError):
        validate_retained_candidate(tmp_path, descriptor)


def test_vendor_failure_sentinel_keeps_raw_evidence_and_all_poses():
    raw = {'rows': [
        {'pose_index': 0, 'r_i_score_docking_score': -9.402, 'r_cdock_cdock_affinity': -8.958},
        {'pose_index': 7, 'r_i_score_docking_score': -9.540, 'r_cdock_cdock_affinity': -9.590},
        {'pose_index': 9, 'r_i_score_docking_score': 999., 'r_cdock_cdock_affinity': 495.491}]}
    selected, invalid = selection_inspection(raw)
    assert invalid == [9] and len(selected['rows']) == len(raw['rows']) == 3
    assert raw['rows'][2]['r_cdock_cdock_affinity'] == 495.491
    assert selected['rows'][2]['r_cdock_cdock_affinity'] is None
    assert selected['rows'][1] == raw['rows'][1]
