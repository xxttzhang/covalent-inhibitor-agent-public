"""Re-read selected CovDock structures using the existing coordinate inspector.

This is an independent execution of the same geometric check, not an independent
scoring model or experimental validation. Never infer evidence from gate columns.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pandas as pd

from .jobs import sha256_file


def recheck_coordinates(
    candidates: pd.DataFrame, *, schrodinger_root: Path, inspector: Path,
    output_dir: Path, attachment_chain: str, attachment_residue: int,
    nucleophilic_atom: str = "SG", severe_clash_distance: float = 1.2,
) -> pd.DataFrame:
    if "compound_id" not in candidates or candidates["compound_id"].isna().any():
        raise ValueError("Coordinate recheck requires compound identity")
    if candidates.empty or not candidates["input_sdf_index"].is_unique:
        raise ValueError("Coordinate recheck requires nonempty unique identities")
    if severe_clash_distance <= 0:
        raise ValueError("Clash distance must be positive")
    output_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "PYTHONHOME"):
        env.pop(name, None)
    env["SCHRODINGER"] = str(schrodinger_root.resolve())
    # Read each raw source once. Preserve failed identities rather than dropping them.
    cache: dict[str, tuple[dict, str, str]] = {}
    errors: dict[str, str] = {}
    rows = []
    for candidate in candidates.to_dict("records"):
        evidence = {"input_sdf_index": int(candidate["input_sdf_index"]),
                    "compound_id": candidate["compound_id"],
                    "coordinate_recheck_status": "FAILED"}
        try:
            source_value = candidate.get("covdock_pose_file")
            if not isinstance(source_value, str) or not source_value:
                raise ValueError("raw_pose_source_missing")
            source = Path(source_value).resolve()
            key = str(source)
            if key in errors:
                raise ValueError(errors[key])
            if key not in cache:
                try:
                    before = sha256_file(source)
                    prefix = output_dir / f"source_{len(cache) + len(errors):04d}"
                    command = [str(schrodinger_root / "run"), "python3", str(inspector),
                               "--input", key, "--output-prefix", str(prefix),
                               "--attachment-chain", attachment_chain,
                               "--attachment-residue", str(attachment_residue),
                               "--nucleophilic-atom", nucleophilic_atom,
                               "--severe-clash-distance", str(severe_clash_distance)]
                    with prefix.with_suffix(".log").open("w") as log:
                        subprocess.run(command, env=env, stdout=log, stderr=log,
                                       check=True, timeout=1800)
                    if sha256_file(source) != before:
                        raise ValueError("raw_pose_changed_during_recheck")
                    audit = prefix.with_suffix(".json")
                    payload = json.loads(audit.read_text())
                    if Path(payload["input"]).resolve() != source or not payload.get("geometry_requested"):
                        raise ValueError("coordinate_inspector_source_mismatch")
                    cache[key] = (payload, before, str(audit))
                except Exception as exc:
                    errors[key] = f"{type(exc).__name__}:{exc}"
                    raise
            payload, digest, audit_path = cache[key]
            index_value = float(candidate["covdock_pose_index"])
            if not index_value.is_integer() or index_value < 0:
                raise ValueError("invalid_pose_index")
            matches = [row for row in payload["rows"] if row["pose_index"] == int(index_value)]
            if len(matches) != 1:
                raise ValueError("selected_pose_not_unique")
            pose = matches[0]
            title = str(pose.get("title", ""))
            identity = re.search(r"__i(\d+)(?:\D|$)", title)
            if not identity or int(identity[1]) != evidence["input_sdf_index"]:
                raise ValueError("pose_identity_mismatch")
            if title != str(candidate.get("covdock_title", "")):
                raise ValueError("pose_title_mismatch")
            evidence.update({
                "coordinate_recheck_status": str(pose.get("geometry_inspection_status", "FAILED")),
                "coordinate_recheck_source": key,
                "coordinate_recheck_source_sha256": digest,
                "coordinate_recheck_audit": audit_path,
                "coordinate_recheck_audit_sha256": sha256_file(Path(audit_path)),
                "coordinate_recheck_link": pose.get("covalent_link_present"),
                "coordinate_recheck_clash": pose.get("severe_clash"),
                "coordinate_recheck_bond_length": pose.get("covalent_bond_length_angstrom"),
                "coordinate_recheck_failure": None,
            })
        except Exception as exc:
            evidence["coordinate_recheck_failure"] = f"{type(exc).__name__}:{exc}"
        rows.append(evidence)
    return pd.DataFrame(rows)
