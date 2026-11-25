#!/usr/bin/env python3
"""Run hash-bound unrestrained NPT equilibration after short-QC."""
import argparse
import json

import _bootstrap
from covscreen.cpu_budget import constrain_cpu_budget

from covscreen_library_first.md_equilibration import run_openmm_equilibration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("parameter-audit", "parameter-audit-sha256", "minimization-directory",
                 "minimization-manifest-sha256", "short-qc-directory",
                 "short-qc-manifest-sha256", "output"):
        parser.add_argument("--"+name, required=True)
    parser.add_argument("--cpu-threads", type=int, required=True)
    parser.add_argument("--platform", choices=("CPU", "CUDA"), required=True)
    parser.add_argument("--platform-properties-json", default="{}")
    parser.add_argument("--duration-ps", type=float, default=500.0)
    parser.add_argument("--report-interval-ps", type=float, default=10.0)
    parser.add_argument("--integrator-seed", type=int, default=20261015)
    parser.add_argument("--barostat-seed", type=int, default=20261016)
    parser.add_argument("--progress-chunk-steps", type=int, default=25)
    args = parser.parse_args()
    properties = json.loads(args.platform_properties_json)
    if not isinstance(properties, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                               for k, v in properties.items()):
        raise ValueError("Platform properties must be a string-to-string object")
    print(json.dumps({"cpu_budget": constrain_cpu_budget(args.cpu_threads)}), flush=True)
    result = run_openmm_equilibration(args.parameter_audit, args.parameter_audit_sha256,
        args.minimization_directory, args.minimization_manifest_sha256,
        args.short_qc_directory, args.short_qc_manifest_sha256, args.output,
        platform_name=args.platform, platform_properties=properties,
        duration_ps=args.duration_ps, report_interval_ps=args.report_interval_ps,
        integrator_seed=args.integrator_seed, barostat_seed=args.barostat_seed,
        progress_chunk_steps=args.progress_chunk_steps)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
