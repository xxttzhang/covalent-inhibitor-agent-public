import pytest
from rdkit import Chem
from covscreen.af3_adduct import transform_adduct


@pytest.mark.parametrize("family,smiles,expected", [
    ("acrylamide", "C=CC(=O)NC", "CCC(=O)NC"),
    ("chloroacetamide", "ClCC(=O)NC", "CC(=O)NC"),
    ("vinyl_sulfonamide", "C=CS(=O)(=O)NC", "CCS(=O)(=O)NC"),
    ("vinyl_sulfone", "C=CS(=O)(=O)C", "CCS(=O)(=O)C"),
    ("propiolamide", "C#CC(=O)NC", "C=CC(=O)NC"),
])
def test_adduct_connectivity(family, smiles, expected):
    mol, index, evidence = transform_adduct(smiles, family)
    assert Chem.MolToSmiles(mol) == Chem.MolToSmiles(Chem.MolFromSmiles(expected))
    assert mol.GetAtomWithIdx(index).GetSymbol() == "C"
    assert mol.GetAtomWithIdx(index).GetDegree() == 1
    assert evidence["warhead_match_count"] == 1
    assert not any(a.GetAtomMapNum() for a in mol.GetAtoms())


def test_reject_wrong_family_or_ambiguous_site():
    with pytest.raises(ValueError):
        transform_adduct("ClCC(=O)NC", "acrylamide")
    with pytest.raises(ValueError):
        transform_adduct("C=CC(=O)NCCNC(=O)C=C", "acrylamide")
