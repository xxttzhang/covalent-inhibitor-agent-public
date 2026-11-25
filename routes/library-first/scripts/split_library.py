from __future__ import annotations

import _bootstrap  # resolve this release, independent of cwd

import argparse
from pathlib import Path

import pandas as pd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=Path("data/processed/BTK/btk_mini.parquet"))
    ap.add_argument("--out-dir", type=Path, default=Path("data/processed/BTK/chunks"))
    ap.add_argument("--chunk-size", type=int, default=100)
    args = ap.parse_args()
    frame = pd.read_parquet(args.input)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, start in enumerate(range(0, len(frame), args.chunk_size)):
        chunk = frame.iloc[start : start + args.chunk_size].copy()
        path = args.out_dir / f"chunk_{i:04d}.parquet"
        chunk.to_parquet(path, index=False)
        manifest.append({"chunk": i, "path": str(path), "rows": len(chunk), "first_index": int(start), "last_index": int(start + len(chunk) - 1)})
    pd.DataFrame(manifest).to_parquet(args.out_dir / "chunk_manifest.parquet", index=False)
    print({"chunks": len(manifest), "rows": len(frame), "sizes": [x["rows"] for x in manifest]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
