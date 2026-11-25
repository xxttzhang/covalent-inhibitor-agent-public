from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
from Bio.PDB import MMCIFParser

from covscreen.jobs import sha256_file, utc_now
from covscreen.pose_consistency import aligned_ligand_rmsd, protein_residue_identity
from covscreen.pose_topology import verify_pose_topology


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def load_coordinate_sources(paths: list[Path]) -> dict[tuple[str, int], dict]:
    """Pose indices are file-local, not global across reaction groups."""
    coordinates = {}
    for path in paths:
        payload = json.loads(path.read_text())
        source = str(Path(payload["input"]).resolve())
        for pose in payload["rows"]:
            index = int(pose["pose_index"])
            if index < 0 or index != float(pose["pose_index"]):
                raise ValueError("invalid_coordinate_pose_index")
            key = (source, index)
            if key in coordinates:
                raise ValueError(f"duplicate_coordinate_pose:{key}")
            coordinates[key] = pose
    return coordinates


def select_coordinates(row: object, coordinates: dict[tuple[str, int], dict]) -> dict:
    index = int(row.covdock_pose_index)
    if index < 0 or index != float(row.covdock_pose_index):
        raise ValueError("invalid_candidate_pose_index")
    source = getattr(row, "covdock_pose_file", None)
    if source is None or pd.isna(source):
        sources = {key[0] for key in coordinates}
        if len(sources) != 1:
            raise ValueError("pose_source_required_for_multiple_files")
        source = next(iter(sources))
    pose = coordinates[(str(Path(str(source)).resolve()), index)]
    title = getattr(row, "covdock_title", None)
    if title is not None and not pd.isna(title) and str(title) != str(pose["title"]):
        raise ValueError("coordinate_pose_title_mismatch")
    return pose


