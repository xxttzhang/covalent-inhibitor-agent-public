#!/usr/bin/env python3
"""Audit one admitted OpenMM production trajectory without interpreting binding."""
import argparse
import json

from covscreen_library_first.md_trajectory_audit import audit_production_trajectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("parameter-audit", "parameter-audit-sha256", "production-directory",
                 "production-manifest-sha256", "output"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = audit_production_trajectory(
        args.parameter_audit,
        args.parameter_audit_sha256,
        args.production_directory,
        args.production_manifest_sha256,
        args.output,
    )
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
