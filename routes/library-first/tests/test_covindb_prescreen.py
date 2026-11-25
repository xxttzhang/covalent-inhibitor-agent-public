from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml
from rdkit import Chem
from rdkit.Chem import Descriptors, rdMolDescriptors

from covscreen.covindb import run_prescreen


def test_covindb_prescreen_preserves_identity_and_builds_three_channel_pool(tmp_path: Path):
    source = tmp_path / "Virtual_Screening_Library.csv"
    rows = [
        ("1", "vendor", "covalent", "C=CC(=O)NCC", "AAA"),
        ("2", "vendor", "covalent", "C=CC(=O)NCC", "AAA"),
        ("3", "vendor", "covalent", "C=CC(=O)Nc1ccccc1", "BBB"),
        ("4", "vendor", "covalent", "C=CC(=O)N(C)C", "CCC"),
        ("5", "vendor", "other", "CC(=O)Nc1ccccc1", "DDD"),
        ("6", "vendor", "covalent", "C=CC(=O)NCCNC(=O)C=C", "EEE"),
        ("7", "vendor", "broken", "not-smiles", "FFF"),
    ]
    with source.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["", "Source", "CompoundType", "SMILES", "InChI_Key"])
        writer.writerows(rows)
    config = tmp_path / "target.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "target_id": "TEST",
                "site_id": "TEST_C100",
                "protein_chain": "A",
                "residue_name": "CYS",
                "residue_number": 100,
                "reaction_type": "Michael Addition",
                "warhead_class": "acrylamide",
                "warhead_smarts": "[C:1]=[C:2][C:3](=[O:4])[N:5]",
                "reference_smiles": "C=CC(=O)Nc1ccccc1",
                "lead_like": {"min_mw": 0},
            }
        )
    )
    output = tmp_path / "output"
    manifest = run_prescreen(source, config, output, top_n=2, chunk_size=2)
    assert manifest["counts"]["input_rows"] == 7
    assert manifest["counts"]["duplicates"] == 1
    assert manifest["counts"]["invalid"] == 1
    assert manifest["counts"]["compatible_warhead"] == 4
    assert manifest["counts"]["unambiguous_electrophile"] == 3
    assert manifest["counts"]["selected"] == 2
    assert manifest["counts"]["hard_gate_rejected"] == 2
    failures = pd.read_parquet(output / "covindb_rejected.parquet")
    assert len(failures) == 4  # Duplicate, invalid input and both hard-gate exclusions.
    assert set(failures["source_record_id"]) == {"2", "5", "6", "7"}
    assert sum(manifest["counts"]["rejection_reasons"].values()) == len(failures)
    assert manifest["quality"]["identity_loss_count"] == 0
    assert manifest["quality"]["selected_warhead_mapping_rate"] == 1.0
    assert manifest["quality"]["source_unambiguous_warhead_rate"] == 0.75
    assert manifest["quality_gates"]["selected_warhead_mapping_rate_ge_0_98"]
    selected = pd.read_parquet(output / "stage2_candidates.parquet")
    assert manifest['selection']['score_contract']['cross_channel_scores_comparable'] is False
    for row in selected.to_dict('records'):
        assert row['channel_score'] == pytest.approx(row[row['score_key']])
        assert row['score_version'] == manifest['algorithm_version']
        assert row['selection_reason']
    assert len(selected) == 2
    assert selected["compound_id"].is_unique
    assert selected["input_sdf_index"].tolist() == [0, 1]
    assert set(selected["selection_channel"]).issubset(
        {"exploitation", "diversity", "exploration", "backfill"}
    )
    assert (output / "stage2_candidates.sdf").is_file()
    progress = json.loads((output / "progress.json").read_text())
    assert progress["status"] == "completed"
    assert manifest["source"]["record_selection"] == "complete_file"
    assert manifest["source"]["records_read"] == 7


