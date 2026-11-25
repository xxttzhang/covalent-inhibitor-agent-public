"""Shared artifact freezing only; synthetic poses do not establish scientific admission."""
import json

import pandas as pd
import pytest

from covscreen.md_candidate_delivery import freeze_md_candidate_delivery, load_shared_candidate_delivery
from test_md_candidate_delivery import inputs, sha


@pytest.mark.parametrize('route', ['library-first', 'ligand-first', 'de-novo'])
@pytest.mark.parametrize('format', ['csv', 'parquet'])
def test_shared_delivery_preserves_route_and_copies_original_evidence(tmp_path, route, format):
    table, manifest = inputs(tmp_path)
    frame = pd.read_parquet(table)
    frame['route'] = route
    frame['compound_id'] = '007'
    frame['target_id'] = '001'
    frame['parent_id'] = '0002'
    if format == 'csv':
        table = table.with_suffix('.csv')
        frame.to_csv(table, index=False)
    else:
        frame.to_parquet(table, index=False)
    result = freeze_md_candidate_delivery(table, sha(table), manifest, sha(manifest),
        'campaign-001', tmp_path/'out', route=route)
    assert result['schema'] == 'shared-md-candidate-delivery-v1'
    assert result['route'] == route
    assert result['candidates'][0]['compound_id'] == '007'
    assert result['candidates'][0]['target_id'] == '001'
    assert result['candidates'][0]['parent_id'] == '0002'
    assert result['native_graph_qc_completed'] is False
    assert result['candidate_selected_for_md'] is False
    assert result['calculation_submitted'] is False
    for role, path in [('candidate_table', table), ('covdock_manifest', manifest)]:
        record = result['source_evidence'][role]
        assert (tmp_path/'out'/record['filename']).read_bytes() == path.read_bytes()
        assert record['sha256'] == sha(path)
    saved = json.loads((tmp_path/'out/candidate-results.json').read_text())
    assert saved == result
    # Replay must depend only on frozen copies, even after original paths disappear.
    for source in (table, manifest, tmp_path/'poses.maegz', tmp_path/'model.cif'):
        source.unlink()
    read = load_shared_candidate_delivery(tmp_path/'out/candidate-results.json',
        sha(tmp_path/'out/candidate-results.json'), expected_route=route, expected_task_id='campaign-001')
    assert read['manifest'] == result


@pytest.mark.parametrize('source', [None, 'library-first'])
def test_nonlibrary_delivery_rejects_missing_or_conflicting_route(tmp_path, source):
    table, manifest = inputs(tmp_path)
    if source:
        frame = pd.read_parquet(table)
        frame['route'] = source
        frame.to_parquet(table, index=False)
    with pytest.raises(ValueError, match='consistent candidate route'):
        freeze_md_candidate_delivery(table, sha(table), manifest, sha(manifest),
            'campaign-001', tmp_path/'out', route='de-novo')
    assert not (tmp_path/'out').exists()


def test_other_routes_cannot_use_library_legacy_profile(tmp_path):
    table, manifest = inputs(tmp_path)
    with pytest.raises(ValueError, match='Unsupported candidate discovery route'):
        freeze_md_candidate_delivery(table, sha(table), manifest, sha(manifest),
            'campaign-001', tmp_path/'out', route='ligand-first', legacy_library=True)


@pytest.mark.parametrize('change', ['candidate', 'route', 'task', 'context', 'mapping', 'path', 'structure', 'source'])
def test_shared_read_rejects_inconsistent_evidence(tmp_path, change):
    table, native = inputs(tmp_path)
    frame = pd.read_parquet(table)
    frame['route'] = 'ligand-first'
    frame.to_parquet(table, index=False)
    value = freeze_md_candidate_delivery(table, sha(table), native, sha(native),
        'campaign-001', tmp_path/'out', route='ligand-first')
    path = tmp_path/'out/candidate-results.json'
    if change == 'candidate': value['candidates'][0]['standardized_smiles'] = 'C'
    elif change == 'route': value['route'] = 'de-novo'
    elif change == 'task': value['task_id'] = 'different-task'
    elif change == 'context': value['target_context']['attachment_residue'] = 'A:797'
    elif change == 'mapping': value['source_assets'] = {}
    elif change == 'path': next(iter(value['assets'].values()))['filename'] = '../poses.maegz'
    elif change == 'source': (tmp_path/'out/candidate_table.parquet').write_bytes(b'changed')
    else: (tmp_path/'out'/next(iter(value['assets'].values()))['filename']).write_bytes(b'changed')
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_shared_candidate_delivery(path, sha(path), expected_route='ligand-first', expected_task_id='campaign-001')
