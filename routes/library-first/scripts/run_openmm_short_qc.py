#!/usr/bin/env python3
"""Run bounded unrestrained short-QC on one minimized OpenMM system."""
import argparse
import json

import _bootstrap
from covscreen.cpu_budget import constrain_cpu_budget
from covscreen_library_first.md_short_qc import run_openmm_short_qc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("parameter-audit", "parameter-audit-sha256", "conversion-directory",
                 "conversion-manifest-sha256", "forcefield-audit-directory",
                 "forcefield-audit-manifest-sha256", "minimization-directory",
                 "minimization-manifest-sha256", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--steps", type=int, default=1000)
    parser.add_argument("--report-interval", type=int, default=100)
    parser.add_argument("--velocity-seed", type=int, default=20261014)
    args = parser.parse_args()
    print(json.dumps({"cpu_budget": constrain_cpu_budget(args.cpu_threads)}), flush=True)
    result = run_openmm_short_qc(args.parameter_audit, args.parameter_audit_sha256,
        args.conversion_directory, args.conversion_manifest_sha256,
        args.forcefield_audit_directory, args.forcefield_audit_manifest_sha256,
        args.minimization_directory, args.minimization_manifest_sha256, args.output,
        cpu_threads=args.cpu_threads, steps=args.steps, report_interval=args.report_interval,
        velocity_seed=args.velocity_seed)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
