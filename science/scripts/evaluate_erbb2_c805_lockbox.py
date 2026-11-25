"""Unblind and evaluate the frozen ERBB2 C805 lock-box protocol.

This is deliberately separate from the multi-target exploratory evaluator.  The
ERBB2 protocol has one target, an AF3-only denominator of 420, and a fixed AF3
Top-21 CovDock/RTMScore cascade.  It accepts an answer key only *after* the
three score files have been locked and re-hashed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from covscreen.fusion_benchmark import deterministic_order
from covscreen.metrics import summarize


METRICS = (
    "adjusted_logauc_percent",
    "bedroc_alpha20",
    "roc_auc",
    "ef1_percent",
    "ef5_percent",
    "top10_active",
    "top25_active",
    "top50_active",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_table(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(path)
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def score_frame(path: Path, name: str) -> pd.DataFrame:
    frame = read_table(path)
    required = {"input_sdf_index", "raw_score", "engine_status", "failure_reason"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{name} missing columns: {missing}")
    if frame["input_sdf_index"].duplicated().any():
        raise ValueError(f"{name} has duplicate input_sdf_index values")
    # Predictions must remain label-blind up through the lock.
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        values = set(frame[column].dropna().astype(str).str.lower().unique())
        if values & {"active", "decoy"}:
            raise ValueError(f"{name} exposes class labels in column {column}")
    out = frame.copy()
    out["input_sdf_index"] = pd.to_numeric(out["input_sdf_index"], errors="raise").astype(int)
    out["raw_score"] = pd.to_numeric(out["raw_score"], errors="coerce")
    out["engine_status"] = out["engine_status"].astype(str)
    if not set(out["engine_status"]).issubset({"SUCCESS", "FAILED"}):
        raise ValueError(f"{name} has an invalid engine_status")
    failed = out["engine_status"].eq("FAILED")
    successful = out["engine_status"].eq("SUCCESS")
    if out.loc[successful, "raw_score"].isna().any():
        raise ValueError(f"{name} has SUCCESS rows without a finite score")
    if out.loc[successful, "failure_reason"].notna().any():
        raise ValueError(f"{name} has SUCCESS rows with a failure reason")
    if out.loc[failed, "failure_reason"].isna().any() or out.loc[failed, "failure_reason"].astype(str).str.strip().eq("").any():
        raise ValueError(f"{name} has FAILED rows without a failure reason")
    if out.loc[failed, "raw_score"].notna().any():
        raise ValueError(f"{name} has FAILED rows with a score")
    return out


def verify_prediction_lock(lock: dict[str, Any], results_root: Path, target_id: str) -> dict[str, Path]:
    if lock.get("status") != "PREDICTIONS_FROZEN_BEFORE_UNBLINDING":
        raise ValueError("prediction lock is not frozen")
    target = next((row for row in lock.get("targets", []) if row.get("target_id") == target_id), None)
    if not target:
        raise ValueError(f"prediction lock has no {target_id} target")
    names = {"af3": "af3_scores.parquet", "covdock_lo": "covdock_lo_scores.parquet", "rtmscore": "rtmscore.parquet"}
    paths: dict[str, Path] = {}
    for engine, filename in names.items():
        record = target.get("scores", {}).get(engine)
        if not record:
            raise ValueError(f"prediction lock omits {target_id}/{engine}")
        path = results_root / target_id / filename
        if path.resolve() != Path(record.get("path", "")).resolve():
            raise ValueError(f"locked path mismatch for {target_id}/{engine}")
        if sha256_file(path) != record.get("sha256"):
            raise ValueError(f"prediction changed after lock: {target_id}/{engine}")
        paths[engine] = path
    return paths


def runtime_seconds(frame: pd.DataFrame) -> float | None:
    """Report an observed aggregate runtime, never replace absent timing by zero."""
    if "runtime_seconds" not in frame:
        return None
    values = pd.to_numeric(frame["runtime_seconds"], errors="coerce")
    if values.isna().any() or (values < 0).any():
        return None
    return float(values.sum())


def rank_with_tail(selected: pd.DataFrame, remainder: pd.DataFrame, selected_rank: pd.Series) -> pd.Series:
    order = list(selected_rank.sort_values(kind="stable").index) + list(
        remainder.sort_values("af3_rank", kind="stable").index
    )
    rank = pd.Series(index=pd.concat([selected, remainder]).index, dtype=int)
    rank.loc[order] = np.arange(1, len(order) + 1, dtype=int)
    return rank.astype(int)


def build_rankings(
    operational: pd.DataFrame,
    af3: pd.DataFrame,
    covdock: pd.DataFrame,
    rtmscore: pd.DataFrame,
    *,
    top_n: int,
    rrf_k: float,
) -> tuple[pd.DataFrame, dict[str, dict[str, float | int | None]]]:
    if operational["input_sdf_index"].duplicated().any():
        raise ValueError("operational panel has duplicate input identities")
    indexes = set(operational["input_sdf_index"])
    if set(af3["input_sdf_index"]) != indexes:
        raise ValueError("AF3 does not cover the complete frozen operational panel")
    identity_columns = ["compound_id", "input_sdf_index"]
    # ``label`` is absent before unblinding and present only in this evaluator's
    # in-memory joined frame.  Retain it when supplied; rankings themselves never
    # use it.
    if "label" in operational:
        identity_columns.append("label")
    out = operational[identity_columns].copy()
    out = out.merge(af3[["input_sdf_index", "raw_score"]].rename(columns={"raw_score": "af3_score"}), on="input_sdf_index", validate="one_to_one")
    out["af3_rank"] = deterministic_order(out, "af3_score", "lower_is_better")
    selected_index = set(out.nsmallest(top_n, "af3_rank")["input_sdf_index"])
    if len(selected_index) != top_n:
        raise ValueError("AF3 deterministic Top-N has wrong size")
    if set(covdock["input_sdf_index"]) != selected_index:
        raise ValueError("CovDock identities must equal the frozen AF3 Top-N")
    successful_covdock = set(covdock.loc[covdock["engine_status"].eq("SUCCESS"), "input_sdf_index"])
    if set(rtmscore["input_sdf_index"]) != successful_covdock:
        raise ValueError("RTMScore identities must equal CovDock finite-score (pose-available) identities")
    out = out.merge(covdock[["input_sdf_index", "raw_score"]].rename(columns={"raw_score": "covdock_score"}), on="input_sdf_index", how="left", validate="one_to_one")
    out = out.merge(rtmscore[["input_sdf_index", "raw_score"]].rename(columns={"raw_score": "rtmscore"}), on="input_sdf_index", how="left", validate="one_to_one")
    selected = out.loc[out["input_sdf_index"].isin(selected_index)].copy()
    remainder = out.loc[~out["input_sdf_index"].isin(selected_index)].copy()
    selected["covdock_subset_rank"] = deterministic_order(selected, "covdock_score", "lower_is_better")
    out["two_stage_rank"] = rank_with_tail(selected, remainder, selected["covdock_subset_rank"])
    selected["af3_subset_rank"] = deterministic_order(selected, "af3_score", "lower_is_better")
    selected["rtmscore_subset_rank"] = deterministic_order(selected, "rtmscore", "higher_is_better")
    selected["three_stage_rrf_score"] = (
        1.0 / (rrf_k + selected["af3_subset_rank"])
        + 1.0 / (rrf_k + selected["covdock_subset_rank"])
        + 1.0 / (rrf_k + selected["rtmscore_subset_rank"])
    )
    selected["three_stage_subset_rank"] = deterministic_order(
        selected, "three_stage_rrf_score", "higher_is_better", secondary_column="af3_subset_rank", secondary_direction="lower_is_better"
    )
    out["three_stage_rank"] = rank_with_tail(selected, remainder, selected["three_stage_subset_rank"])
    out["selected_for_cascade"] = out["input_sdf_index"].isin(selected_index)
    for column in ("covdock_subset_rank", "af3_subset_rank", "rtmscore_subset_rank", "three_stage_rrf_score", "three_stage_subset_rank"):
        out[column] = selected[column]
    evidence: dict[str, dict[str, float | int | None]] = {
        "af3_mpae": {"evaluated": len(af3), "succeeded": int(af3["engine_status"].eq("SUCCESS").sum()), "runtime_seconds": runtime_seconds(af3), "failure_reasons": af3.loc[af3["engine_status"].eq("FAILED"), "failure_reason"].value_counts().to_dict()},
        "af3_mpae_to_covdock_lo": {"evaluated": len(covdock), "succeeded": int(covdock["engine_status"].eq("SUCCESS").sum()), "runtime_seconds": runtime_seconds(covdock), "failure_reasons": covdock.loc[covdock["engine_status"].eq("FAILED"), "failure_reason"].value_counts().to_dict()},
        "af3_mpae_to_covdock_lo_to_rtmscore": {"evaluated": len(rtmscore), "succeeded": int(rtmscore["engine_status"].eq("SUCCESS").sum()), "runtime_seconds": runtime_seconds(rtmscore), "failure_reasons": rtmscore.loc[rtmscore["engine_status"].eq("FAILED"), "failure_reason"].value_counts().to_dict()},
    }
    return out, evidence


def method_metrics(frame: pd.DataFrame, method: str, rank_column: str, evidence: dict[str, float | int | None]) -> dict[str, Any]:
    labels = frame["label"].map({"active": 1, "decoy": 0}).astype(int).to_numpy()
    result: dict[str, Any] = {"method": method, **summarize(labels, frame[rank_column], "lower_is_better")}
    result["evaluated_count"] = int(evidence["evaluated"])
    result["successful_count"] = int(evidence["succeeded"])
    result["success_rate"] = float(evidence["succeeded"]) / float(evidence["evaluated"]) if evidence["evaluated"] else float("nan")
    result["runtime_seconds"] = evidence["runtime_seconds"]
    result["runtime_status"] = "OBSERVED" if evidence["runtime_seconds"] is not None else "MISSING_NOT_ZERO"
    result["failed_count"] = int(evidence["evaluated"]) - int(evidence["succeeded"])
    result["failure_reason_counts"] = json.dumps(evidence["failure_reasons"], sort_keys=True)
    return result


def bootstrap_deltas(frame: pd.DataFrame, method_rank: str, iterations: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    active = frame.loc[frame["label"].eq("active")]
    decoy = frame.loc[frame["label"].eq("decoy")]
    rows: list[dict[str, float | int]] = []
    for iteration in range(iterations):
        sample = pd.concat([
            active.iloc[rng.integers(0, len(active), len(active))],
            decoy.iloc[rng.integers(0, len(decoy), len(decoy))],
        ], ignore_index=True)
        labels = sample["label"].map({"active": 1, "decoy": 0}).astype(int).to_numpy()
        baseline = summarize(labels, sample["af3_rank"], "lower_is_better")
        candidate = summarize(labels, sample[method_rank], "lower_is_better")
        rows.append({"bootstrap_iteration": iteration, **{f"delta_{metric}": float(candidate[metric]) - float(baseline[metric]) for metric in METRICS}})
    return pd.DataFrame(rows)


def comparison_summary(point: dict[str, Any], bootstrap: pd.DataFrame, candidate_method: str) -> list[dict[str, Any]]:
    rows = []
    for metric in METRICS:
        values = bootstrap[f"delta_{metric}"].to_numpy(dtype=float)
        rows.append({
            "reference_method": "AF3-mPAE",
            "candidate_method": candidate_method,
            "metric": metric,
            "point_delta": float(point[metric]),
            "bootstrap_ci95_low": float(np.quantile(values, 0.025)),
            "bootstrap_ci95_high": float(np.quantile(values, 0.975)),
            "bootstrap_fraction_positive": float(np.mean(values > 0)),
            "bootstrap_iterations": int(len(values)),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen-protocol", type=Path, required=True)
    parser.add_argument("--prediction-lock", type=Path, required=True)
    parser.add_argument("--blinded-manifest", type=Path, required=True)
    parser.add_argument("--operational-root", type=Path, required=True)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--answer-key", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    protocol = json.loads(args.frozen_protocol.read_text())
    target = protocol["targets"][0]
    target_id = str(target["target_id"])
    lock = json.loads(args.prediction_lock.read_text())
    paths = verify_prediction_lock(lock, args.results_root, target_id)
    manifest = json.loads(args.blinded_manifest.read_text())
    expected_answer_hash = manifest["plaintext_commitments"]["answer_key_parquet_sha256"]
    if sha256_file(args.answer_key) != expected_answer_hash:
        raise ValueError("revealed answer key does not match precommitted plaintext hash")
    answer = read_table(args.answer_key)
    required_answer = {"panel_target_id", "blind_id", "input_sdf_index", "label"}
    if missing := sorted(required_answer - set(answer.columns)):
        raise ValueError(f"answer key missing columns: {missing}")
    operational = read_table(args.operational_root / target_id / f"{target_id.lower()}_blind_mini.parquet")
    if "label" in operational.columns:
        raise ValueError("operational panel must remain label-blind before answer-key join")
    if operational["input_sdf_index"].duplicated().any():
        raise ValueError("operational panel has duplicate input_sdf_index values")
    labels = answer.loc[answer["panel_target_id"].eq(target_id), ["blind_id", "input_sdf_index", "label"]]
    if labels.duplicated(["blind_id", "input_sdf_index"]).any():
        raise ValueError("answer key has duplicate target identities")
    required_rows = int(protocol["dataset"]["rows_per_target"])
    required_active = int(protocol["dataset"]["active_count"])
    required_decoy = int(protocol["dataset"]["decoy_count"])
    if required_active + required_decoy != required_rows:
        raise ValueError("frozen protocol has inconsistent panel counts")
    ranked_input = operational.merge(labels, left_on=["compound_id", "input_sdf_index"], right_on=["blind_id", "input_sdf_index"], how="left", validate="one_to_one")
    label_counts = ranked_input["label"].value_counts(dropna=False).to_dict()
    if (len(ranked_input) != required_rows or set(ranked_input["label"].dropna()) != {"active", "decoy"}
            or int(label_counts.get("active", 0)) != required_active
            or int(label_counts.get("decoy", 0)) != required_decoy):
        raise ValueError("answer key does not completely and validly map to the operational panel")
    top_n = int(protocol["algorithm"]["af3_selected_n_for_420_inputs"])
    top_fraction = float(protocol["algorithm"]["af3_top_fraction"])
    if top_n != math.ceil(required_rows * top_fraction):
        raise ValueError("frozen AF3 Top-N is inconsistent with the frozen top fraction")
    ranked, evidence = build_rankings(ranked_input, score_frame(paths["af3"], "AF3"), score_frame(paths["covdock_lo"], "CovDock-LO"), score_frame(paths["rtmscore"], "RTMScore"), top_n=top_n, rrf_k=float(protocol["algorithm"]["rrf_k"]))
    methods = (("AF3-mPAE", "af3_rank", "af3_mpae"), ("AF3-mPAE_to_CovDock-LO", "two_stage_rank", "af3_mpae_to_covdock_lo"), ("AF3-mPAE_to_CovDock-LO_to_RTMScore", "three_stage_rank", "af3_mpae_to_covdock_lo_to_rtmscore"))
    metric_rows = [method_metrics(ranked, method, rank, evidence[key]) for method, rank, key in methods]
    base = metric_rows[0]
    iterations = int(protocol["statistics"]["paired_bootstrap_iterations"])
    seed = int(protocol["statistics"]["bootstrap_seed"])
    comparisons: list[dict[str, Any]] = []
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for offset, (method, rank, _) in enumerate(methods[1:], start=1):
        boot = bootstrap_deltas(ranked, rank, iterations, seed + offset)
        boot.to_parquet(args.output_dir / f"paired_bootstrap_af3_vs_{method.lower()}.parquet", index=False)
        point = next(row for row in metric_rows if row["method"] == method)
        comparisons.extend(comparison_summary({metric: float(point[metric]) - float(base[metric]) for metric in METRICS}, boot, method))
    ranked.to_parquet(args.output_dir / "erbb2_ranked_after_unblinding.parquet", index=False)
    pd.DataFrame(metric_rows).to_csv(args.output_dir / "erbb2_method_metrics.csv", index=False)
    pd.DataFrame(comparisons).to_csv(args.output_dir / "erbb2_paired_bootstrap_comparisons.csv", index=False)
    summary = {
        "status": "PASS_EVALUATED_AFTER_PREDICTION_LOCK",
        "panel_id": protocol["panel_id"],
        "prediction_lock_id": lock["prediction_lock_id"],
        "methods": [row["method"] for row in metric_rows],
        "cost": {"value": None, "currency": None, "status": "MISSING_NOT_ZERO", "reason": "No approved rate card was supplied to the frozen analysis."},
        "repeatability": {"status": "NOT_RUN", "reason": "The frozen ERBB2 v1 protocol contains one independent panel, not replicate computations."},
        "interpretation": "Single-target lock-box evidence is directional only; bootstrap intervals quantify within-panel uncertainty and do not establish cross-target superiority.",
    }
    (args.output_dir / "erbb2_lockbox_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
