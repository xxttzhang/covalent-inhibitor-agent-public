"""Run CovDock-LO by per-candidate reaction type and merge identities.

The licensed CovDock CLI accepts one named reaction per invocation. Ligand-first
enumeration intentionally spans several electrophile classes, so a single
global ``Michael Addition`` invocation would be chemically wrong. This wrapper
partitions the AF3-selected table, runs the existing registered CovDock stage
for each supported reaction, and restores one auditable denominator.
"""

from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from covscreen.jobs import sha256_file, utc_now


SUPPORTED = {
    "Michael Addition",
    "Nucleophilic Substitution",
    "Conjugate Addition to Alkyne (carbonyl activated)",
}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _identity_sha256(frame: pd.DataFrame) -> str:
    """Hash the ordered scientific identity, independent of parquet bytes."""
    rows = [
        {
            "input_sdf_index": int(row.input_sdf_index),
            "compound_id": str(row.compound_id),
            "standardized_smiles": str(row.standardized_smiles),
        }
        for row in frame.sort_values("input_sdf_index", kind="stable").itertuples(index=False)
    ]
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _successful_registered_manifest(path: Path, expected: pd.DataFrame) -> bool:
    """Return true only for a complete shard with the exact requested identities."""
    if not path.is_file():
        return False
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    if payload.get("status") != "completed" or payload.get("input_count") != len(expected):
        return False
    source_table = Path(str(payload.get("input_candidate_table", "")))
    if not source_table.is_file():
        return False
    try:
        source = _read_table(source_table)
    except Exception:
        return False
    required = {"input_sdf_index", "compound_id", "standardized_smiles"}
    return required.issubset(source) and _identity_sha256(source) == _identity_sha256(expected)


def _append_optional_arguments(
    command: list[str],
    args: argparse.Namespace,
    consistency_path: Path | None,
) -> None:
    if args.runtime_lib_dir:
        command.extend(["--runtime-lib-dir", str(args.runtime_lib_dir.resolve())])
    if args.prepare_receptor:
        command.append("--prepare-receptor")
    if args.grid_file:
        command.extend(["--grid-file", str(args.grid_file.resolve())])
    if args.min_radius is not None:
        command.extend(["--min-radius", str(args.min_radius)])
    if args.score_property:
        command.extend(["--score-property", args.score_property])
    if consistency_path is not None:
        command.extend(["--pose-consistency-table", str(consistency_path.resolve())])
    if args.resume:
        command.append("--resume")


