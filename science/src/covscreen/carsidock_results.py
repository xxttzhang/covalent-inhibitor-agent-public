from __future__ import annotations

from collections import Counter
import math
from pathlib import Path

import pandas as pd
from rdkit import Chem


def sdf_molecules(path: Path) -> list[Chem.Mol]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    return [
        molecule
        for molecule in Chem.SDMolSupplier(
            str(path), removeHs=False, sanitize=False
        )
        if molecule is not None
    ]


def sdf_count(path: Path) -> int:
    return len(sdf_molecules(path))


def pose_name(molecule: Chem.Mol) -> str:
    return molecule.GetProp("_Name").strip() if molecule.HasProp("_Name") else ""


def resolve_pose_compound_id(name: str, compound_ids: list[str]) -> str | None:
    """Resolve a CarsiDock ``<compound>-<protomer>`` name to its input ID."""
    matches = [
        compound_id
        for compound_id in compound_ids
        if name == compound_id or name.startswith(f"{compound_id}-")
    ]
    return max(matches, key=len) if matches else None


def audit_carsidock_outputs(
    output_prefix: str | Path,
    input_sdf: str | Path,
) -> dict[str, object]:
    prefix = Path(output_prefix)
    input_sdf = Path(input_sdf)
    pose_sdf = prefix.with_suffix(".sdf")
    no_dummy_sdf = Path(f"{prefix}_nodumat.sdf")
    pocket_pdb = Path(f"{prefix}_withpckt.pdb")
    score_csv = Path(f"{prefix}_rtms.csv")
    inputs = sdf_molecules(input_sdf)
    poses = sdf_molecules(pose_sdf)
    input_count = len(inputs)
    pose_count = len(poses)
    no_dummy_count = sdf_count(no_dummy_sdf)

    compound_ids = [
        molecule.GetProp("COMPOUND_ID")
        for molecule in inputs
        if molecule.HasProp("COMPOUND_ID")
    ]
    identity_mapping_available = bool(
        input_count
        and len(compound_ids) == input_count
        and len(set(compound_ids)) == input_count
        and any(pose_name(molecule) for molecule in poses)
    )
    pose_counts_by_input: Counter[str] = Counter()
    variant_counts: Counter[str] = Counter()
    unmapped_pose_count = 0
    finite_carsiscores = 0
    inputs_with_finite_carsiscore: set[str] = set()
    if identity_mapping_available:
        for molecule in poses:
            name = pose_name(molecule)
            compound_id = resolve_pose_compound_id(name, compound_ids)
            if compound_id is None:
                unmapped_pose_count += 1
                continue
            pose_counts_by_input[compound_id] += 1
            variant_counts[name] += 1
            if molecule.HasProp("CarsiScore"):
                value = pd.to_numeric(molecule.GetProp("CarsiScore"), errors="coerce")
                if math.isfinite(value):
                    finite_carsiscores += 1
                    inputs_with_finite_carsiscore.add(compound_id)

    mapped_input_count = len(pose_counts_by_input)
    variant_count = len(variant_counts)
    poses_per_variant = None
    if variant_counts and len(set(variant_counts.values())) == 1:
        poses_per_variant = next(iter(variant_counts.values()))

    score_rows = 0
    finite_scores = 0
    score_column = None
    if score_csv.is_file():
        scores = pd.read_csv(score_csv)
        score_rows = len(scores)
        score_column = next(
            (
                column
                for column in ("score", "raw_score", "rtmscore")
                if column in scores
            ),
            None,
        )
        if score_column:
            finite_scores = int(
                pd.to_numeric(scores[score_column], errors="coerce")
                .map(math.isfinite)
                .sum()
            )

    poses_per_input = None
    if identity_mapping_available:
        counts = [pose_counts_by_input.get(compound_id, 0) for compound_id in compound_ids]
        if counts and len(set(counts)) == 1:
            poses_per_input = counts[0]
    elif input_count > 0 and pose_count % input_count == 0:
        poses_per_input = pose_count // input_count

    if identity_mapping_available:
        docking_identity_complete = bool(
            mapped_input_count == input_count and unmapped_pose_count == 0
        )
        carsi_scoring_complete = len(inputs_with_finite_carsiscore) == input_count
    else:
        docking_identity_complete = poses_per_input is not None
        carsi_scoring_complete = False
    docking_success = bool(
        input_count > 0
        and pose_count >= input_count
        and pose_count == no_dummy_count
        and docking_identity_complete
        and pocket_pdb.is_file()
    )
    legacy_rtms_success = bool(
        score_column
        and score_rows == input_count
        and finite_scores == input_count
    )
    scoring_success = bool(carsi_scoring_complete or legacy_rtms_success)
    if docking_success and scoring_success:
        status = "SUCCESS"
    elif docking_success:
        status = "DOCKING_SUCCESS_SCORING_FAILED"
    else:
        status = "DOCKING_FAILED"
    return {
        "status": status,
        "input_sdf": str(input_sdf),
        "input_count": input_count,
        "pose_sdf": str(pose_sdf),
        "pose_count": pose_count,
        "poses_per_input": poses_per_input,
        "variant_count": variant_count,
        "poses_per_variant": poses_per_variant,
        "identity_mapping_available": identity_mapping_available,
        "mapped_input_count": mapped_input_count,
        "unmapped_pose_count": unmapped_pose_count,
        "finite_carsiscores": finite_carsiscores,
        "inputs_with_finite_carsiscore": len(inputs_with_finite_carsiscore),
        "no_dummy_sdf": str(no_dummy_sdf),
        "no_dummy_count": no_dummy_count,
        "pocket_pdb": str(pocket_pdb),
        "pocket_pdb_exists": pocket_pdb.is_file(),
        "score_csv": str(score_csv),
        "score_csv_exists": score_csv.is_file(),
        "score_column": score_column,
        "score_rows": score_rows,
        "finite_scores": finite_scores,
        "docking_success": docking_success,
        "scoring_success": scoring_success,
    }
