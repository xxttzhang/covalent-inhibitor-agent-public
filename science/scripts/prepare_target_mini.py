from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from covscreen.dataset import build_target_mini


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--target-id", required=True)
    ap.add_argument("--site-id", required=True)
    ap.add_argument("--active-count", type=int, default=20)
    ap.add_argument("--decoys-per-active", type=int, default=20)
    ap.add_argument("--seed", type=int, default=20260824)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    frame = (
        pd.read_parquet(args.input)
        if args.input.suffix.lower() == ".parquet"
        else pd.read_csv(args.input)
    )
    mini = build_target_mini(
        frame,
        target_id=args.target_id,
        site_id=args.site_id,
        active_count=args.active_count,
        decoys_per_active=args.decoys_per_active,
        seed=args.seed,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    mini.to_parquet(args.output, index=False)
    mini.to_csv(args.output.with_suffix(".csv"), index=False)
    print(
        json.dumps(
            {
                "target_id": args.target_id,
                "site_id": args.site_id,
                "rows": len(mini),
                "active": int(mini["label"].eq("active").sum()),
                "decoy": int(mini["label"].eq("decoy").sum()),
                "unique_active_parents": int(
                    mini.loc[mini["label"].eq("active"), "parent_id"].nunique()
                ),
                "seed": args.seed,
                "output": str(args.output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
