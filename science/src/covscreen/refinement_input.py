"""Exact-input guard for resource-approved small refinement batches.

This verifies input identity, not permission to allocate resources. The platform
must obtain native approval separately and supply all three frozen values.
Legacy standalone callers may omit the entire contract, never part of it.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd

CONTRACT_PARAMETERS = ("expected_input_count", "expected_input_sha256", "expected_identity_sha256")


def read_candidate_bytes(path: Path, contents: bytes) -> pd.DataFrame:
    """Parse the same candidate bytes the caller verified, in either supported format."""
    from io import BytesIO
    if Path(path).suffix.lower() == ".parquet":
        return pd.read_parquet(BytesIO(contents))
    if Path(path).suffix.lower() == ".csv":
        return pd.read_csv(BytesIO(contents), dtype={"compound_id": str})
    raise ValueError("Refinement candidate table must be CSV or Parquet")


def refinement_input_arguments(path: Path, parameters: dict) -> list[str]:
    """Verify a declared contract and forward it to the existing worker guard."""
    present = set(CONTRACT_PARAMETERS).intersection(parameters)
    if not present:
        if "refinement_input_contract" in parameters:
            raise ValueError("Refinement receipt cannot replace the frozen input contract")
        return []
    if present != set(CONTRACT_PARAMETERS) or any(parameters[key] is None for key in present):
        raise ValueError("Refinement requires count, file hash and identity hash together")
    verified = verify_refinement_input(path, *(parameters[key] for key in CONTRACT_PARAMETERS))
    parameters["refinement_input_contract"] = verified
    return [part for key in CONTRACT_PARAMETERS for part in
            ("--" + key.replace("_", "-"), str(parameters[key]))]


def verify_refinement_identities(rows: list[dict], expected_count: int, expected_sha256: str) -> str:
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise ValueError("Refinement preparation changed the frozen input denominator")
    indexes, compounds, identities = set(), set(), []
    for row in rows:
        index, compound = row.get("input_sdf_index"), row.get("compound_id")
        if type(index) is not int or index < 0 or not isinstance(compound, str) or not compound.strip():
            raise ValueError("Refinement input has an invalid molecular identity")
        if index in indexes or compound in compounds:
            raise ValueError("Refinement input has duplicated molecular identities")
        indexes.add(index)
        compounds.add(compound)
        identities.append([index, compound])
    identity = hashlib.sha256(json.dumps(sorted(identities), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    if identity != expected_sha256:
        raise ValueError("Refinement molecular identities differ from the frozen subset")
    return identity


def verify_refinement_input(
    path: Path,
    expected_count: int | None,
    expected_sha256: str | None,
    expected_identity_sha256: str | None,
    *, expected_route: str | None = None,
) -> dict[str, object] | None:
    values = (expected_count, expected_sha256, expected_identity_sha256)
    if all(value is None for value in values):
        if expected_route is not None:
            raise ValueError("Route-bound refinement requires an exact input contract")
        return None  # Historical CLI behavior; not a platform-approved contract.
    if any(value is None for value in values):
        raise ValueError("Refinement requires count, file hash and identity hash together")
    if type(expected_count) is not int or not 1 <= expected_count <= 100:
        raise ValueError("Refinement input count must be an explicit small batch in [1,100]")
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
           for value in (expected_sha256, expected_identity_sha256)):
        raise ValueError("Refinement input hashes must be lowercase SHA256")
    contents = path.read_bytes()
    checksum = hashlib.sha256(contents).hexdigest()
    if checksum != expected_sha256:
        raise ValueError("Refinement candidate file differs from the frozen subset")
    # Parse the very bytes just hashed, not a second read of a mutable path.
    frame = read_candidate_bytes(path, contents)
    if len(frame) != expected_count:
        raise ValueError("Refinement input count differs from the frozen subset; top-n is not a compute cap")
    if expected_route is not None and (
            "route" not in frame or not frame["route"].eq(expected_route).all()):
        raise ValueError("Refinement candidate route differs from the registered source route")
    if not {"input_sdf_index", "compound_id", "standardized_smiles"}.issubset(frame.columns):
        raise ValueError("Refinement subset lacks required identity or molecular input columns")
    rows = frame[["input_sdf_index", "compound_id", "standardized_smiles"]].to_dict(orient="records")
    for row in rows:
        if not isinstance(row["standardized_smiles"], str) or not row["standardized_smiles"].strip():
            raise ValueError("Refinement input has an empty SMILES")
    identity = verify_refinement_identities(rows, expected_count, expected_identity_sha256)
    return {"input_count": len(frame), "input_sha256": checksum, "identity_sha256": identity,
            "verified": True, "grants_resource_approval": False}
