"""Inspect top-level Maestro properties from ``.mae``/``.maegz`` files.

This parser is intentionally limited to the scalar property table at the start
of every ``f_m_ct`` block.  It does not interpret atom or bond blocks and does
not require a Schrödinger Python runtime.  The resulting JSON is compatible
with ``parse_covdock_scores.py`` while retaining the energy and strain fields
needed for a score audit.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO


IDENTITY_PROPERTIES = {
    "s_m_title",
    "s_sd_LABEL",
    "s_sd_TARGET_ID",
    "s_sd_SITE_ID",
    "s_sd_COMPOUND_ID",
    "s_sd_PARENT_ID",
    "i_sd_PROTOMER_ID",
    "i_sd_INPUT_SDF_INDEX",
}
SCORE_MARKERS = (
    "affinity",
    "strain",
    "score",
    "gscore",
    "prime_energy",
    "potential_energy",
)
PROPERTY_NAME = re.compile(r"^[sibr]_[A-Za-z0-9_\\-]+$")


def _clean_property_name(value: str) -> str:
    return value.strip().replace("\\_", "_")


def _decode_scalar(name: str, value: str) -> object:
    value = value.strip()
    if value in {"", "<>"}:
        return None
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].replace(r'\"', '"').replace(r"\\", "\\")
    try:
        if name.startswith("i_"):
            return int(value)
        if name.startswith("r_"):
            return float(value)
        if name.startswith("b_"):
            return value.lower() in {"1", "true", "yes"}
    except ValueError:
        # Preserve malformed or vendor-version-specific values for auditing.
        return value
    return value


def _retain_property(name: str, explicit: set[str]) -> bool:
    lower = name.lower()
    return (
        name in IDENTITY_PROPERTIES
        or name in explicit
        or (name.startswith("r_") and any(marker in lower for marker in SCORE_MARKERS))
    )


def iter_scalar_property_tables(handle: TextIO) -> Iterator[dict[str, object]]:
    """Yield the scalar property dictionary at the start of each CT block."""
    state = "seek"
    names: list[str] = []
    values: list[str] = []
    for raw_line in handle:
        stripped = raw_line.strip()
        if state == "seek":
            if stripped == "f_m_ct {":
                state = "names"
                names = []
                values = []
            continue
        if state == "names":
            if stripped == ":::":
                if not names:
                    raise ValueError("Encountered an empty f_m_ct property header")
                state = "values"
                continue
            if not PROPERTY_NAME.fullmatch(stripped):
                raise ValueError(f"Unexpected Maestro property name: {stripped!r}")
            names.append(_clean_property_name(stripped))
            continue

        values.append(stripped)
        if len(values) == len(names):
            yield {
                name: _decode_scalar(name, value)
                for name, value in zip(names, values)
            }
            state = "seek"
        elif len(values) > len(names):
            raise ValueError("Maestro scalar property value count exceeded header count")

    if state != "seek":
        raise ValueError(f"Truncated Maestro file while reading state={state}")


def _open_text(path: Path) -> TextIO:
    if path.suffix.lower() in {".maegz", ".gz"}:
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def inspect(path: Path, explicit: set[str]) -> tuple[list[dict[str, object]], list[str]]:
    rows: list[dict[str, object]] = []
    candidates: set[str] = set()
    with _open_text(path) as handle:
        for pose_index, properties in enumerate(iter_scalar_property_tables(handle)):
            retained = {
                key: value
                for key, value in properties.items()
                if _retain_property(key, explicit)
            }
            score_names = {
                key
                for key in retained
                if key.startswith("r_")
                and any(marker in key.lower() for marker in SCORE_MARKERS)
            }
            candidates.update(score_names)
            rows.append(
                {
                    "pose_index": pose_index,
                    "title": properties.get("s_m_title"),
                    **retained,
                }
            )
    return rows, sorted(candidates)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output-prefix", type=Path, required=True)
    ap.add_argument(
        "--property",
        action="append",
        default=[],
        help="Additional scalar property to retain; may be repeated.",
    )
    args = ap.parse_args()

    rows, candidate_score_properties = inspect(
        args.input, {_clean_property_name(value) for value in args.property}
    )
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.output_prefix.with_suffix(".json")
    csv_path = args.output_prefix.with_suffix(".csv")
    json_path.write_text(
        json.dumps(
            {
                "input": str(args.input.resolve()),
                "pose_count": len(rows),
                "candidate_score_properties": candidate_score_properties,
                "rows": rows,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    fieldnames = sorted({key for row in rows for key in row})
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(
        json.dumps(
            {
                "pose_count": len(rows),
                "candidate_score_properties": candidate_score_properties,
                "json": str(json_path),
                "csv": str(csv_path),
            },
            indent=2,
        )
    )
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
