#!/usr/bin/env python3
"""Launch one bounded native adduct-parameterization process."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import pwd
import subprocess


def regular(path, kind="file"):
    path = Path(path)
    check = path.is_dir() if kind == "directory" else path.is_file()
    if path.is_symlink() or not check:
        raise ValueError("Registered native runtime input is unavailable: " + str(path))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--vendor-root", type=Path, required=True)
    parser.add_argument("--license-file", type=Path, required=True)
    parser.add_argument("--runtime-lib-dir", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--qc", type=Path, required=True)
    parser.add_argument("--qc-sha256", required=True)
    parser.add_argument("--expected-system-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=1200)
    args = parser.parse_args()
    launcher = regular(args.launcher)
    vendor = regular(args.vendor_root, "directory")
    license_file = regular(args.license_file)
    runtime_lib = regular(args.runtime_lib_dir, "directory")
    source, qc = regular(args.source), regular(args.qc)
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Native parameter output must be a new path")
    if not 60 <= args.timeout_seconds <= 3600:
        raise ValueError("Native parameterization timeout must be in [60, 3600] seconds")
    route_script = Path(__file__).with_name("parameterize_covalent_adduct.py")
    identity = pwd.getpwuid(os.geteuid()).pw_name
    environment = {key: value for key, value in os.environ.items()
                   if key in {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR"}}
    environment.update(USER=identity, LOGNAME=identity, SCHRODINGER=str(vendor),
        SCHRODINGER_LICENSE_FILE=str(license_file), LD_LIBRARY_PATH=str(runtime_lib),
        OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2", MKL_NUM_THREADS="2",
        PYTHONDONTWRITEBYTECODE="1")
    command = [str(launcher), "python3", "-B", str(route_script),
        "--source", str(source), "--qc", str(qc), "--qc-sha256", args.qc_sha256,
        "--output", str(args.output), "--vendor", str(vendor),
        "--expected-system-id", args.expected_system_id]
    completed = subprocess.run(command, cwd=args.output.parent, env=environment,
                               stdin=subprocess.DEVNULL, timeout=args.timeout_seconds)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
