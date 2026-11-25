#!/usr/bin/env python3
"""Audit the public, label-blind side of the ERBB2 Cys805 lock-box pilot."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
from pathlib import Path

import pandas as pd
from rdkit import Chem


ACRYLAMIDE = Chem.MolFromSmarts("C=CC(=O)N")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--operational-root", type=Path, required=True)
    parser.add_argument("--sealed-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest_path = args.operational_root / "blinded_panel_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    table_path = args.operational_root / "erbb2_blind_mini.parquet"
    table = pd.read_parquet(table_path)
    expected_rows = 420
    checks: dict[str, bool] = {
        "manifest_blinded_and_sealed": manifest.get("status") == "BLINDED_AND_SEALED",
        "selection_frozen_before_scoring": manifest.get("selection_frozen_before_scoring") is True,
        "labels_not_exposed": manifest.get("labels_exposed_to_scoring_pipeline") is False,
        "row_count_420": len(table) == expected_rows == int(manifest.get("rows", -1)),
        "complete_input_index": table["input_sdf_index"].tolist() == list(range(expected_rows)),
        "blind_labels_only": set(table["label"].astype(str).str.lower()) == {"blinded"},
        "unique_compound_ids": table["compound_id"].nunique() == expected_rows,
        "unique_smiles": table["standardized_smiles"].nunique() == expected_rows,
        "operational_hash_matches": sha256_file(table_path)
        == manifest["operational_files"]["parquet_sha256"],
        "selected_counts_20_plus_400": (
            manifest["selection"]["selected_active_compounds"] == 20
            and manifest["selection"]["selected_decoys"] == 400
        ),
        "active_scaffolds_20": manifest["selection"]["selected_active_scaffolds"] == 20,
        "maximum_similarity_below_0_40": (
            manifest["selection"]["maximum_observed_active_decoy_tanimoto"] < 0.40
        ),
    }
    forbidden = {"active", "decoy"}
    object_values = set()
    for column in table.select_dtypes(include=["object", "string"]).columns:
        object_values.update(table[column].dropna().astype(str).str.lower().unique())
    checks["no_class_tokens_in_operational_table"] = not bool(object_values & forbidden)

    exact_single_warhead = True
    for smiles in table["standardized_smiles"].astype(str):
        molecule = Chem.MolFromSmiles(smiles)
        if molecule is None or ACRYLAMIDE is None or len(molecule.GetSubstructMatches(ACRYLAMIDE)) != 1:
            exact_single_warhead = False
            break
    checks["all_rows_have_exactly_one_acrylamide"] = exact_single_warhead

    sealed_map = {
        "answer_key_parquet": args.sealed_root / "answer_key.parquet.enc",
        "answer_key_csv": args.sealed_root / "answer_key.csv.enc",
        "wrapped_data_key": args.sealed_root / "answer_key_data_key.rsa",
    }
    checks["all_sealed_files_exist"] = all(path.is_file() for path in sealed_map.values())
    checks["sealed_files_are_owner_read_only"] = all(
        stat.S_IMODE(path.stat().st_mode) == 0o400 for path in sealed_map.values()
    )
    for name, path in sealed_map.items():
        checks[f"{name}_encrypted_hash_matches"] = sha256_file(path) == manifest["sealed_files"][
            f"{name}_encrypted_sha256" if name != "wrapped_data_key" else "wrapped_data_key_sha256"
        ]

    failed = sorted(name for name, passed in checks.items() if not passed)
    report = {
        "status": "PASS" if not failed else "FAIL",
        "scope": "public label-blind audit; answer key was not decrypted",
        "checks": checks,
        "failed_checks": failed,
        "operational_manifest_sha256": sha256_file(manifest_path),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
