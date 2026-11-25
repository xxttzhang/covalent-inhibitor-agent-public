"""Parser/gate fixtures only; these tiny structures are not AF3 predictions."""
import copy
import json
import runpy
from pathlib import Path

import numpy as np
import pytest
import yaml
from Bio.PDB import MMCIFIO
from Bio.PDB.Atom import Atom
from Bio.PDB.Chain import Chain
from Bio.PDB.Model import Model
from Bio.PDB.Residue import Residue
from Bio.PDB.Structure import Structure

from covscreen.af3_results import confidence_summary, reactive_distance, af3_sequence_residue_from_config, parse_af3_job


def confidence_files(root, mutate=lambda summary, confidence: None):
    summary = {"ranking_score": 0.8, "iptm": 0.7, "ptm": 0.6, "has_clash": False,
               "chain_pair_pae_min": [[1.0, 2.0], [7.5, 3.0]]}
    confidence = {"token_chain_ids": ["A", "A", "L"], "pae": [[1, 2, 3], [4, 5, 6], [7, 8, 9]],
                  "atom_chain_ids": ["A", "L", "L"], "atom_plddts": [90, 70, 80]}
    mutate(summary, confidence)
    summary_path, confidence_path = root / "summary.json", root / "confidences.json"
    summary_path.write_text(json.dumps(summary))
    confidence_path.write_text(json.dumps(confidence))
    return summary_path, confidence_path


@pytest.mark.parametrize("mutate", [
    lambda s, c: c.update(pae=[[1, 2], [3, 4]]),
    lambda s, c: c["pae"][0].append(0),
    lambda s, c: c["pae"][0].__setitem__(0, float("nan")),
    lambda s, c: c["pae"][0].__setitem__(0, -1),
    lambda s, c: c.update(token_chain_ids=[["A"], ["L"]]),
    lambda s, c: c.update(atom_chain_ids=["A", "A", "A"]),
    lambda s, c: c.update(atom_plddts=[90, 70]),
    lambda s, c: c.update(atom_plddts=[90, 101, 80]),
    lambda s, c: c.update(atom_plddts=[90, float("inf"), 80]),
    lambda s, c: s.update(chain_pair_pae_min=[[1]]),
    lambda s, c: s.update(chain_pair_pae_min=[[1, 2], [float("nan"), 3]]),
    lambda s, c: s.update(iptm=1.2),
    lambda s, c: s.update(ptm=True),
    lambda s, c: s.update(ranking_score=float("inf")),
    lambda s, c: s.update(has_clash=0.5),
    lambda s, c: s.update(has_clash="false"),
])
def test_invalid_confidence_cannot_produce_a_success_score(tmp_path, mutate):
    with pytest.raises(ValueError):
        confidence_summary(*confidence_files(tmp_path, mutate))


def test_valid_summary_preserves_project_mpae_direction_and_fallback(tmp_path):
    result = confidence_summary(*confidence_files(tmp_path))
    assert result["mpae"] == 7.5  # Keep the declared summary metric, not an invented average.
    assert result["ligand_plddt_mean"] == 75
    fallback = confidence_summary(*confidence_files(tmp_path, lambda s, c: s.pop("chain_pair_pae_min")))
    assert fallback["mpae"] == 7
    with pytest.raises(ValueError, match="Distinct"):
        confidence_summary(*confidence_files(tmp_path), protein_chain="L", ligand_chain="L")


@pytest.mark.parametrize("config", [
    {"cysteine_sequence_index": 105.5}, {"cysteine_sequence_index": True}, {"cysteine_sequence_index": 0},
    {"residue_number": 105, "sequence_start_residue": 106},
    {"residue_number": 105, "sequence_start_residue": 1.5},
])
def test_residue_mapping_cannot_truncate_or_wrap_invalid_positions(config):
    with pytest.raises(ValueError, match="positive integer"):
        af3_sequence_residue_from_config(config)


def structure_file(root, mutate=lambda structure: None):
    structure = Structure("fixture")
    model = Model(0)
    structure.add(model)
    protein, ligand = Chain("A"), Chain("L")
    model.add(protein)
    model.add(ligand)
    cysteine = Residue((" ", 105, " "), "CYS", " ")
    component = Residue(("H_LIG", 1, " "), "LIG", " ")
    protein.add(cysteine)
    ligand.add(component)
    cysteine.add(Atom("SG", np.array([0., 0., 0.]), 20., 1., " ", " SG ", 1, element="S"))
    # Atom inventory of the hydrogen-capped NC(=O)C=C Michael adduct.
    for i, (name, element) in enumerate((("N1", "N"), ("C1", "C"), ("O1", "O"), ("C2", "C"), ("C3", "C"))):
        coordinate = [1.8, 0., 0.] if name == "C3" else [4. + i, 2., 1.]
        component.add(Atom(name, np.array(coordinate), 20., 1., " ", name.ljust(4), i + 2, element=element))
    mutate(structure)
    path = root / "fixture_model.cif"
    writer = MMCIFIO()
    writer.set_structure(structure)
    writer.save(str(path))
    target = root / "target.yaml"
    target.write_text(yaml.safe_dump({"protein_chain": "A", "residue_name": "CYS", "nucleophilic_atom": "SG",
        "residue_number": 797, "cysteine_sequence_index": 105, "warhead_smarts": "[C:1]=[C:2][C:3](=[O:4])[N:5]"}))
    return path, target


