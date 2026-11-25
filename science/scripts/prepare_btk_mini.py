from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from covscreen.dataset import build_btk_mini


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=Path("data/interim/covalid_protomers.parquet"))
    ap.add_argument("--out", type=Path, default=Path("data/processed/BTK/btk_mini.parquet"))
    args = ap.parse_args()
    if not args.input.exists():
        raise SystemExit("Run prepare_covalid.py first")
    frame = pd.read_parquet(args.input)
    mini = build_btk_mini(frame, active_count=20, decoys_per_active=20, seed=20260725)
    # S3 identifies the BTK worksheet pair as "BTK"; S1 supplies the
    # residue-specific benchmark site used by the docking command.
    mini["site_id"] = "BTK_C481"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    mini.to_parquet(args.out, index=False)
    mini.to_csv(args.out.with_suffix(".csv"), index=False)
    print({"rows": len(mini), "active": int((mini.label == "active").sum()), "decoy": int((mini.label == "decoy").sum()), "unique_active_parents": int(mini.loc[mini.label == "active", "parent_id"].nunique())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
