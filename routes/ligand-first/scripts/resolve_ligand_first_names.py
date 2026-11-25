"""Plan (default) or execute at most three public identity queries."""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
from pathlib import Path
from covscreen.ligand_identity import identity_plan, resolve_names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gene", required=True)
    parser.add_argument("--taxon", type=int, required=True)
    parser.add_argument("--cys", type=int, required=True)
    parser.add_argument("--ligand", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if args.execute and args.output_dir is None:
        parser.error("--execute requires a new --output-dir")
    values = (args.gene, args.taxon, args.cys, args.ligand)
    result = resolve_names(*values, args.output_dir) if args.execute else identity_plan(*values)
    print(json.dumps(result, indent=2))
    return 1 if result.get("status") == "lookup_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
