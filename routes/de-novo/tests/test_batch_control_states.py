import pytest
from rdkit import Chem
from covscreen_de_novo.batch_control_states import export


def write(path, smiles):
    with Chem.MaeWriter(str(path)) as writer:
        for text in smiles:
            molecule = Chem.MolFromSmiles(text)
            Chem.rdDepictor.Compute2DCoords(molecule)
            writer.write(molecule)


def test_control_ionization_keeps_parent_and_regenerates_coordinates(tmp_path):
    neutral = 'CN(C)C/C=C/C(=O)NCC'
    charged = 'C[NH+](C)C/C=C/C(=O)NCC'
    source = tmp_path / 'states.mae'
    write(source, [neutral, charged])
    result = export(source, neutral, tmp_path / 'fresh')
    assert len(result['states']) == 2
    assert all(row['state'] == 'PREPARED_NOT_DOCKED' for row in result['states'])
    assert not result['docking_performed']


def test_control_state_rejects_graph_changes_and_duplicates(tmp_path):
    source = tmp_path / 'states.mae'
    neutral = 'CN(C)C/C=C/C(=O)NCC'
    write(source, [neutral, neutral])
    with pytest.raises(ValueError, match='duplicated'):
        export(source, neutral, tmp_path / 'duplicate')
    write(source, ['CN(C)C/C=C/C(=O)NCCC'])
    with pytest.raises(ValueError, match='parent graph'):
        export(source, neutral, tmp_path / 'changed')
