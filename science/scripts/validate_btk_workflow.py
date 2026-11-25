from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = {"compound_id", "input_sdf_index", "raw_score"}


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def audit_table(path: Path, library: pd.DataFrame) -> dict[str, object]:
    report: dict[str, object] = {
        "path": str(path),
        "exists": path.is_file(),
        "status": "MISSING",
    }
    if not path.is_file():
        return report
    try:
        frame = _read_table(path)
    except Exception as exc:
        report.update(
            {
                "status": "UNREADABLE",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return report
    missing = sorted(REQUIRED_COLUMNS - set(frame.columns))
    duplicate_indices = int(frame["input_sdf_index"].duplicated().sum()) if "input_sdf_index" in frame else None
    known_indices = set(library["input_sdf_index"].astype(int))
    observed_indices = set(pd.to_numeric(frame["input_sdf_index"], errors="coerce").dropna().astype(int)) if "input_sdf_index" in frame else set()
    unknown_indices = sorted(observed_indices - known_indices)
    successful = (
        frame["engine_status"].eq("SUCCESS")
        if "engine_status" in frame
        else pd.to_numeric(frame["raw_score"], errors="coerce").notna()
    )
    finite_scores = pd.to_numeric(frame["raw_score"], errors="coerce").notna()
    report.update(
        {
            "rows": int(len(frame)),
            "missing_columns": missing,
            "duplicate_input_sdf_index": duplicate_indices,
            "unknown_input_sdf_index": unknown_indices,
            "successful_rows": int((successful & finite_scores).sum()),
            "failed_rows": int((~(successful & finite_scores)).sum()),
            "score_directions": sorted(
                frame["score_direction"].dropna().astype(str).unique()
            )
            if "score_direction" in frame
            else [],
        }
    )
    report["status"] = (
        "PASS"
        if not missing
        and duplicate_indices == 0
        and not unknown_indices
        and bool((successful & finite_scores).any())
        else "INVALID"
    )
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--library",
        type=Path,
        default=Path("data/processed/BTK/btk_mini.parquet"),
    )
    ap.add_argument("--expected-rows", type=int, default=420)
    ap.add_argument("--expected-active", type=int, default=20)
    ap.add_argument("--expected-decoy", type=int, default=400)
    ap.add_argument(
        "--engine",
        action="append",
        default=[],
        metavar="NAME=PATH",
        help="repeatable engine result table declaration",
    )
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--require-all", action="store_true")
    args = ap.parse_args()

    library = _read_table(args.library)
    identity_columns = ["compound_id", "input_sdf_index"]
    missing_identity = [column for column in identity_columns if column not in library]
    if missing_identity:
        raise ValueError(f"Library is missing identity columns: {missing_identity}")
    library_report = {
        "path": str(args.library),
        "rows": int(len(library)),
        "active": int(library["label"].eq("active").sum()) if "label" in library else None,
        "decoy": int(library["label"].eq("decoy").sum()) if "label" in library else None,
        "expected_rows": args.expected_rows,
        "expected_active": args.expected_active,
        "expected_decoy": args.expected_decoy,
        "duplicate_compound_id": int(library["compound_id"].duplicated().sum()),
        "duplicate_input_sdf_index": int(library["input_sdf_index"].duplicated().sum()),
    }
    engines: dict[str, dict[str, object]] = {}
    for declaration in args.engine:
        if "=" not in declaration:
            raise ValueError(f"Engine declaration must be NAME=PATH: {declaration}")
        name, raw_path = declaration.split("=", 1)
        engines[name] = audit_table(Path(raw_path), library)
    result = {
        "library": library_report,
        "engines": engines,
        "status": "PASS",
    }
    if (
        library_report["rows"] != args.expected_rows
        or library_report["active"] != args.expected_active
        or library_report["decoy"] != args.expected_decoy
        or library_report["duplicate_compound_id"]
        or library_report["duplicate_input_sdf_index"]
    ):
        result["status"] = "INVALID_LIBRARY"
    if any(report["status"] in {"INVALID", "UNREADABLE"} for report in engines.values()):
        result["status"] = "INVALID_ENGINE_TABLE"
    if args.require_all and (
        not engines or any(report["status"] != "PASS" for report in engines.values())
    ):
        result["status"] = "INCOMPLETE"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
