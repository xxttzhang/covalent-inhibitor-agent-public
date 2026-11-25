#!/usr/bin/env python3
"""Run one admitted 10 ns OpenMM production replica."""
import argparse
import json

from covscreen_library_first.md_production import run_openmm_production


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("parameter-audit", "parameter-audit-sha256", "conversion-directory",
                 "conversion-manifest-sha256", "minimization-directory",
                 "minimization-manifest-sha256", "equilibration-directory",
                 "equilibration-manifest-sha256", "output"):
        p.add_argument("--"+name, required=True)
    p.add_argument("--replica", type=int, required=True)
    p.add_argument("--velocity-seed", type=int, required=True)
    p.add_argument("--integrator-seed", type=int, required=True)
    p.add_argument("--barostat-seed", type=int, required=True)
    p.add_argument("--platform", choices=("CPU", "CUDA"), required=True)
    p.add_argument("--platform-properties-json", default="{}")
    a = p.parse_args(); properties = json.loads(a.platform_properties_json)
    if not isinstance(properties, dict): raise ValueError("Platform properties must be an object")
    result = run_openmm_production(a.parameter_audit, a.parameter_audit_sha256,
        a.conversion_directory, a.conversion_manifest_sha256,
        a.minimization_directory, a.minimization_manifest_sha256,
        a.equilibration_directory, a.equilibration_manifest_sha256, a.output,
        replica=a.replica, velocity_seed=a.velocity_seed, integrator_seed=a.integrator_seed,
        barostat_seed=a.barostat_seed, platform_name=a.platform,
        platform_properties=properties, duration_ps=10000, trajectory_interval_ps=10)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__": main()
