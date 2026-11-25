import pandas as pd

from rdkit import Chem

from covscreen.chemistry import prepare_sdf, select_warhead_match


def test_invalid_smiles_is_recorded(tmp_path):
    frame = pd.DataFrame([{"compound_id": "bad", "standardized_smiles": "not-a-smiles", "label": "active"}])
    failures = prepare_sdf(frame, tmp_path / "x.sdf")
    assert len(failures) == 1
    assert failures.iloc[0]["compound_id"] == "bad"


def test_warhead_selection_prefers_less_substituted_beta_carbon():
    molecule = Chem.MolFromSmiles("C=CC(=O)NC1=CC(=O)N(C)C1")
    query = Chem.MolFromSmarts("[C:1]=[C:2][C:3](=[O:4])[N:5]")
    match, count = select_warhead_match(molecule, query)
    assert count == 2
    beta = molecule.GetAtomWithIdx(match[0])
    assert sum(n.GetAtomicNum() > 1 for n in beta.GetNeighbors()) == 1
