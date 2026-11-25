from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd


AF3_JSON_VERSION = 4


def sanitise_job_name(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_")
    return text or "compound"


def make_af3_smiles_input(
    row: pd.Series,
    protein_sequence: str,
    seed: int,
    *,
    target_id: str = "BTK",
    protein_description: str = "BTK 5P9J chain A; Cys481 is sequence index 88",
) -> dict[str, object]:
    """Build an AF3 input while retaining the screening identifiers.

    AF3 does not allow bondedAtomPairs for a ligand supplied as SMILES because
    SMILES atoms do not have CCD atom names.  These inputs therefore perform
    an unconstrained protein-ligand cofolding smoke run.  Covalent geometry is
    assessed downstream relative to BTK Cys481 (sequence index 88).
    """
    compound_id = str(row["compound_id"])
    protomer_id = str(row["protomer_id"])
    target_token = sanitise_job_name(target_id)
    job_name = sanitise_job_name(
        f"{target_token}_{compound_id}_protomer_{protomer_id}"
    )
    ligand_description = json.dumps(
        {
            "compound_id": compound_id,
            "parent_id": str(row["parent_id"]),
            "protomer_id": protomer_id,
            "label": str(row["label"]),
            "matched_active_id": str(row["matched_active_id"]),
            "input_sdf_index": int(row["input_sdf_index"]),
        },
        sort_keys=True,
    )
    return {
        "name": job_name,
        "sequences": [
            {
                "protein": {
                    "id": "A",
                    "sequence": protein_sequence.strip(),
                    "description": protein_description,
                }
            },
            {
                "ligand": {
                    "id": "L",
                    "smiles": str(row["standardized_smiles"]),
                    "description": ligand_description,
                }
            },
        ],
        "modelSeeds": [int(seed)],
        "dialect": "alphafold3",
        "version": AF3_JSON_VERSION,
    }


def write_af3_inputs(
    frame: pd.DataFrame,
    protein_sequence: str,
    output_dir: str | Path,
    seed: int,
    *,
    target_id: str = "BTK",
    protein_description: str = "BTK 5P9J chain A; Cys481 is sequence index 88",
) -> pd.DataFrame:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for _, row in frame.iterrows():
        payload = make_af3_smiles_input(
            row,
            protein_sequence,
            seed,
            target_id=target_id,
            protein_description=protein_description,
        )
        json_path = output_dir / f"{payload['name']}.json"
        json_path.write_text(json.dumps(payload, indent=2) + "\n")
        records.append(
            {
                "job_name": payload["name"],
                "json_path": str(json_path),
                "compound_id": str(row["compound_id"]),
                "parent_id": str(row["parent_id"]),
                "protomer_id": str(row["protomer_id"]),
                "label": str(row["label"]),
                "matched_active_id": str(row["matched_active_id"]),
                "input_sdf_index": int(row["input_sdf_index"]),
                "model_seed": int(seed),
                "ligand_mode": "smiles",
                "covalent_bond_constraint": False,
                "covalent_bond_note": (
                    "AF3 bondedAtomPairs does not support SMILES ligands; "
                    f"evaluate ligand proximity to {target_id} downstream"
                ),
            }
        )
    return pd.DataFrame.from_records(records)
