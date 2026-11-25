"""Split a Maestro file by stable ``input_sdf_index`` ranges in titles.

Run with the licensed Schrödinger Python, for example::

    $SCHRODINGER/run python3 scripts/split_mae_by_ranges.py ...

LigPrep can produce multiple Maestro records for the same input index. Those
variants are intentionally retained and routed to the same output range.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from schrodinger.structure import StructureReader, StructureWriter


INDEX_PATTERN = re.compile(r"__i(\d+)(?:\D|$)")


def parse_ranges(value: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for item in value.split(","):
        if "-" not in item:
            raise ValueError(f"Range must be START-END: {item!r}")
        start_text, end_text = item.split("-", 1)
        start = int(start_text)
        end = int(end_text)
        if start > end:
            raise ValueError(f"Range start is greater than end: {item!r}")
        ranges.append((start, end))
    for i, (start_a, end_a) in enumerate(ranges):
        for start_b, end_b in ranges[i + 1 :]:
            if max(start_a, start_b) <= min(end_a, end_b):
                raise ValueError(f"Overlapping ranges: {(start_a, end_a)} and {(start_b, end_b)}")
    return ranges


def split_mae(
    input_mae: Path,
    output_dir: Path,
    ranges: list[tuple[int, int]],
    prefix: str,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    writers = []
    outputs = []
    for start, end in ranges:
        path = output_dir / f"{prefix}_{start}_{end}.mae"
        outputs.append(path)
        writers.append(StructureWriter(str(path)))

    counts = [0 for _ in ranges]
    indices: list[list[int]] = [[] for _ in ranges]
    unmatched: list[dict[str, object]] = []
    try:
        for structure in StructureReader(str(input_mae)):
            match = INDEX_PATTERN.search(structure.title)
            if not match:
                raise ValueError(f"Cannot recover input_sdf_index from title: {structure.title!r}")
            input_index = int(match.group(1))
            target = None
            for i, (start, end) in enumerate(ranges):
                if start <= input_index <= end:
                    target = i
                    break
            if target is None:
                unmatched.append({"title": structure.title, "input_sdf_index": input_index})
                continue
            writers[target].append(structure)
            counts[target] += 1
            indices[target].append(input_index)
    finally:
        for writer in writers:
            writer.close()

    duplicate_groups = [
        sum(1 for index in set(values) if values.count(index) > 1)
        for values in indices
    ]
    return {
        "input": str(input_mae),
        "outputs": [str(path) for path in outputs],
        "ranges": [[start, end] for start, end in ranges],
        "counts": counts,
        "unique_indices": [len(set(values)) for values in indices],
        "duplicate_input_sdf_index_groups": duplicate_groups,
        "index_ranges": [
            [min(values), max(values)] if values else None for values in indices
        ],
        "unmatched_count": len(unmatched),
        "unmatched_examples": unmatched[:10],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-mae", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--ranges", required=True, help="Comma-separated START-END ranges.")
    ap.add_argument("--prefix", default="covdock_btk_mini_q2split")
    ap.add_argument("--manifest", type=Path, required=True)
    args = ap.parse_args()
    report = split_mae(args.input_mae, args.output_dir, parse_ranges(args.ranges), args.prefix)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
