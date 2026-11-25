"""Prepare a cryptographically sealed, label-blinded COValid validation panel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pandas as pd

from covscreen.dataset import build_target_mini


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def target_seed(global_seed: int, target_id: str) -> int:
    token = hashlib.sha256(f"{global_seed}:{target_id}".encode()).digest()
    return (global_seed + int.from_bytes(token[:4], "big")) % (2**31 - 1)


def blind_operational_table(mini: pd.DataFrame, target_id: str) -> pd.DataFrame:
    prefix = "".join(character for character in target_id.upper() if character.isalnum())[:6]
    blind_ids = [f"{prefix}B{index:04d}" for index in mini["input_sdf_index"]]
    operational = pd.DataFrame(
        {
            "standardized_smiles": mini["standardized_smiles"].astype(str),
            "label": "blinded",
            "target_id": target_id,
            "site_id": mini["site_id"].astype(str),
            "compound_id": blind_ids,
            "parent_id": blind_ids,
            "protomer_id": "0",
            "matched_active_id": "",
            "input_sdf_index": mini["input_sdf_index"].astype(int),
        }
    )
    forbidden = {"active", "decoy"}
    for column in operational.columns:
        values = set(operational[column].astype(str).str.lower().unique())
        if values & forbidden:
            raise ValueError(f"Operational column {column} leaks a class label")
    return operational


def answer_key(mini: pd.DataFrame, operational: pd.DataFrame) -> pd.DataFrame:
    key = mini.copy()
    key.insert(0, "blind_id", operational["compound_id"].to_numpy())
    return key


def encrypt_file(plain: Path, encrypted: Path, key_file: Path) -> None:
    subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-salt",
            "-in",
            str(plain),
            "-out",
            str(encrypted),
            "-pass",
            f"file:{key_file}",
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--panel-config", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--sealed-root", type=Path, required=True)
    args = parser.parse_args()

    panel = json.loads(args.panel_config.read_text())
    source = pd.read_parquet(args.source)
    dataset = panel["dataset"]
    global_seed = int(dataset["global_sampling_seed"])
    active_count = int(dataset["active_count"])
    decoys_per_active = int(dataset["decoys_per_active"])
    expected_rows = int(dataset["rows_per_target"])
    args.output_root.mkdir(parents=True, exist_ok=True)
    args.sealed_root.mkdir(parents=True, exist_ok=True)

    panel_keys: list[pd.DataFrame] = []
    target_manifests: list[dict[str, object]] = []
    for target in panel["targets"]:
        target_id = str(target["target_id"])
        site_id = str(target["site_id"])
        seed = target_seed(global_seed, target_id)
        mini = build_target_mini(
            source,
            target_id=target_id,
            site_id=site_id,
            active_count=active_count,
            decoys_per_active=decoys_per_active,
            seed=seed,
        )
        if len(mini) != expected_rows:
            raise ValueError(f"{target_id}: expected {expected_rows} rows, found {len(mini)}")
        if mini["input_sdf_index"].tolist() != list(range(expected_rows)):
            raise ValueError(f"{target_id}: input_sdf_index is not a complete fixed range")
        operational = blind_operational_table(mini, target_id)
        target_dir = args.output_root / target_id
        target_dir.mkdir(parents=True, exist_ok=True)
        parquet = target_dir / f"{target_id.lower()}_blind_mini.parquet"
        csv = target_dir / f"{target_id.lower()}_blind_mini.csv"
        operational.to_parquet(parquet, index=False)
        operational.to_csv(csv, index=False)
        key = answer_key(mini, operational)
        key["panel_target_id"] = target_id
        panel_keys.append(key)
        target_manifests.append(
            {
                "target_id": target_id,
                "site_id": site_id,
                "seed": seed,
                "rows": len(operational),
                "expected_active": active_count,
                "expected_decoy": active_count * decoys_per_active,
                "operational_parquet": str(parquet),
                "operational_parquet_sha256": sha256_file(parquet),
                "operational_csv": str(csv),
                "operational_csv_sha256": sha256_file(csv),
                "labels_exposed": False,
            }
        )

    all_keys = pd.concat(panel_keys, ignore_index=True)
    with tempfile.TemporaryDirectory(prefix="three_stage_blind_labels_") as temp_name:
        temp = Path(temp_name)
        plain_parquet = temp / "answer_key.parquet"
        plain_csv = temp / "answer_key.csv"
        data_key = temp / "data_key.bin"
        all_keys.to_parquet(plain_parquet, index=False)
        all_keys.to_csv(plain_csv, index=False)
        data_key.write_bytes(os.urandom(48))
        parquet_enc = args.sealed_root / "answer_key.parquet.enc"
        csv_enc = args.sealed_root / "answer_key.csv.enc"
        wrapped_key = args.sealed_root / "answer_key_data_key.rsa"
        encrypt_file(plain_parquet, parquet_enc, data_key)
        encrypt_file(plain_csv, csv_enc, data_key)
        subprocess.run(
            [
                "openssl",
                "pkeyutl",
                "-encrypt",
                "-pubin",
                "-inkey",
                str(args.public_key),
                "-in",
                str(data_key),
                "-out",
                str(wrapped_key),
            ],
            check=True,
        )
        plaintext_commitments = {
            "answer_key_parquet_sha256": sha256_file(plain_parquet),
            "answer_key_csv_sha256": sha256_file(plain_csv),
        }

    manifest = {
        "schema_version": 1,
        "panel_id": panel["panel_id"],
        "status": "BLINDED_AND_SEALED",
        "source": str(args.source),
        "source_sha256": sha256_file(args.source),
        "panel_config": str(args.panel_config),
        "panel_config_sha256": sha256_file(args.panel_config),
        "public_key_sha256": sha256_file(args.public_key),
        "encryption": "AES-256-CBC/PBKDF2 data files; RSA-wrapped random data key",
        "sealed_files": {
            "answer_key_parquet": str(parquet_enc),
            "answer_key_parquet_encrypted_sha256": sha256_file(parquet_enc),
            "answer_key_csv": str(csv_enc),
            "answer_key_csv_encrypted_sha256": sha256_file(csv_enc),
            "wrapped_data_key": str(wrapped_key),
            "wrapped_data_key_sha256": sha256_file(wrapped_key),
        },
        "plaintext_commitments": plaintext_commitments,
        "targets": target_manifests,
        "total_rows": sum(int(row["rows"]) for row in target_manifests),
        "labels_exposed_to_scoring_pipeline": False,
    }
    manifest_path = args.output_root / "blinded_panel_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    os.chmod(parquet_enc, 0o400)
    os.chmod(csv_enc, 0o400)
    os.chmod(wrapped_key, 0o400)
    print(
        json.dumps(
            {
                "status": manifest["status"],
                "panel_id": manifest["panel_id"],
                "targets": len(target_manifests),
                "total_rows": manifest["total_rows"],
                "manifest": str(manifest_path),
                "labels_exposed_to_scoring_pipeline": False,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
