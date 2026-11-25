from pathlib import Path

import pytest

from covscreen.target_site_audit import audit_target_site

SOURCE = Path(__file__).resolve().parents[1] / 'projects/egfr-c797/ligand_first/source_structures/1M17.pdb'
MAPPING = {'status': 'sequence_mapping_verified_coordinates_pending', 'entry_id': '1M17',
           'uniprot': 'P00533', 'uniprot_residue': 797, 'auth_asym_id': 'A', 'author_residue_id': '773'}


def changed(tmp_path, transform):
    path = tmp_path / 'structure.pdb'
    path.write_text('\n'.join(transform(SOURCE.read_text().splitlines())) + '\n')
    return path


def at_site(line):
    return line.startswith('ATOM  ') and line[21] == 'A' and line[22:26].strip() == '773'


def test_actual_cys_is_audited_without_any_parent_ligand():
    result = audit_target_site(SOURCE, MAPPING)
    assert {a['name'] for a in result['site_heavy_atoms']} == {'N', 'CA', 'C', 'O', 'CB', 'SG'}
    assert result['canonical_residue'] == 797 and result['author_residue'] == '773'
    assert result['explicit_site_connections'] == []
    assert not result['receptor_prepared'] and not result['reactivity_validated']


@pytest.mark.parametrize('atom', ['N', 'CA', 'C', 'O', 'CB', 'SG'])
def test_each_missing_heavy_atom_stops_preparation(tmp_path, atom):
    path = changed(tmp_path, lambda lines: [l for l in lines if not (at_site(l) and l[12:16].strip() == atom)])
    with pytest.raises(ValueError, match='complete heavy-atom'):
        audit_target_site(path, MAPPING)


def test_author_insertion_code_is_retained(tmp_path):
    path = changed(tmp_path, lambda lines: [l[:26] + 'A' + l[27:] if at_site(l) else l for l in lines])
    assert audit_target_site(path, {**MAPPING, 'author_residue_id': '773A'})['author_residue'] == '773A'
    with pytest.raises(ValueError, match='absent'):
        audit_target_site(path, MAPPING)


def test_alternate_site_requires_selection(tmp_path):
    path = changed(tmp_path, lambda lines: [l[:16] + 'A' + l[17:] if at_site(l) else l for l in lines])
    with pytest.raises(ValueError, match='Alternate'):
        audit_target_site(path, MAPPING)
    assert audit_target_site(path, MAPPING, altloc='A')['altloc'] == 'A'


def test_zero_occupancy_and_bad_side_chain_geometry_are_rejected(tmp_path):
    path = changed(tmp_path, lambda lines: [l[:54] + '  0.00' + l[60:] if at_site(l) else l for l in lines])
    with pytest.raises(ValueError, match='zero-occupancy'):
        audit_target_site(path, MAPPING)
    path = changed(tmp_path, lambda lines: [l[:30] + ' 999.000' + l[38:]
        if at_site(l) and l[12:16].strip() == 'SG' else l for l in lines])
    with pytest.raises(ValueError, match='geometry'):
        audit_target_site(path, MAPPING)


def test_disulfide_and_ligand_connections_require_review(tmp_path):
    lines = SOURCE.read_text().splitlines()
    sg = next(int(l[6:11]) for l in lines if at_site(l) and l[12:16].strip() == 'SG')
    ligand = next(int(l[6:11]) for l in lines if l.startswith('HETATM') and l[17:20] == 'AQ4')
    path = changed(tmp_path, lambda rows: rows + [f'CONECT{sg:5d}{ligand:5d}'])
    assert audit_target_site(path, MAPPING)['status'] == 'site_linkage_review_required'
    record = list(' ' * 80)
    for start, value in [(0, 'SSBOND'), (15, 'A'), (17, ' 773'), (29, 'A'), (31, ' 751')]:
        record[start:start + len(value)] = value
    path = changed(tmp_path, lambda rows: rows + [''.join(record)])
    result = audit_target_site(path, MAPPING)
    assert result['explicit_site_connections'][0]['record'] == 'SSBOND'
    assert not result['campaign_ready']


def test_duplicate_serials_fail_in_shared_parser(tmp_path):
    path = changed(tmp_path, lambda lines: lines + [next(l for l in lines if at_site(l))])
    with pytest.raises(ValueError, match='Duplicate atom serials'):
        audit_target_site(path, MAPPING)
