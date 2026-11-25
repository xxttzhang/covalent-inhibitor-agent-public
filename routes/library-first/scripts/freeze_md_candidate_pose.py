#!/usr/bin/env python3
"""Freeze one quality-passed Library-first CovDock pose for native MD QC."""
import argparse
import json

from covscreen_library_first.md_candidate_pose import freeze_candidate_pose


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--candidate-results", required=True)
parser.add_argument("--candidate-results-sha256", required=True)
parser.add_argument("--covdock-source", required=True)
parser.add_argument("--covdock-source-sha256", required=True)
parser.add_argument("--compound-id", required=True)
parser.add_argument("--input-sdf-index", required=True, type=int)
parser.add_argument("--prepared-receptor", required=True)
parser.add_argument("--prepared-receptor-sha256", required=True)
parser.add_argument("--receptor-context", required=True)
parser.add_argument("--receptor-context-sha256", required=True)
parser.add_argument("--output", required=True)
args = parser.parse_args()
print(json.dumps(freeze_candidate_pose(
    args.candidate_results, args.candidate_results_sha256,
    args.covdock_source, args.covdock_source_sha256, args.compound_id,
    args.input_sdf_index, args.prepared_receptor, args.prepared_receptor_sha256,
    args.receptor_context, args.receptor_context_sha256, args.output),
    ensure_ascii=False, allow_nan=False))