def score_row(
    row: object,
    extracted: dict[str, object],
    *,
    protein_alignment_radius: float | None,
) -> dict[str, object]:
    structure = MMCIFParser(QUIET=True).get_structure("af3", str(row.model_cif))
    model = next(structure.get_models())
    protein_chain = str(getattr(row, "protein_chain", "A"))
    ligand_chain = str(getattr(row, "ligand_chain", "L"))
    topology = verify_pose_topology(Path(row.model_cif), extracted,
        protein_chain=protein_chain, ligand_chain=ligand_chain,
        af3_residue=int(row.af3_sequence_residue))
    source_start = int(row.source_residue_number) - int(row.af3_sequence_residue) + 1

    covdock_ca = {
        (str(atom["chain"]), int(atom["resnum"])): atom
        for atom in extracted["protein_ca"]
    }
    target_key = (protein_chain, int(row.source_residue_number))
    if target_key not in covdock_ca:
        raise ValueError(f"target_ca_missing:{target_key}")
    target_ca = np.asarray(covdock_ca[target_key]["xyz"], dtype=float)
    fixed_protein = []
    moving_protein = []
    for residue in model[protein_chain].get_residues():
        if "CA" not in residue:
            continue
        af3_index = int(residue.id[1])
        key = (protein_chain, source_start + af3_index - 1)
        if key not in covdock_ca:
            continue
        if protein_residue_identity(str(covdock_ca[key]["resname"])) != protein_residue_identity(str(residue.resname)):
            raise ValueError(f"protein_sequence_identity_mismatch:{key}")
        fixed_xyz = np.asarray(covdock_ca[key]["xyz"], dtype=float)
        if (
            protein_alignment_radius is not None
            and np.linalg.norm(fixed_xyz - target_ca) > protein_alignment_radius
        ):
            continue
        fixed_protein.append(fixed_xyz)
        moving_protein.append(residue["CA"].coord)

    af3_ligand = {
        atom.get_name().strip().upper(): {
            "element": str(atom.element).upper(),
            "xyz": atom.coord,
        }
        for residue in model[ligand_chain].get_residues()
        for atom in residue.get_atoms()
        if str(atom.element).upper() != "H"
    }
    covdock_ligand = {
        str(atom["atom_name"]).strip().upper(): atom
        for atom in extracted["ligand_heavy_atoms"]
    }
    if len(af3_ligand) != len(covdock_ligand):
        raise ValueError(
            f"heavy_atom_count_mismatch:af3={len(af3_ligand)},covdock={len(covdock_ligand)}"
        )
    if set(af3_ligand) != set(covdock_ligand):
        missing = sorted(set(af3_ligand) - set(covdock_ligand))
        extra = sorted(set(covdock_ligand) - set(af3_ligand))
        raise ValueError(f"heavy_atom_name_mismatch:missing={missing},extra={extra}")
    atom_names = sorted(af3_ligand)
    element_mismatch = [
        name
        for name in atom_names
        if af3_ligand[name]["element"] != str(covdock_ligand[name]["element"]).upper()
    ]
    if element_mismatch:
        raise ValueError(f"heavy_atom_element_mismatch:{element_mismatch}")

    metrics = aligned_ligand_rmsd(
        np.asarray(fixed_protein),
        np.asarray(moving_protein),
        np.asarray([covdock_ligand[name]["xyz"] for name in atom_names]),
        np.asarray([af3_ligand[name]["xyz"] for name in atom_names]),
    )
    return {
        **topology,
        **metrics,
        "protein_alignment_atom_count": len(fixed_protein),
        "ligand_mapped_heavy_atom_count": len(atom_names),
        "pose_consistency_status": "SUCCESS",
        "pose_consistency_failure_reason": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate-table", type=Path, required=True)
    parser.add_argument("--coordinates-json", type=Path, nargs="+", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--protein-alignment-radius",
        type=float,
        default=15.0,
        help="C-alpha radius in angstrom around the target residue; use 0 for all common C-alpha atoms.",
    )
    args = parser.parse_args()

    frame = _read_table(args.candidate_table).copy()
    coordinates = load_coordinate_sources(args.coordinates_json)
    alignment_radius = (
        None if args.protein_alignment_radius == 0 else args.protein_alignment_radius
    )
    if alignment_radius is not None and alignment_radius <= 0:
        parser.error("--protein-alignment-radius must be positive or zero")
    results = []
    for row in frame.itertuples(index=False):
        try:
            result = score_row(
                row,
                select_coordinates(row, coordinates),
                protein_alignment_radius=alignment_radius,
            )
        except Exception as exc:
            result = {
                "protein_alignment_rmsd_angstrom": np.nan,
                "af3_covdock_ligand_rmsd_angstrom": np.nan,
                "protein_alignment_atom_count": 0,
                "ligand_mapped_heavy_atom_count": 0,
                "pose_consistency_status": "FAILED",
                "pose_consistency_failure_reason": f"{type(exc).__name__}:{exc}",
            }
        results.append(result)
    metrics = pd.DataFrame(results, index=frame.index)
    for column in metrics:
        frame[column] = metrics[column]
    args.output_dir.mkdir(parents=True, exist_ok=True)
    parquet = args.output_dir / "af3_covdock_pose_consistency.parquet"
    csv_path = args.output_dir / "af3_covdock_pose_consistency.csv"
    frame.to_parquet(parquet, index=False)
    frame.to_csv(csv_path, index=False)
    success = int(frame["pose_consistency_status"].eq("SUCCESS").sum())
    rmsd = pd.to_numeric(frame["af3_covdock_ligand_rmsd_angstrom"], errors="coerce")
    manifest = {
        "schema_version": "1.0",
        "algorithm_version": "af3-covdock-topology-checked-fixed-name-rmsd-v2.0",
        "status": "completed" if success == len(frame) else "partial",
        "generated_at": utc_now(),
        "input_count": len(frame),
        "success_count": success,
        "failure_count": len(frame) - success,
        "rmsd_le_3_count": int(rmsd.le(3.0).sum()),
        "rmsd_summary_angstrom": {
            "minimum": float(rmsd.min()) if rmsd.notna().any() else None,
            "median": float(rmsd.median()) if rmsd.notna().any() else None,
            "maximum": float(rmsd.max()) if rmsd.notna().any() else None,
        },
        "mapping_policy": "fixed atom names/elements and heavy-atom bond orders verified against AF3 userCCD; model/input covalent links checked; hydrogens excluded; not symmetry-corrected",
        "alignment_policy": (
            "common residue-name-matched protein-chain C-alpha atoms using "
            "AF3-to-source residue offset"
        ),
        "protein_alignment_radius_angstrom": alignment_radius,
        "inputs": {
            "candidate_table_sha256": sha256_file(args.candidate_table),
            "coordinate_sources": [
                {"path": str(path.resolve()), "sha256": sha256_file(path)}
                for path in args.coordinates_json
            ],
        },
        "outputs": {
            parquet.name: sha256_file(parquet),
            csv_path.name: sha256_file(csv_path),
        },
    }
    _atomic_json(args.output_dir / "pose_consistency_manifest.json", manifest)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if success == len(frame) else 2


if __name__ == "__main__":
    raise SystemExit(main())
