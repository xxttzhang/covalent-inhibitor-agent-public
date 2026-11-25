"""Unblind and evaluate a frozen AF3 -> CovDock-LO -> RTMScore panel."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from covscreen.fusion_benchmark import deterministic_order
from covscreen.metrics import summarize


METRICS = (
    "roc_auc",
    "adjusted_logauc_percent",
    "ef1_percent",
    "ef5_percent",
    "bedroc_alpha20",
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
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def verify_prediction_lock(lock: dict, results_root: Path) -> None:
    if lock.get("status") != "PREDICTIONS_FROZEN_BEFORE_UNBLINDING":
        raise ValueError("Prediction lock status is not frozen")
    for target in lock["targets"]:
        target_id = target["target_id"]
        for engine, record in target["scores"].items():
            expected = results_root / target_id / {
                "af3": "af3_scores.parquet",
                "covdock_lo": "covdock_lo_scores.parquet",
                "rtmscore": "rtmscore.parquet",
            }[engine]
            if expected.resolve() != Path(record["path"]).resolve():
                raise ValueError(f"Locked path mismatch for {target_id}/{engine}")
            if sha256_file(expected) != record["sha256"]:
                raise ValueError(f"Prediction changed after lock: {target_id}/{engine}")


def score_column(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    required = {"input_sdf_index", "raw_score"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{name} missing columns: {missing}")
    if frame["input_sdf_index"].duplicated().any():
        raise ValueError(f"{name} has duplicate input identities")
    result = frame[["input_sdf_index", "raw_score"]].copy()
    result["raw_score"] = pd.to_numeric(result["raw_score"], errors="coerce")
    return result.rename(columns={"raw_score": name})


def frozen_ranking(frame: pd.DataFrame, top_fraction: float, rrf_k: float) -> pd.DataFrame:
    out = frame.copy()
    out["af3_rank"] = deterministic_order(out, "af3_score", "lower_is_better")
    top_n = max(1, math.ceil(len(out) * top_fraction))
    selected_mask = out["af3_rank"].le(top_n)
    selected = out.loc[selected_mask].copy()
    if len(selected) != top_n:
        raise ValueError("AF3-selected subset size is inconsistent")
    selected["af3_subset_rank"] = deterministic_order(selected, "af3_score", "lower_is_better")
    selected["covdock_subset_rank"] = deterministic_order(selected, "covdock_score", "lower_is_better")
    selected["rtmscore_subset_rank"] = deterministic_order(selected, "rtmscore", "higher_is_better")
    selected["three_stage_rrf_score"] = (
        1.0 / (rrf_k + selected["af3_subset_rank"])
        + 1.0 / (rrf_k + selected["covdock_subset_rank"])
        + 1.0 / (rrf_k + selected["rtmscore_subset_rank"])
    )
    selected_order = selected.sort_values(
        ["three_stage_rrf_score", "af3_subset_rank", "input_sdf_index"],
        ascending=[False, True, True],
        kind="stable",
    )
    remainder = out.loc[~selected_mask].sort_values("af3_rank", kind="stable")
    final_order = list(selected_order.index) + list(remainder.index)
    out["three_stage_rank"] = 0
    out.loc[final_order, "three_stage_rank"] = np.arange(1, len(out) + 1)
    out["selected_for_cascade"] = selected_mask
    for column in (
        "af3_subset_rank",
        "covdock_subset_rank",
        "rtmscore_subset_rank",
        "three_stage_rrf_score",
    ):
        out[column] = selected[column]
    return out


def metric_row(frame: pd.DataFrame, method: str, rank: str) -> dict[str, object]:
    labels = frame["label"].map({"active": 1, "decoy": 0}).astype(int).to_numpy()
    return {"method": method, **summarize(labels, frame[rank], "lower_is_better")}


def bootstrap_target(frame: pd.DataFrame, iterations: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    active = frame.loc[frame["label"].eq("active")]
    decoy = frame.loc[frame["label"].eq("decoy")]
    records = []
    for iteration in range(iterations):
        sample = pd.concat(
            [
                active.iloc[rng.integers(0, len(active), len(active))],
                decoy.iloc[rng.integers(0, len(decoy), len(decoy))],
            ],
            ignore_index=True,
        )
        labels = sample["label"].map({"active": 1, "decoy": 0}).to_numpy()
        baseline = summarize(labels, sample["af3_rank"], "lower_is_better")
        cascade = summarize(labels, sample["three_stage_rank"], "lower_is_better")
        row = {"bootstrap_iteration": iteration}
        for metric in METRICS:
            row[f"delta_{metric}"] = float(cascade[metric]) - float(baseline[metric])
        records.append(row)
    return pd.DataFrame(records)


def exact_one_sided_sign_p(values: np.ndarray) -> float:
    nonzero = values[values != 0]
    positives = int(np.count_nonzero(nonzero > 0))
    n = len(nonzero)
    if n == 0:
        return 1.0
    return float(sum(math.comb(n, k) for k in range(positives, n + 1)) / (2**n))


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
    lock = json.loads(args.prediction_lock.read_text())
    blind_manifest = json.loads(args.blinded_manifest.read_text())
    verify_prediction_lock(lock, args.results_root)
    expected_answer_hash = blind_manifest["plaintext_commitments"]["answer_key_parquet_sha256"]
    if sha256_file(args.answer_key) != expected_answer_hash:
        raise ValueError("Revealed answer key does not match the precommitted plaintext hash")
    labels = read_table(args.answer_key)
    top_fraction = float(protocol["algorithm"]["af3_top_fraction"])
    rrf_k = float(protocol["algorithm"]["rrf_k"])
    bootstrap_iterations = int(protocol["statistics"]["within_target_bootstrap_iterations"])
    bootstrap_seed = int(protocol["statistics"]["bootstrap_seed"])

    metric_rows = []
    delta_rows = []
    ranked_frames = []
    for offset, target in enumerate(protocol["targets"]):
        target_id = target["target_id"]
        operational = read_table(
            args.operational_root / target_id / f"{target_id.lower()}_blind_mini.parquet"
        )
        target_labels = labels.loc[labels["panel_target_id"].eq(target_id), ["blind_id", "input_sdf_index", "label"]]
        frame = operational.merge(
            target_labels,
            left_on=["compound_id", "input_sdf_index"],
            right_on=["blind_id", "input_sdf_index"],
            how="left",
            validate="one_to_one",
            suffixes=("_blinded", ""),
        )
        if set(frame["label"].dropna()) != {"active", "decoy"}:
            raise ValueError(f"{target_id}: incomplete answer-key merge")
        root = args.results_root / target_id
        frame = frame.merge(score_column(read_table(root / "af3_scores.parquet"), "af3_score"), on="input_sdf_index", how="left", validate="one_to_one")
        frame = frame.merge(score_column(read_table(root / "covdock_lo_scores.parquet"), "covdock_score"), on="input_sdf_index", how="left", validate="one_to_one")
        frame = frame.merge(score_column(read_table(root / "rtmscore.parquet"), "rtmscore"), on="input_sdf_index", how="left", validate="one_to_one")
        ranked = frozen_ranking(frame, top_fraction, rrf_k)
        ranked["panel_target_id"] = target_id
        ranked_frames.append(ranked)
        baseline = metric_row(ranked, "af3_mpae", "af3_rank")
        cascade = metric_row(ranked, "frozen_three_stage_rrf", "three_stage_rank")
        for row in (baseline, cascade):
            row["target_id"] = target_id
            metric_rows.append(row)
        delta = {"target_id": target_id}
        for metric in METRICS:
            delta[metric] = float(cascade[metric]) - float(baseline[metric])
        delta_rows.append(delta)
        boot = bootstrap_target(ranked, bootstrap_iterations, bootstrap_seed + offset)
        boot.insert(0, "target_id", target_id)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        boot.to_parquet(args.output_dir / f"{target_id.lower()}_paired_bootstrap.parquet", index=False)

    metrics = pd.DataFrame(metric_rows)
    deltas = pd.DataFrame(delta_rows)
    primary = protocol["statistics"]["primary_metrics"]
    cross_target = {}
    for metric in METRICS:
        values = deltas[metric].to_numpy(dtype=float)
        cross_target[metric] = {
            "mean_delta": float(np.mean(values)),
            "median_delta": float(np.median(values)),
            "positive_targets": int(np.count_nonzero(values > 0)),
            "noninferior_targets": int(np.count_nonzero(values >= -float(protocol["statistics"]["noninferiority_margins"].get(metric, 0.0)))),
            "one_sided_exact_sign_p": exact_one_sided_sign_p(values),
        }
    repeatable = all(
        cross_target[metric]["positive_targets"] == len(protocol["targets"])
        and cross_target[metric]["one_sided_exact_sign_p"] <= 0.05
        for metric in primary
    )
    summary = {
        "status": "PASS_EVALUATED",
        "freeze_id": protocol["freeze_id"],
        "prediction_lock_id": lock["prediction_lock_id"],
        "targets": len(protocol["targets"]),
        "cross_target": cross_target,
        "predeclared_repeatable_gain_gate_pass": bool(repeatable),
        "interpretation": (
            "Repeatable gain is supported only when every target is positive and the one-sided exact sign test is <=0.05 for both primary metrics."
        ),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    pd.concat(ranked_frames, ignore_index=True).to_parquet(args.output_dir / "all_targets_ranked.parquet", index=False)
    metrics.to_csv(args.output_dir / "per_target_metrics.csv", index=False)
    deltas.to_csv(args.output_dir / "per_target_deltas.csv", index=False)
    (args.output_dir / "blind_panel_summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
