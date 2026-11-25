from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluate_erbb2_c805_lockbox.py"


def load_module():
    spec = importlib.util.spec_from_file_location("erbb2_lockbox_evaluator", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_panel(tmp_path: Path) -> dict[str, Path]:
    target = "ERBB2"
    operational_root = tmp_path / "operational"
    results_root = tmp_path / "results"
    target_dir = results_root / target
    target_dir.mkdir(parents=True)
    (operational_root / target).mkdir(parents=True)
    identifiers = list(range(8))
    operational = pd.DataFrame({"compound_id": [f"ERBB2B{i:04d}" for i in identifiers], "input_sdf_index": identifiers})
    operational_path = operational_root / target / "erbb2_blind_mini.parquet"
    operational.to_parquet(operational_path, index=False)
    pd.DataFrame({"input_sdf_index": identifiers, "raw_score": [1, 2, 3, 4, 5, 6, 7, 8], "engine_status": ["SUCCESS"] * 8, "failure_reason": [None] * 8, "runtime_seconds": [1.0] * 8}).to_parquet(target_dir / "af3_scores.parquet", index=False)
    pd.DataFrame({"input_sdf_index": [0, 1], "raw_score": [10.0, 1.0], "engine_status": ["SUCCESS", "SUCCESS"], "failure_reason": [None, None], "runtime_seconds": [2.0, 2.0]}).to_parquet(target_dir / "covdock_lo_scores.parquet", index=False)
    pd.DataFrame({"input_sdf_index": [0, 1], "raw_score": [5.0, 1.0], "engine_status": ["SUCCESS", "SUCCESS"], "failure_reason": [None, None], "runtime_seconds": [0.5, 0.5]}).to_parquet(target_dir / "rtmscore.parquet", index=False)
    answer = pd.DataFrame({"panel_target_id": [target] * 8, "blind_id": operational["compound_id"], "input_sdf_index": identifiers, "label": ["active", "active", "decoy", "decoy", "decoy", "decoy", "decoy", "decoy"]})
    answer_path = tmp_path / "answer.parquet"
    answer.to_parquet(answer_path, index=False)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"plaintext_commitments": {"answer_key_parquet_sha256": sha256(answer_path)}}))
    paths = {name: target_dir / filename for name, filename in {"af3": "af3_scores.parquet", "covdock_lo": "covdock_lo_scores.parquet", "rtmscore": "rtmscore.parquet"}.items()}
    lock_path = tmp_path / "prediction_lock.json"
    lock_path.write_text(json.dumps({"status": "PREDICTIONS_FROZEN_BEFORE_UNBLINDING", "prediction_lock_id": "test-lock", "targets": [{"target_id": target, "scores": {name: {"path": str(path), "sha256": sha256(path)} for name, path in paths.items()}}]}))
    protocol_path = tmp_path / "protocol.json"
    protocol_path.write_text(json.dumps({"panel_id": "test-panel", "dataset": {"rows_per_target": 8, "active_count": 2, "decoy_count": 6}, "targets": [{"target_id": target}], "algorithm": {"af3_selected_n_for_420_inputs": 2, "af3_top_fraction": 0.25, "rrf_k": 60.0}, "statistics": {"paired_bootstrap_iterations": 20, "bootstrap_seed": 7}}))
    return {"operational_root": operational_root, "results_root": results_root, "answer": answer_path, "manifest": manifest_path, "lock": lock_path, "protocol": protocol_path}


def test_erbb2_evaluator_honours_frozen_top_subset_and_outputs_three_methods(tmp_path: Path):
    paths = write_panel(tmp_path)
    output = tmp_path / "out"
    completed = subprocess.run([
        sys.executable, str(SCRIPT), "--frozen-protocol", str(paths["protocol"]), "--prediction-lock", str(paths["lock"]),
        "--blinded-manifest", str(paths["manifest"]), "--operational-root", str(paths["operational_root"]),
        "--results-root", str(paths["results_root"]), "--answer-key", str(paths["answer"]), "--output-dir", str(output),
    ], cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}, text=True, capture_output=True)
    assert completed.returncode == 0, completed.stderr
    metrics = pd.read_csv(output / "erbb2_method_metrics.csv")
    assert metrics["method"].tolist() == ["AF3-mPAE", "AF3-mPAE_to_CovDock-LO", "AF3-mPAE_to_CovDock-LO_to_RTMScore"]
    assert metrics["runtime_status"].tolist() == ["OBSERVED", "OBSERVED", "OBSERVED"]
    assert metrics["failed_count"].tolist() == [0, 0, 0]
    ranked = pd.read_parquet(output / "erbb2_ranked_after_unblinding.parquet").sort_values("input_sdf_index")
    assert ranked.loc[ranked["input_sdf_index"].eq(1), "two_stage_rank"].item() == 1
    assert ranked.loc[ranked["input_sdf_index"].eq(0), "three_stage_rank"].item() == 1
    assert len(pd.read_csv(output / "erbb2_paired_bootstrap_comparisons.csv")) == 16
    assert (output / "paired_bootstrap_af3_vs_af3-mpae_to_covdock-lo.parquet").is_file()
    assert (output / "paired_bootstrap_af3_vs_af3-mpae_to_covdock-lo_to_rtmscore.parquet").is_file()


def test_erbb2_evaluator_rejects_rtmscore_outside_successful_covdock_subset(tmp_path: Path):
    paths = write_panel(tmp_path)
    module = load_module()
    operational = pd.read_parquet(paths["operational_root"] / "ERBB2" / "erbb2_blind_mini.parquet")
    af3 = module.score_frame(paths["results_root"] / "ERBB2" / "af3_scores.parquet", "AF3")
    covdock = module.score_frame(paths["results_root"] / "ERBB2" / "covdock_lo_scores.parquet", "CovDock")
    bad_rtmscore = pd.DataFrame({"input_sdf_index": [0], "raw_score": [1.0], "engine_status": ["SUCCESS"], "failure_reason": [None]})
    with pytest.raises(ValueError, match="RTMScore identities"):
        module.build_rankings(operational, af3, covdock, bad_rtmscore, top_n=2, rrf_k=60.0)


def test_erbb2_evaluator_rejects_protocol_top_fraction_drift(tmp_path: Path):
    paths = write_panel(tmp_path)
    protocol = json.loads(paths["protocol"].read_text())
    protocol["algorithm"]["af3_top_fraction"] = 0.5
    paths["protocol"].write_text(json.dumps(protocol))
    completed = subprocess.run([
        sys.executable, str(SCRIPT), "--frozen-protocol", str(paths["protocol"]), "--prediction-lock", str(paths["lock"]),
        "--blinded-manifest", str(paths["manifest"]), "--operational-root", str(paths["operational_root"]),
        "--results-root", str(paths["results_root"]), "--answer-key", str(paths["answer"]), "--output-dir", str(tmp_path / "out"),
    ], cwd=ROOT, env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")}, text=True, capture_output=True)
    assert completed.returncode != 0
    assert "Top-N is inconsistent" in completed.stderr
