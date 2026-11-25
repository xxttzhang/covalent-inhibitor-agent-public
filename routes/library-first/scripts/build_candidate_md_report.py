#!/usr/bin/env python3
"""Build the versioned Library-first candidate MD triage report."""
import argparse
import json

from covscreen_library_first.md_candidate_report import build_candidate_md_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--structural-analysis", required=True)
    parser.add_argument("--structural-analysis-sha256", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = build_candidate_md_report(args.structural_analysis,
        args.structural_analysis_sha256, args.output)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
