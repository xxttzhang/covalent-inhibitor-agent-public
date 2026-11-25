from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lock_blind_predictions.py"


def module():
    spec = importlib.util.spec_from_file_location("lock_blind_predictions", SCRIPT)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def write_scores(path: Path, frame: pd.DataFrame) -> Path:
    frame.to_parquet(path, index=False)
    return path


def test_prediction_lock_records_failed_rows_and_reason_counts(tmp_path: Path):
    path = write_scores(tmp_path / "scores.parquet", pd.DataFrame({
        "input_sdf_index": [0, 1], "raw_score": [1.2, None],
        "engine_status": ["SUCCESS", "FAILED"], "failure_reason": [None, "engine_timeout"],
    }))
    result = module().inspect_score_file(path, expected_rows=2)
    assert result["successful_rows"] == 1
    assert result["failed_rows"] == 1
    assert result["failure_reason_counts"] == {"engine_timeout": 1}


@pytest.mark.parametrize("frame, message", [
    (pd.DataFrame({"input_sdf_index": [0], "raw_score": [None], "engine_status": ["SUCCESS"], "failure_reason": [None]}), "SUCCESS rows"),
    (pd.DataFrame({"input_sdf_index": [0], "raw_score": [1.0], "engine_status": ["FAILED"], "failure_reason": ["bad"]}), "FAILED rows with raw_score"),
    (pd.DataFrame({"input_sdf_index": [0], "raw_score": [None], "engine_status": ["FAILED"], "failure_reason": [None]}), "FAILED rows without failure_reason"),
])
def test_prediction_lock_rejects_incoherent_failure_rows(tmp_path: Path, frame: pd.DataFrame, message: str):
    path = write_scores(tmp_path / "scores.parquet", frame)
    with pytest.raises(ValueError, match=message):
        module().inspect_score_file(path)
