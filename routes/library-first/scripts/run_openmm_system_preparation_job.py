#!/usr/bin/env python3
"""Convert, audit and bounded-minimize one parameterized covalent CMS."""
import argparse
import hashlib
import json
from pathlib import Path

import _bootstrap
from covscreen.cpu_budget import constrain_cpu_budget
from covscreen_library_first.md_system_audit import audit_openmm_conversion
from covscreen_library_first.md_system_conversion import convert_parameterized_cms
from covscreen_library_first.md_system_minimization import minimize_audited_openmm_system


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--parameter-audit", required=True)
    parser.add_argument("--parameter-audit-sha256", required=True)
    parser.add_argument("--intermol-source", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--cpu-threads", type=int, default=2)
    parser.add_argument("--max-iterations", type=int, default=100)
    args = parser.parse_args()
    print(json.dumps({"cpu_budget": constrain_cpu_budget(args.cpu_threads)}), flush=True)
    output = Path(args.output)
    if output.exists() or output.is_symlink():
        raise ValueError("OpenMM preparation output must be a new directory")
    output.mkdir(parents=True, exist_ok=False)
    conversion_dir = output / "conversion"
    conversion = convert_parameterized_cms(args.source, args.source_sha256,
        args.parameter_audit, args.parameter_audit_sha256, args.intermol_source,
        conversion_dir)
    conversion_sha = sha(conversion_dir / "conversion.json")
    audit_dir = output / "forcefield-audit"
    audit = audit_openmm_conversion(args.source, args.source_sha256,
        args.parameter_audit, args.parameter_audit_sha256, conversion_dir,
        conversion_sha, audit_dir)
    audit_sha = sha(audit_dir / "forcefield-audit.json")
    minimization_dir = output / "minimization"
    minimization = minimize_audited_openmm_system(args.parameter_audit,
        args.parameter_audit_sha256, conversion_dir, conversion_sha, audit_dir,
        audit_sha, minimization_dir, cpu_threads=args.cpu_threads,
        max_iterations=args.max_iterations)
    result = {"schema": "openmm-system-preparation-artifact-v1",
        "status": "OPENMM_SYSTEM_MINIMIZED_SHORT_QC_PENDING",
        "system_id": conversion["system_id"],
        "parents": {"source_cms_sha256": args.source_sha256,
            "parameter_audit_sha256": args.parameter_audit_sha256},
        "stages": {"conversion_manifest_sha256": conversion_sha,
            "forcefield_audit_manifest_sha256": audit_sha,
            "minimization_manifest_sha256": sha(minimization_dir / "minimization.json")},
        "atoms": minimization["atoms"], "independent_forcefield_audit_completed": True,
        "bounded_minimization_completed": True, "short_qc_completed": False,
        "equilibration_completed": False, "md_system_ready": False,
        "campaign_ready": False, "simulation_started": False}
    (output / "openmm-preparation.json").write_text(
        json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
