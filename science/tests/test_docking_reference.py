import pytest
from rdkit import Chem

from covscreen.docking_reference import validate_reference
from covscreen.jobs import sha256_file


def asset(root):
    pdb = root / 'receptor.pdb'
    def atom(record, number, name, residue, chain, seq, x, y, z, element):
        return f'{record:<6}{number:5d} {name:^4} {residue:>3} {chain}{seq:4d}    {x:8.3f}{y:8.3f}{z:8.3f}{1:6.2f}{20:6.2f}          {element:>2}\n'
    pdb.write_text(atom('ATOM', 1, 'SG', 'CYS', 'A', 773, 1, 2, 3, 'S') +
                   atom('HETATM', 2, 'C1', 'REF', 'A', 999, 4, 5, 6, 'C'))
    sdf = root / 'reference.sdf'
    mol = Chem.MolFromSmiles('C')
    conf = Chem.Conformer(1)
    conf.SetAtomPosition(0, (4, 5, 6))
    mol.AddConformer(conf)
    with Chem.SDWriter(str(sdf)) as writer:
        writer.write(mol)
    return {'pdb': str(pdb), 'pdb_sha256': sha256_file(pdb), 'reference_ligand': str(sdf),
            'reference_sha256': sha256_file(sdf), 'component_id': 'REF', 'component_chain': 'A',
            'component_residue_id': '999'}


def test_reference_uses_author_number_and_matching_experimental_coordinates(tmp_path):
    value = asset(tmp_path)
    result = validate_reference(value, {'input_sha256': value['pdb_sha256'], 'model': 1},
                                {'author_chain': 'A', 'author_residue_id': '773', 'canonical_position': 797})
    assert result['covres'] == 'A:CYS:773'
    assert result['canonical_position'] == 797
    assert result['matched_heavy_atoms'] == 1


@pytest.mark.parametrize('change', ['hash', 'residue', 'component', 'pose', 'model'])
def test_reference_rejects_mismatched_scientific_inputs(tmp_path, change):
    value = asset(tmp_path)
    structure = {'input_sha256': value['pdb_sha256'], 'model': 1}
    site = {'author_chain': 'A', 'author_residue_id': '773', 'canonical_position': 797}
    if change == 'hash':
        structure['input_sha256'] = '0' * 64
    if change == 'residue':
        site['author_residue_id'] = '797'
    if change == 'component':
        value['component_id'] = 'OTHER'
    if change == 'model':
        structure['model'] = 2
    if change == 'pose':
        from pathlib import Path
        sdf = Path(value['reference_ligand'])
        mol = list(Chem.SDMolSupplier(str(sdf)))[0]
        mol.GetConformer().SetAtomPosition(0, (99, 99, 99))
        with Chem.SDWriter(str(sdf)) as writer:
            writer.write(mol)
        value['reference_sha256'] = sha256_file(sdf)
    with pytest.raises(ValueError):
        validate_reference(value, structure, site)
