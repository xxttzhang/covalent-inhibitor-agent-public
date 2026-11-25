from __future__ import annotations

import argparse
import json
from pathlib import Path

from covscreen.recall_pool import build_recall_pool


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the identity-preserving high-recall pool after CarsiDock-Cov and RTMScore."
    )
    parser.add_argument("--carsidock-scores", type=Path, required=True)
    parser.add_argument("--rtmscore-scores", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--top-n", type=int, default=2_000)
    parser.add_argument("--score-union-n", type=int)
    parser.add_argument("--diversity-n", type=int)
    parser.add_argument("--rescue-n", type=int)
    parser.add_argument("--min-carsidock-success-rate", type=float, default=0.75)
    args = parser.parse_args()

    manifest = build_recall_pool(
        args.carsidock_scores.resolve(),
        args.output_dir.resolve(),
        rtmscore_scores=(
            args.rtmscore_scores.resolve() if args.rtmscore_scores else None
        ),
        top_n=args.top_n,
        score_union_n=args.score_union_n,
        diversity_n=args.diversity_n,
        rescue_n=args.rescue_n,
        min_carsidock_success_rate=args.min_carsidock_success_rate,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
