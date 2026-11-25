"""Source-bound judgments never become experimental proof or execution approval."""
from copy import deepcopy
import json

import pytest
from jsonschema import validate as validate_schema

from covscreen.jobs import sha256_file
from covscreen.receptor_environment_review import (
    CONCERNS, REVIEW_SCHEMA, freeze, load, validate, read_evidence, cited_value)


def test_evidence_reader_pages_real_pointers_and_rejects_unbound_or_changed_files(tmp_path):
    source = tmp_path / 'evidence.json'
    payload = {'a/b~c': [{'observation': 'x' * 600, 'index': i} for i in range(45)],
               'missing': [27, 28], 'sequence': 'A' * 9000}
    source.write_text(json.dumps(payload))
    inventory = {'evidence.json': sha256_file(source)}
    catalog = read_evidence(tmp_path, inventory)
    assert catalog['documents'] == [{'path': 'evidence.json', 'sha256': inventory['evidence.json']}]
    root = read_evidence(tmp_path, inventory, evidence_path='evidence.json')
    assert '/a~1b~0c' in [child['locator'] for child in root['children']]
    observed, values, offset = [], [], 0
    while offset is not None:
        page = read_evidence(tmp_path, inventory, evidence_path='evidence.json',
                             evidence_locator='/a~1b~0c', evidence_offset=offset)
        assert len(json.dumps(page)) < 9000 and not page['complete']
        assert page['coverage']['total_children'] == 45
        assert page['coverage']['complete_child_values'] == len(page['children'])
        assert not page['coverage']['whole_node_returned']
        observed.extend(item['locator'] for item in page['children'])
        values.extend(item['value'] for item in page['children'])
        offset = page['next_offset']
    assert observed == ['/a~1b~0c/' + str(i) for i in range(45)]
    assert values == payload['a/b~c']
    leaf = read_evidence(tmp_path, inventory, evidence_path='evidence.json', evidence_locator=observed[23])
    assert leaf['complete'] and leaf['value'] == payload['a/b~c'][23]
    assert cited_value(tmp_path, leaf) == leaf['value']
    fragment = read_evidence(tmp_path, inventory, evidence_path='evidence.json', evidence_locator='/sequence')
    assert fragment['next_offset'] == 1024 and 'value' not in fragment
    for args in ({'evidence_path': '../private.json'}, {'evidence_path': 'private-plan.json'},
                 {'evidence_path': 'evidence.json', 'evidence_locator': '/a~2b'},
                 {'evidence_path': 'evidence.json', 'evidence_locator': '/a~1b~0c/01'},
                 {'evidence_path': 'evidence.json', 'evidence_offset': -1},
                 {'evidence_path': 'evidence.json', 'evidence_locator': '/a~1b~0c', 'evidence_offset': 45}):
        with pytest.raises(ValueError):
            read_evidence(tmp_path, inventory, **args)
    source.write_text('{}')
    with pytest.raises(ValueError, match='checksum'):
        read_evidence(tmp_path, inventory, evidence_path='evidence.json')


def test_large_child_keeps_pointer_and_does_not_hide_following_records(tmp_path):
    source = tmp_path / 'candidates.json'
    payload = [{'candidate_id': str(i), 'mapping': 'x' * size}
               for i, size in enumerate((9000, 3000, 3000, 3000))]
    source.write_text(json.dumps(payload))
    inventory = {'candidates.json': sha256_file(source)}
    observed, offset = [], 0
    while offset is not None:
        page = read_evidence(tmp_path, inventory, evidence_path='candidates.json', evidence_offset=offset)
        assert len(json.dumps(page)) < 9000
        observed.extend(page['children'])
        offset = page['next_offset']
    assert [item['locator'] for item in observed] == ['/0', '/1', '/2', '/3']
    assert not observed[0]['complete'] and 'value' not in observed[0]
    assert [item['value'] for item in observed[1:]] == payload[1:]


