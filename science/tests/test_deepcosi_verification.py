import json
import shutil

import pytest
from covscreen.deepcosi_verification import verify_prediction_outputs
from covscreen.jobs import sha256_file
from test_deepcosi_identity import sample, runner, collect


def prepared(sample):
    root, normalized, mapping, canonical, raw, validation = sample
    output = root / 'output'
    shutil.copyfile(normalized, output / 'normalized-selected-chain.pdb')
    map_path = output / 'residue-number-mapping.json'
    map_path.write_text(json.dumps({'schema': 'deepcosi-residue-map-v1', 'source_sha256': sha256_file(root / 'structure.pdb'),
        'normalized_sha256': sha256_file(normalized), 'author_chain': 'A', 'model': 1, 'normalized_chain': 'A', 'residues': mapping}))
    entry = json.loads((root / 'entity_1.json').read_text())['rcsb_polymer_entity_container_identifiers']['entry_id']
    params = {'receptor_input_artifact_id': 'receptor-input-' + 'a'*32, 'manifest_sha256': 'b'*64,
              'runtime_registration': {k: validation[k] for k in ('entry_sha256', 'model_sha256')}}
    loaded = {'acquisition_directory': root, 'canonical_sequence': 'X' * 1210,
        'identity': {'plan': {'gene': 'EGFR', 'taxon': 9606},
        'identity': {'candidates': [{'uniprot_id': 'P00533'}]}},
        'acquisition': {'selection': {'model': 1}, 'acquisition': {'plan': {'entry': entry},
            'mappings': [{'auth_asym_id': 'A', 'entity_id': '1', 'label_asym_id': 'A', 'uniprot': 'P00533'}]}}}
    rows = collect(sample)
    doc = {'schema': runner.PREDICTION_SCHEMA, 'status': 'COMPLETED',
        'target': {'gene': 'EGFR', 'taxon': 9606, 'uniprot': 'P00533'},
        'structure': {'entry': entry, 'model': 1, 'author_chain': 'A', 'input_sha256': sha256_file(root / 'structure.pdb'),
            'normalized_input_sha256': sha256_file(normalized), 'residue_mapping_path': map_path.name,
            'residue_mapping_sha256': sha256_file(map_path)},
        'parents': {'receptor_input_artifact_id': params['receptor_input_artifact_id'], 'target_acquisition_manifest_sha256': params['manifest_sha256']},
        'runtime': params['runtime_registration'], 'candidate_count': 6, 'scored_candidate_count': 6, 'failure_count': 0,
        'candidates': rows, 'raw_result_sha256': sha256_file(raw / 'build/prediction_result.csv')}
    path = output / 'site-prediction.json'; path.write_text(json.dumps(doc))
    records = {p.resolve(): None for p in output.rglob('*') if p.is_file()}
    return output, loaded, params, records, path


def test_replay_retained_raw_evidence_without_inference(sample):
    output, loaded, params, records, path = prepared(sample)
    before = {p: p.read_bytes() for p in records}
    coverage = verify_prediction_outputs(output, loaded, params, records)
    assert coverage['canonical_sequence_length'] == 1210
    assert coverage['mapped_observed_residue_count'] > 200
    assert coverage['all_canonical_positions_have_coordinates'] is False
    assert any(start <= 797 <= end for start, end in coverage['observed_canonical_ranges'])
    assert all(p.read_bytes() == content for p, content in before.items())


@pytest.mark.parametrize('change', ['canonical', 'score', 'count', 'runtime', 'parent', 'mapping', 'normalized', 'registration'])
def test_tampering_is_rejected_even_if_outer_manifest_rehashed(sample, change):
    output, loaded, params, records, path = prepared(sample)
    doc = json.loads(path.read_text())
    if change == 'canonical': doc['candidates'][0]['canonical_position'] += 1
    if change == 'score': doc['candidates'][0]['model_score'] += .1
    if change == 'count': doc['candidate_count'] = 99
    if change == 'runtime': params['runtime_registration']['model_sha256'] = '0'*64
    if change == 'parent': doc['parents']['target_acquisition_manifest_sha256'] = '0'*64
    if change == 'mapping':
        p = output / 'residue-number-mapping.json'; p.write_text('{}')
        doc['structure']['residue_mapping_sha256'] = sha256_file(p)
    if change == 'normalized':
        p = output / 'normalized-selected-chain.pdb'; p.write_text(p.read_text().replace('CYS', 'SER', 1))
    if change == 'registration': records.pop(output / 'site-prediction.json')
    path.write_text(json.dumps(doc))
    with pytest.raises((ValueError, RuntimeError)):
        verify_prediction_outputs(output, loaded, params, records)


def test_public_verifier_reports_missing_prediction_ancestry_as_failure(tmp_path):
    from test_result_verification import fixture
    from covscreen.result_verification import verify_job
    registry, result, _ = fixture(tmp_path)
    spec = registry.load_spec(result.run_id)
    spec.engine = 'shared-deepcosi-site-prediction'
    result.engine = spec.engine
    registry.write_spec(spec)
    registry.write_result(result)
    report = verify_job(tmp_path, result.run_id)
    assert report['verified'] is False
    assert report['status'] == 'FAIL'
    assert any(e.startswith('deepcosi_verification_failed:') for e in report['errors'])
    assert not any('engine_verifier_not_implemented' in e for e in report['errors'])


def test_legacy_manifest_binds_referenced_evidence_without_promoting_auxiliary_files(sample):
    output, loaded, params, _, path = prepared(sample)
    records = {path: None}
    coverage = verify_prediction_outputs(output, loaded, params, records)
    assert coverage['mapped_observed_residue_count'] > 200
    manifest = json.loads(path.read_text())
    pocket = output / manifest['candidates'][0]['pocket_path']
    pocket.write_text(pocket.read_text() + 'REMARK modified pocket\n')
    with pytest.raises(ValueError, match='candidates differ'):
        verify_prediction_outputs(output, loaded, params, records)
