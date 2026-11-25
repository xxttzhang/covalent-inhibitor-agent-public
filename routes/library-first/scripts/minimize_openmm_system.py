#!/usr/bin/env python3
"""Run bounded CPU minimization on one independently audited OpenMM system."""
import argparse
import json

from covscreen_library_first.md_system_minimization import minimize_audited_openmm_system


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--parameter-audit", required=True)
parser.add_argument("--parameter-audit-sha256", required=True)
parser.add_argument("--conversion-directory", required=True)
parser.add_argument("--conversion-manifest-sha256", required=True)
parser.add_argument("--forcefield-audit-directory", required=True)
parser.add_argument("--forcefield-audit-manifest-sha256", required=True)
parser.add_argument("--output", required=True)
parser.add_argument("--cpu-threads", type=int, default=2)
parser.add_argument("--max-iterations", type=int, default=100)
parser.add_argument("--tolerance-kj-mol-nm", type=float, default=10.0)
args = parser.parse_args()
print(json.dumps(minimize_audited_openmm_system(args.parameter_audit,
    args.parameter_audit_sha256, args.conversion_directory,
    args.conversion_manifest_sha256, args.forcefield_audit_directory,
    args.forcefield_audit_manifest_sha256, args.output, args.cpu_threads,
    args.max_iterations, args.tolerance_kj_mol_nm), ensure_ascii=False, allow_nan=False))
