import json

import pytest
from rdkit import Chem

from covscreen.af3_results import (
    adduct_electrophile_name,
    af3_sequence_residue_from_config,
    af3_atom_name_for_rdkit_index,
    confidence_summary,
    mapped_warhead_atom_name,
)


@pytest.mark.parametrize("smiles,family,expected", [
    ("NC(=O)C=C", "acrylamide", "C3"),
    ("NC(=O)CCl", "chloroacetamide", "C2"),
    ("NC(=O)C#C", "propiolamide", "C3"),
    ("NS(=O)(=O)C=C", "vinyl_sulfonamide", "C2"),
])
def test_reaction_specific_af3_electrophile_names(smiles, family, expected):
    assert adduct_electrophile_name(smiles, family) == expected


def test_smiles_atom_names_match_af3_element_counters():
    mol = Chem.MolFromSmiles("C=CC(=O)N")
    assert af3_atom_name_for_rdkit_index(mol, 0) == "C1"
    assert af3_atom_name_for_rdkit_index(mol, 2) == "C3"
    assert af3_atom_name_for_rdkit_index(mol, 3) == "O1"
    assert mapped_warhead_atom_name(
        "C=CC(=O)N", "[C:1]=[C:2][C:3](=[O:4])[N:5]"
    ) == "C1"


def test_warhead_mapping_resolves_ambiguous_match_deterministically():
    assert mapped_warhead_atom_name(
        "C=CC(=O)NC=CC(=O)N",
        "[C:1]=[C:2][C:3](=[O:4])[N:5]",
    ) == "C1"


def test_af3_sequence_residue_prefers_explicit_sequence_index():
    assert af3_sequence_residue_from_config(
        {
            "cysteine_sequence_index": 105,
            "residue_number": 797,
            "sequence_start_residue": 693,
        }
    ) == 105


def test_af3_sequence_residue_supports_legacy_source_numbering():
    assert af3_sequence_residue_from_config(
        {"residue_number": 481, "sequence_start_residue": 382}
    ) == 100


def test_af3_sequence_residue_requires_a_resolvable_mapping():
    with pytest.raises(ValueError, match="cysteine_sequence_index"):
        af3_sequence_residue_from_config({"residue_number": 797})


def test_confidence_summary_extracts_cross_chain_values(tmp_path):
    summary = {
        "ranking_score": 0.8,
        "iptm": 0.7,
        "ptm": 0.6,
        "has_clash": 0.0,
        "chain_pair_pae_min": [[1.0, 2.0], [7.5, 3.0]],
    }
    confidence = {
        "token_chain_ids": ["A", "A", "L"],
        "pae": [[1, 2, 3], [4, 5, 6], [7, 8, 9]],
        "atom_chain_ids": ["A", "L", "L"],
        "atom_plddts": [90, 70, 80],
    }
    summary_path = tmp_path / "summary.json"
    confidence_path = tmp_path / "confidence.json"
    summary_path.write_text(json.dumps(summary))
    confidence_path.write_text(json.dumps(confidence))
    result = confidence_summary(summary_path, confidence_path)
    assert result["protein_to_ligand_pae_min"] == 3
    assert result["ligand_to_protein_pae_min"] == 7
    assert result["mpae"] == 7.5
    assert result["protein_ligand_pae_mean"] == pytest.approx(6)
    assert result["ligand_plddt_mean"] == 75
