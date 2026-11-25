import pytest
from covscreen.structure_coverage import observed_coverage
from covscreen.jobs import sha256_file


def atom(n, insertion='', chain='A', record='ATOM'):
    return f'{record:<6}{1:5d} {"CA":^4} {"ALA":>3} {chain}{n:4d}{insertion:1}   {1:8.3f}{2:8.3f}{3:8.3f}{1:6.2f}{20:6.2f}           C\n'


def test_observed_ranges_preserve_gaps_insertions_chain_and_model(tmp_path):
    path = tmp_path / 'input.pdb'
    path.write_text('MODEL        1\n' + atom(10) + atom(11) + atom(11, 'A') + atom(14) + atom(99, chain='B') + atom(100, record='HETATM') + 'ENDMDL\nMODEL        2\n' + atom(500) + 'ENDMDL\n')
    result = observed_coverage(path, {'input_sha256': sha256_file(path), 'model': 1, 'author_chain': 'A'})
    assert result['observed_author_ranges'] == [[10, 11], [14, 14]]
    assert result['observed_polymer_residue_count'] == 4
    assert result['insertion_coded_residues'] == ['11A']
    assert result['full_length_coverage_verified'] is False
    assert result['canonical_coverage_status'].startswith('UNKNOWN')


@pytest.mark.parametrize('change', ['hash', 'chain', 'model', 'nonfinite', 'ambiguous'])
def test_invalid_coverage_is_rejected(tmp_path, change):
    path = tmp_path / 'input.pdb'
    text = atom(10)
    if change == 'nonfinite':
        text = text[:30] + '     nan' + text[38:]
    if change == 'ambiguous':
        text += atom(10).replace('ALA', 'GLY')
    path.write_text(text)
    structure = {'input_sha256': sha256_file(path), 'model': 1, 'author_chain': 'A'}
    if change == 'hash': structure['input_sha256'] = '0' * 64
    if change == 'chain': structure['author_chain'] = 'B'
    if change == 'model': structure['model'] = 2
    with pytest.raises(ValueError):
        observed_coverage(path, structure)


def test_mapped_coverage_keeps_internal_gaps_and_insertion_uncertainty():
    from covscreen.structure_coverage import mapped_coordinate_coverage
    residues = {1: {'author_residue_number': '10', 'insertion_code': None},
                2: {'author_residue_number': '11', 'insertion_code': None},
                3: {'author_residue_number': '11', 'insertion_code': 'A'},
                4: {'author_residue_number': '14', 'insertion_code': None}}
    report = mapped_coordinate_coverage(residues, {'10': 2, '11': 3, '14': 6}, 8)
    assert report['observed_canonical_ranges'] == [[2, 3], [6, 6]]
    assert report['canonical_positions_without_verified_coordinates'] == [[1, 1], [4, 5], [7, 8]]
    assert report['unmapped_observed_author_residues'] == ['11A']
    assert report['canonical_coordinate_fraction'] == 3 / 8
    assert report['mapping_complete_for_observed_residues'] is False
    assert report['all_canonical_positions_have_coordinates'] is False


@pytest.mark.parametrize('mapping', [{'10': 0}, {'10': 9}, {'10': True}, {'10': 2, '11': 2}])
def test_mapped_coverage_rejects_invalid_or_ambiguous_positions(mapping):
    from covscreen.structure_coverage import mapped_coordinate_coverage
    residues = {1: {'author_residue_number': '10', 'insertion_code': None},
                2: {'author_residue_number': '11', 'insertion_code': None}}
    with pytest.raises(ValueError):
        mapped_coordinate_coverage(residues, mapping, 8)
