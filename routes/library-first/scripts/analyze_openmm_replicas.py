#!/usr/bin/env python3
"""Analyze exactly three formally audited OpenMM production replicas."""
import argparse
import json

from covscreen_library_first.md_structural_analysis import analyze_three_replicas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("parameter-audit", "parameter-audit-sha256", "atom-mapping",
                 "atom-mapping-sha256", "reference-positions",
                 "reference-positions-sha256", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--replica-json", action="append", required=True)
    args = parser.parse_args()
    replicas = [json.loads(value) for value in args.replica_json]
    result = analyze_three_replicas(args.parameter_audit, args.parameter_audit_sha256,
        args.atom_mapping, args.atom_mapping_sha256, args.reference_positions,
        args.reference_positions_sha256, replicas, args.output)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
