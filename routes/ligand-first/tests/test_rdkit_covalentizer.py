from pathlib import Path
import os

import pytest
from rdkit import Chem

from covscreen.ligand_first_inputs import build_parent_pose_mol, load_campaign
from covscreen.rdkit_covalentizer import (
    FORMAL_COVALENTIZER_TRANSFORMATIONS,
    WARHEAD_ATOM_MAPS,
    covalentize_parent,
    evaluate_chemistry_quality,
)


ROOT = (Path(__file__).resolve().parents[3] / "science")
CAMPAIGN_PATH = Path(os.environ.get('COVALENTIZER_TEST_CAMPAIGN',
    str(ROOT / 'projects/egfr-c797/ligand_first'))) / 'campaign.json'


def _campaign_and_parent():
    campaign = load_campaign(CAMPAIGN_PATH)
    return campaign, build_parent_pose_mol(campaign, CAMPAIGN_PATH.parent)


def test_real_parent_is_covalentized_only_at_the_approved_c6_exit(tmp_path):
    campaign, parent = _campaign_and_parent()
    candidate, product = covalentize_parent(
        parent,
        campaign,
        "quinazoline-c6-primary",
        "replace_aryl_ether_with_acrylamide",
    )
    assert candidate.quality_gate.status == "PASS", candidate.quality_gate.failure_reasons
    assert candidate.warhead_name == "acrylamide"
    assert candidate.removed_parent_atom_indices == [10, 11, 12, 13, 14]
    assert candidate.quality_gate.radical_electron_count == 0
    assert candidate.quality_gate.formal_charge == 0
    assert candidate.quality_gate.warhead_match_count == 1
    assert candidate.quality_gate.reaction_atom_mapping_valid is True
    assert candidate.quality_gate.protected_core_retained is True
    assert candidate.quality_gate.protected_pharmacophore_retained is True
    assert candidate.quality_gate.protected_substituent_bonds_retained is True
    reaction_index = candidate.warhead_atom_map_to_product_index[
        WARHEAD_ATOM_MAPS["reaction_beta_carbon"]
    ]
    assert candidate.quality_gate.reaction_atom_index == reaction_index
    assert product.GetAtomWithIdx(reaction_index).GetSymbol() == "C"
    output = tmp_path / "candidate.sdf"
    writer = Chem.SDWriter(str(output))
    writer.write(product)
    writer.close()
    reread = next(mol for mol in Chem.SDMolSupplier(str(output), removeHs=False) if mol is not None)
    persisted = [
        atom.GetIdx()
        for atom in reread.GetAtoms()
        if atom.GetAtomMapNum() == WARHEAD_ATOM_MAPS["reaction_beta_carbon"]
    ]
    assert persisted == [reaction_index]


def test_protected_c7_exit_cannot_be_modified():
    campaign, parent = _campaign_and_parent()
    with pytest.raises(ValueError, match="protected/non-advancing"):
        covalentize_parent(
            parent,
            campaign,
            "quinazoline-c7-negative-control",
            "replace_aryl_ether_with_acrylamide",
        )


def test_quality_gate_rejects_a_radical_even_when_other_graph_checks_pass():
    campaign, parent = _campaign_and_parent()
    _, product = covalentize_parent(
        parent,
        campaign,
        "quinazoline-c6-primary",
        "replace_aryl_ether_with_acrylamide",
    )
    product.GetAtomWithIdx(0).SetNumRadicalElectrons(1)
    gate = evaluate_chemistry_quality(product, parent, campaign)
    assert gate.status == "FAIL"
    assert gate.radical_electron_count == 1
    assert any("radical electrons" in reason for reason in gate.failure_reasons)


def test_quality_gate_rejects_missing_reaction_atom_map():
    campaign, parent = _campaign_and_parent()
    _, product = covalentize_parent(
        parent,
        campaign,
        "quinazoline-c6-primary",
        "replace_aryl_ether_with_acrylamide",
    )
    reaction_atom = next(
        atom
        for atom in product.GetAtoms()
        if atom.GetAtomMapNum() == WARHEAD_ATOM_MAPS["reaction_beta_carbon"]
    )
    reaction_atom.SetAtomMapNum(0)
    gate = evaluate_chemistry_quality(product, parent, campaign)
    assert gate.status == "FAIL"
    assert gate.reaction_atom_mapping_valid is False
    assert gate.reaction_atom_index is None


def test_real_parent_supports_four_warheads_and_three_short_linkers():
    campaign, parent = _campaign_and_parent()
    four_warheads = [
        "acrylamide",
        "chloroacetamide",
        "vinyl_sulfone",
        "propiolamide",
    ]
    primary = campaign.exit_vectors[0].model_copy(
        update={"allowed_transformations": list(FORMAL_COVALENTIZER_TRANSFORMATIONS)}
    )
    expanded = campaign.model_copy(
        update={
            "allowed_warheads": four_warheads,
            "exit_vectors": [primary, campaign.exit_vectors[1]],
        }
    )
    candidates = []
    for transformation in FORMAL_COVALENTIZER_TRANSFORMATIONS:
        candidate, _ = covalentize_parent(
            parent,
            expanded,
            "quinazoline-c6-primary",
            transformation,
        )
        assert candidate.quality_gate.status == "PASS", (
            transformation,
            candidate.quality_gate.failure_reasons,
        )
        candidates.append(candidate)
    assert len(candidates) == 12
    assert {candidate.warhead_name for candidate in candidates} == set(four_warheads)
    assert {candidate.linker_name for candidate in candidates} == {
        "direct",
        "methylene",
        "ethylene",
    }
