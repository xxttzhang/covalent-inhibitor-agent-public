#!/usr/bin/env python3
"""Convert one audited parameterized CMS; independent force audit remains pending."""
import argparse
import json

from covscreen_library_first.md_system_conversion import convert_parameterized_cms


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", required=True)
parser.add_argument("--source-sha256", required=True)
parser.add_argument("--parameter-audit", required=True)
parser.add_argument("--parameter-audit-sha256", required=True)
parser.add_argument("--intermol-source", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
print(json.dumps(convert_parameterized_cms(args.source, args.source_sha256,
    args.parameter_audit, args.parameter_audit_sha256, args.intermol_source, args.output),
    ensure_ascii=False, allow_nan=False))
