import json
from pathlib import Path

import pandas as pd
import pytest
from rdkit import Chem

from covscreen.upstream_handoff import (identify_warhead, export_handoff, bind_full_parent_atoms,
                                       full_parent_attachment_geometry)
from covscreen.upstream_parent_mapping import parent_mapping_evidence


@pytest.mark.parametrize("smiles,name", [
    ("C=CC(=O)Nc1ccccc1", "acrylamide"),
    ("O=C(CCl)Nc1ccccc1", "chloroacetamide"),
    ("C=CS(=O)(=O)Nc1ccccc1", "vinyl_sulfonamide"),
    ("C#CC(=O)Nc1ccccc1", "propiolamide"),
])
def test_reaction_atom_is_carbon_and_warhead_is_explicit(smiles, name):
    mol = Chem.MolFromSmiles(smiles)
    row = identify_warhead(mol)
    assert row["warhead_class"] == name
    assert mol.GetAtomWithIdx(row["reaction_atom_index"]).GetAtomicNum() == 6
    if name == "chloroacetamide":
        assert any(a.GetAtomicNum() == 17 for a in mol.GetAtomWithIdx(row["reaction_atom_index"]).GetNeighbors())


def test_two_warheads_are_rejected():
    with pytest.raises(ValueError, match="observed 2"):
        identify_warhead(Chem.MolFromSmiles("C=CC(=O)NCCNC(=O)C=C"))


def test_missing_parent_and_symmetric_parent_have_distinct_failure_reasons():
    product = Chem.MolFromSmiles('C=CC(=O)Nc1ccccc1')
    with pytest.raises(ValueError, match='multiple graph mappings.*not evidence of chemical infeasibility'):
        bind_full_parent_atoms(Chem.MolFromSmiles('c1ccccc1'), product)
    with pytest.raises(ValueError, match='no chirality-aware full-parent match'):
        bind_full_parent_atoms(Chem.MolFromSmiles('c1ccncc1'), product)


def test_full_parent_binding_rebuilds_untrusted_maps_and_round_trips():
    parent = Chem.MolFromSmiles('COc1ncccc1F')
    source = Chem.MolFromSmiles('COc1ncc(NC(=O)C=C)cc1F')
    for atom in source.GetAtoms():
        atom.SetAtomMapNum(906)
    bound, evidence = bind_full_parent_atoms(parent, source)
    restored = Chem.MolFromMolBlock(Chem.MolToMolBlock(bound))
    mapping = evidence['parent_atom_index_to_product_index']
    assert len(mapping) == parent.GetNumAtoms()
    for index, target in mapping.items():
        assert restored.GetAtomWithIdx(target).GetAtomicNum() == parent.GetAtomWithIdx(index).GetAtomicNum()
        assert restored.GetAtomWithIdx(target).GetAtomMapNum() == 100 + index
    for bond in parent.GetBonds():
        mapped = restored.GetBondBetweenAtoms(mapping[bond.GetBeginAtomIdx()], mapping[bond.GetEndAtomIdx()])
        assert mapped.GetBondType() == bond.GetBondType()
    assert sum(atom.GetAtomMapNum() == 906 for atom in restored.GetAtoms()) == 1
    assert evidence['reaction_ancestry_verified'] is False
    assert evidence['pose_assessment'] == 'NOT_RUN'
    assert source.GetAtomWithIdx(0).GetAtomMapNum() == 906


@pytest.mark.parametrize('parent,product,reason', [
    ('c1ccccc1', 'C=CC(=O)Nc1ccccc1', 'unambiguous'),
    ('COc1ncccc1F', 'COc1ncc(NC(=O)C=C)cc1Cl', 'unambiguous'),
    ('C[C@H](O)c1ncccc1F', 'C[C@@H](O)c1ncc(NC(=O)C=C)cc1F', 'unambiguous'),
    ('COc1ncccc1F', 'COc1nc(C)c(NC(=O)C=C)cc1F', 'one attachment'),
    ('COc1ncccc1F', 'COc1ncc(NC(=O)CCl)cc1F', 'requires acrylamide'),
    ('C=CC(=O)NCc1ncccc1F', 'C=CC(=O)NCc1ncc(C)cc1F', 'overlap'),
])
def test_full_parent_binding_rejects_incompatible_or_ambiguous_inputs(parent, product, reason):
    with pytest.raises(ValueError, match=reason):
        bind_full_parent_atoms(Chem.MolFromSmiles(parent), Chem.MolFromSmiles(product))


