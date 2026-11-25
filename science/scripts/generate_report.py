from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _engine_status_lines(results_root: Path) -> list[str]:
    manifests = sorted(results_root.rglob("*.run_manifest.json"))
    if not manifests:
        return ["- No engine run manifests were found."]
    lines = []
    for path in manifests:
        report = _load_json(path)
        engine = report.get("engine", "unknown")
        status = report.get("status", "UNKNOWN")
        runtime = report.get("runtime_seconds")
        runtime_text = (
            f", runtime {float(runtime):.1f} s"
            if isinstance(runtime, (int, float))
            else ""
        )
        lines.append(
            f"- `{engine}`: **{status}**{runtime_text} "
            f"(`{path.relative_to(results_root.parent.parent)}`)."
        )
    return lines


def _metrics_lines(results_root: Path) -> list[str]:
    metric_files = sorted(results_root.rglob("*metrics*.json"))
    if not metric_files:
        return [
            "- No completed metric reports were found; metrics remain pending "
            "for engines without a validated score table."
        ]
    lines: list[str] = []
    for path in metric_files:
        report = _load_json(path)
        if not report:
            lines.append(f"- Invalid metric JSON: `{path}`.")
            continue
        n_scored = report.get("n_scoring_success", report.get("n_success", 0))
        lines.extend(
            [
                (
                    f"- `{report.get('engine', path.stem)}`: "
                    f"{n_scored}/{report.get('n_input', 0)} "
                    f"scored; failures {report.get('n_failed', 0)}; direction "
                    f"`{report.get('score_direction', 'UNKNOWN')}`."
                ),
                "",
                "```json",
                json.dumps(report, indent=2, ensure_ascii=False),
                "```",
            ]
        )
    return lines


def main() -> int:
    out = Path("reports/BTK-mini/report.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    covalid = Path("data/interim/covalid_protomers.parquet")
    mini = Path("data/processed/BTK/btk_mini.parquet")
    failures = Path("data/processed/BTK/chemistry_failures.csv")
    results_root = Path("results/BTK-mini")
    lines = ["# BTK-mini Library-first report", "", "## Data and preparation", ""]
    if covalid.is_file():
        c = pd.read_parquet(covalid)
        lines.append(f"- COValid normalized rows: {len(c)} ({int((c.label == 'active').sum())} active, {int((c.label == 'decoy').sum())} decoy).")
    else:
        lines.append("- COValid normalized table: missing.")
    if mini.is_file():
        m = pd.read_parquet(mini)
        lines.append(f"- BTK-mini rows: {len(m)} ({int((m.label == 'active').sum())} active, {int((m.label == 'decoy').sum())} decoy).")
        lines.append(f"- Active parent compounds: {m.loc[m.label == 'active', 'parent_id'].nunique()}; site: {', '.join(sorted(m.site_id.astype(str).unique()))}.")
    else:
        lines.append("- BTK-mini table: missing.")
    if failures.is_file():
        f = pd.read_csv(failures)
        lines.append(f"- Chemistry failures: {len(f)}; recorded in `{failures}`.")
    lines.extend(
        [
            "",
            "## Receptor input",
            "",
            "- PDB: `data/raw/pdb/5P9J.pdb` (chain A, Cys481).",
            "- Reference ligand: `data/raw/pdb/5p9j_B_8E8.sdf` (RCSB ligand 8E8).",
            "",
            "## Engine runs",
            "",
            *_engine_status_lines(results_root),
            "",
            "## Metrics",
            "",
            *_metrics_lines(results_root),
            "",
            "## Reproducibility policy",
            "",
            "- Missing or failed engine outputs remain explicit; no synthetic score is generated.",
            "- Identity joins use `compound_id`, `protomer_id`, and `input_sdf_index`.",
            "- Score direction must be frozen before ranking metrics or rank fusion.",
            "",
        ]
    )
    out.write_text("\n".join(lines) + "\n")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
