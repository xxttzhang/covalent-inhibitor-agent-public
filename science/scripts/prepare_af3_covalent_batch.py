from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pandas as pd

from covscreen.af3_identity import af3_job_name, merge_source_metadata
from covscreen.af3_environment import af3_environment
from covscreen.af3_target import validate_target


def preparation_fingerprint(row: dict, context: dict) -> str:
    payload = json.dumps({"row": row, "context": context}, sort_keys=True, allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--af3-python", type=Path, required=True)
    ap.add_argument(
        "--library-table",
        "--library-csv",
        dest="library_table",
        type=Path,
        required=True,
        help="Discovery or benchmark candidate table in CSV or Parquet format.",
    )
    ap.add_argument("--source-data-json", type=Path, required=True)
    ap.add_argument("--expected-source-data-sha256")
    selection = ap.add_mutually_exclusive_group(required=True)
    selection.add_argument("--matched-active-id")
    selection.add_argument("--all-valid", action="store_true")
    ap.add_argument("--protein-chain", default="A")
    ap.add_argument("--ligand-chain", default="L")
    ap.add_argument("--cysteine-sequence-index", type=int, default=88)
    ap.add_argument("--target-id", default="BTK")
    ap.add_argument("--source-residue-number", type=int, default=481)
    ap.add_argument("--nucleophilic-atom", default="SG")
    ap.add_argument("--random-seed", type=int, default=20260725)
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--output-dir", type=Path, required=True)
    args = ap.parse_args()

    source_bytes = args.source_data_json.read_bytes()
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    if args.expected_source_data_sha256 is not None and source_sha != args.expected_source_data_sha256:
        raise ValueError("AF3 source seed differs from the frozen preparation")
    seed_check = validate_target(json.loads(source_bytes), args.protein_chain,
        args.cysteine_sequence_index, args.ligand_chain, args.nucleophilic_atom)
    if args.library_table.suffix.lower() == ".parquet":
        table = pd.read_parquet(args.library_table)
    elif args.library_table.suffix.lower() == ".csv":
        table = pd.read_csv(args.library_table, dtype={"compound_id": str, "protomer_id": str})
    else:
        raise ValueError("AF3 candidate table must be CSV or Parquet")
    required = {"compound_id", "standardized_smiles", "input_sdf_index"}
    missing = sorted(required - set(table.columns))
    if missing:
        raise ValueError(f"AF3 candidate table is missing columns: {missing}")
    if table["input_sdf_index"].duplicated().any():
        raise ValueError("AF3 candidate table has duplicate input_sdf_index values")
    defaults = {
        "protomer_id": "0",
        "label": "unknown",
        "parent_id": None,
        "matched_active_id": "",
    }
    for column, default in defaults.items():
        if column not in table:
            table[column] = table["compound_id"] if column == "parent_id" else default
    # Pandas/numpy scalars and NaN are not portable JSON provenance. Round-trip
    # once through pandas' JSON encoder so every upstream evidence column can be
    # carried into AF3, CovDock and Pareto without lossy hand-picked allowlists.
    rows = json.loads(table.to_json(orient="records", date_format="iso"))
    if args.all_valid:
        selected = rows
    else:
        selected = [
            row
            for row in rows
            if (
                str(row["compound_id"]) == args.matched_active_id
                and row["label"] == "active"
            )
            or (
                str(row["matched_active_id"]) == args.matched_active_id
                and row["label"] == "decoy"
            )
        ]
    selected.sort(key=lambda row: int(row["input_sdf_index"]))
    if not selected:
        raise ValueError(f"No matched set found for {args.matched_active_id}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    inputs_dir = args.output_dir / "inputs"
    manifests_dir = args.output_dir / "manifests"
    ccd_dir = args.output_dir / "ccd"
    for directory in (inputs_dir, manifests_dir, ccd_dir):
        directory.mkdir(parents=True, exist_ok=True)
    single_script = Path(__file__).with_name("prepare_af3_covalent_input.py")
    adduct_module = Path(__file__).resolve().parents[1] / "src/covscreen/af3_adduct.py"
    context = {
        "source_sha256": source_sha,
        "af3_target_seed_check": seed_check,
        "target_check_sha256": hashlib.sha256(
            (adduct_module.with_name("af3_target.py")).read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(single_script.read_bytes()).hexdigest(),
        "adduct_sha256": hashlib.sha256(adduct_module.read_bytes()).hexdigest(),
        "protein_chain": args.protein_chain, "ligand_chain": args.ligand_chain,
        "cysteine_sequence_index": args.cysteine_sequence_index,
        "nucleophilic_atom": args.nucleophilic_atom, "random_seed": args.random_seed,
        "target_id": args.target_id, "source_residue_number": args.source_residue_number,
    }
    manifest_rows = []
    for row in selected:
        job_name = af3_job_name(
            args.target_id,
            row["compound_id"],
            row["protomer_id"],
            int(row["input_sdf_index"]),
        )
        final_manifest_path = manifests_dir / f"{job_name}.manifest.json"
        fingerprint = preparation_fingerprint(row, context)
        if args.resume and final_manifest_path.is_file():
            previous = json.loads(final_manifest_path.read_text())
            if previous.get("preparation_fingerprint") != fingerprint:
                raise ValueError(f"Stale or unverifiable AF3 preparation for {job_name}; use a new output directory")
            if previous.get("status") != "PREPARATION_FAILED":
                for field in ("input_json", "user_ccd"):
                    if not previous.get(field) or not Path(previous[field]).is_file():
                        raise ValueError(f"Missing reusable AF3 {field} for {job_name}")
                manifest_rows.append(previous)
                continue
        command = [
            str(args.af3_python),
            str(single_script),
            "--source-data-json",
            str(args.source_data_json),
            "--expected-source-data-sha256",
            source_sha,
            "--ligand-smiles",
            str(row["standardized_smiles"]),
            "--compound-id",
            str(row["compound_id"]),
            "--protomer-id",
            str(row["protomer_id"]),
            "--job-name",
            job_name,
            "--component-id",
            "LIG",
            "--protein-chain",
            args.protein_chain,
            "--ligand-chain",
            args.ligand_chain,
            "--cysteine-sequence-index",
            str(args.cysteine_sequence_index),
            "--target-id",
            args.target_id,
            "--source-residue-number",
            str(args.source_residue_number),
            "--nucleophilic-atom",
            args.nucleophilic_atom,
            "--random-seed",
            str(args.random_seed),
            "--output-dir",
            str(inputs_dir),
        ]
        if "warhead_class" in row:
            if not isinstance(row["warhead_class"], str) or not row["warhead_class"].strip():
                raise ValueError(f"Missing declared warhead_class for {row['compound_id']}")
            command.extend(["--warhead-class", row["warhead_class"]])
        environment = af3_environment(
            args.af3_python, Path(__file__).resolve().parents[1] / "src", os.environ
        )
        completed = subprocess.run(command, check=False, env=environment)
        if completed.returncode != 0:
            failure = {
                **row,
                "job_name": job_name,
                "engine": "alphafold3",
                "status": "PREPARATION_FAILED",
                "preparation_fingerprint": fingerprint,
                "failure_reason": (
                    f"prepare_af3_covalent_input_return_code={completed.returncode}"
                ),
            }
            (manifests_dir / f"{job_name}.manifest.json").write_text(
                json.dumps(failure, indent=2) + "\n"
            )
            manifest_rows.append(failure)
            if args.fail_fast:
                raise subprocess.CalledProcessError(completed.returncode, command)
            continue
        manifest_path = inputs_dir / f"{job_name}.manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest = merge_source_metadata(manifest, row)
        manifest["preparation_fingerprint"] = fingerprint
        ccd_source = inputs_dir / "LIG.cif"
        ccd_target = ccd_dir / f"{job_name}.cif"
        ccd_source.replace(ccd_target)
        manifest["user_ccd"] = str(ccd_target.resolve())
        final_manifest_path = manifests_dir / manifest_path.name
        final_manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        manifest_path.unlink()
        manifest_rows.append(manifest)

    keys = sorted({key for row in manifest_rows for key in row})
    with (args.output_dir / "batch_manifest.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(manifest_rows)
    (args.output_dir / "batch_manifest.json").write_text(
        json.dumps(manifest_rows, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "matched_active_id": args.matched_active_id,
                "jobs": len(manifest_rows),
                "active": sum(row["label"] == "active" for row in manifest_rows),
                "decoy": sum(row["label"] == "decoy" for row in manifest_rows),
                "output_dir": str(args.output_dir),
                "input_dir": str(inputs_dir),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
