import copy
import hashlib
import json
from pathlib import Path

import pytest

from covscreen.construct_audit import audit_construct


FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.fixture
def inputs():
    entity = json.loads((FIXTURES / 'target_acquisition/entity_1.json').read_text())
    sequence = ''.join((FIXTURES / 'target_identity/P00533.fasta').read_text().splitlines()[1:])
    return entity, sequence


def test_actual_deposited_construct_has_unaligned_tag_not_missing_loops(inputs):
    entity, sequence = inputs
    source = next(s for s in json.loads((FIXTURES / 'target_acquisition/source.json').read_text())
                  if s['path'] == 'entity_1.json')
    assert hashlib.sha256((FIXTURES / 'target_acquisition/entity_1.json').read_bytes()).hexdigest() == source['sha256']
    audit = audit_construct(entity, sequence, 'P00533', 9606, include_segment_evidence=True)
    assert audit['aligned_positions'] == 328
    assert audit['unaligned_entity_positions'] == [1, 2, 3, 4, 5]
    assert audit['sequence_differences'] == []
    assert audit['source_species_verified'] is True
    assert len(audit['canonical_positions_absent_from_alignment']) == 882
    assert audit['campaign_ready'] is False
    segment, = audit['unaligned_segments']
    assert segment['entity_positions'] == [1, 2, 3, 4, 5]
    assert segment['sequence'] == ''.join(entity['entity_poly']['pdbx_seq_one_letter_code_can'].split())[:5]
    assert segment['location_relative_to_alignment'] == 'N_terminal'
    assert segment['origin'] == 'not_assigned'
    assert segment['coordinate_presence'] == 'not_assessed'
    # Existing signed inventories keep exactly their previous payload shape.
    original = audit_construct(entity, sequence, 'P00533', 9606)
    assert {key: value for key, value in audit.items() if key != 'unaligned_segments'} == original


def test_unaligned_segments_distinguish_internal_and_terminal_without_approving(inputs):
    entity, sequence = inputs
    entity['entity_poly']['pdbx_seq_one_letter_code_can'] = 'GXACQDEK'
    entity['rcsb_polymer_entity_align'] = [{
        'reference_database_name': 'UniProt', 'reference_database_accession': 'P00533',
        'aligned_regions': [dict(entity_beg_seq_id=3, ref_beg_seq_id=1, length=2),
                            dict(entity_beg_seq_id=6, ref_beg_seq_id=3, length=2)]}]
    audit = audit_construct(entity, 'ACDE', 'P00533', 9606, include_segment_evidence=True)
    segments = audit['unaligned_segments']
    assert [row['sequence'] for row in segments] == ['GX', 'Q', 'K']
    assert [row['location_relative_to_alignment'] for row in segments] == ['N_terminal', 'internal', 'C_terminal']
    assert segments[0]['nonstandard_entity_positions'] == [2]
    assert not audit['campaign_ready'] and not audit['receptor_prepared']
    assert audit['status'] == 'construct_review_required'


def test_mutation_report_uses_canonical_and_construct_numbering(inputs):
    entity, sequence = inputs
    chars = list(entity['entity_poly']['pdbx_seq_one_letter_code_can'])
    chars[107] = 'S'
    entity['entity_poly']['pdbx_seq_one_letter_code_can'] = ''.join(chars)
    audit = audit_construct(entity, sequence, 'P00533', 9606)
    assert audit['sequence_differences'] == [{'entity_position': 108, 'canonical_position': 797,
        'construct_residue': 'S', 'canonical_residue': 'C'}]


@pytest.mark.parametrize('organisms', [[], [{'ncbi_taxonomy_id': 10090}],
    [{'ncbi_taxonomy_id': 9606}, {}], [{'ncbi_taxonomy_id': '9606'}]])
def test_missing_mixed_or_different_species_is_not_verified(inputs, organisms):
    entity, sequence = inputs
    entity['rcsb_entity_source_organism'] = organisms
    assert not audit_construct(entity, sequence, 'P00533', 9606)['source_species_verified']


@pytest.mark.parametrize('region', [dict(entity_beg_seq_id=1, ref_beg_seq_id=1, length=10000),
    dict(entity_beg_seq_id=True, ref_beg_seq_id=1, length=1),
    dict(entity_beg_seq_id=6, ref_beg_seq_id=696, length=1)])
def test_invalid_or_conflicting_alignment_is_rejected(inputs, region):
    entity, sequence = inputs
    entity['rcsb_polymer_entity_align'][0]['aligned_regions'].append(region)
    with pytest.raises(ValueError):
        audit_construct(entity, sequence, 'P00533', 9606)


def test_duplicate_consistent_alignment_is_not_double_counted(inputs):
    entity, sequence = inputs
    entity['rcsb_polymer_entity_align'].append(copy.deepcopy(entity['rcsb_polymer_entity_align'][0]))
    assert audit_construct(entity, sequence, 'P00533', 9606)['aligned_positions'] == 328


def test_unknown_amino_acid_cannot_be_reported_as_a_matching_residue(inputs):
    entity, sequence = inputs
    chars = list(entity['entity_poly']['pdbx_seq_one_letter_code_can'])
    chars[107] = 'X'
    entity['entity_poly']['pdbx_seq_one_letter_code_can'] = ''.join(chars)
    audit = audit_construct(entity, sequence, 'P00533', 9606)
    assert audit['uncertain_residues'][0]['canonical_position'] == 797
    assert audit['sequence_differences'] == []


def test_reordered_alignment_blocks_are_rejected(inputs):
    entity, sequence = inputs
    entity['rcsb_polymer_entity_align'][0]['aligned_regions'] = [
        dict(entity_beg_seq_id=1, ref_beg_seq_id=900, length=1),
        dict(entity_beg_seq_id=2, ref_beg_seq_id=800, length=1)]
    with pytest.raises(ValueError, match='Non-monotonic'):
        audit_construct(entity, sequence, 'P00533', 9606)
