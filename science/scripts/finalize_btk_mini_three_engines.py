from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_SCORE_CANDIDATES = (
    "r_i_docking_score",
    "r_i_glide_gscore",
    "r_i_glide_score",
    "r_i_CovDock_Score",
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
    for row in data.get("rows", []):
        if row.get(property_name) not in (None, ""):
            return True
    return False


def _select_score_property(paths: list[Path], explicit: str | None) -> str:
    if explicit:
        missing = [str(path) for path in paths if not _inspection_has_property(path, explicit)]
        if missing:
            raise ValueError(
                f"Explicit score property {explicit!r} is absent in: {missing}"
            )
        return explicit

    for candidate in DEFAULT_SCORE_CANDIDATES:
        if all(_inspection_has_property(path, candidate) for path in paths):
            return candidate

    candidates: dict[str, list[str]] = {}
    for path in paths:
        data = json.loads(path.read_text())
        candidates[str(path)] = data.get("candidate_score_properties", [])
    raise ValueError(
        "Could not choose a CovDock score property automatically. "
        f"Candidate properties by audit file: {json.dumps(candidates, indent=2)}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Finalize BTK-mini metrics, validation, fusion, and report after CovDock q0-q3 finish."
    )
    ap.add_argument("--results-dir", type=Path, default=Path("results/BTK-mini"))
    ap.add_argument("--library", type=Path, default=Path("data/processed/BTK/btk_mini.parquet"))
    ap.add_argument("--covdock-identity-table", type=Path, default=Path("data/processed/BTK/btk_mini.parquet"))
    ap.add_argument("--schrodinger-root", type=Path, default=Path(os.environ.get("SCHRODINGER", "")))
    ap.add_argument("--score-property", default=None)
    ap.add_argument("--score-direction", choices=["lower_is_better", "higher_is_better"], default="lower_is_better")
    ap.add_argument("--force-inspect", action="store_true")
    args = ap.parse_args()

    if not args.schrodinger_root or not (args.schrodinger_root / "run").is_file():
        raise SystemExit("Set --schrodinger-root or SCHRODINGER to a valid Schrodinger installation.")

    scripts = Path("scripts")
    inspections: list[Path] = []
    runtime_manifests: list[Path] = []
    for partition in range(4):
        qdir = args.results_dir / f"covdock_btk_mini_q{partition}"
        out_maegz = _first_file(qdir, "*-out.maegz")
        runtime_manifests.append(_first_file(qdir, "*.run_manifest.json"))
        prefix = args.results_dir / f"covdock_dist_q{partition}_property_audit"
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
    covdock_scores = args.results_dir / "covdock_btk_mini_scores_final.parquet"
    parse_command = [
        sys.executable,
        str(scripts / "parse_covdock_scores.py"),
        "--identity-table",
        str(args.covdock_identity_table),
        "--score-property",
        score_property,
        "--score-direction",
        args.score_direction,
        "--output",
        str(covdock_scores),
    ]
    for path in inspections:
        parse_command.extend(["--inspection-json", str(path)])
    _run(parse_command)

    metrics_command = [
        sys.executable,
        str(scripts / "compute_metrics.py"),
        "--input",
        str(covdock_scores),
        "--output",
        str(args.results_dir / "covdock_metrics.json"),
        "--engine",
        "schrodinger-covdock-vs",
        "--score-direction",
        args.score_direction,
    ]
    for path in runtime_manifests:
        metrics_command.extend(["--runtime-manifest", str(path)])
    _run(metrics_command)

    carsidock_scores = args.results_dir / "carsidock_scores_current.parquet"
    af3_scores = args.results_dir / "af3_btk_mini_scores_final.parquet"
    _run(
        [
            sys.executable,
            str(scripts / "validate_btk_workflow.py"),
            "--library",
            str(args.library),
            "--engine",
            f"carsidock={carsidock_scores}",
            "--engine",
            f"af3={af3_scores}",
            "--engine",
            f"covdock={covdock_scores}",
            "--require-all",
            "--output",
            str(Path("reports/BTK-mini/final_validation.json")),
        ]
    )

    fused = args.results_dir / "three_engine_fused.parquet"
    _run(
        [
            sys.executable,
            str(scripts / "fuse_rankings.py"),
            "--library",
            str(args.library),
            "--engine",
            f"carsidock:{carsidock_scores}:raw_score:lower_is_better",
            "--engine",
            f"af3:{af3_scores}:raw_score:lower_is_better",
            "--engine",
            f"covdock:{covdock_scores}:raw_score:{args.score_direction}",
            "--output",
            str(fused),
        ]
    )
    _run(
        [
            sys.executable,
            str(scripts / "analyze_consensus.py"),
            "--input",
            str(fused),
            "--score-column",
            "carsidock_score",
            "--score-column",
            "af3_score",
            "--score-column",
            "covdock_score",
            "--output",
            str(args.results_dir / "three_engine_conflict_summary.json"),
        ]
    )
    _run([sys.executable, str(scripts / "generate_report.py")])

    summary = {
        "status": "PASS",
        "covdock_score_property": score_property,
        "covdock_score_direction": args.score_direction,
        "covdock_scores": str(covdock_scores),
        "covdock_metrics": str(args.results_dir / "covdock_metrics.json"),
        "fused_rankings": str(fused),
        "conflict_summary": str(args.results_dir / "three_engine_conflict_summary.json"),
        "validation": "reports/BTK-mini/final_validation.json",
        "report": "reports/BTK-mini/report.md",
    }
    summary_path = args.results_dir / "three_engine_finalize_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
