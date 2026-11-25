import importlib.util
import json
import hashlib
from pathlib import Path

import pandas as pd
import pytest

spec = importlib.util.spec_from_file_location('preparation_diagnostic', Path(__file__).with_name('preparation_diagnostic.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(tmp_path):
    root, project = tmp_path / 'executor', tmp_path / 'science'
    task = 'screen-' + 'a' * 24
    base = root / 'tasks' / task
    dest = base / 'prepare'
    dest.mkdir(parents=True)
    target = project / 'configs/covindb/EGFR_C797_mvp_v1.yaml'
    target.parent.mkdir(parents=True)
    target.write_text('lead_like: {min_mw: 180, max_mw: 650, min_logp: -1, max_logp: 6}\n')
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    source = '/not-read/Virtual_Screening_Library.csv'
    plan = {'id': task, 'runtime': {'root': str(root), 'project': str(project)},
            'target': 'EGFR', 'site': 'C797', 'library': 'CovInDB/Virtual_Screening_Library.csv',
            'scope': 'bounded', 'max_records': 2, 'shortlist': 1,
            'assets': {str(target): checksum, source: 'b' * 64}}
    expected = module.digest(plan)
    (base / 'plan.json').write_text(json.dumps(plan))
    (dest / 'started.json').write_text(json.dumps({'plan_digest': expected}))
    (dest / 'error.json').write_text(json.dumps({'task_id': task, 'plan_digest': expected, 'phase': 'prepare', 'status': 'FAILED'}))
    metadata = {'source': {'max_rows': 2, 'path': source, 'sha256': 'b' * 64},
                'target': {'config_path': str(target), 'config_sha256': checksum},
                'selection': {'top_n_requested': 1},
                'counts': {'input_rows': 2, 'unique': 2, 'duplicates': 0, 'invalid': 0,
                           'compatible_warhead': 1, 'unambiguous_electrophile': 1, 'hard_gate_pass': 0}}
    (dest / 'stage2_selection_manifest.json').write_text(json.dumps(metadata))
    pd.DataFrame({'eligible': [False, False], 'mw': [50., 300.], 'clogp': [-2., 2.], 'tpsa': [20., 20.],
                  'rotatable_bonds': [2, 2], 'formal_charge': [0, 0], 'warhead_count': [1, 0],
                  'electrophile_atom_index': [0, -1]}).to_parquet(dest / 'covindb_master.parquet', index=False)
    return root, project, task, expected


def test_real_files_aggregate_without_source_scan_or_mutation(tmp_path):
    args = fixture(tmp_path)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    result = module.inspect(*args)
    assert result['rows'] == 2 and result['eligible'] == 0
    assert result['valid_warhead'] == 1 and result['incompatible'] == 1
    assert result['valid_warhead_property_failures']['clogp_below_min'] == 1
    assert result['valid_warhead_property_failures']['mw_below_min'] == 1
    assert result['scientific_quality_verified'] is False
    assert result['compute_submitted'] is False
    assert len(result['files']) == 6
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    assert 'smiles' not in json.dumps(result).lower()


@pytest.mark.parametrize('change', ['config', 'identity', 'eligible', 'missing', 'symlink', 'counts', 'scope', 'nonfinite', 'success'])
def test_changed_or_unusable_diagnostic_files_fail_closed(tmp_path, change):
    args = fixture(tmp_path)
    root, project, task, expected = args
    dest = root / 'tasks' / task / 'prepare'
    if change == 'config':
        (project / 'configs/covindb/EGFR_C797_mvp_v1.yaml').write_text('lead_like: {min_mw: 0}')
    if change == 'identity':
        args = (root, project, task, 'c' * 64)
    if change in {'eligible', 'nonfinite'}:
        frame = pd.read_parquet(dest / 'covindb_master.parquet')
        frame.loc[0, 'eligible' if change == 'eligible' else 'mw'] = True if change == 'eligible' else float('inf')
        frame.to_parquet(dest / 'covindb_master.parquet', index=False)
    if change == 'missing':
        (dest / 'error.json').unlink()
    if change == 'symlink':
        source = dest / 'error.json'
        moved = dest / 'real-error.json'
        source.rename(moved)
        source.symlink_to(moved)
    if change in {'counts', 'scope'}:
        path = dest / 'stage2_selection_manifest.json'
        value = json.loads(path.read_text())
        if change == 'counts':
            value['counts']['hard_gate_pass'] = 1
        if change == 'scope':
            value['source']['max_rows'] = 20000
        path.write_text(json.dumps(value))
    if change == 'success':
        (dest / 'manifest.json').write_text('{}')
    with pytest.raises((ValueError, FileNotFoundError)):
        module.inspect(*args)
