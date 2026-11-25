"""Assign and audit native OPLS parameters for one prepared Cys adduct."""
import argparse
from pathlib import Path

import _bootstrap
from covscreen_library_first.md_parameterization import parameterize


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--qc', type=Path, required=True)
    parser.add_argument('--qc-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--vendor', type=Path, required=True)
    parser.add_argument('--expected-system-id', required=True)
    args = parser.parse_args()
    return parameterize(args.source, args.qc, args.qc_sha256, args.output, args.vendor,
                        expected_system_id=args.expected_system_id)


if __name__ == '__main__':
    raise SystemExit(main())
