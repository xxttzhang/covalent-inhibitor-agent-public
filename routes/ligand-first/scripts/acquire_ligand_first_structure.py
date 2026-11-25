"""Acquire public PDB and Cys mapping, then run the existing coordinate audit."""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
from pathlib import Path
from covscreen.structure_acquisition import acquisition_plan, acquire_structure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("entry", "uniprot", "component"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--cys", type=int, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--label-chain")
    parser.add_argument("--ligand-chain")
    parser.add_argument("--ligand-residue")
    parser.add_argument("--model", type=int)
    parser.add_argument("--altloc")
    parser.add_argument("--expected-inchikey")
    args = parser.parse_args()
    values = (args.entry, args.uniprot, args.cys, args.component)
    if args.execute and args.output_dir is None:
        parser.error("--execute requires a new --output-dir")
    result = acquire_structure(*values, args.output_dir, label_chain=args.label_chain,
        ligand_chain=args.ligand_chain, ligand_residue=args.ligand_residue,
        model=args.model, altloc=args.altloc, expected_inchikey=args.expected_inchikey) if args.execute else acquisition_plan(*values)
    if not args.execute and args.expected_inchikey:
        result.update(expected_inchikey=args.expected_inchikey, max_calls=27)
    print(json.dumps(result, indent=2))
    return 1 if result.get("status") == "acquisition_or_audit_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
