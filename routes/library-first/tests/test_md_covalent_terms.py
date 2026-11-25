"""Real local CMS terms plus deliberate corruptions; no simulated native engine."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from covscreen_library_first.md_parameterization import audit_covalent_terms


@pytest.fixture
def data():
    fixture = Path(__file__).parent / 'fixtures/library_038_covalent_terms.json'
    return json.loads(fixture.read_text())


def audit(data):
    return audit_covalent_terms(data['pair'], data['edges'], data['bonds'], data['angles'], data['dihedrals'])


def test_actual_cms_terms_cover_the_covalent_graph(data):
    result = audit(data)
    assert result['cross_link_angle_terms'] == 4
    assert result['cross_link_dihedral_terms'] == 9
    assert result['central_proper_torsions'] == 3
    assert result['equilibrium_length_A'] == 1.81


@pytest.mark.parametrize('group', ['bonds', 'angles', 'dihedrals'])
def test_every_individual_missing_term_is_detected(data, group):
    for index in range(len(data[group])):
        incomplete = deepcopy(data)
        incomplete[group].pop(index)
        with pytest.raises(ValueError, match='term|coverage'):
            audit(incomplete)


@pytest.mark.parametrize('group', ['bonds', 'angles', 'dihedrals'])
@pytest.mark.parametrize('bad', [float('nan'), float('inf'), True])
def test_bad_parameters_are_rejected(data, group, bad):
    data[group][0]['parameters'][1] = bad
    with pytest.raises(ValueError, match='parameters'):
        audit(data)


def test_improper_terms_cannot_substitute_for_proper_torsions(data):
    data['dihedrals'][0]['function'] = 'OPLS_IMPROPER'
    with pytest.raises(ValueError, match='proper-torsion coverage'):
        audit(data)


def test_explicit_zero_torsions_are_not_confused_with_missing_terms(data):
    for term in data['dihedrals']:
        term['parameters'] = [0.0] * 8
    assert audit(data)['local_graph_coverage_complete']


@pytest.mark.parametrize('group', ['bonds', 'angles', 'dihedrals'])
def test_duplicates_are_rejected_even_with_reversed_atom_order(data, group):
    duplicate = deepcopy(data[group][0])
    duplicate['atoms'].reverse()
    data[group].append(duplicate)
    with pytest.raises(ValueError, match='Duplicate'):
        audit(data)


def test_angle_constraint_label_does_not_hide_double_counting(data):
    duplicate = deepcopy(data['angles'][0])
    duplicate['function'] = 'Harm_constrained'
    data['angles'].append(duplicate)
    with pytest.raises(ValueError, match='Duplicate'):
        audit(data)


def test_angle_with_same_atoms_in_wrong_order_is_rejected(data):
    data['angles'][0]['atoms'][0:2] = reversed(data['angles'][0]['atoms'][0:2])
    with pytest.raises(ValueError, match='structure graph'):
        audit(data)


def test_complete_graph_not_parameter_tables_defines_required_terms(data):
    # An additional real graph neighbor would require new angles and torsions;
    # retaining the old parameter tables must not pass.
    data['edges'].append([data['pair'][0], 999999])
    with pytest.raises(ValueError, match='coverage'):
        audit(data)


def test_indices_are_not_specific_to_egfr_or_a_fixed_candidate(data):
    shifted = deepcopy(data)
    shifted['pair'] = [n + 500 for n in shifted['pair']]
    shifted['edges'] = [[n + 500 for n in edge] for edge in shifted['edges']]
    for group in ('bonds', 'angles', 'dihedrals'):
        for term in shifted[group]:
            term['atoms'] = [n + 500 for n in term['atoms']]
    assert audit(shifted)['central_proper_torsions'] == audit(data)['central_proper_torsions']
