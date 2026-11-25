from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from rdkit import Chem

from parse_carsidock_scores import parse_chunk
from covscreen.jobs import sha256_file


SUPPORTED_REACTION_TYPES = {
    "Michael Addition",
    "Nucleophilic Addition to a Double Bond",
    "Nucleophilic Addition to a Triple Bond",
    "Nucleophilic Substitution",
    "Boronic Acid Addition",
    "Epoxide/Aziridine Opening",
    "Imine Condensation",
    "Beta Lactone/Lactam Addition",
    "Gamma Lactone/Lactam Addition",
    "Disulfide Formation",
    "Others",
}


def _read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def _slug(value: str) -> str:
    return "_".join(value.lower().replace("/", " ").split())


def validate_retained_group(prefix: Path, group_sdf: Path, args, reaction: str) -> dict:
    path = Path(f"{prefix}.run_manifest.json")
    manifest = json.loads(path.read_text())
    if manifest.get("status") != "SUCCESS" or manifest.get("return_code") != 0:
        raise ValueError("Retained group did not complete successfully")
    command = manifest["command"]
    def value(flag):
        if command.count(flag) != 1:
            raise ValueError(f"Retained command flag missing or ambiguous: {flag}")
        return command[command.index(flag) + 1]
    for flag, expected in {"-rectype": reaction, "-covres": args.covres,
                           "--seed": str(args.seed),
                           "--num_conformer": str(args.num_conformer)}.items():
        if value(flag) != expected:
            raise ValueError(f"Retained parameter mismatch: {flag}")
    if not manifest.get("enforce_electrophile_map"):
        raise ValueError("Retained grouped run lacks electrophile guard evidence")
    if sha256_file(args.checkpoint) != manifest.get("availability", {}).get("checkpoint_sha256"):
        raise ValueError("Retained checkpoint mismatch")
    input_evidence = {}
    historical = manifest.get("input_sha256", {})
    for flag, current in [("-p", args.pdb), ("-rl", args.reference_ligand), ("-l", group_sdf)]:
        source = Path(value(flag)).resolve()
        digest = sha256_file(current)
        if sha256_file(source) != digest:
            raise ValueError(f"Retained input differs from requested input: {flag}")
        if str(source) in historical and historical[str(source)] != digest:
            raise ValueError(f"Retained historical input hash mismatch: {flag}")
        input_evidence[flag] = {"sha256": digest, "historically_frozen": str(source) in historical}
    outputs = [Path(f"{prefix}.sdf"), Path(f"{prefix}_nodumat.sdf"), Path(f"{prefix}_withpckt.pdb")]
    if not all(p.is_file() and p.stat().st_size for p in outputs):
        raise ValueError("Retained group outputs incomplete")
    return {"mode": "retained_pose_reparse", "new_docking_performed": False,
            "source_manifest": str(path), "source_manifest_sha256": sha256_file(path),
            "input_evidence": input_evidence,
            "historical_input_hashes_verified": all(v["historically_frozen"] for v in input_evidence.values()),
            "output_hashes": {str(p): sha256_file(p) for p in outputs}}


