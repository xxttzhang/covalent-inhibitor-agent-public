#!/usr/bin/env python3
"""Admit one candidate-specific covalent adduct handoff."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import _bootstrap
from covscreen_library_first.md_adduct_input import admit_covalent_adduct_input


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "graph-qc", "candidate-evidence", "prepared-receptor",
                 "receptor-context"):
        parser.add_argument("--" + name, type=Path, required=True)
        parser.add_argument("--" + name + "-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = admit_covalent_adduct_input(args.source, args.source_sha256,
        args.graph_qc, args.graph_qc_sha256, args.candidate_evidence,
        args.candidate_evidence_sha256, args.prepared_receptor,
        args.prepared_receptor_sha256, args.receptor_context,
        args.receptor_context_sha256, args.output)
    print(json.dumps({"status": result["status"], "system_id": result["system_id"],
        "manifest_sha256": sha(args.output / "adduct-input.json"),
        "md_system_ready": result["md_system_ready"]}))


if __name__ == "__main__":
    main()
