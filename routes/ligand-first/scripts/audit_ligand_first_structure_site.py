"""Audit an RCSB-mapped Cys and parent-ligand instance without modifying inputs."""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(_bootstrap.PROJECT / "src"))
from covscreen.structure_site_audit import audit_structure_site


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdb", type=Path, required=True)
    parser.add_argument("--mapping-json", type=Path, required=True)
    parser.add_argument("--component", required=True)
    parser.add_argument("--ligand-chain")
    parser.add_argument("--ligand-residue")
    parser.add_argument("--model", type=int)
    parser.add_argument("--altloc")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw_mapping = args.mapping_json.read_bytes()
    result = audit_structure_site(args.pdb, json.loads(raw_mapping), args.component,
        ligand_chain=args.ligand_chain, ligand_residue=args.ligand_residue,
        model=args.model, altloc=args.altloc)
    result.update(mapping_sha256=hashlib.sha256(raw_mapping).hexdigest(),
                  mapping_path=str(args.mapping_json.resolve()),
                  structure_path=str(args.pdb.resolve()),
                  audited_at=datetime.now(timezone.utc).isoformat())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
