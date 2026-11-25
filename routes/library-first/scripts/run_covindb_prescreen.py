from __future__ import annotations

import _bootstrap  # resolve this release, independent of cwd

import argparse
import json
import sys
from pathlib import Path

ROOT = _bootstrap.PROJECT
sys.path.insert(0, str(ROOT / "src"))

from covscreen.covindb import run_prescreen, sha256_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run streaming CovalentInDB chemical QC and three-channel 2D prescreen."
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--target-config", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--record-selection", choices=["file_order_prefix", "reservoir"], default="file_order_prefix")
    parser.add_argument("--input-sha256", help="Frozen source digest; checked before chemistry runs")
    parser.add_argument("--top-n", type=int, default=20_000)
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--chunk-size", type=int, default=5_000)
    parser.add_argument("--seed", type=int, default=20260725)
    args = parser.parse_args()
    if args.top_n < 1:
        parser.error("--top-n must be positive")
    if args.max_rows is not None and args.max_rows < 1:
        parser.error("--max-rows must be positive")
    if args.max_rows is not None and args.top_n > args.max_rows:
        parser.error("--top-n cannot exceed --max-rows")
    if args.input_sha256 and sha256_file(args.input) != args.input_sha256:
        raise ValueError("Prescreen source checksum changed before execution")
    manifest = run_prescreen(
        args.input.resolve(),
        args.target_config.resolve(),
        args.output_dir.resolve(),
        top_n=args.top_n,
        max_rows=args.max_rows,
        chunk_size=args.chunk_size,
        seed=args.seed,
        record_selection=args.record_selection,
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0 if all(manifest["quality_gates"].values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