def test_descriptors_roundtrip_from_standardized_structure_and_prefix_scope(tmp_path: Path):
    # Deliberately misleading CSV property values must not replace computed descriptors.
    # The final valid molecule is outside the authorized prefix and cannot enter the pool.
    source = tmp_path / "Virtual_Screening_Library.csv"
    with source.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["Compound", "Source", "CompoundType", "SMILES", "InChI_Key", "clogp", "MW"])
        writer.writerows([
            ("polar", "fixture", "other", "OCC(O)CO", "A", 999, 999),
            ("aromatic", "fixture", "covalent", "C=CC(=O)Nc1ccccc1", "B", -999, 999),
            ("salt", "fixture", "covalent", "C=CC(=O)NCC[NH3+].[Cl-]", "C", 999, 999),
            # A valid molecule with an oversized metadata field: parsing this row
            # would raise csv.Error, proving the prefix bound prevents even parsing it.
            ("outside", "x" * (csv.field_size_limit() + 1), "covalent", "C=CC(=O)Nc1ccc(C)cc1", "D", 0, 0),
        ])
    config = tmp_path / "target.yaml"
    config.write_text(yaml.safe_dump({
        "target_id": "TEST", "site_id": "TEST_C100", "protein_chain": "A",
        "residue_name": "CYS", "residue_number": 100, "reaction_type": "Michael Addition",
        "warhead_class": "acrylamide", "warhead_smarts": "[C:1]=[C:2][C:3](=[O:4])[N:5]",
        "lead_like": {"min_mw": 0},
    }))
    output = tmp_path / "output"
    manifest = run_prescreen(source, config, output, top_n=3, max_rows=3, chunk_size=1)
    assert manifest["algorithm_version"] == "covindb-prescreen-v1.3"
    frame = pd.read_parquet(output / "covindb_master.parquet")
    assert frame["input_index"].tolist() == [0, 1, 2]
    assert set(frame["source_record_id"]) == {"polar", "aromatic", "salt"}
    for row in frame.to_dict("records"):
        # A freshly parsed molecule avoids accepting stale descriptor caches on the
        # standardization object; use the lower-level Crippen API as a cross-check.
        fresh = Chem.MolFromSmiles(row["standardized_smiles"])
        assert fresh is not None
        assert row["clogp"] == pytest.approx(rdMolDescriptors.CalcCrippenDescriptors(fresh)[0], abs=1e-9)
        assert row["mw"] == pytest.approx(Descriptors.MolWt(fresh), abs=1e-9)
        assert row["tpsa"] == pytest.approx(rdMolDescriptors.CalcTPSA(fresh), abs=1e-9)
    indexed = frame.set_index("source_record_id")
    assert indexed.loc["polar", "clogp"] < -1
    assert indexed.loc["aromatic", "clogp"] > 1
    assert bool(indexed.loc["aromatic", "eligible"])
    assert indexed.loc["salt", "formal_charge"] == 0
    assert "." not in indexed.loc["salt", "standardized_smiles"]
    assert manifest["source"]["max_rows"] == 3
    assert manifest["source"]["records_read"] == 3
    assert manifest["source"]["record_selection"] == "file_order_prefix"
    assert "not a random or representative sample" in manifest["source"]["sampling_note"]
    candidates = pd.read_parquet(output / "stage2_candidates.parquet")
    assert "outside" not in set(candidates["source_record_id"])
    with source.open(newline="") as handle, pytest.raises(csv.Error):
        list(csv.DictReader(handle))  # Verify the out-of-scope sentinel really cannot be parsed.