def test_actual_mmcif_distance_uses_exact_cysteine_and_post_reaction_atom_names(tmp_path):
    path, target = structure_file(tmp_path)
    result = reactive_distance(path, "NC(=O)C=C", target, warhead_class="acrylamide")
    assert result["reactive_distance_angstrom"] == pytest.approx(1.8)
    assert result["electrophilic_atom"] == "C3"
    assert result["source_residue"] == 797
    assert result["af3_sequence_residue"] == 105


def extra_model(structure):
    second = copy.deepcopy(structure[0])
    second.id, second.serial_num = 1, 2
    structure.add(second)


def extra_ligand(structure):
    second = copy.deepcopy(next(structure[0]["L"].get_residues()))
    second.id = ("H_LIG", 2, " ")
    structure[0]["L"].add(second)


@pytest.mark.parametrize("mutate", [
    extra_model, extra_ligand,
    lambda s: setattr(s[0]["A"][105], "resname", "ALA"),
    lambda s: setattr(s[0]["A"][105]["SG"], "element", "C"),
    lambda s: next(s[0]["L"].get_residues()).detach_child("N1"),
    lambda s: setattr(next(s[0]["L"].get_residues())["C3"], "element", "N"),
    lambda s: s[0]["A"][105]["SG"].set_coord(np.array([float("nan"), 0., 0.])),
    lambda s: next(s[0]["L"].get_residues())["C3"].set_coord(np.zeros(3)),
])
def test_ambiguous_or_invalid_structures_are_not_silently_accepted(tmp_path, mutate):
    path, target = structure_file(tmp_path, mutate)
    with pytest.raises(ValueError):
        reactive_distance(path, "NC(=O)C=C", target, warhead_class="acrylamide")


def test_real_batch_parser_keeps_invalid_confidence_as_an_explicit_failure_row(tmp_path):
    job = tmp_path / "results/fixture"
    job.mkdir(parents=True)
    path, target = structure_file(job)
    summary, confidence = confidence_files(job, lambda s, c: s.update(iptm=float("nan")))
    summary.rename(job / "fixture_summary_confidences.json")
    confidence.rename(job / "fixture_confidences.json")
    manifest = tmp_path / "batch.json"
    manifest.write_text(json.dumps([{"job_name": "fixture", "input_sdf_index": 7, "compound_id": "fixture-id",
        "standardized_smiles": "NC(=O)C=C", "warhead_class": "acrylamide", "status": "SUCCESS"}]))
    parse = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/parse_af3_batch.py"))["parse_batch"]
    result = parse(None, job.parent, target, batch_manifest=manifest)
    assert len(result) == 1
    assert result.iloc[0]["engine_status"] == "FAILED"
    assert result.iloc[0]["compound_id"] == "fixture-id"
    assert "Invalid AF3 confidence" in result.iloc[0]["failure_reason"]
    assert np.isnan(result.iloc[0]["raw_score"])


def test_failed_preparation_is_not_rescued_by_leftover_prediction_files(tmp_path):
    job = tmp_path / "results/fixture"
    job.mkdir(parents=True)
    _, target = structure_file(job)
    summary, confidence = confidence_files(job)
    summary.rename(job / "fixture_summary_confidences.json")
    confidence.rename(job / "fixture_confidences.json")
    manifest = tmp_path / "batch.json"
    manifest.write_text(json.dumps([{"job_name": "fixture", "input_sdf_index": 7, "compound_id": "fixture-id",
        "standardized_smiles": "NC(=O)C=C", "warhead_class": "acrylamide", "status": "FAILED",
        "failure_reason": "frozen_input_did_not_pass"}]))
    parse = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/parse_af3_batch.py"))["parse_batch"]
    result = parse(None, job.parent, target, batch_manifest=manifest)
    assert result.iloc[0]["engine_status"] == "FAILED"
    assert result.iloc[0]["failure_reason"] == "preparation_failed:frozen_input_did_not_pass"
    assert np.isnan(result.iloc[0]["raw_score"])


def test_job_parser_uses_the_same_declared_chain_for_confidence_and_geometry(tmp_path):
    job = tmp_path / "fixture"
    job.mkdir()
    _, target = structure_file(job, lambda s: setattr(s[0]["A"], "id", "B"))
    config = yaml.safe_load(target.read_text())
    config["protein_chain"] = "B"
    target.write_text(yaml.safe_dump(config))
    summary, confidence = confidence_files(job, lambda s, c: c.update(
        token_chain_ids=["B", "B", "L"], atom_chain_ids=["B", "L", "L"]))
    summary.rename(job / "fixture_summary_confidences.json")
    confidence.rename(job / "fixture_confidences.json")
    result = parse_af3_job(job, "NC(=O)C=C", target, warhead_class="acrylamide")
    assert result["mpae"] == 7.5
    assert result["reactive_distance_angstrom"] == pytest.approx(1.8)
