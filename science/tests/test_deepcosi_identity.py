"""Replay retained FAILED run outputs; no model inference or new scientific result."""
import csv
import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from covscreen.deepcosi_identity import verified_result_keys, result_number

SCRIPT = Path(__file__).parents[1] / 'scripts/run_deepcosi_site_prediction.py'
spec = importlib.util.spec_from_file_location('deepcosi_runner', SCRIPT)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
FIXTURE = Path(__file__).parent / 'fixtures/deepcosi-1m14'


@pytest.fixture
def sample(tmp_path):
    shutil.copytree(FIXTURE, tmp_path / 'fixture')
    root = tmp_path / 'fixture'
    normalized = root / 'normalized.pdb'
    mapping = runner._normalize_structure(root / 'structure.pdb', normalized, 'A', 1)
    canonical = runner._canonical_map(json.loads((root / 'entity_1.json').read_text()),
                                     json.loads((root / 'chain_A.json').read_text()), 'P00533')
    raw = root / 'output/raw-deepcosi'
    validation = json.loads((raw / 'validation.json').read_text())
    return root, normalized, mapping, canonical, raw, validation


def collect(sample):
    root, normalized, mapping, canonical, raw, validation = sample
    return runner.collect_candidates(raw, normalized, mapping, canonical, validation, 'A', root / 'output')


def test_real_six_sites_map_to_author_and_canonical_without_offsets(sample):
    rows = collect(sample)
    assert {r['raw_key']: (r['author_residue_id'], r['canonical_position']) for r in rows} == {
        'input_A_80': ('751', 775), 'input_A_86': ('757', 781),
        'input_A_102': ('773', 797), 'input_A_123': ('794', 818),
        'input_A_244': ('915', 939), 'input_A_255': ('926', 950)}
    assert all(r['prediction_status'] == 'SCORED' and not r['blockers'] for r in rows)
    assert rows[0]['canonical_position'] == 797
    raw_rows = {r['key']: float(r['probability']) for r in csv.DictReader((sample[4] / 'build/prediction_result.csv').open())}
    assert all(r['model_score'] == raw_rows[r['raw_key']] for r in rows)


@pytest.mark.parametrize('key', ['input_B_255', 'other_A_255', 'input_A_999', 'input_A_255_extra', '../input_A_255', 'input_CYS_255'])
def test_wrong_chain_prefix_number_or_path_rejected(sample, key):
    _, normalized, mapping, _, raw, validation = sample
    with pytest.raises(ValueError, match='identity'):
        result_number(key, verified_result_keys(raw, normalized, mapping, validation))


def test_changed_processed_structure_rejected(sample):
    path = sample[4] / 'build/input_process.pdb'
    path.write_text(path.read_text().replace('CYS', 'SER', 1))
    with pytest.raises(ValueError, match='ATOM'):
        collect(sample)


def test_tampered_result_receipt_rejected(sample):
    sample[-1]['result_sha256'] = '0' * 64
    with pytest.raises(RuntimeError, match='checksum'):
        collect(sample)


def test_duplicate_detected_site_rejected(sample):
    path = sample[4] / 'build/prediction_cysteines.csv'
    path.write_text(path.read_text() + '7,input,A,255\n')
    with pytest.raises(ValueError, match='Duplicate'):
        collect(sample)


def test_inserted_residue_is_not_silently_mapped(sample):
    sample[2][255]['insertion_code'] = 'A'
    row = next(r for r in collect(sample) if r['raw_key'] == 'input_A_255')
    assert row['author_residue_id'] == '926A'
    assert row['canonical_position'] is None
    assert 'author_insertion_code_requires_manual_mapping' in row['blockers']


def test_conflicting_alignment_and_wrong_target_rejected():
    entity = json.loads((FIXTURE / 'entity_1.json').read_text())
    chain = json.loads((FIXTURE / 'chain_A.json').read_text())
    with pytest.raises(ValueError, match='accession'):
        runner._canonical_map(entity, chain, 'OTHER')
    entity['rcsb_polymer_entity_align'][0]['aligned_regions'].append(
        {'entity_beg_seq_id': 6, 'ref_beg_seq_id': 696, 'length': 328})
    with pytest.raises(ValueError, match='Ambiguous'):
        runner._canonical_map(entity, chain, 'P00533')
