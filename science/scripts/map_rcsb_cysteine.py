"""Map one explicit UniProt cysteine through RCSB entity/instance metadata.

Sequence mapping only: presence of an SG coordinate and ligand proximity still
require inspection of the downloaded structure. No structure is modified.
"""
import argparse
import hashlib
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


from covscreen.rcsb_mapping import map_cysteine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--entity", required=True)
    parser.add_argument("--chain", required=True, help="RCSB label_asym_id, not author chain")
    parser.add_argument("--uniprot", required=True)
    parser.add_argument("--residue", type=int, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    for value in (args.entry, args.entity, args.chain, args.uniprot):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,20}", value):
            parser.error("Only explicit safe identifiers are accepted")
    if args.residue < 1:
        parser.error("UniProt residue must be positive")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    payloads, provenance = [], []
    for kind, identifier in (("polymer_entity", args.entity), ("polymer_entity_instance", args.chain)):
        url = f"https://data.rcsb.org/rest/v1/core/{kind}/{args.entry}/{identifier}"
        with urllib.request.urlopen(url, timeout=30) as reply:
            raw = reply.read(10_000_001)
        if len(raw) > 10_000_000:
            raise ValueError("RCSB metadata exceeds size limit")
        payloads.append(json.loads(raw))
        (args.output_dir / f"{kind}.json").write_bytes(raw)
        provenance.append({"url": url, "sha256": hashlib.sha256(raw).hexdigest()})
    result = map_cysteine(*payloads, args.uniprot, args.residue)
    result.update(accessed_at=datetime.now(timezone.utc).isoformat(), sources=provenance)
    (args.output_dir / "cys_mapping.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
