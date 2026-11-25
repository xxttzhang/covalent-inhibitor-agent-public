"""Freeze read-only candidate milestones from an interrupted grouped CovDock run.

This audit never promotes scratch output to a scientific score and never marks a
candidate reusable.  It exists to localize a timeout/failure and preserve exact
evidence before a separately approved recovery is planned.
"""

from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd

from covscreen.jobs import sha256_file, utc_now


MILESTONES = {
    "sampling_raw": re.compile(r"_sample_Docking_(\d{6})_raw\.maegz$"),
    "sampling_library": re.compile(r"_sample_Docking_(\d{6})_lib\.maegz$"),
    # The vendor creates this aggregate-looking file before every per-pose
    # strain subjob has necessarily finished.  It is evidence that strain work
    # was reached, never evidence that candidate-level strain completed.
    "ligand_strain_artifact_unverified": re.compile(
        r"_LigandStrain_(\d{6})-out\.maegz$"
    ),
    "score_library": re.compile(r"_score_Docking_(\d{6})_lib\.maegz$"),
}
STRAIN_POSE_FILE = re.compile(r"_LigandStrain_(\d{6})_(\d{5})_")
POSITIONED_ERROR = re.compile(r"_(?:LigandStrain|OptimizedDockedGeometry)_(\d{6})(?:_|\.)")


def _read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def _identity_digest(frame: pd.DataFrame) -> str:
    required = {"input_sdf_index", "compound_id", "standardized_smiles"}
    missing = sorted(required - set(frame))
    if missing:
        raise ValueError(f"candidate table is missing identity columns: {missing}")
    if frame.empty or frame["input_sdf_index"].duplicated().any():
        raise ValueError("candidate table identity denominator must be non-empty and unique")
    rows = [
        {
            "batch_position": position,
            "input_sdf_index": int(row.input_sdf_index),
            "compound_id": str(row.compound_id),
            "standardized_smiles": str(row.standardized_smiles),
        }
        for position, row in enumerate(frame.itertuples(index=False))
    ]
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def audit(scratch: Path, candidate_table: Path) -> dict[str, Any]:
    frame = _read_table(candidate_table).copy()
    digest = _identity_digest(frame)
    observed: dict[int, dict[str, Any]] = {}
    pose_indices: dict[int, set[int]] = {}
    locks: dict[int, list[dict[str, Any]]] = {}
    nonempty_errors: dict[int, list[dict[str, Any]]] = {}
    for path in scratch.rglob("*"):
        if not path.is_file():
            continue
        stat = path.stat()
        strain_match = STRAIN_POSE_FILE.search(path.name)
        positioned_error = POSITIONED_ERROR.search(path.name) if path.suffix == ".err" else None
        position_match = strain_match or positioned_error
        if position_match:
            position = int(position_match.group(1))
            if position >= len(frame):
                raise ValueError(
                    f"scratch candidate position {position} is outside denominator {len(frame)}"
                )
            if strain_match:
                pose_indices.setdefault(position, set()).add(int(strain_match.group(2)))
                if path.suffix == ".lck" and stat.st_size > 0:
                    locks.setdefault(position, []).append({
                        "path": str(path.resolve()),
                        "size_bytes": stat.st_size,
                        "sha256": sha256_file(path),
                        "mtime_ns": stat.st_mtime_ns,
                    })
            if positioned_error and stat.st_size > 0:
                nonempty_errors.setdefault(position, []).append({
                    "path": str(path.resolve()),
                    "size_bytes": stat.st_size,
                    "sha256": sha256_file(path),
                    "mtime_ns": stat.st_mtime_ns,
                })
        if stat.st_size <= 0:
            continue
        for milestone, pattern in MILESTONES.items():
            match = pattern.search(path.name)
            if not match:
                continue
            position = int(match.group(1))
            if position >= len(frame):
                raise ValueError(
                    f"scratch candidate position {position} is outside denominator {len(frame)}"
                )
            record = observed.setdefault(position, {})
            existing = record.get(milestone)
            evidence = {
                "path": str(path.resolve()),
                "size_bytes": stat.st_size,
                "sha256": sha256_file(path),
                "mtime_ns": stat.st_mtime_ns,
            }
            if existing is None or evidence["mtime_ns"] > existing["mtime_ns"]:
                record[milestone] = evidence

    candidates = []
    for position, row in enumerate(frame.itertuples(index=False)):
        evidence = observed.get(position, {})
        indices = sorted(pose_indices.get(position, set()))
        candidates.append({
            "batch_position": position,
            "input_sdf_index": int(row.input_sdf_index),
            "compound_id": str(row.compound_id),
            "milestones": evidence,
            "highest_observed_milestone": (
                "score_library" if "score_library" in evidence
                else "ligand_strain_artifact_unverified"
                if "ligand_strain_artifact_unverified" in evidence
                else "sampling_library" if "sampling_library" in evidence
                else "sampling_raw" if "sampling_raw" in evidence
                else "not_observed"
            ),
            "strain_pose_indices_observed": indices,
            "highest_strain_pose_index_observed": indices[-1] if indices else None,
            "strain_pose_observation_is_not_completion": True,
            "nonempty_error_files": sorted(
                nonempty_errors.get(position, []), key=lambda item: item["path"]
            ),
            "lock_files_observed": sorted(
                locks.get(position, []), key=lambda item: item["path"]
            ),
            "scientifically_validated": False,
            "reusable_checkpoint": False,
        })
    return {
        "schema_version": "1.1",
        "audit_kind": "ligand-first-covdock-partial-evidence-v2",
        "status": "PARTIAL_EVIDENCE_ONLY",
        "generated_at": utc_now(),
        "scratch_directory": str(scratch.resolve()),
        "candidate_table": str(candidate_table.resolve()),
        "candidate_table_sha256": sha256_file(candidate_table),
        "identity_sha256": digest,
        "expected_candidate_count": len(frame),
        "observed_candidate_count": sum(
            item["highest_observed_milestone"] != "not_observed" for item in candidates
        ),
        "score_library_candidate_count": sum(
            item["highest_observed_milestone"] == "score_library" for item in candidates
        ),
        "nonempty_error_file_count": sum(
            len(item["nonempty_error_files"]) for item in candidates
        ),
        "candidates": candidates,
        "interpretation": (
            "Scratch milestones localize execution progress only. They are not registered "
            "CovDock scores, validated poses, successful shards or permission to retry. "
            "In particular, ligand_strain_artifact_unverified means only that the vendor "
            "created a top-level strain artifact; per-pose strain work may still be active."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scratch-dir", required=True, type=Path)
    parser.add_argument("--candidate-table", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.scratch_dir.is_dir():
        parser.error("scratch directory is missing")
    if not args.candidate_table.is_file():
        parser.error("candidate table is missing")
    payload = audit(args.scratch_dir.resolve(), args.candidate_table.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(args.output)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
