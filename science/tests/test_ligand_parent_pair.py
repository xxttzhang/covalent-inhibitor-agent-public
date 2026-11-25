import pytest

from covscreen.ligand_parent_pair import assess, cys_connections


def records(distance=5.9):
    return ({'status': 'coordinate_checks_passed_linkage_review_pending',
             'structure_sha256': 'a' * 64, 'sg_ligand_min_distance_angstrom': distance,
             'cys_environment': {'connections': []}},
            {'status': 'declared_parent_identity_and_heavy_atoms_verified',
             'structure_sha256': 'a' * 64})


def test_nearby_parent_is_only_eligible_for_further_review():
    result = assess(*records())
    assert result['status'] == 'ELIGIBLE_FOR_ENUMERATION_REVIEW'
    assert not result['execution_ready']
    assert result['reaction_feasibility'] == 'not_established'


@pytest.mark.parametrize('distance,status', [(6.0, 'OUTSIDE_CURRENT_SCOPE'),
    (9.0, 'OUTSIDE_CURRENT_SCOPE'), (2.1, 'EVIDENCE_INSUFFICIENT'),
    (float('nan'), 'EVIDENCE_INSUFFICIENT'), (True, 'EVIDENCE_INSUFFICIENT')])
def test_distance_boundaries_do_not_imply_molecular_failure(distance, status):
    assert assess(*records(distance))['status'] == status


def test_unknown_or_modified_site_cannot_pass():
    audit, chemical = records()
    audit.pop('cys_environment')
    assert assess(audit, chemical)['status'] == 'EVIDENCE_INSUFFICIENT'
    audit['cys_environment'] = {'connections': [{'record': 'SSBOND'}]}
    assert 'cys_connection_requires_review' in assess(audit, chemical)['reasons']


def test_same_distance_from_different_structure_is_not_accepted():
    audit, chemical = records()
    chemical['structure_sha256'] = 'b' * 64
    assert assess(audit, chemical)['status'] == 'EVIDENCE_INSUFFICIENT'


def test_explicit_cys_connection_handles_insertion_code_and_other_chain():
    line = list(' ' * 80)
    for start, text in [(0, 'SSBOND'), (15, 'B'), (17, ' 773'), (21, 'A'),
                        (29, 'C'), (31, '  42')]:
        line[start:start+len(text)] = text
    site = {'chain': 'B', 'residue': '773A', 'serial': 1}
    assert cys_connections([''.join(line)], [], site)['connections']
    site['chain'] = 'A'
    assert not cys_connections([''.join(line)], [], site)['connections']


@pytest.mark.parametrize('element,name,distance,key', [('S', 'SG', 2.5, 'nearby_sulfur'),
    ('ZN', 'ZN', 2.4, 'nearby_metals')])
def test_unannotated_contacts_require_review(element, name, distance, key):
    site = {'chain': 'A', 'residue': '773', 'serial': 1, 'xyz': (0, 0, 0), 'altloc': ''}
    atom = {'chain': 'B', 'residue': '10', 'serial': 2, 'xyz': (distance, 0, 0),
            'altloc': '', 'occupancy': 1, 'element': element, 'name': name}
    environment = cys_connections([], [atom], site)
    assert environment[key][0]['distance_angstrom'] == distance
    audit, chemical = records()
    audit['cys_environment'] = environment
    assert assess(audit, chemical)['status'] == 'EVIDENCE_INSUFFICIENT'
    atom['occupancy'] = 0
    assert not cys_connections([], [atom], site)[key]
