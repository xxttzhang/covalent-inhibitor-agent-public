#!/usr/bin/env python3
"""Create a descriptive site-context artifact from registered receptor parents."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from covscreen.receptor_input import load_receptor_preparation_input
from covscreen.receptor_site_context import assess_receptor_site_context


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def within(root, value):
    path = Path(value).resolve()
    path.relative_to(root)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--receptor-input-artifact-id", required=True)
    parser.add_argument("--receptor-input-manifest-sha256", required=True)
    parser.add_argument("--prepared-receptor-dir", type=Path, required=True)
    parser.add_argument("--prepared-receptor-manifest-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    root = args.project_root.resolve()
    prepared = within(root, args.prepared_receptor_dir)
    output = Path(args.output_dir).resolve()
    output.parent.relative_to(root)
    loaded = load_receptor_preparation_input(root, args.receptor_input_artifact_id,
                                             args.receptor_input_manifest_sha256)
    result = assess_receptor_site_context(loaded["acquisition_directory"],
        args.receptor_input_manifest_sha256, loaded["acquisition"], prepared,
        args.prepared_receptor_manifest_sha256, output)
    print(json.dumps({"status": result["status"], "output": str(output),
        "manifest_sha256": sha(output / "site-context.json"),
        "campaign_ready": result["campaign_ready"]}, allow_nan=False))


if __name__ == "__main__":
    main()
