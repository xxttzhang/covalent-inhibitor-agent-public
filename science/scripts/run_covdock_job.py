from __future__ import annotations

import argparse
import json
import os
import pwd
import subprocess
import time
from pathlib import Path

from covscreen.adapters.covdock import CovDockAdapter
from covscreen.schrodinger import validate_license_config
from covscreen.jobs import sha256_file
from covscreen.covdock_receptor import inspect_receptor, verify_reused_inputs, read_effective_receptor


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schrodinger-root", type=Path, required=True)
    ap.add_argument("--receptor", type=Path, required=True)
    ap.add_argument("--ligand", type=Path, required=True)
    ap.add_argument("--attachment-residue", default="A:481")
    ap.add_argument("--nucleophilic-atom", default="SG")
    ap.add_argument("--expected-receptor-sha256")
    ap.add_argument("--expected-ligand-sha256")
    ap.add_argument("--expected-grid-sha256")
    ap.add_argument("--reaction-type", default="Michael Addition")
    ap.add_argument("--mode", default="enrichment")
    ap.add_argument("--jobname", required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--prepare-receptor", action="store_true")
    ap.add_argument("--njobs", type=int, default=1)
    ap.add_argument(
        "--host",
        help="Optional Schrödinger -HOST value, e.g. localhost:24.",
    )
    ap.add_argument(
        "--grid-file",
        type=Path,
        help="Reuse a validated CovDock grid for an independent ligand chunk",
    )
    ap.add_argument(
        "--restart",
        action="store_true",
        help="Pass -RESTART to CovDock to reuse completed subjob outputs when possible.",
    )
    ap.add_argument(
        "--covdock-flag",
        action="append",
        choices=["affinity", "strain", "mmgbsa"],
        default=[],
        help="Optional CovDock scoring flags, passed as -affinity/-strain/-mmgbsa.",
    )
    ap.add_argument(
        "--min-radius",
        type=float,
        default=None,
        help="Optional Prime optimization radius passed as -min_radius.",
    )
    args = ap.parse_args()

    receptor_check = inspect_receptor(args.receptor, args.attachment_residue,
        args.nucleophilic_atom, expected_sha256=args.expected_receptor_sha256)
    ligand_sha = sha256_file(args.ligand)
    if args.expected_ligand_sha256 is not None and ligand_sha != args.expected_ligand_sha256:
        raise ValueError("CovDock ligand differs from the frozen prepared input")
    grid_sha = sha256_file(args.grid_file) if args.grid_file else None
    if args.expected_grid_sha256 is not None and grid_sha != args.expected_grid_sha256:
        raise ValueError("CovDock grid differs from the frozen plan")
    license_value = os.environ.get("SCHROD_LICENSE_FILE", "")
    acceptable, reason = validate_license_config(license_value)
    # CovDock and JobDJ run with the output directory as their working
    # directory.  Resolve it once so prepared receptor/grid paths are not
    # accidentally interpreted relative to that directory a second time.
    output_dir = args.output_dir.resolve()
    manifest_path = output_dir / f"{args.jobname}.run_manifest.json"
    previous = None
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text())
        verify_reused_inputs(previous, receptor_check['receptor_sha256'], ligand_sha,
            args.attachment_residue, args.reaction_type, args.mode,
            prepare_receptor=args.prepare_receptor, grid_sha256=grid_sha)
        if not args.restart:
            raise ValueError("Existing CovDock invocation requires an explicit verified restart or a new output directory")
    elif args.restart:
        raise ValueError("CovDock restart requires a frozen prior invocation manifest")
    output_dir.mkdir(parents=True, exist_ok=True)
    engine_name = (
        "schrodinger-covdock-lo"
        if args.mode == "leadopt"
        else "schrodinger-covdock-vs"
    )
    manifest: dict[str, object] = {
        "engine": engine_name,
        "schrodinger_root": str(args.schrodinger_root),
        "license_configured": bool(license_value),
        "license_value": "REDACTED" if license_value else None,
        "license_preflight": reason,
        "input_receptor": str(args.receptor.resolve()),
        "input_receptor_sha256": receptor_check['receptor_sha256'],
        "receptor_input_check": receptor_check,
        "input_ligand": str(args.ligand.resolve()),
        "input_ligand_sha256": ligand_sha,
        "prepare_receptor": args.prepare_receptor,
        "grid_sha256": grid_sha,
        "attachment_residue": args.attachment_residue,
        "reaction_type": args.reaction_type,
        "mode": args.mode,
        "host": args.host,
        "restart": args.restart,
        "covdock_flags": args.covdock_flag,
        "min_radius": args.min_radius,
        "status": "PRECHECK_PENDING",
    }
    if not acceptable:
        manifest["status"] = "LICENSE_BLOCKED"
        if previous is None:
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        print(json.dumps(manifest, indent=2))
        return 3

    env = os.environ.copy()
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "PYTHONHOME", "PYTHONPATH"):
        env.pop(key, None)
    env["SCHRODINGER"] = str(args.schrodinger_root)
    # Slurm containers may omit login-shell variables; Schrodinger job control
    # requires them even when local LigPrep can run without them.
    username = pwd.getpwuid(os.getuid()).pw_name
    for key in ("USER", "LOGNAME"):
        if not env.get(key, "").strip():
            env[key] = username
    manifest["job_control_username"] = env["USER"]
    receptor = args.receptor.resolve()
    start = time.monotonic()

    if args.prepare_receptor and args.restart:
        read_effective_receptor(manifest_path, args.receptor, args.attachment_residue,
            args.nucleophilic_atom)
        receptor = output_dir / f"{args.jobname}_receptor_prepared.mae"
        manifest.update({key: value for key, value in previous.items() if key.startswith('prepwizard_')})
        manifest['preparation_reused'] = True
    elif args.prepare_receptor:
        prepared = output_dir / f"{args.jobname}_receptor_prepared.mae"
        prep_command = [
            str(args.schrodinger_root / "utilities" / "prepwizard"),
            str(receptor),
            prepared.name,
            "-WAIT",
            "-LOCAL",
            "-NJOBS",
            "1",
        ]
        prep_log = output_dir / f"{args.jobname}.prepwizard.log"
        with prep_log.open("w") as log:
            prep = subprocess.run(
                prep_command,
                cwd=output_dir,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
        manifest["prepwizard_command"] = prep_command
        manifest["prepwizard_return_code"] = prep.returncode
        manifest["prepwizard_log"] = str(prep_log)
        if prep.returncode != 0 or not prepared.is_file():
            manifest["status"] = "PREPARATION_FAILED"
            manifest["runtime_seconds"] = time.monotonic() - start
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
            return 1
        receptor = prepared

    effective_check = inspect_receptor(receptor, args.attachment_residue, args.nucleophilic_atom,
        expected_sha256=None if args.prepare_receptor else receptor_check['receptor_sha256'])
    manifest['effective_receptor'] = str(receptor)
    manifest['effective_receptor_sha256'] = effective_check['receptor_sha256']
    manifest['effective_receptor_input_check'] = effective_check
    if sha256_file(args.receptor) != receptor_check['receptor_sha256']:
        raise ValueError("CovDock source receptor changed during preparation")
    if sha256_file(args.ligand) != ligand_sha:
        raise ValueError("CovDock ligand changed during preparation")
    if args.grid_file and sha256_file(args.grid_file) != grid_sha:
        raise ValueError("CovDock grid changed during preparation")
    adapter = CovDockAdapter(str(args.schrodinger_root))
    extra_options = [f"-{flag}" for flag in args.covdock_flag]
    # Keep job-control scratch on the same persistent filesystem, but outside
    # output_dir: parse_output recursively scans that directory and must not
    # mistake an unfinished subjob's structures for final docking output.
    scratch = output_dir.parent / f"{output_dir.name}_job_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    env["SCHRODINGER_TMPDIR"] = str(scratch)
    env["SCHRODINGER_SAVE_JOBDIR"] = "1"
    extra_options.extend(["-TMPDIR", str(scratch), "-SAVE"])
    manifest["job_scratch_directory"] = str(scratch)
    manifest["retain_job_scratch"] = True
    if args.host:
        extra_options.extend(["-HOST", args.host])
    if args.restart:
        extra_options.append("-RESTART")
    if args.min_radius is not None:
        extra_options.extend(["-min_radius", str(args.min_radius)])
    command = adapter.build_command(
        str(receptor),
        str(args.ligand.resolve()),
        args.attachment_residue,
        args.jobname,
        reaction_type=args.reaction_type,
        mode=args.mode,
        njobs=args.njobs,
        grid_file=str(args.grid_file.resolve()) if args.grid_file else None,
        extra_options=extra_options,
    )
    if args.mode == 'leadopt':
        launcher = Path(__file__).resolve().with_name('covdock_isolated_progress.py')
        if not launcher.is_file():
            raise ValueError('CovDock progress-isolation launcher is missing')
        command = [str(args.schrodinger_root / 'run'), str(launcher), *command[1:]]
        manifest['progress_channel_adapter'] = {
            'version': 'covdock-isolated-progress-v1', 'path': str(launcher),
            'sha256': sha256_file(launcher), 'scientific_options_changed': False}
    stdout_path = output_dir / f"{args.jobname}.stdout.log"
    stderr_path = output_dir / f"{args.jobname}.stderr.log"
    # Persist before entering vendor job control: Slurm can kill this process
    # without allowing a final write. STARTED is evidence of invocation only,
    # never proof that the vendor finished or that a pose passed validation.
    manifest.update({
        "command": command,
        "stdout_file": str(stdout_path),
        "stderr_file": str(stderr_path),
        "status": "STARTED",
    })
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        proc = subprocess.run(
            command,
            cwd=output_dir,
            env=env,
            stdout=stdout,
            stderr=stderr,
            check=False,
        )
    parsed = adapter.parse_output(output_dir)
    if (sha256_file(receptor) != effective_check['receptor_sha256']
            or sha256_file(args.ligand) != ligand_sha
            or (args.grid_file and sha256_file(args.grid_file) != grid_sha)):
        raise ValueError("CovDock effective inputs changed during execution")
    manifest.update(
        {
            "command": command,
            "return_code": proc.returncode,
            "runtime_seconds": time.monotonic() - start,
            "stdout_file": str(stdout_path),
            "stderr_file": str(stderr_path),
            "parsed_outputs": parsed,
            "status": (
                "SUCCESS"
                if proc.returncode == 0 and adapter.validate_output(parsed)
                else "FAILED"
            ),
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return 0 if manifest["status"] == "SUCCESS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
