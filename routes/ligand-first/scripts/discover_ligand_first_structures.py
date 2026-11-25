"""Find RCSB candidate entries by explicit UniProt and CCD identifiers."""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(_bootstrap.PROJECT / "src"))
from covscreen.structure_discovery import ENDPOINT, build_query, discover, summarize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uniprot", required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(discover(args.uniprot.upper(), args.component.upper(),
                              args.limit, args.output_dir), indent=2))


if __name__ == "__main__":
    main()
