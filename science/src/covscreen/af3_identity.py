from __future__ import annotations

import hashlib
import re
from typing import Any


_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9_-]+")
_COMPONENT_ID = re.compile(r"[A-Z0-9]{1,5}")


def safe_token(value: object, *, max_length: int = 40) -> str:
    """Return a bounded filesystem-safe token while retaining collision evidence."""

    original = str(value)
    normalized = _SAFE_TOKEN.sub("_", original).strip("_-") or "unknown"
    if normalized == original and len(normalized) <= max_length:
        return normalized
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:10]
    prefix_length = max(1, max_length - len(digest) - 2)
    return f"{normalized[:prefix_length]}__{digest}"


def af3_job_name(
    target_id: object,
    compound_id: object,
    protomer_id: object,
    input_sdf_index: int,
) -> str:
    """Build a stable AF3 job name independent of private/raw source formatting."""

    if input_sdf_index < 0:
        raise ValueError("input_sdf_index must be non-negative")
    return (
        f"{safe_token(target_id, max_length=20)}_"
        f"{safe_token(compound_id, max_length=40)}__"
        f"p{safe_token(protomer_id, max_length=20)}__i{input_sdf_index}_covalent"
    )


def validate_component_id(value: str) -> str:
    """Validate the short CCD identifier used inside one isolated AF3 job."""

    if not _COMPONENT_ID.fullmatch(value):
        raise ValueError("AF3 component_id must contain 1-5 uppercase letters or digits")
    return value


def merge_source_metadata(
    manifest: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any]:
    """Carry upstream discovery evidence without overwriting AF3 identity fields."""

    protected = {
        "job_name",
        "input_json",
        "user_ccd",
        "component_id",
        "compound_id",
        "protomer_id",
        "input_sdf_index",
        "af3_target_seed_check",
        "protein_chain",
        "cysteine_sequence_index",
        "source_residue_number",
        "ligand_chain",
        "electrophilic_atom",
        "nucleophilic_atom",
        "bonded_atom_pairs",
        "protein_msa_templates_reused_from",
    }
    merged = dict(manifest)
    merged.update({key: value for key, value in row.items() if key not in protected})
    merged["compound_id"] = str(row["compound_id"])
    merged["protomer_id"] = str(row["protomer_id"])
    merged["input_sdf_index"] = int(row["input_sdf_index"])
    return merged
