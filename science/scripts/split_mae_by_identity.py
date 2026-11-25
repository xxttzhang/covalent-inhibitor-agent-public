"""Split a Maestro file by the stable ``input_sdf_index`` in each title.

Run with the licensed Schrödinger Python, for example::

    $SCHRODINGER/run python3 scripts/split_mae_by_identity.py ...

The title is the only identity source used here; output order is never used for
joining results back to the library.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from schrodinger.structure import StructureReader, StructureWriter


INDEX_PATTERN = re.compile(r"__i(\d+)(?:\D|$)")


def split_mae(
    input_mae: Path,
    output_a: Path,
    output_b: Path,
    split_index: int,
) -> dict[str, object]:
    output_a.parent.mkdir(parents=True, exist_ok=True)
    output_b.parent.mkdir(parents=True, exist_ok=True)
    writers = [StructureWriter(str(output_a)), StructureWriter(str(output_b))]
    counts = [0, 0]
    indices: list[list[int]] = [[], []]
    try:
        for structure in StructureReader(str(input_mae)):
            match = INDEX_PATTERN.search(structure.title)
            if not match:
                raise ValueError(
                    f"Cannot recover input_sdf_index from title: {structure.title!r}"
                )
            input_index = int(match.group(1))
            part = 0 if input_index < split_index else 1
            writers[part].append(structure)
            counts[part] += 1
            indices[part].append(input_index)
    finally:
        for writer in writers:
            writer.close()
    if any(len(set(values)) != len(values) for values in indices):
        raise ValueError("Duplicate input_sdf_index found in Maestro input")
    return {
        "input": str(input_mae),
        "outputs": [str(output_a), str(output_b)],
        "split_index": split_index,
        "counts": counts,
        "index_ranges": [
            [min(values), max(values)] if values else None for values in indices
        ],
        "unique_indices": [len(set(values)) for values in indices],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-mae", type=Path, required=True)
    ap.add_argument("--output-a", type=Path, required=True)
    ap.add_argument("--output-b", type=Path, required=True)
    ap.add_argument("--split-index", type=int, default=212)
    ap.add_argument("--manifest", type=Path, required=True)
    args = ap.parse_args()
    report = split_mae(
        args.input_mae, args.output_a, args.output_b, args.split_index
    )
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
