"""Prepare native case inputs from a frozen acquisition; no runtime registration."""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
from pathlib import Path
from covscreen.native_case import prepare_native_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acquisition-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--gene", required=True)
    parser.add_argument("--taxon", type=int, required=True)
    parser.add_argument("--ligand-name", required=True)
    parser.add_argument("--noncovalent-parent-assertion", required=True)
    args = parser.parse_args()
    result = prepare_native_case(args.acquisition_dir, args.output_dir, campaign_id=args.campaign_id,
        gene=args.gene, taxon=args.taxon, ligand_name=args.ligand_name,
        noncovalent_parent_assertion=args.noncovalent_parent_assertion)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
