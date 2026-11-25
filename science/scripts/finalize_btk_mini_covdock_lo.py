from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_SCORE_CANDIDATES = (
    "r_cdock_cdock_affinity",
    "r_i_docking_score",
    "r_i_sample_docking_score",
    "r_i_sample_glide_gscore",
)


def _run(command: list[str], env: dict[str, str] | None = None) -> None:
    print(json.dumps({"running": command}, ensure_ascii=False))
    subprocess.run(command, check=True, env=env)


def _first_file(directory: Path, pattern: str) -> Path:
    matches = sorted(directory.glob(pattern))
    if not matches:
        raise FileNotFoundError(f"No {pattern} found in {directory}")
    return matches[0]


def _inspection_has_property(path: Path, property_name: str) -> bool:
    data = json.loads(path.read_text())
    return any(row.get(property_name) not in (None, "") for row in data.get("rows", []))


def _select_score_property(paths: list[Path], explicit: str | None) -> str:
    if explicit:
        missing = [str(path) for path in paths if not _inspection_has_property(path, explicit)]
        if missing:
            raise ValueError(f"Explicit score property {explicit!r} is absent in: {missing}")
        return explicit
    for candidate in DEFAULT_SCORE_CANDIDATES:
        if all(_inspection_has_property(path, candidate) for path in paths):
            return candidate
    candidates = {
        str(path): json.loads(path.read_text()).get("candidate_score_properties", [])
        for path in paths
    }
    raise ValueError(
        "Could not choose a CovDock-LO score property automatically. "
        f"Candidate properties by audit file: {json.dumps(candidates, indent=2)}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Finalize BTK-mini CovDock-LO after q0-q3 finish.")
    ap.add_argument("--results-dir", type=Path, default=Path("results/BTK-mini"))
    ap.add_argument("--identity-table", type=Path, default=Path("data/processed/BTK/btk_mini.parquet"))
    ap.add_argument("--schrodinger-root", type=Path, default=Path(os.environ.get("SCHRODINGER", "")))
    ap.add_argument("--score-property", default=None)
    ap.add_argument("--score-direction", choices=["lower_is_better", "higher_is_better"], default="lower_is_better")
    ap.add_argument(
        "--partition",
        action="append",
        default=None,
        help="Partition suffix without leading q, e.g. 0, 1, 2a, 2b. Defaults to 0,1,2,3.",
    )
    ap.add_argument("--force-inspect", action="store_true")
    args = ap.parse_args()

    if not args.schrodinger_root or not (args.schrodinger_root / "run").is_file():
        raise SystemExit("Set --schrodinger-root or SCHRODINGER to a valid Schrodinger installation.")

    scripts = Path("scripts")
    partitions = args.partition or ["0", "1", "2", "3"]
    inspections: list[Path] = []
    runtime_manifests: list[Path] = []
    for partition in partitions:
        qdir = args.results_dir / f"covdock_lo_btk_mini_q{partition}"
        out_maegz = _first_file(qdir, "*-out.maegz")
        runtime_manifests.append(_first_file(qdir, "*.run_manifest.json"))
        prefix = args.results_dir / f"covdock_lo_dist_q{partition}_property_audit"
        inspection_json = prefix.with_suffix(".json")
        if args.force_inspect or not inspection_json.is_file():
            schrodinger_env = os.environ.copy()
            schrodinger_env["SCHRODINGER"] = str(args.schrodinger_root)
            for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME"):
                schrodinger_env.pop(key, None)
            _run(
                [
                    str(args.schrodinger_root / "run"),
                    "python3",
                    str(scripts / "inspect_covdock_output.py"),
                    "--input",
                    str(out_maegz),
                    "--output-prefix",
                    str(prefix),
                ],
                env=schrodinger_env,
            )
        inspections.append(inspection_json)

    score_property = _select_score_property(inspections, args.score_property)
    scores_path = args.results_dir / "covdock_lo_btk_mini_scores_final.parquet"
    parse_command = [
        sys.executable,
        str(scripts / "parse_covdock_scores.py"),
        "--identity-table",
        str(args.identity_table),
        "--score-property",
        score_property,
        "--score-direction",
        args.score_direction,
        "--engine",
        "schrodinger-covdock-lo",
        "--output",
        str(scores_path),
    ]
    for path in inspections:
        parse_command.extend(["--inspection-json", str(path)])
    _run(parse_command)

    metrics_command = [
        sys.executable,
        str(scripts / "compute_metrics.py"),
        "--input",
        str(scores_path),
        "--output",
        str(args.results_dir / "covdock_lo_metrics.json"),
        "--engine",
        "schrodinger-covdock-lo",
        "--score-direction",
        args.score_direction,
    ]
    for path in runtime_manifests:
        metrics_command.extend(["--runtime-manifest", str(path)])
    _run(metrics_command)

    summary = {
        "status": "PASS",
        "score_property": score_property,
        "score_direction": args.score_direction,
        "scores": str(scores_path),
        "metrics": str(args.results_dir / "covdock_lo_metrics.json"),
        "inspection_json": [str(path) for path in inspections],
    }
    summary_path = args.results_dir / "covdock_lo_finalize_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