def test_zero_candidates_explains_filters_without_relaxing_quality_gates(tmp_path: Path):
    source = tmp_path / "Virtual_Screening_Library.csv"
    source.write_text(
        ",Source,CompoundType,SMILES,InChI_Key\n"
        "1,fixture,covalent,C=CC(=O)NCC,AAA\n"
        "2,fixture,other,CC,BBB\n"
    )
    config = tmp_path / "target.yaml"
    config.write_text(yaml.safe_dump({
        "target_id": "TEST", "site_id": "TEST_C100", "protein_chain": "A",
        "residue_name": "CYS", "residue_number": 100, "reaction_type": "Michael Addition",
        "warhead_class": "acrylamide", "warhead_smarts": "[C:1]=[C:2][C:3](=[O:4])[N:5]",
        "lead_like": {"min_mw": 180, "max_mw": 650, "min_logp": -1, "max_logp": 6},
    }))
    output = tmp_path / "output"
    manifest = run_prescreen(source, config, output, top_n=1, chunk_size=1)
    assert manifest["diagnostics_version"] == "prescreen-rejections-v1"
    assert manifest["selection_outcome"] == "NO_CANDIDATES_SELECTED"
    assert manifest["filters"]["min_mw"] == 180
    assert manifest["counts"]["hard_gate_pass"] == 0
    assert manifest["counts"]["hard_gate_rejected"] == 2
    assert manifest["counts"]["selected"] == 0
    assert manifest["counts"]["property_rejection_reasons"]["mw_below_min"] == 2
    assert manifest["quality"]["identity_loss_count"] == 0
    assert manifest["quality_gates"]["rdkit_parse_rate_ge_0_99"]
    assert not manifest["quality_gates"]["selected_warhead_mapping_rate_ge_0_98"]
    assert not all(manifest["quality_gates"].values())
    rejected = pd.read_parquet(output / "covindb_rejected.parquet")
    master = pd.read_parquet(output / "covindb_master.parquet")
    assert len(rejected) == len(master) == 2
    assert master["eligible"].sum() == 0
    assert set(rejected["input_index"]) == set(master["input_index"])
    assert all("outside_lead_like_window" in value for value in rejected["failure_reason"])
    assert all("mw_below_min" in value for value in master["property_gate_reason"])
    assert sum(manifest["counts"]["rejection_reasons"].values()) == 2
    assert pd.read_parquet(output / "stage2_candidates.parquet").empty
    cli = subprocess.run([
        sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/run_covindb_prescreen.py"),
        "--input", str(source), "--target-config", str(config),
        "--output-dir", str(tmp_path / "cli-output"), "--top-n", "1", "--max-rows", "2",
    ], capture_output=True, text=True, timeout=30)
    assert cli.returncode == 2  # Diagnostics never turn a blocked cohort into success.
    assert json.loads(cli.stdout)["selection_outcome"] == "NO_CANDIDATES_SELECTED"


def test_covindb_target_config_rejects_non_cysteine_protocol(tmp_path: Path):
    source = tmp_path / "Virtual_Screening_Library.csv"
    source.write_text(",Source,CompoundType,SMILES,InChI_Key\n")
    config = tmp_path / "target.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "target_id": "TEST",
                "site_id": "TEST_S100",
                "protein_chain": "A",
                "residue_name": "SER",
                "residue_number": 100,
                "reaction_type": "Michael Addition",
                "warhead_class": "acrylamide",
                "warhead_smarts": "C=CC(=O)N",
            }
        )
    )
    try:
        run_prescreen(source, config, tmp_path / "output", top_n=1)
    except ValueError as exc:
        assert "CYS targets only" in str(exc)
    else:
        raise AssertionError("non-CYS protocol was not rejected")


def test_covindb_prescreen_writes_empty_failure_table(tmp_path: Path):
    source = tmp_path / "Virtual_Screening_Library.csv"
    source.write_text(
        ",Source,CompoundType,SMILES,InChI_Key\n"
        "1,vendor,covalent,C=CC(=O)Nc1ccccc1,AAA\n"
    )
    config = tmp_path / "target.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "target_id": "TEST",
                "site_id": "TEST_C100",
                "protein_chain": "A",
                "residue_name": "CYS",
                "residue_number": 100,
                "reaction_type": "Michael Addition",
                "warhead_class": "acrylamide",
                "warhead_smarts": "[C:1]=[C:2][C:3](=[O:4])[N:5]",
                "lead_like": {"min_mw": 0},
            }
        )
    )
    manifest = run_prescreen(source, config, tmp_path / "output", top_n=1)
    assert manifest["quality"]["failure_table_present"]
    assert pd.read_parquet(tmp_path / "output" / "covindb_rejected.parquet").empty
