from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import time
from pathlib import Path

from covscreen.schrodinger import sha256_file, validate_license_config


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run Schrödinger LigPrep with a legitimate license."
    )
    ap.add_argument("--schrodinger-root", type=Path, required=True)
    ap.add_argument("--license-file", type=Path, required=True)
    ap.add_argument("--input-sdf", type=Path, required=True)
    ap.add_argument("--output-mae", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--runtime-lib-dir", type=Path)
    ap.add_argument("--jobname", required=True)
    args = ap.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_dir / f"{args.jobname}.run_manifest.json"
    if not args.license_file.is_file():
        manifest = {
            "engine": "schrodinger-ligprep",
            "jobname": args.jobname,
            "license_preflight": "license file is missing",
            "license_file_sha256": None,
            "status": "LICENSE_MISSING",
        }
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        return 3
    license_ok, license_reason = validate_license_config(
        str(args.license_file)
    )
    manifest: dict[str, object] = {
        "engine": "schrodinger-ligprep",
        "jobname": args.jobname,
        "schrodinger_root": str(args.schrodinger_root),
        "license_preflight": license_reason,
        "license_file_sha256": (
            sha256_file(args.license_file) if args.license_file.is_file() else None
        ),
        "input_sdf": str(args.input_sdf.resolve()),
        "input_sdf_sha256": (
            sha256_file(args.input_sdf) if args.input_sdf.is_file() else None
        ),
        "output_mae": str(args.output_mae.resolve()),
        "status": "PRECHECK_PENDING",
    }
    if not license_ok:
        manifest["status"] = "LICENSE_BLOCKED"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        return 3
    if not args.input_sdf.is_file():
        manifest["status"] = "INPUT_MISSING"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        return 2

    executable = args.schrodinger_root / "ligprep"
    if not executable.is_file():
        manifest["status"] = "EXECUTABLE_MISSING"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        return 2
    command = [
        str(executable),
        "-isd",
        str(args.input_sdf.resolve()),
        "-omae",
        str(args.output_mae.resolve()),
        "-i",
        "0",
        "-s",
        "1",
        "-NJOBS",
        "1",
        "-LOCAL",
        "-WAIT",
    ]
    env = os.environ.copy()
    for key in (
        "SCHRODINGER_LICENSE_FILE",
        "LM_LICENSE_FILE",
        "PYTHONHOME",
        "PYTHONPATH",
        "LD_PRELOAD",
        "LD_AUDIT",
    ):
        env.pop(key, None)
    env["SCHRODINGER"] = str(args.schrodinger_root)
    env["SCHROD_LICENSE_FILE"] = str(args.license_file.resolve())
    # Some cluster/container launchers omit USER and LOGNAME.  Schrödinger's
    # JobDJ backend treats those variables as required even for local jobs.
    username = env.get("USER") or env.get("LOGNAME") or getpass.getuser()
    env["USER"] = username
    env["LOGNAME"] = username
    if args.runtime_lib_dir:
        env["LD_LIBRARY_PATH"] = str(args.runtime_lib_dir.resolve())
    stdout_path = args.output_dir / f"{args.jobname}.stdout.log"
    stderr_path = args.output_dir / f"{args.jobname}.stderr.log"
    start = time.monotonic()
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.run(
            command,
            cwd=args.output_dir,
            env=env,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    manifest.update(
        {
            "command": command,
            "return_code": process.returncode,
            "runtime_seconds": time.monotonic() - start,
            "stdout_file": str(stdout_path),
            "stderr_file": str(stderr_path),
            "output_mae_exists": args.output_mae.is_file(),
            "output_mae_sha256": (
                sha256_file(args.output_mae)
                if args.output_mae.is_file()
                else None
            ),
            "status": (
                "SUCCESS"
                if process.returncode == 0 and args.output_mae.is_file()
                else "FAILED"
            ),
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return 0 if manifest["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
