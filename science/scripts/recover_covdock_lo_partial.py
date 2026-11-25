from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path


SUBJOB_SUFFIX = re.compile(r"\.(\d{5})-out\.maegz$")
LIBRARY_INDEX = re.compile(r"__i(\d+)$")


def _subjob_number(path: Path) -> int:
    match = SUBJOB_SUFFIX.search(path.name)
    if not match:
        raise ValueError(f"Not a CovDock subjob output: {path}")
    return int(match.group(1))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recover completed CovDock-LO subjobs from a stranded JobDJ directory."
    )
    ap.add_argument("--schrodinger-root", type=Path, required=True)
    ap.add_argument("--recovery-dir", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--jobname", required=True)
    ap.add_argument("--input-ligand", type=Path, required=True)
    ap.add_argument("--expected-subjobs", type=int, required=True)
    ap.add_argument("--failed-subjob", action="append", type=int, default=[])
    ap.add_argument("--failed-title", action="append", default=[])
    args = ap.parse_args()

    structcat = args.schrodinger_root / "utilities" / "structcat"
    if not structcat.is_file():
        raise SystemExit(f"Missing Schrödinger structcat: {structcat}")
    if not args.recovery_dir.is_dir():
        raise SystemExit(f"Missing recovery directory: {args.recovery_dir}")

    pattern = f"{args.jobname}.[0-9][0-9][0-9][0-9][0-9]-out.maegz"
    completed = sorted(args.recovery_dir.glob(pattern), key=_subjob_number)
    completed_numbers = [_subjob_number(path) for path in completed]
    expected_numbers = set(range(1, args.expected_subjobs + 1))
    failed_numbers = set(args.failed_subjob)
    missing_numbers = expected_numbers - set(completed_numbers)

    if not completed:
        raise SystemExit(f"No completed subjob outputs matching {pattern}")
    if missing_numbers != failed_numbers:
        raise SystemExit(
            "Declared failed subjobs do not match missing outputs: "
            f"declared={sorted(failed_numbers)}, missing={sorted(missing_numbers)}"
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    merged_output = args.output_dir / f"{args.jobname}-out.maegz"
    started = time.monotonic()
    command = [
        str(structcat),
        "-imae",
        *[str(path) for path in completed],
        "-omae",
        str(merged_output),
    ]
    subprocess.run(command, check=True)
    recovery_runtime_seconds = time.monotonic() - started

    input_chunks = sorted(
        args.recovery_dir.glob(
            f"{args.jobname}.[0-9][0-9][0-9][0-9][0-9].maegz"
        )
    )
    observed_files = completed + input_chunks
    for number in failed_numbers:
        failed_log = args.recovery_dir / f"{args.jobname}.{number:05d}.log"
        if failed_log.is_file():
            observed_files.append(failed_log)
    observed_start = min(path.stat().st_mtime for path in observed_files)
    observed_end = max(path.stat().st_mtime for path in observed_files)

    affected_indices: list[int] = []
    for title in args.failed_title:
        match = LIBRARY_INDEX.search(title)
        if not match:
            raise SystemExit(f"Failed title lacks __i<index> suffix: {title}")
        affected_indices.append(int(match.group(1)))

    manifest = {
        "engine": "schrodinger-covdock-lo",
        "schrodinger_root": str(args.schrodinger_root.resolve()),
        "license_configured": True,
        "license_value": "REDACTED",
        "license_preflight": "configured",
        "input_ligand": str(args.input_ligand.resolve()),
        "mode": "leadopt",
        "status": "PARTIAL_SUCCESS",
        "declared_failed": True,
        "expected_subjobs": args.expected_subjobs,
        "recovered_subjobs": completed_numbers,
        "recovered_subjob_count": len(completed_numbers),
        "failed_subjobs": sorted(failed_numbers),
        "failed_input_titles": args.failed_title,
        "affected_input_library_indices": sorted(affected_indices),
        "failure_scope_note": (
            "Affected input indices are not necessarily failed final library rows; "
            "another protomer or duplicate input may still provide a valid score."
        ),
        "runtime_seconds": observed_end - observed_start,
        "runtime_policy": "wall time inferred from preserved JobDJ file mtimes",
        "recovery_runtime_seconds": recovery_runtime_seconds,
        "recovery_command": command,
        "recovery_source": str(args.recovery_dir.resolve()),
        "parsed_outputs": {
            "maegz": [str(merged_output)],
            "mae": [],
            "pose_files": [str(merged_output)],
            "csv": [],
            "logs": [
                str(path)
                for path in sorted(args.recovery_dir.glob(f"{args.jobname}*.log"))
            ],
            "all_entries_failed": False,
        },
    }
    manifest_path = args.output_dir / f"{args.jobname}.run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")

    print(
        json.dumps(
            {
                "status": manifest["status"],
                "merged_output": str(merged_output),
                "manifest": str(manifest_path),
                "recovered_subjob_count": len(completed_numbers),
                "failed_subjobs": sorted(failed_numbers),
                "affected_input_library_indices": sorted(affected_indices),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
