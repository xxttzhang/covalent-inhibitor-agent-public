"""Extract identity-mapped ligand coordinates from selected CovDock MAEGZ poses.

The implementation reads Maestro atom/bond tables directly and retains atoms
marked by CovDock's ``i_cdock_ligand_atom`` flag.  It therefore avoids invoking
the Schrödinger runtime during result analysis.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import shlex
from collections.abc import Iterator
from pathlib import Path

import pandas as pd


BLOCK_PATTERN = re.compile(r"^([A-Za-z0-9_]+)\[(\d+)\] \{$")
ELEMENTS = (
    "", "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg",
    "Al", "Si", "P", "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr",
    "Mn", "Fe", "Co", "Ni", "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr",
    "Rb", "Sr", "Y", "Zr", "Nb", "Mo", "Tc", "Ru", "Rh", "Pd", "Ag", "Cd",
    "In", "Sn", "Sb", "Te", "I", "Xe", "Cs", "Ba", "La", "Ce", "Pr", "Nd",
    "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb", "Lu", "Hf",
    "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po",
    "At", "Rn",
)


def _read(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def _missing(value: str) -> bool:
    return value in {"", "<>"}


def _flag(value: str) -> bool:
    return not _missing(value) and value.lower() in {"1", "true", "yes"}


def _read_indexed_block(handle: Iterator[str]) -> tuple[list[str], list[list[str]]]:
    columns: list[str] = []
    for raw in handle:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == ":::":
            break
        columns.append(line.replace("\\_", "_"))
    rows: list[list[str]] = []
    for raw in handle:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == ":::":
            break
        tokens = shlex.split(line, posix=True)
        if len(tokens) != len(columns) + 1:
            raise ValueError(
                f"Maestro indexed row has {len(tokens) - 1} values for "
                f"{len(columns)} columns: {line[:160]}"
            )
        rows.append(tokens)
    return columns, rows


def _selected_structures(
    path: Path,
    target_pose_indices: set[int],
) -> Iterator[tuple[int, list[str], list[list[str]], list[str], list[list[str]]]]:
    pose_index = -1
    current_target: int | None = None
    atom_columns: list[str] = []
    atom_rows: list[list[str]] = []
    with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
        iterator = iter(handle)
        for raw in iterator:
            line = raw.strip()
            if line == "f_m_ct {":
                pose_index += 1
                current_target = pose_index if pose_index in target_pose_indices else None
                atom_columns, atom_rows = [], []
                continue
            if current_target is None:
                continue
            match = BLOCK_PATTERN.fullmatch(line)
            if not match:
                continue
            block_name = match.group(1)
            if block_name == "m_atom":
                atom_columns, atom_rows = _read_indexed_block(iterator)
            elif block_name == "m_bond":
                bond_columns, bond_rows = _read_indexed_block(iterator)
                if not atom_rows:
                    raise ValueError(f"Bond block preceded atom block for pose {pose_index}")
                yield pose_index, atom_columns, atom_rows, bond_columns, bond_rows
                current_target = None


def _row_dict(columns: list[str], row: list[str]) -> tuple[int, dict[str, str]]:
    return int(row[0]), dict(zip(columns, row[1:]))


def _molfile(
    metadata: dict[str, object],
    atom_columns: list[str],
    atom_rows: list[list[str]],
    bond_columns: list[str],
    bond_rows: list[list[str]],
) -> str:
    ligand_atoms: list[tuple[int, dict[str, str]]] = []
    for row in atom_rows:
        index, values = _row_dict(atom_columns, row)
        if _flag(values.get("i_cdock_ligand_atom", "<>")):
            atomic_number = int(values["i_m_atomic_number"])
            if atomic_number > 0:
                ligand_atoms.append((index, values))
    if not ligand_atoms:
        raise ValueError(f"No CovDock ligand atoms found for {metadata}")
    old_to_new = {old: new for new, (old, _) in enumerate(ligand_atoms, start=1)}

    bond_map: dict[tuple[int, int], int] = {}
    for row in bond_rows:
        _, values = _row_dict(bond_columns, row)
        left = int(values["i_m_from"])
        right = int(values["i_m_to"])
        if left not in old_to_new or right not in old_to_new or left == right:
            continue
        pair = tuple(sorted((old_to_new[left], old_to_new[right])))
        order = int(float(values["i_m_order"]))
        bond_map[pair] = max(order, bond_map.get(pair, 0))

    title = f"{metadata['compound_id']}__p{metadata['protomer_id']}__i{metadata['input_sdf_index']}"
    lines = [
        title,
        "  CovScreen direct MAEGZ extraction",
        "",
        f"{len(ligand_atoms):>3}{len(bond_map):>3}  0  0  0  0            999 V2000",
    ]
    charges: list[tuple[int, int]] = []
    for new_index, (_, values) in enumerate(ligand_atoms, start=1):
        atomic_number = int(values["i_m_atomic_number"])
        if not 0 < atomic_number < len(ELEMENTS):
            raise ValueError(f"Unsupported atomic number: {atomic_number}")
        formal_charge = int(values.get("i_m_formal_charge", "0") or 0)
        if formal_charge:
            charges.append((new_index, formal_charge))
        lines.append(
            f"{float(values['r_m_x_coord']):10.4f}"
            f"{float(values['r_m_y_coord']):10.4f}"
            f"{float(values['r_m_z_coord']):10.4f} "
            f"{ELEMENTS[atomic_number]:<3} 0  0  0  0  0  0  0  0  0  0  0  0"
        )
    for (left, right), order in sorted(bond_map.items()):
        lines.append(f"{left:>3}{right:>3}{order:>3}  0  0  0  0")
    for offset in range(0, len(charges), 8):
        chunk = charges[offset : offset + 8]
        lines.append(
            "M  CHG"
            + f"{len(chunk):>3}"
            + "".join(f"{atom:>4}{charge:>4}" for atom, charge in chunk)
        )
    lines.append("M  END")
    for key in (
        "input_sdf_index",
        "compound_id",
        "protomer_id",
        "label",
        "af3_rank",
        "covdock_partition",
        "covdock_selected_pose_index",
        "raw_score",
    ):
        lines.extend([f">  <{key}>", str(metadata.get(key, "")), ""])
    lines.append("$$$$")
    return "\n".join(lines) + "\n"


def _parse_maegz_arg(value: str) -> tuple[int, Path]:
    partition, separator, path = value.partition(":")
    if not separator:
        raise argparse.ArgumentTypeError("--maegz must use PARTITION:PATH")
    return int(partition), Path(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score-table", type=Path, required=True)
    ap.add_argument("--fused-table", type=Path, required=True)
    ap.add_argument("--top-n", type=int, default=21)
    ap.add_argument("--maegz", action="append", type=_parse_maegz_arg, required=True)
    ap.add_argument("--output-sdf", type=Path, required=True)
    ap.add_argument("--output-manifest", type=Path, required=True)
    ap.add_argument(
        "--allow-missing",
        action="store_true",
        help="Write all available selected poses and record unavailable identities as failures.",
    )
    args = ap.parse_args()

    scores = _read(args.score_table)
    fused = _read(args.fused_table)
    selected = fused.sort_values("af3_rank", kind="stable").head(args.top_n)[
        ["input_sdf_index", "af3_rank"]
    ].merge(scores, on="input_sdf_index", how="left", validate="one_to_one")
    failed = selected.loc[
        ~selected["engine_status"].eq("SUCCESS"), "input_sdf_index"
    ].astype(int).tolist()
    if failed and not args.allow_missing:
        raise ValueError(f"Selected AF3 candidates lack successful CovDock poses: {failed}")
    available = selected.loc[selected["engine_status"].eq("SUCCESS")].copy()

    metadata_by_partition: dict[int, dict[int, dict[str, object]]] = {}
    for row in available.to_dict(orient="records"):
        partition = int(row["covdock_partition"])
        pose_index = int(row["covdock_selected_pose_index"])
        metadata_by_partition.setdefault(partition, {})[pose_index] = row

    extracted: dict[int, str] = {}
    for partition, path in args.maegz:
        targets = metadata_by_partition.get(partition, {})
        for pose_index, atom_columns, atom_rows, bond_columns, bond_rows in _selected_structures(
            path, set(targets)
        ):
            metadata = targets[pose_index]
            input_index = int(metadata["input_sdf_index"])
            extracted[input_index] = _molfile(
                metadata, atom_columns, atom_rows, bond_columns, bond_rows
            )

    expected = set(available["input_sdf_index"].astype(int))
    missing = sorted(expected - set(extracted))
    if missing and not args.allow_missing:
        raise ValueError(f"Selected CovDock poses were not found: {missing}")
    failed = sorted(set(failed) | set(missing))
    args.output_sdf.parent.mkdir(parents=True, exist_ok=True)
    order = selected.sort_values("af3_rank")["input_sdf_index"].astype(int)
    extracted_order = [index for index in order if index in extracted]
    args.output_sdf.write_text(
        "".join(extracted[index] for index in extracted_order), encoding="utf-8"
    )
    manifest = {
        "status": "SUCCESS",
        "selection_rule": f"AF3 rank <= {args.top_n}",
        "selected_count": len(selected),
        "eligible_covdock_pose_count": len(available),
        "extracted_count": len(extracted),
        "input_indices_in_af3_order": order.tolist(),
        "extracted_input_indices_in_af3_order": extracted_order,
        "failed_input_indices": failed,
        "output_sdf": str(args.output_sdf),
        "maegz_inputs": {str(partition): str(path) for partition, path in args.maegz},
    }
    args.output_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.output_manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
