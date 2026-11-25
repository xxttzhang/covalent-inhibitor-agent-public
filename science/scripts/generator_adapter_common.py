from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from rdkit import Chem


COMMON_COLUMNS = [
    "candidate_id",
    "generator",
    "source_index",
    "smiles",
    "canonical_smiles",
    "valid",
    "unique",
    "engine_status",
    "failure_reason",
    "raw_score",
    "score_name",
    "score_direction",
    "source_file",
]


def clean_generator_environment(
    python_path: Path, cuda_device_index: int, *, use_cuda: bool = True
) -> dict[str, str]:
    """Build a minimal generator environment without CarsiDock or license leakage."""
    environment: dict[str, str] = {}
    for key in ("HOME", "LANG", "LC_ALL", "TMPDIR"):
        value = os.environ.get(key)
        if value:
            environment[key] = value
    environment["PATH"] = os.pathsep.join(
        [str(python_path.parent), "/usr/local/bin", "/usr/bin", "/bin"]
    )
    environment["CUDA_VISIBLE_DEVICES"] = str(cuda_device_index) if use_cuda else ""
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def canonical_smiles(smiles: str) -> str | None:
    molecule = Chem.MolFromSmiles(smiles) if smiles else None
    if molecule is None:
        return None
    return Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)


def stable_candidate_id(generator: str, source_index: int, canonical: str | None) -> str:
    identity = canonical or f"failed:{source_index}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{generator}-{source_index:04d}-{digest}"


def write_candidate_table(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = list(rows)
    extra = sorted({key for row in normalized for key in row} - set(COMMON_COLUMNS))
    columns = COMMON_COLUMNS + extra
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(normalized)


def write_generation_report(
    path: Path,
    *,
    generator: str,
    requested: int,
    rows: list[dict[str, Any]],
    seed: int,
    external_return_code: int,
) -> dict[str, Any]:
    report = {
        "generator": generator,
        "n_requested": requested,
        "n_rows": len(rows),
        "n_valid": sum(bool(row.get("valid")) for row in rows),
        "n_unique_valid": sum(
            bool(row.get("valid")) and bool(row.get("unique")) for row in rows
        ),
        "n_failed": sum(row.get("engine_status") != "SUCCESS" for row in rows),
        "seed": seed,
        "external_return_code": external_return_code,
        "scientific_scope": "proposal_generation_only_not_binding_or_reactivity_evidence",
    }
    path.write_text(json.dumps(report, indent=2) + "\n")
    return report
