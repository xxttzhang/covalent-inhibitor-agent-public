from __future__ import annotations

import _bootstrap  # resolve this release, independent of cwd

import argparse
from pathlib import Path

import pandas as pd

from covscreen.chemistry import prepare_sdf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=Path("data/processed/BTK/btk_mini.parquet"))
    ap.add_argument("--sdf", type=Path, default=Path("data/processed/BTK/btk_mini.sdf"))
    args = ap.parse_args()
    frame = pd.read_parquet(args.input)
    failures = prepare_sdf(frame, args.sdf)
    failures.to_csv(args.sdf.with_name("chemistry_failures.csv"), index=False)
    print({"input": len(frame), "failures": len(failures), "sdf": str(args.sdf)})
    return 1 if len(failures) == len(frame) else 0


if __name__ == "__main__":
    raise SystemExit(main())