def test_pdb_header_citations_preserve_source_lines_without_inventing_assembly(tmp_path):
    source = tmp_path / 'deposited.pdb'
    lines = ['HEADER    CONTROLLED SOURCE', 'REMARK 350 BIOMOLECULE: 1',
             'REMARK 350 AUTHOR DETERMINED BIOLOGICAL UNIT: MONOMERIC',
             'ATOM      1  CA  ALA A   1       0.000   0.000   0.000', 'END']
    source.write_text('\n'.join(lines) + '\n')
    inventory = {'deposited.pdb': sha256_file(source)}
    catalog = read_evidence(tmp_path, inventory)
    assert catalog['documents'][0]['path'] == 'deposited.pdb'
    header = read_evidence(tmp_path, inventory, evidence_path='deposited.pdb',
                           evidence_locator='/header_records/REMARK 350')
    assert header['value'] == [{'line_number': i + 1, 'text': lines[i]} for i in (1, 2)]
    assert cited_value(tmp_path, header) == header['value']
    root = read_evidence(tmp_path, inventory, evidence_path='deposited.pdb')
    assert set(root['value']['header_records']) == {'HEADER', 'REMARK 350'}
    with pytest.raises(ValueError, match='locator'):
        read_evidence(tmp_path, inventory, evidence_path='deposited.pdb', evidence_locator='/header_records/ATOM')
    citation = {k: header[k] for k in ('path', 'sha256', 'locator')}
    citation['observation'] = 'The deposited source declares a monomer; no independent validation is claimed.'
    review = {name: {'decision': 'evidence_insufficient',
        'rationale': 'Header declarations alone do not address all scientific concerns.',
        'evidence': [citation], 'limitations': ['No independent biological-context validation.']} for name in CONCERNS}
    ref = freeze(tmp_path, {'id': 'bound-source'}, inventory, review)
    assert load(tmp_path, ref, {'id': 'bound-source'}, inventory)['decision'] == 'evidence_insufficient'
    source.write_text('HEADER    NO ASSEMBLY DECLARATION\nEND\n')
    with pytest.raises(ValueError, match='checksum'):
        cited_value(tmp_path, citation)
    absent = read_evidence(tmp_path, {'deposited.pdb': sha256_file(source)}, evidence_path='deposited.pdb')
    assert 'REMARK 350' not in absent['value']['header_records']


@pytest.fixture
def evidence(tmp_path):
    source = tmp_path / 'source.json'
    source.write_text('{"assembly": "unresolved"}\n')
    inventory = {'source.json': sha256_file(source)}
    item = {'decision': 'evidence_insufficient',
        'rationale': 'The supplied measurements do not resolve the relevant scientific question.',
        'evidence': [{'path': 'source.json', 'sha256': inventory['source.json'],
                      'locator': '/assembly', 'observation': 'The source explicitly records an unresolved assembly.'}],
        'limitations': ['No independent biological-context verification is available.']}
    review = {name: deepcopy(item) for name in CONCERNS}
    return tmp_path, inventory, review


def test_complete_source_bound_judgment_is_repeatable_and_scope_limited(evidence):
    root, inventory, review = evidence
    validate_schema(review, REVIEW_SCHEMA)
    bound = {'artifact_id': 'fixture-input', 'manifest_sha256': inventory['source.json']}
    ref = freeze(root, bound, inventory, review)
    assert freeze(root, bound, inventory, review) == ref
    payload = load(root, ref, bound, inventory)
    assert payload['decision'] == 'evidence_insufficient'
    assert payload['reviewer_kind'] == 'agent_assessment'
    assert payload['cited_values']['assembly'][0]['value'] == 'unresolved'
    assert not payload['citation_entailment_verified']
    assert not payload['execution_authorized'] and not payload['biological_suitability_verified']
    with pytest.raises(ValueError, match='current bound evidence'):
        load(root, ref, {'artifact_id': 'another-study'}, inventory)
    path = root / ref['path']
    value = json.loads(path.read_text())
    value['biological_suitability_verified'] = True
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match='checksum'):
        load(root, ref, bound, inventory)


def test_vs_and_refinement_assessments_are_not_interchangeable(evidence):
    root, inventory, review = evidence
    bound = {'artifact_id':'vs-fixture'}
    for item in review.values():
        item['decision'] = 'suitable_for_exploratory_vs'
    ref = freeze(root, bound, inventory, review, scope='covdock-vs')
    payload = load(root, ref, bound, inventory, scope='covdock-vs')
    assert payload['review_scope'] == 'covdock-vs'
    assert not payload['execution_authorized']
    with pytest.raises(ValueError, match='judgment'):
        load(root, ref, bound, inventory)


@pytest.mark.parametrize('decisions,expected', [
    (['suitable_for_exploratory_refinement'] * 4, 'suitable_for_exploratory_refinement'),
    (['suitable_for_exploratory_refinement'] * 3 + ['evidence_insufficient'], 'evidence_insufficient'),
    (['evidence_insufficient'] * 3 + ['requires_revision'], 'requires_revision')])
def test_aggregate_keeps_one_unresolved_or_negative_concern(evidence, decisions, expected):
    root, inventory, review = evidence
    for item, decision in zip(review.values(), decisions):
        item['decision'] = decision
    assert validate(root, review, inventory) == expected
    ref = freeze(root, {'artifact_id': 'fixture-input'}, inventory, review)
    payload = load(root, ref, {'artifact_id': 'fixture-input'}, inventory)
    assert payload['decision'] == expected
    assert not payload['reactivity_validated'] and not payload['md_ready']


