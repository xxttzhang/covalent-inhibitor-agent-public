import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from covscreen.refinement_input import verify_refinement_input, verify_refinement_identities


def rows():
    return [{"input_sdf_index": 5, "compound_id": "候选-05", "standardized_smiles": "C=CC(=O)N"},
            {"input_sdf_index": 9, "compound_id": "候选-09", "standardized_smiles": "C=CC(=O)NC"}]


def identity(values):
    content = sorted([[row["input_sdf_index"], row["compound_id"]] for row in values])
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def save(path, values):
    frame = pd.DataFrame(values)
    if path.suffix == ".parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("suffix", [".csv", ".parquet"])
def test_exact_subset_file_and_identities(tmp_path, suffix):
    table = tmp_path / ("candidates" + suffix)
    checksum = save(table, rows())
    expected = identity(rows())
    result = verify_refinement_input(table, 2, checksum, expected)
    assert result == {"input_count": 2, "input_sha256": checksum, "identity_sha256": expected,
                      "verified": True, "grants_resource_approval": False}
    with pytest.raises(ValueError, match="top-n is not a compute cap"):
        verify_refinement_input(table, 1, checksum, expected)
    with pytest.raises(ValueError, match="file differs"):
        verify_refinement_input(table, 2, "0" * 64, expected)
    with pytest.raises(ValueError, match="identities differ"):
        verify_refinement_input(table, 2, checksum, "0" * 64)
    changed = [{**row, "compound_id": row["compound_id"] + "-changed"} for row in rows()]
    checksum = save(table, changed)
    with pytest.raises(ValueError, match="identities differ"):
        verify_refinement_input(table, 2, checksum, expected)


def test_contract_is_all_or_none_and_never_approval(tmp_path):
    table = tmp_path / "missing.parquet"
    assert verify_refinement_input(table, None, None, None) is None
    for contract in [(2, None, None), (None, "a" * 64, "b" * 64), (2, "a" * 64, None),
                     (True, "a" * 64, "b" * 64), (101, "a" * 64, "b" * 64), (0, "a" * 64, "b" * 64),
                     (2, "bad", "b" * 64)]:
        with pytest.raises(ValueError):
            verify_refinement_input(table, *contract)


def test_prepared_batch_cannot_change_expand_or_duplicate_identities():
    expected = identity(rows())
    assert verify_refinement_identities(list(reversed(rows())), 2, expected) == expected
    for changed in [rows()[:1], rows() + rows(), [rows()[0], rows()[0]],
                    [{**row, "input_sdf_index": row["input_sdf_index"] + 1} for row in rows()],
                    [{**row, "input_sdf_index": float(row["input_sdf_index"])} for row in rows()]]:
        with pytest.raises(ValueError):
            verify_refinement_identities(changed, 2, expected)


@pytest.mark.parametrize("engine", ["af3", "covdock"])
def test_real_entrypoint_rejects_expanded_table_before_creating_outputs(tmp_path, engine):
    root = Path(__file__).resolve().parents[1]
    table = tmp_path / "candidates.parquet"
    checksum = save(table, rows())
    output = tmp_path / "must-not-exist"
    common = ["--candidate-table", str(table), "--output-dir", str(output),
              "--expected-input-count", "1", "--expected-input-sha256", checksum,
              "--expected-identity-sha256", identity(rows())]
    if engine == "af3":
        script = "run_af3_registered_batch.py"
        specific = ["--source-data-json", "unused", "--target-config", "unused", "--runner", "unused",
                    "--af3-python", "unused", "--model-dir", "unused", "--database-dir", "unused",
                    "--cysteine-sequence-index", "105", "--target-id", "EGFR", "--source-residue-number", "797",
                    "--top-n", "1"]
    else:
        script = "run_covdock_lo_registered.py"
        specific = ["--receptor", "unused", "--attachment-residue", "A:773",
                    "--schrodinger-root", "unused", "--license-file", "unused", "--final-n", "1"]
    result = subprocess.run([sys.executable, str(root / "scripts" / script), *common, *specific],
                            env={**os.environ, "PYTHONPATH": str(root / "src")}, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "top-n is not a compute cap" in result.stderr
    assert not output.exists()


def test_covdock_contract_requires_explicit_score_source_before_outputs(tmp_path):
    root = Path(__file__).resolve().parents[1]
    table = tmp_path / "candidates.parquet"
    checksum = save(table, rows())
    output = tmp_path / "must-not-exist"
    result = subprocess.run([sys.executable, str(root / "scripts/run_covdock_lo_registered.py"),
        "--candidate-table", str(table), "--expected-input-count", "2", "--expected-input-sha256", checksum,
        "--expected-identity-sha256", identity(rows()), "--output-dir", str(output),
        "--receptor", "unused", "--attachment-residue", "A:773", "--schrodinger-root", "unused", "--license-file", "unused"],
        env={**os.environ, "PYTHONPATH": str(root / "src")}, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "Exact-input CovDock requires --input-score-source" in result.stderr
    assert not output.exists()
