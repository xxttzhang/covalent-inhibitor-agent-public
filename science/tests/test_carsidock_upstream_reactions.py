"""Run unmodified upstream reaction functions without loading the GPU stack.

AST extraction omits unrelated ProDy/Torch imports only. This tests reaction
preparation, not model inference, pocket preparation, or complete docking.
"""
import ast
from pathlib import Path

import pytest
from rdkit.Chem import AllChem as Chem

from covscreen.upstream_handoff import identify_warhead
from covscreen.carsidock_reaction_guard import select_declared_products


def load_upstream_mol_edit():
    source = Path(__file__).resolve().parents[1] / "external/CarsiDock-Cov/src/utils/cov_prepare.py"
    if not source.is_file():
        pytest.skip("official CarsiDock-Cov checkout required")
    tree = ast.parse(source.read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in {"mol_edit", "set_mol_name"}]
    assert len(nodes) == 2
    namespace = {"Chem": Chem}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), namespace)
    return namespace["mol_edit"]


@pytest.mark.parametrize("smiles", [
    "C=CC(=O)Nc1ccccc1", "O=C(CCl)Nc1ccccc1",
    "C=CS(=O)(=O)Nc1ccccc1", "C#CC(=O)Nc1ccccc1",
])
def test_upstream_adds_cys_at_the_annotated_electrophile(smiles):
    mol = Chem.MolFromSmiles(smiles)
    annotation = identify_warhead(mol)
    mol.GetAtomWithIdx(annotation["reaction_atom_index"]).SetAtomMapNum(906)
    products = load_upstream_mol_edit()(mol, 16, "A:CYS:797", annotation["carsidock_reaction_type"])
    selected = select_declared_products(mol, products)
    assert selected
    for product in selected:
        covatoms = [a for a in product.GetAtoms() if a.HasProp("covatom")]
        assert len(covatoms) == 1
        assert covatoms[0].GetAtomicNum() == 16
        assert any(a.GetAtomMapNum() == 906 for a in covatoms[0].GetNeighbors())


def test_triple_bond_label_does_not_recognize_propiolamide():
    products = load_upstream_mol_edit()(
        Chem.MolFromSmiles("C#CC(=O)Nc1ccccc1"), 16, "A:CYS:797",
        "Nucleophilic Addition to a Triple Bond")
    assert products == []


def test_guard_removes_alternative_aryl_amide_substitution():
    mol = Chem.MolFromSmiles("O=C(CCl)Nc1ccccc1")
    mol.GetAtomWithIdx(identify_warhead(mol)["reaction_atom_index"]).SetAtomMapNum(906)
    products = load_upstream_mol_edit()(mol, 16, "A:CYS:797", "Nucleophilic Substitution")
    selected = select_declared_products(mol, products)
    assert 0 < len(selected) < len(products)


def test_guard_rejects_missing_declared_atom():
    with pytest.raises(ValueError, match="exactly one"):
        select_declared_products(Chem.MolFromSmiles("CC"), [])