def _registered_command(
    *,
    args: argparse.Namespace,
    candidate_table: Path,
    output_dir: Path,
    reaction_type: str,
    input_count: int,
    consistency_path: Path | None,
    reuse_dir: Path | None,
    jobname_prefix: str,
) -> list[str]:
    slots = min(args.njobs, input_count)
    command = [
        sys.executable,
        str((_bootstrap.PROJECT / "scripts/run_covdock_lo_registered.py")),
        "--candidate-table", str(candidate_table),
        "--receptor", str(args.receptor.resolve()),
        "--attachment-residue", args.attachment_residue,
        "--nucleophilic-atom", args.nucleophilic_atom,
        "--reaction-type", reaction_type,
        "--output-dir", str(output_dir),
        "--schrodinger-root", str(args.schrodinger_root.resolve()),
        "--license-file", str(args.license_file.resolve()),
        "--njobs", str(slots),
        "--host", f"localhost:{slots}",
        "--jobname-prefix", jobname_prefix,
        "--final-n", str(input_count),
        "--max-local-strain", str(args.max_local_strain),
        "--hard-max-strain", str(args.hard_max_strain),
        "--min-covalent-bond-length", str(args.min_covalent_bond_length),
        "--max-covalent-bond-length", str(args.max_covalent_bond_length),
        "--max-pose-rmsd", str(args.max_pose_rmsd),
        "--severe-clash-distance", str(args.severe_clash_distance),
        "--min-success-rate", str(args.min_success_rate),
    ]
    _append_optional_arguments(command, args, consistency_path)
    if reuse_dir is not None:
        command.extend(["--reuse-artifact-dir", str(reuse_dir.resolve())])
    return command


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run identity-preserving, reaction-routed Ligand-first CovDock-LO."
    )
    parser.add_argument("--candidate-table", type=Path, required=True)
    parser.add_argument("--reaction-type-column", default="covdock_reaction_type")
    parser.add_argument("--receptor", type=Path, required=True)
    parser.add_argument("--attachment-residue", required=True)
    parser.add_argument("--nucleophilic-atom", default="SG")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--schrodinger-root", type=Path, required=True)
    parser.add_argument("--license-file", type=Path, required=True)
    parser.add_argument("--runtime-lib-dir", type=Path)
    parser.add_argument("--prepare-receptor", action="store_true")
    parser.add_argument("--grid-file", type=Path)
    parser.add_argument("--njobs", type=int, default=1)
    parser.add_argument("--min-radius", type=float)
    parser.add_argument("--score-property")
    parser.add_argument("--pose-consistency-table", type=Path)
    parser.add_argument("--reuse-artifact-dir", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--retained-candidate", nargs=2, action="append", default=[],
                        metavar=("DESCRIPTOR", "SHA256"))
    parser.add_argument("--final-n", type=int, default=30)
    parser.add_argument("--max-local-strain", type=float, default=10.0)
    parser.add_argument("--hard-max-strain", type=float, default=20.0)
    parser.add_argument("--min-covalent-bond-length", type=float, default=1.5)
    parser.add_argument("--max-covalent-bond-length", type=float, default=2.2)
    parser.add_argument("--max-pose-rmsd", type=float, default=3.0)
    parser.add_argument("--severe-clash-distance", type=float, default=1.2)
    parser.add_argument("--min-success-rate", type=float, default=0.80)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--checkpoint-size",
        type=int,
        default=0,
        help=(
            "Split each reaction group into immutable candidate shards of this size. "
            "Zero preserves the historical one-batch behavior. Use 1 for per-candidate recovery."
        ),
    )
    parser.add_argument(
        "--checkpoint-workers",
        type=int,
        default=1,
        help="Maximum number of immutable candidate shards executed concurrently.",
    )
    parser.add_argument(
        "--checkpoint-worker-cpus",
        type=int,
        default=1,
        help="Approved CPU share per concurrent shard; also bounds threaded math libraries.",
    )
    args = parser.parse_args()
    if args.checkpoint_size < 0:
        parser.error("--checkpoint-size must be zero or positive")
    if args.checkpoint_workers < 1 or args.checkpoint_worker_cpus < 1:
        parser.error("checkpoint workers and worker CPUs must be positive")
    if args.checkpoint_size == 0 and args.checkpoint_workers != 1:
        parser.error("parallel checkpoint workers require --checkpoint-size")

    retained = {}
    if args.retained_candidate:
        if args.project_root is None:
            parser.error("retained candidates require --project-root")
        from covscreen.covdock_retained import validate_retained_recovery
        retained = validate_retained_recovery(args.project_root, vars(args),
            [{'path': name, 'sha256': digest} for name, digest in args.retained_candidate])

    frame = _read_table(args.candidate_table.resolve()).copy()
    required = {
        "input_sdf_index",
        "compound_id",
        "standardized_smiles",
        args.reaction_type_column,
    }
    missing = sorted(required - set(frame))
    if missing:
        parser.error(f"candidate table is missing columns: {missing}")
    frame["input_sdf_index"] = pd.to_numeric(
        frame["input_sdf_index"], errors="raise"
    ).astype("int64")
    if frame["input_sdf_index"].duplicated().any() or frame.empty:
        parser.error("candidate identity denominator must be non-empty and unique")
    reaction_types = frame[args.reaction_type_column].astype(str).str.strip()
    unsupported = sorted(set(reaction_types) - SUPPORTED)
    if unsupported:
        parser.error(f"unsupported CovDock reaction types: {unsupported}")
    consistency = None
    if args.pose_consistency_table:
        consistency = _read_table(args.pose_consistency_table.resolve())
        if ("input_sdf_index" not in consistency
                or consistency["input_sdf_index"].duplicated().any()
                or set(consistency["input_sdf_index"]) != set(frame["input_sdf_index"])):
            parser.error("pose-consistency table must preserve the exact candidate identity set")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    group_manifests: dict[str, Any] = {}
    group_frames: list[pd.DataFrame] = []
    group_scores: list[pd.DataFrame] = []
    group_failures: list[pd.DataFrame] = []
    overall_ok = True
    for reaction_type in sorted(set(reaction_types)):
        subset = frame.loc[reaction_types.eq(reaction_type)].copy()
        slug = _slug(reaction_type)
        group_dir = args.output_dir / "reaction_groups" / slug
        group_dir.mkdir(parents=True, exist_ok=True)
        group_table = group_dir / "candidates.parquet"
        subset.to_parquet(group_table, index=False)
        consistency_path = None
        if consistency is not None:
            consistency_path = group_dir / "pose_consistency.parquet"
            consistency.loc[consistency["input_sdf_index"].isin(subset["input_sdf_index"])].to_parquet(
                consistency_path, index=False)

        if args.checkpoint_size == 0:
            reuse_dir = (
                args.reuse_artifact_dir.resolve() / "reaction_groups" / slug
                if args.reuse_artifact_dir else None
            )
            command = _registered_command(
                args=args,
                candidate_table=group_table,
                output_dir=group_dir,
                reaction_type=reaction_type,
                input_count=len(subset),
                consistency_path=consistency_path,
                reuse_dir=reuse_dir,
                jobname_prefix=f"lf_{slug[:32]}",
            )
            completed = subprocess.run(command, check=False)
            manifest_path = group_dir / "covdock_lo_registered_manifest.json"
            manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
            group_manifests[reaction_type] = {
                "input_count": len(subset),
                "identity_sha256": _identity_sha256(subset),
                "checkpoint_size": 0,
                "return_code": completed.returncode,
                "status": manifest.get("status", "missing_manifest"),
                "manifest": str(manifest_path),
                "manifest_sha256": sha256_file(manifest_path) if manifest_path.is_file() else None,
            }
            group_ok = completed.returncode == 0 and manifest.get("status") == "completed"
            shard_dirs = [group_dir] if group_ok else []
        else:
            shard_records: list[dict[str, Any]] = []
            shard_dirs: list[Path] = []
            group_ok = True
            ordered = subset.sort_values("input_sdf_index", kind="stable").reset_index(drop=True)
            shard_jobs: list[dict[str, Any]] = []
            for shard_number, start in enumerate(range(0, len(ordered), args.checkpoint_size)):
                shard = ordered.iloc[start:start + args.checkpoint_size].copy()
                first_index = int(shard.iloc[0]["input_sdf_index"])
                shard_name = f"shard_{shard_number:04d}_i{first_index}"
                shard_dir = group_dir / "shards" / shard_name
                shard_dir.mkdir(parents=True, exist_ok=True)
                shard_table = shard_dir / "candidates.parquet"
                shard.to_parquet(shard_table, index=False)
                shard_consistency_path = None
                if consistency is not None:
                    shard_consistency_path = shard_dir / "pose_consistency.parquet"
                    consistency.loc[
                        consistency["input_sdf_index"].isin(shard["input_sdf_index"])
                    ].to_parquet(shard_consistency_path, index=False)
                source_shard = None
                if args.reuse_artifact_dir:
                    proposed = (
                        args.reuse_artifact_dir.resolve() / "reaction_groups" / slug
                        / "shards" / shard_name
                    )
                    if _successful_registered_manifest(
                        proposed / "covdock_lo_registered_manifest.json", shard
                    ):
                        source_shard = proposed
                command = _registered_command(
                    args=args,
                    candidate_table=shard_table,
                    output_dir=shard_dir,
                    reaction_type=reaction_type,
                    input_count=len(shard),
                    consistency_path=shard_consistency_path,
                    reuse_dir=source_shard,
                    jobname_prefix=f"lf_{slug[:20]}_{shard_name}",
                )
                retained_source = retained.get(first_index)
                result_dir = shard_dir
                if retained_source is not None:
                    if _identity_sha256(shard) != _identity_sha256(retained_source['candidate']):
                        raise ValueError('Retained candidate differs from requested shard')
                    # A fresh readback has its own provenance. Do not invent a
                    # successful vendor run or overwrite the interrupted parent.
                    result_dir = shard_dir / 'retained_readback'
                    command = [sys.executable,
                        str((_bootstrap.PROJECT / "scripts/reparse_covdock_retained_candidate.py")),
                        '--project-root', str(args.project_root.resolve()),
                        '--descriptor', str(retained_source['descriptor_path']),
                        '--expected-descriptor-sha256', retained_source['descriptor_sha256'],
                        '--schrodinger-root', str(args.schrodinger_root.resolve()),
                        '--output-dir', str(result_dir)]
                shard_jobs.append({
                    "number": shard_number,
                    "name": shard_name,
                    "frame": shard,
                    "dir": result_dir,
                    "command": command,
                    "source": source_shard,
                    "retained": retained_source,
                })

            def execute_shard(job: dict[str, Any]) -> dict[str, Any]:
                environment = os.environ.copy()
                threads = str(args.checkpoint_worker_cpus)
                for key in (
                    "OMP_NUM_THREADS",
                    "OPENBLAS_NUM_THREADS",
                    "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS",
                ):
                    environment[key] = threads
                completed = subprocess.run(job["command"], check=False, env=environment)
                shard = job["frame"]
                shard_dir = job["dir"]
                source_shard = job["source"]
                shard_name = job["name"]
                shard_manifest = shard_dir / "covdock_lo_registered_manifest.json"
                manifest = json.loads(shard_manifest.read_text()) if shard_manifest.is_file() else {}
                shard_ok = completed.returncode == 0 and manifest.get("status") == "completed"
                return {
                    "number": job["number"],
                    "ok": shard_ok,
                    "dir": shard_dir,
                    "record": {
                        "shard": shard_name,
                        "input_sdf_indices": [
                            int(value) for value in shard["input_sdf_index"]
                        ],
                        "identity_sha256": _identity_sha256(shard),
                        "return_code": completed.returncode,
                        "status": manifest.get("status", "missing_manifest"),
                        "reused": source_shard is not None,
                        "execution_kind": manifest.get("execution_kind", "new_docking"),
                        "retained_descriptor": (
                            str(job['retained']['descriptor_path']) if job['retained'] else None
                        ),
                        "retained_descriptor_sha256": (
                            job['retained']['descriptor_sha256'] if job['retained'] else None
                        ),
                        "source_parent_run_id": (
                            job['retained']['descriptor']['parent_run_id'] if job['retained'] else None
                        ),
                        "source_artifact_dir": (
                            str(source_shard) if source_shard else None
                        ),
                        "manifest": str(shard_manifest),
                        "manifest_sha256": (
                            sha256_file(shard_manifest)
                            if shard_manifest.is_file()
                            else None
                        ),
                    },
                }

            shard_jobs.sort(key=lambda job: (job['retained'] is None, job['number']))
            worker_count = min(args.checkpoint_workers, len(shard_jobs))
            if worker_count == 1:
                shard_results = [execute_shard(job) for job in shard_jobs]
            else:
                with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
                    shard_results = list(executor.map(execute_shard, shard_jobs))
            for result in sorted(shard_results, key=lambda item: item["number"]):
                shard_records.append(result["record"])
                group_ok = group_ok and result["ok"]
                if result["ok"]:
                    shard_dirs.append(result["dir"])
            group_manifests[reaction_type] = {
                "input_count": len(subset),
                "identity_sha256": _identity_sha256(subset),
                "checkpoint_size": args.checkpoint_size,
                "checkpoint_workers": worker_count,
                "checkpoint_worker_cpus": args.checkpoint_worker_cpus,
                "status": "completed" if group_ok and len(shard_dirs) == len(shard_records) else "failed",
                "shards": shard_records,
            }
        overall_ok = overall_ok and group_ok
        if not group_ok:
            break
        for result_dir in shard_dirs:
            group_frames.append(pd.read_parquet(result_dir / "stage5_gated_candidates.parquet"))
            group_scores.append(pd.read_parquet(result_dir / "covdock_lo_all_scores.parquet"))
            failures_path = result_dir / "stage5_failures.parquet"
            if failures_path.is_file():
                group_failures.append(pd.read_parquet(failures_path))

    merged = pd.concat(group_frames, ignore_index=True) if group_frames else pd.DataFrame()
    identity_complete = bool(
        overall_ok
        and len(merged) == len(frame)
        and not merged["input_sdf_index"].duplicated().any()
        and set(merged["input_sdf_index"].astype(int))
        == set(frame["input_sdf_index"].astype(int))
    )
    outputs: dict[str, dict[str, Any]] = {}
    if identity_complete:
        merged = merged.sort_values("input_sdf_index", kind="stable")
        scores = pd.concat(group_scores, ignore_index=True).sort_values(
            "input_sdf_index", kind="stable"
        )
        eligible = merged.loc[merged["covdock_gate"].isin(["A", "B"])].copy()
        rank_column = "af3_mpae_rank" if "af3_mpae_rank" in eligible else "input_sdf_index"
        eligible = eligible.sort_values(rank_column, kind="stable").head(args.final_n)
        eligible["final_rank"] = range(1, len(eligible) + 1)
        failures = (
            pd.concat(group_failures, ignore_index=True)
            if group_failures
            else merged.loc[merged["covdock_gate"].eq("C")].copy()
        )
        files = {
            "stage5_gated_candidates.parquet": merged,
            "stage5_gated_candidates.csv": merged,
            "covdock_lo_all_scores.parquet": scores,
            "covdock_lo_all_scores.csv": scores,
            "final_candidates.parquet": eligible,
            "final_candidates.csv": eligible,
            "stage5_failures.parquet": failures,
            "stage5_failures.csv": failures,
        }
        for name, table in files.items():
            path = args.output_dir / name
            if path.suffix == ".parquet":
                table.to_parquet(path, index=False)
            else:
                table.to_csv(path, index=False)
            outputs[name] = {"path": str(path), "sha256": sha256_file(path)}

    quality_gates = {
        "all_reaction_groups_completed": overall_ok,
        "identity_denominator_complete": identity_complete,
        "reaction_types_explicit_and_supported": not unsupported,
    }
    aggregate = {
        "schema_version": "1.0",
        "algorithm_version": "ligand-first-covdock-grouped-v1.1",
        "execution_kind": (
            "retained_and_new_candidate_recovery" if retained
            else "candidate_checkpoint_recovery" if args.checkpoint_size
            else "retained_pose_reparse" if args.reuse_artifact_dir
            else "new_docking"
        ),
        "checkpoint_size": args.checkpoint_size,
        "checkpoint_workers": args.checkpoint_workers,
        "checkpoint_worker_cpus": args.checkpoint_worker_cpus,
        "reused_artifact_dir": str(args.reuse_artifact_dir.resolve()) if args.reuse_artifact_dir else None,
        "status": "completed" if all(quality_gates.values()) else "quality_gate_failed",
        "generated_at": utc_now(),
        "input_candidate_table": str(args.candidate_table.resolve()),
        "input_candidate_table_sha256": sha256_file(args.candidate_table.resolve()),
        "input_count": len(frame),
        "retained_candidate_indices": sorted(retained),
        "requested_new_docking_candidate_indices": (
            sorted(int(value) for value in set(frame['input_sdf_index']) - set(retained))
            if retained else None
        ),
        "reaction_type_column": args.reaction_type_column,
        "pose_consistency_table_sha256": (
            sha256_file(args.pose_consistency_table.resolve())
            if args.pose_consistency_table else None
        ),
        "reaction_groups": group_manifests,
        "gate_quality_gates": quality_gates,
        "outputs": outputs,
    }
    _atomic_json(args.output_dir / "covdock_lo_registered_manifest.json", aggregate)
    print(json.dumps(aggregate, ensure_ascii=False, indent=2))
    return 0 if aggregate["status"] == "completed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
