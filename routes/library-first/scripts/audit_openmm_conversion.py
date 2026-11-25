#!/usr/bin/env python3
"""Independently compare one OpenMM TOP/GRO conversion with its source CMS."""
import argparse
import json

from covscreen_library_first.md_system_audit import audit_openmm_conversion


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", required=True)
parser.add_argument("--source-sha256", required=True)
parser.add_argument("--parameter-audit", required=True)
parser.add_argument("--parameter-audit-sha256", required=True)
parser.add_argument("--conversion-directory", required=True)
parser.add_argument("--conversion-manifest-sha256", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
print(json.dumps(audit_openmm_conversion(args.source, args.source_sha256,
    args.parameter_audit, args.parameter_audit_sha256, args.conversion_directory,
    args.conversion_manifest_sha256, args.output), ensure_ascii=False, allow_nan=False))