@pytest.mark.parametrize('change', ['missing_concern', 'extra_concern', 'empty_rationale',
    'missing_limitations', 'missing_citations', 'foreign_path', 'wrong_hash', 'empty_observation',
    'empty_locator', 'approval_claim'])
def test_unsupported_and_unbound_reviews_are_rejected(evidence, change):
    root, inventory, review = evidence
    item = review['assembly']
    if change == 'missing_concern':
        del review['reaction_scope']
    elif change == 'extra_concern':
        review['approved'] = True
    elif change == 'empty_rationale':
        item['rationale'] = ' ' * 30
    elif change == 'missing_limitations':
        item['limitations'] = []
    elif change == 'missing_citations':
        item['evidence'] = []
    elif change == 'foreign_path':
        item['evidence'][0]['path'] = '../other-study.json'
    elif change == 'wrong_hash':
        item['evidence'][0]['sha256'] = '0' * 64
    elif change == 'empty_observation':
        item['evidence'][0]['observation'] = ' ' * 30
    elif change == 'empty_locator':
        item['evidence'][0]['locator'] = ''
    elif change == 'approval_claim':
        item['human_approved'] = True
    with pytest.raises(ValueError):
        freeze(root, {'artifact_id': 'fixture-input'}, inventory, review)


def test_uncited_parent_change_also_revokes_the_review(evidence):
    root, inventory, review = evidence
    other = root / 'uncited.json'
    other.write_text('{}')
    inventory['uncited.json'] = sha256_file(other)
    ref = freeze(root, {'artifact_id': 'fixture-input'}, inventory, review)
    other.write_text('{"changed": true}')
    with pytest.raises(ValueError, match='checksum'):
        load(root, ref, {'artifact_id': 'fixture-input'}, inventory)


@pytest.mark.parametrize('locator', ['/nonexistent', '/assembly/0', 'assembly', '/~9bad'])
def test_existing_source_does_not_validate_a_fabricated_citation_location(evidence, locator):
    root, inventory, review = evidence
    review['assembly']['evidence'][0]['locator'] = locator
    with pytest.raises(ValueError, match='locator'):
        freeze(root, {'artifact_id': 'fixture-input'}, inventory, review)


def test_json_pointer_decodes_escaped_fields_and_array_indices(evidence):
    root, inventory, review = evidence
    source = root / 'source.json'
    source.write_text(json.dumps({'rows': [{'a/b~c': 'observed'}]}))
    inventory['source.json'] = sha256_file(source)
    for item in review.values():
        item['evidence'][0].update(sha256=inventory['source.json'], locator='/rows/0/a~1b~0c')
    ref = freeze(root, {'artifact_id': 'fixture-input'}, inventory, review)
    assert load(root, ref, {'artifact_id': 'fixture-input'}, inventory)['cited_values']['assembly'][0]['value'] == 'observed'


def test_motion_review_binds_all_concerns_and_current_evidence(tmp_path):
    from covscreen.receptor_environment_review import MOTION_CONCERNS, MOTION_REVIEW_SCHEMA
    source = tmp_path / 'motion.json'
    source.write_text('{"maximum_displacement_angstrom": 0.2}')
    inventory = {'motion.json': sha256_file(source)}
    citation = {'path': 'motion.json', 'sha256': inventory['motion.json'],
        'locator': '/maximum_displacement_angstrom',
        'observation': 'Synthetic measurement only; no real receptor acceptance.'}
    assessment = {name: {'decision': 'evidence_insufficient',
        'rationale': 'Synthetic displacement alone cannot establish binding-site preservation.',
        'evidence': [deepcopy(citation)], 'limitations': ['No actual receptor experiment.']}
        for name in MOTION_CONCERNS}
    validate_schema(assessment, MOTION_REVIEW_SCHEMA)
    ref = freeze(tmp_path, {'evaluation': 'synthetic'}, inventory, assessment, scope='restrained-receptor')
    result = load(tmp_path, ref, {'evaluation': 'synthetic'}, inventory, scope='restrained-receptor')
    assert result['decision'] == 'evidence_insufficient'
    assert result['execution_authorized'] is False and result['md_ready'] is False
    missing = deepcopy(assessment)
    missing.pop('target_site_preservation')
    with pytest.raises(ValueError, match='four concerns'):
        validate(tmp_path, missing, inventory, scope='restrained-receptor')
    with pytest.raises(ValueError):
        load(tmp_path, ref, {'evaluation': 'different'}, inventory, scope='restrained-receptor')
    source.write_text('{"maximum_displacement_angstrom": 9.0}')
    with pytest.raises(ValueError, match='checksum'):
        load(tmp_path, ref, {'evaluation': 'synthetic'}, inventory, scope='restrained-receptor')
