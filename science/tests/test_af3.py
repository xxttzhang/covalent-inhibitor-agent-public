import json

import pandas as pd

from covscreen.af3 import make_af3_smiles_input, write_af3_inputs


def _row():
    return pd.Series(
        {
            "compound_id": "B1852",
            "parent_id": "B1852",
            "protomer_id": "1",
            "label": "active",
            "matched_active_id": "B1852",
            "input_sdf_index": 27,
            "standardized_smiles": "C=CC(=O)NCC",
        }
    )


def test_make_af3_smiles_input_preserves_identity():
    payload = make_af3_smiles_input(_row(), "ACDC", 7)
    assert payload["version"] == 4
    assert payload["modelSeeds"] == [7]
    assert payload["sequences"][0]["protein"]["sequence"] == "ACDC"
    ligand = payload["sequences"][1]["ligand"]
    assert ligand["smiles"] == "C=CC(=O)NCC"
    assert json.loads(ligand["description"])["compound_id"] == "B1852"
    assert "bondedAtomPairs" not in payload


def test_write_af3_inputs_creates_manifest(tmp_path):
    manifest = write_af3_inputs(
        pd.DataFrame([_row()]),
        protein_sequence="ACDC",
        output_dir=tmp_path,
        seed=11,
    )
    assert len(manifest) == 1
    assert manifest.loc[0, "covalent_bond_constraint"] == False  # noqa: E712
    assert (tmp_path / "BTK_B1852_protomer_1.json").is_file()


def test_make_af3_input_supports_non_btk_target():
    payload = make_af3_smiles_input(
        _row(),
        "ACDC",
        7,
        target_id="EGFR",
        protein_description="EGFR 4G5J chain A; Cys797 is sequence index 102",
    )
    assert payload["name"].startswith("EGFR_")
    assert "Cys797" in payload["sequences"][0]["protein"]["description"]