@pytest.mark.parametrize('linker,name', [('', 'direct'), ('C', 'methylene'), ('CC', 'ethylene')])
def test_full_parent_linker_maps_survive_sdf_roundtrip(linker, name):
    bound, evidence = bind_full_parent_atoms(
        Chem.MolFromSmiles('COc1ncccc1F'),
        Chem.MolFromSmiles(f'COc1ncc({linker}NC(=O)C=C)cc1F'))
    restored = Chem.MolFromMolBlock(Chem.MolToMolBlock(bound))
    assert evidence['linker_name'] == name
    assert evidence['linker_heavy_atom_count'] == len(linker)
    assert all(atom.GetAtomMapNum() > 0 for atom in restored.GetAtoms())
    for number, index in evidence['linker_atom_map_to_product_index'].items():
        assert restored.GetAtomWithIdx(index).GetAtomMapNum() == number


@pytest.mark.parametrize('linker', ['CCC', 'C(C)', 'O', 'C=C'])
def test_full_parent_binding_rejects_out_of_contract_linkers(linker):
    with pytest.raises(ValueError, match='linker'):
        bind_full_parent_atoms(Chem.MolFromSmiles('COc1ncccc1F'),
                              Chem.MolFromSmiles(f'COc1ncc({linker}NC(=O)C=C)cc1F'))


def test_addition_geometry_uses_inferred_hydrogen_and_preserves_parent_pose():
    from rdkit.Chem import AllChem
    source = Chem.AddHs(Chem.MolFromSmiles('COc1ncccc1F'))
    assert AllChem.EmbedMolecule(source, randomSeed=27) == 0
    parent = Chem.RemoveHs(source)
    before = Chem.MolToMolBlock(parent)
    anchor = next(a.GetIdx() for a in parent.GetAtoms() if a.GetIsAromatic()
                  and a.GetSymbol() == 'C' and a.GetTotalNumHs() == 1)
    with_h = Chem.AddHs(parent, addCoords=True)
    hydrogen = next(a.GetIdx() for a in with_h.GetAtomWithIdx(anchor).GetNeighbors()
                    if a.GetAtomicNum() == 1)
    origin = with_h.GetConformer().GetAtomPosition(anchor)
    direction = with_h.GetConformer().GetAtomPosition(hydrogen) - origin
    direction.Normalize()
    limits = dict(max_distance_angstrom=12, max_vector_angle_degrees=100)
    forward = full_parent_attachment_geometry(parent, anchor, tuple(origin + direction * 5), **limits)
    reverse = full_parent_attachment_geometry(parent, anchor, tuple(origin - direction * 5), **limits)
    distant = full_parent_attachment_geometry(parent, anchor, tuple(origin + direction * 13), **limits)
    assert forward['prefilter_passed']
    assert forward['inferred_hydrogen_angles_degrees'][0] == pytest.approx(0, abs=1e-5)
    assert not forward['direction_is_experimental']
    assert forward['pose_assessment'] == 'NOT_RUN'
    assert not reverse['prefilter_passed']
    assert reverse['inferred_hydrogen_angles_degrees'][0] == pytest.approx(180, abs=1e-5)
    assert not distant['prefilter_passed']
    no_h = next(a.GetIdx() for a in parent.GetAtoms() if a.GetSymbol() == 'C' and a.GetTotalNumHs() == 0)
    assert not full_parent_attachment_geometry(parent, no_h, (100, 100, 100), **limits)['prefilter_passed']
    assert Chem.MolToMolBlock(parent) == before
    with pytest.raises(ValueError, match='finite target'):
        full_parent_attachment_geometry(parent, anchor, (float('nan'), 0, 0), **limits)
    parent.RemoveAllConformers()
    with pytest.raises(ValueError, match='3D parent'):
        full_parent_attachment_geometry(parent, anchor, (1, 2, 3), **limits)


def test_sdf_table_identity_and_atom_map_round_trip(tmp_path):
    smiles = "C=CC(=O)Nc1ccccc1"
    source = tmp_path / "source.json"
    source.write_text(json.dumps({"parent_gate_evaluated": True, "candidates": [{
        "candidate_id": "probe_1", "smiles": smiles, "input_smiles": "c1ccccc1", "input_id": "parent",
        "parent_mapping": parent_mapping_evidence("c1ccccc1", "c1ccccc1", smiles, min_parent_fraction=1)
    }]}))
    result = export_handoff(source, tmp_path / "out", target_id="EGFR", site_id="C797", parent_id="probe")
    assert result["accepted_count"] == 1
    table = pd.read_csv(result["outputs"]["ligand_first_candidates.csv"]["path"])
    mol = next(iter(Chem.SDMolSupplier(result["outputs"]["ligand_first_candidates.sdf"]["path"])))
    assert mol.GetProp("COMPOUND_ID") == table.iloc[0].compound_id
    assert int(mol.GetProp("INPUT_SDF_INDEX")) == table.iloc[0].input_sdf_index
    assert mol.GetAtomWithIdx(int(table.iloc[0].reaction_atom_index)).GetAtomMapNum() == 906
    assert not mol.GetConformer().Is3D()
