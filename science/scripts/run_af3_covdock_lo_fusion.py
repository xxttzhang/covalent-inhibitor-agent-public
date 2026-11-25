from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from covscreen.fusion_benchmark import (
    freeze_payload,
    merge_pair,
    select_policy,
    simulate_fusions,
)


def _read(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def _json_default(value):
    if hasattr(value, "item"):
        return value.item()
    raise TypeError(type(value).__name__)


def _markdown_table(frame: pd.DataFrame) -> str:
    def cell(value) -> str:
        if pd.isna(value):
            return ""
        if isinstance(value, float):
            return f"{value:.4f}"
        return str(value).replace("|", "\\|")

    headers = [str(column).replace("|", "\\|") for column in frame.columns]
    rows = ["| " + " | ".join(headers) + " |"]
    rows.append("| " + " | ".join(["---"] * len(headers)) + " |")
    rows.extend(
        "| " + " | ".join(cell(value) for value in row) + " |"
        for row in frame.itertuples(index=False, name=None)
    )
    return "\n".join(rows)


def _write_report(
    path: Path,
    metrics: pd.DataFrame,
    common_metrics: pd.DataFrame,
    policy: dict,
    freeze: dict,
) -> None:
    display_columns = [
        column
        for column in [
            "method",
            "covdock_selected_n",
            "covdock_fraction",
            "adjusted_logauc_percent",
            "roc_auc",
            "ef1_percent",
            "ef5_percent",
            "bedroc_alpha20",
            "top10_active",
            "top25_active",
            "top50_active",
            "projected_runtime_hours",
            "projected_savings_vs_full_dual",
        ]
        if column in metrics.columns
    ]
    display = metrics[display_columns].copy()
    common_display = common_metrics[
        [column for column in display.columns if column in common_metrics.columns]
    ].copy()
    lines = [
        "# BTK-mini AF3–mPAE + CovDock-LO 离线融合实验",
        "",
        f"- 冻结编号：`{freeze['freeze_id']}`",
        f"- 算法版本：`{freeze['algorithm_version']}`",
        f"- 开发靶点：`{freeze['development_target']}`",
        f"- 选定策略：`{policy.get('selected_method')}`",
        f"- 选择状态：`{policy['status']}`",
        "- 失败策略：失败或缺失分数统一排在末尾。",
        "- 数据用途：BTK-mini 仅用于开发和冻结；EGFR C797 为首次独立验证。",
        "",
        "## 敏感性结果",
        "",
        _markdown_table(display),
        "",
        "## 两引擎共同成功子集",
        "",
        _markdown_table(common_display),
        "",
        "## 冻结规则",
        "",
        "```json",
        json.dumps(freeze, indent=2, ensure_ascii=False, default=_json_default),
        "```",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def _runtime_total(path: Path | None) -> float | None:
    if path is None:
        return None
    data = json.loads(path.read_text())
    value = data.get("runtime_seconds_total")
    return float(value) if value is not None else None


def _add_projected_runtime(
    metrics: pd.DataFrame,
    *,
    n_input: int,
    af3_total: float | None,
    covdock_total: float | None,
) -> pd.DataFrame:
    out = metrics.copy()
    if af3_total is None or covdock_total is None:
        return out
    covdock_per_input = covdock_total / n_input
    full_dual = af3_total + covdock_total
    projected = []
    for row in out.itertuples(index=False):
        if row.method == "af3_mpae":
            seconds = af3_total
        elif row.method == "covdock_lo":
            seconds = covdock_total
        elif row.method == "full_rrf":
            seconds = full_dual
        else:
            seconds = af3_total + covdock_per_input * int(row.covdock_selected_n)
        projected.append(seconds)
    out["projected_runtime_seconds"] = projected
    out["projected_runtime_hours"] = out["projected_runtime_seconds"] / 3600.0
    out["projected_savings_vs_full_dual"] = 1.0 - (
        out["projected_runtime_seconds"] / full_dual
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", type=Path, required=True)
    ap.add_argument("--af3", type=Path, required=True)
    ap.add_argument("--covdock-lo", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--af3-metrics", type=Path)
    ap.add_argument("--covdock-lo-metrics", type=Path)
    ap.add_argument("--fraction", type=float, action="append", default=None)
    ap.add_argument("--rrf-k", type=float, default=60.0)
    ap.add_argument("--bedroc-margin", type=float, default=0.03)
    ap.add_argument("--top10-margin", type=int, default=1)
    ap.add_argument("--top25-margin", type=int, default=1)
    args = ap.parse_args()

    source_paths = {
        "library": args.library,
        "af3_scores": args.af3,
        "covdock_lo_scores": args.covdock_lo,
    }
    if args.af3_metrics:
        source_paths["af3_metrics"] = args.af3_metrics
    if args.covdock_lo_metrics:
        source_paths["covdock_lo_metrics"] = args.covdock_lo_metrics
    merged = merge_pair(_read(args.library), _read(args.af3), _read(args.covdock_lo))
    fused, metrics = simulate_fusions(
        merged,
        fractions=args.fraction or (0.05, 0.10, 0.20),
        rrf_k=args.rrf_k,
    )
    metrics = _add_projected_runtime(
        metrics,
        n_input=len(merged),
        af3_total=_runtime_total(args.af3_metrics),
        covdock_total=_runtime_total(args.covdock_lo_metrics),
    )
    common = merged.loc[merged["af3_success"] & merged["covdock_lo_success"]].copy()
    _, common_metrics = simulate_fusions(
        common,
        fractions=args.fraction or (0.05, 0.10, 0.20),
        rrf_k=args.rrf_k,
    )
    policy = select_policy(
        metrics,
        bedroc_margin=args.bedroc_margin,
        top10_margin=args.top10_margin,
        top25_margin=args.top25_margin,
    )
    freeze = freeze_payload(
        policy,
        source_paths=source_paths,
        rrf_k=args.rrf_k,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fused.to_csv(args.output_dir / "btk_mini_af3_covdock_lo_fused.csv", index=False)
    fused.to_parquet(args.output_dir / "btk_mini_af3_covdock_lo_fused.parquet", index=False)
    metrics.to_csv(args.output_dir / "fusion_sensitivity_metrics.csv", index=False)
    common_metrics.to_csv(
        args.output_dir / "fusion_sensitivity_metrics_common_success.csv", index=False
    )
    (args.output_dir / "fusion_policy.json").write_text(
        json.dumps(policy, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "frozen_fusion_policy.json").write_text(
        json.dumps(freeze, indent=2, ensure_ascii=False, default=_json_default) + "\n",
        encoding="utf-8",
    )
    _write_report(
        args.output_dir / "BTK_MINI_AF3_COVDOCK_LO_FUSION_REPORT.md",
        metrics,
        common_metrics,
        policy,
        freeze,
    )

    print(
        json.dumps(
            {
                "rows": len(fused),
                "af3_success": int(fused["af3_success"].sum()),
                "covdock_lo_success": int(fused["covdock_lo_success"].sum()),
                "common_success": int((fused["af3_success"] & fused["covdock_lo_success"]).sum()),
                "selected_method": policy.get("selected_method"),
                "freeze_id": freeze["freeze_id"],
                "output_dir": str(args.output_dir),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
