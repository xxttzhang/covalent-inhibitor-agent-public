from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from covscreen.consensus_analysis import analyze_consensus


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--score-column", action="append", required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    frame = pd.read_parquet(args.input) if args.input.suffix.lower() == ".parquet" else pd.read_csv(args.input)
    report = analyze_consensus(frame, args.score_column)
    report["input"] = str(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    print(json.dumps({"output": str(args.output), "rows": report["rows"], "engines": report["engines"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
