from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from covscreen.dataset import normalize_protomers


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=Path("data/raw/covalid/ja5c22222_si_004.xlsx"))
    ap.add_argument("--out", type=Path, default=Path("data/interim/covalid_protomers.parquet"))
    args = ap.parse_args()
    if not args.input.exists():
        raise SystemExit(f"PENDING_CONFIRMATION: missing COValid input {args.input}")
    frame, report = normalize_protomers(args.input)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(args.out, index=False)
    Path("data/interim/covalid_field_mapping.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"rows": len(frame), "labels": frame["label"].value_counts().to_dict(), "report": report}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