def _write_reaction_group(
    table: pd.DataFrame,
    source_sdf: Path,
    group_dir: Path,
    reaction_type: str,
) -> tuple[Path, Path]:
    group_dir.mkdir(parents=True, exist_ok=True)
    table_path = group_dir / "candidates.parquet"
    sdf_path = group_dir / "candidates.sdf"
    table.to_parquet(table_path, index=False)
    wanted = set(pd.to_numeric(table["input_sdf_index"], errors="raise").astype(int))
    molecules = [
        molecule
        for molecule in Chem.SDMolSupplier(str(source_sdf), removeHs=False, sanitize=False)
        if molecule is not None
    ]
    selected = [
        molecule
        for molecule in molecules
        if molecule.HasProp("INPUT_SDF_INDEX")
        and int(molecule.GetProp("INPUT_SDF_INDEX")) in wanted
    ]
    if len(selected) != len(wanted):
        raise ValueError(
            f"reaction group {reaction_type!r} has {len(wanted)} table rows but "
            f"{len(selected)} identity-matched SDF records"
        )
    writer = Chem.SDWriter(str(sdf_path))
    try:
        for molecule in selected:
            writer.write(molecule)
    finally:
        writer.close()
    return table_path, sdf_path


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one registered CarsiDock-Cov job and emit an identity-mapped score table."
    )
    parser.add_argument("--chunk", type=int, required=True)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    parser.add_argument("--pdb", type=Path, required=True)
    parser.add_argument("--reference-ligand", type=Path, required=True)
    parser.add_argument("--ligand-sdf", type=Path, required=True)
    parser.add_argument("--ligand-table", type=Path, required=True)
    parser.add_argument("--covres", required=True)
    parser.add_argument("--reaction-type", default="Michael Addition")
    parser.add_argument(
        "--reaction-type-column",
        help=(
            "Optional candidate-table column. When provided, candidates are split and "
            "run with the matching CarsiDock reaction type, then identity-merged."
        ),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-scores", type=Path, required=True)
    parser.add_argument("--carsidock-repo", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--num-conformer", type=int, default=1)
    parser.add_argument("--cuda-device-index", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260725)
    parser.add_argument(
        "--reuse-output-prefix",
        type=Path,
        help="Parse retained CarsiDock outputs without rerunning docking.",
    )
    parser.add_argument("--reuse-grouped-output-dir", type=Path,
                        help="Reparse retained reaction groups; never run new docking.")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.reaction_type not in SUPPORTED_REACTION_TYPES:
        parser.error(f"unsupported --reaction-type: {args.reaction_type}")
    source_table = _read_table(args.ligand_table)
    required_identity = {"input_sdf_index", "compound_id"}
    missing_identity = sorted(required_identity - set(source_table))
    if missing_identity:
        parser.error(f"ligand table is missing identity columns: {missing_identity}")
    if source_table["input_sdf_index"].duplicated().any():
        parser.error("ligand table has duplicate input_sdf_index values")
    if args.reaction_type_column and args.reuse_output_prefix is not None:
        parser.error("grouped reaction routing cannot reuse a single legacy output prefix")
    if args.reuse_grouped_output_dir and (not args.reaction_type_column or args.reuse_output_prefix):
        parser.error("grouped reuse requires reaction-type-column and excludes reuse-output-prefix")

    if args.reaction_type_column:
        if args.reaction_type_column not in source_table:
            parser.error(
                f"reaction-type column is absent: {args.reaction_type_column}"
            )
        reaction_types = source_table[args.reaction_type_column].astype(str).str.strip()
        unsupported = sorted(set(reaction_types) - SUPPORTED_REACTION_TYPES)
        if unsupported:
            parser.error(f"unsupported reaction types in candidate table: {unsupported}")
        groups = [
            (reaction_type, source_table.loc[reaction_types.eq(reaction_type)].copy())
            for reaction_type in sorted(set(reaction_types))
        ]
    else:
        groups = [(args.reaction_type, source_table)]

    grouped_frames: list[pd.DataFrame] = []
    grouped_audits: dict[str, object] = {}
    grouped_return_codes: dict[str, int | None] = {}
    reuse_evidence = {}
    for group_index, (reaction_type, group_table) in enumerate(groups):
        group_slug = _slug(reaction_type)
        if args.reaction_type_column:
            group_table_path, group_sdf_path = _write_reaction_group(
                group_table,
                args.ligand_sdf,
                args.output_dir / "reaction_groups" / group_slug,
                reaction_type,
            )
            output_prefix = args.output_dir / f"carsidock_{group_slug}"
        else:
            group_table_path, group_sdf_path = args.ligand_table, args.ligand_sdf
            output_prefix = (
                args.reuse_output_prefix.resolve()
                if args.reuse_output_prefix is not None
                else args.output_dir / "carsidock"
            )
        upstream_return_code = None
        if args.reuse_grouped_output_dir:
            output_prefix = args.reuse_grouped_output_dir.resolve() / f"carsidock_{group_slug}"
            reuse_evidence[reaction_type] = validate_retained_group(
                output_prefix, group_sdf_path, args, reaction_type)
        if args.reuse_output_prefix is None and args.reuse_grouped_output_dir is None:
            command = [
                sys.executable,
                str(Path(__file__).with_name("run_chunk.py")),
                "--chunk",
                str(args.chunk + group_index),
                "--device",
                args.device,
                "--pdb",
                str(args.pdb),
                "--reference-ligand",
                str(args.reference_ligand),
                "--ligand-sdf",
                str(group_sdf_path),
                "--covres",
                args.covres,
                "--reaction-type",
                reaction_type,
                "--output",
                str(output_prefix),
                "--num-conformer",
                str(args.num_conformer),
                "--cuda-device-index",
                str(args.cuda_device_index),
                "--seed",
                str(args.seed),
                "--carsidock-repo",
                str(args.carsidock_repo),
                "--checkpoint",
                str(args.checkpoint),
            ]
            if "electrophilic_atom_map" in group_table:
                maps = pd.to_numeric(group_table["electrophilic_atom_map"], errors="raise")
                if not maps.eq(906).all():
                    raise ValueError("declared-electrophile routing currently requires atom map 906")
                command.append("--enforce-electrophile-map")
            completed = subprocess.run(command, check=False)
            upstream_return_code = completed.returncode
        frame, audit = parse_chunk(group_table_path, group_sdf_path, output_prefix)
        frame["carsidock_reaction_type_used"] = reaction_type
        grouped_frames.append(frame)
        grouped_audits[reaction_type] = audit
        grouped_return_codes[reaction_type] = upstream_return_code

    frame = pd.concat(grouped_frames, ignore_index=True).sort_values(
        "input_sdf_index", kind="stable"
    )
    if len(frame) != len(source_table) or frame["input_sdf_index"].duplicated().any():
        raise RuntimeError("grouped CarsiDock routing lost or duplicated candidate identities")
    if set(frame["input_sdf_index"].astype(int)) != set(
        source_table["input_sdf_index"].astype(int)
    ):
        raise RuntimeError("grouped CarsiDock routing changed the candidate identity set")
    args.output_scores.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(args.output_scores, index=False)
    frame.to_csv(args.output_scores.with_suffix(".csv"), index=False)
    failures = frame.loc[
        frame["engine_status"].ne("SUCCESS"),
        [
            column
            for column in (
                "compound_id",
                "protomer_id",
                "input_sdf_index",
                "label",
                "carsidock_reaction_type_used",
                "failure_reason",
            )
            if column in frame.columns
        ],
    ].copy()
    failures["engine"] = "carsidock-cov"
    failures["stage"] = np.where(
        failures["failure_reason"].eq("chemistry_preparation_failed_before_docking"),
        "chemistry_preparation",
        "pose_scoring",
    )
    failures.to_csv(
        args.output_scores.with_name(f"{args.output_scores.stem}_failures.csv"),
        index=False,
    )
    audit_payload = {
        "registered_adapter": "carsidock-cov",
        "execution_kind": "retained_pose_reparse" if args.reuse_grouped_output_dir or args.reuse_output_prefix else "new_docking",
        "reuse_evidence": reuse_evidence,
        "reaction_routing": (
            f"candidate-column:{args.reaction_type_column}"
            if args.reaction_type_column
            else f"single:{args.reaction_type}"
        ),
        "reaction_groups": {
            reaction_type: {
                "input_rows": int(len(group_table)),
                "upstream_return_code": grouped_return_codes[reaction_type],
                "output_audit": grouped_audits[reaction_type],
            }
            for reaction_type, group_table in groups
        },
        "input_rows": len(frame),
        "success_rows": int(frame["engine_status"].eq("SUCCESS").sum()),
        "failed_rows": int(frame["engine_status"].ne("SUCCESS").sum()),
        "identity_denominator_complete": len(frame) == len(source_table),
        "partial_results_retained": True,
    }
    args.output_scores.with_name(
        f"{args.output_scores.stem}_audit.json"
    ).write_text(json.dumps(audit_payload, indent=2) + "\n")
    print(json.dumps(audit_payload, indent=2))
    return 0 if audit_payload["success_rows"] > 0 else 2

if __name__ == "__main__":
    raise SystemExit(main())
