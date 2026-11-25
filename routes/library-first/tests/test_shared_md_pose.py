"""Shared selected-pose artifacts with explicit synthetic structural evidence."""
import json
import shutil

import pandas as pd
import pytest

from covscreen.md_candidate_delivery import freeze_md_candidate_delivery
from covscreen.md_candidate_pose import freeze_delivered_candidate_pose, load_candidate_pose_input
from test_md_candidate_delivery import inputs as delivery_inputs, sha
from test_md_candidate_pose import inputs as receptor_inputs


def fixture(tmp_path, route):
    source = tmp_path/'producer'; source.mkdir()
    table, native = delivery_inputs(source)
    frame = pd.read_parquet(table); frame['route'] = route; frame.to_parquet(table, index=False)
    freeze_md_candidate_delivery(table, sha(table), native, sha(native), 'campaign-001', tmp_path/'delivery', route=route)
    receptor = tmp_path/'receptor'; receptor.mkdir()
    values = receptor_inputs(receptor)
    delivery = tmp_path/'delivery/candidate-results.json'
    return [delivery, sha(delivery), 'CMP-1', 7, values[6], values[7], values[8], values[9]]


@pytest.mark.parametrize('route', ['library-first', 'ligand-first', 'de-novo'])
def test_shared_pose_keeps_discovery_provenance_and_replays_without_originals(tmp_path, route):
    args = fixture(tmp_path, route)
    folder = tmp_path/'state/candidate_pose_input'/('c'*32)
    value = freeze_delivered_candidate_pose(*args, folder, expected_route=route, expected_task_id='campaign-001')
    assert value['schema'] == 'candidate-pose-input-artifact-v3'
    assert value['origin'] == dict(route=route, task_id='campaign-001', delivery_sha256=args[1])
    assert value['candidate_id'].startswith(route+'-')
    assert value['native_graph_qc_completed'] is False
    assert value['md_system_ready'] is False
    for path in ['producer', 'delivery', 'receptor']: shutil.rmtree(tmp_path/path)
    read = load_candidate_pose_input(tmp_path, 'candidate-pose-input-'+'c'*32, sha(folder/'candidate-pose-input.json'))
    assert read['manifest'] == value
    assert len(read['evidence_files']) > 5


@pytest.mark.parametrize('change', ['origin', 'chemistry', 'target', 'pose', 'source'])
def test_shared_pose_rejects_forged_current_evidence(tmp_path, change):
    args = fixture(tmp_path, 'de-novo')
    folder = tmp_path/'state/candidate_pose_input'/('c'*32)
    value = freeze_delivered_candidate_pose(*args, folder, expected_route='de-novo', expected_task_id='campaign-001')
    if change == 'origin': value['origin']['route'] = 'ligand-first'
    elif change == 'chemistry': value['chemistry']['standardized_smiles'] = 'C'
    elif change == 'target': value['target']['author_residue'] = '797'
    elif change == 'pose': value['pose_index'] = 99
    else: (folder/'source-delivery/candidate_table.parquet').write_bytes(b'changed')
    path = folder/'candidate-pose-input.json'; path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        load_candidate_pose_input(tmp_path, 'candidate-pose-input-'+'c'*32, sha(path))


def test_shared_pose_requires_bound_source_identity(tmp_path):
    args = fixture(tmp_path, 'de-novo')
    with pytest.raises(ValueError, match='identity'):
        freeze_delivered_candidate_pose(*args, tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_legacy_delivery_cannot_impersonate_bound_shared_route(tmp_path):
    from test_md_candidate_pose import delivered_inputs
    args = delivered_inputs(tmp_path)
    with pytest.raises(ValueError, match='cannot replace'):
        freeze_delivered_candidate_pose(*args, tmp_path/'out', expected_route='de-novo', expected_task_id='delivery-run-000001')
    assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('route', ['library-first', 'ligand-first', 'de-novo'])
def test_shared_delivery_rejects_fractional_pose_before_freezing(tmp_path, route):
    source = tmp_path/'producer'; source.mkdir()
    table, native = delivery_inputs(source)
    frame = pd.read_parquet(table)
    frame['route'] = route
    frame['covdock_pose_index'] = '3.5'
    frame.to_parquet(table, index=False)
    with pytest.raises(ValueError, match='pose index'):
        freeze_md_candidate_delivery(table, sha(table), native, sha(native),
            'campaign-001', tmp_path/'delivery', route=route)
    assert not (tmp_path/'delivery').exists()


@pytest.mark.parametrize('field,value', [('pose_index', 3.0), ('input_sdf_index', 7.0),
    ('calculation_submitted', True), ('campaign_ready', True)])
def test_shared_pose_replay_rejects_changed_identity_types_and_readiness(tmp_path, field, value):
    args = fixture(tmp_path, 'de-novo')
    folder = tmp_path/'state/candidate_pose_input'/('c'*32)
    manifest = freeze_delivered_candidate_pose(*args, folder,
        expected_route='de-novo', expected_task_id='campaign-001')
    manifest[field] = value
    path = folder/'candidate-pose-input.json'; path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='unsupported state'):
        load_candidate_pose_input(tmp_path, 'candidate-pose-input-'+'c'*32, sha(path))


@pytest.mark.parametrize('change', ['status', 'validity', 'target', 'reaction', 'nucleophile'])
def test_legacy_replay_rechecks_the_same_admission_as_freezing(tmp_path, change):
    from test_md_candidate_pose import delivered_inputs
    args = delivered_inputs(tmp_path)
    folder = tmp_path/'state/candidate_pose_input'/('d'*32)
    manifest = freeze_delivered_candidate_pose(*args, folder)
    delivery_path = folder/'candidate-delivery.json'
    delivery = json.loads(delivery_path.read_text())
    row = delivery['candidates'][0]
    if change == 'status': row['covdock_status'] = 'FAILED'
    elif change == 'validity': row['physical_validity'] = 'UNKNOWN'
    elif change == 'target': delivery['target_context']['attachment_residue'] = 'B:481'
    elif change == 'reaction': delivery['target_context']['reaction_type'] = 'unregistered'
    else: row['nucleophilic_atom'] = 'A:CYS:482:SG'
    delivery_path.write_text(json.dumps(delivery))
    manifest['artifacts']['candidate-delivery.json'] = sha(delivery_path)
    manifest['parents']['candidate_delivery_sha256'] = sha(delivery_path)
    path = folder/'candidate-pose-input.json'; path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='structural evidence|differs from receptor'):
        load_candidate_pose_input(tmp_path, 'candidate-pose-input-'+'d'*32, sha(path))
