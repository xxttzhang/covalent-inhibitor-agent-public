import importlib.util
from pathlib import Path

import pandas as pd
import pytest


SCRIPT = (Path(__file__).resolve().parents[3] / "science") / "scripts/audit_ligand_covdock_partial.py"


def _load():
    spec = importlib.util.spec_from_file_location("partial_covdock_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _table(tmp_path: Path) -> Path:
    path = tmp_path / "candidates.parquet"
    pd.DataFrame({
        "input_sdf_index": [58, 60, 66],
        "compound_id": ["a", "b", "c"],
        "standardized_smiles": ["CC", "CCC", "CCCC"],
    }).to_parquet(path, index=False)
    return path


def test_partial_audit_maps_batch_positions_without_promoting_scratch(tmp_path):
    module = _load()
    table = _table(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "job_score_Docking_000000_lib.maegz").write_bytes(b"score")
    (scratch / "job_sample_Docking_000001_raw.maegz").write_bytes(b"sampling")
    (scratch / "job_sample_Docking_000001_lib.maegz").write_bytes(b"sample-library")
    (scratch / "job_LigandStrain_000001-out.maegz").write_bytes(b"strain")
    (scratch / "job_LigandStrain_000001_00003_globalsample-out.lck").write_bytes(b"lock")
    (scratch / "job_LigandStrain_000001_00003_globalsample.err").write_bytes(b"failed")
    payload = module.audit(scratch, table)
    assert payload["expected_candidate_count"] == 3
    assert payload["observed_candidate_count"] == 2
    assert payload["score_library_candidate_count"] == 1
    assert [row["input_sdf_index"] for row in payload["candidates"]] == [58, 60, 66]
    assert [row["highest_observed_milestone"] for row in payload["candidates"]] == [
        "score_library", "ligand_strain_artifact_unverified", "not_observed"
    ]
    assert payload["schema_version"] == "1.1"
    assert payload["audit_kind"].endswith("-v2")
    assert "per-pose strain work may still be active" in payload["interpretation"]
    second = payload["candidates"][1]
    assert second["strain_pose_indices_observed"] == [3]
    assert second["highest_strain_pose_index_observed"] == 3
    assert second["strain_pose_observation_is_not_completion"] is True
    assert len(second["lock_files_observed"]) == 1
    assert len(second["nonempty_error_files"]) == 1
    assert payload["nonempty_error_file_count"] == 1
    assert not any(row["scientifically_validated"] for row in payload["candidates"])
    assert not any(row["reusable_checkpoint"] for row in payload["candidates"])


def test_completed_sampling_library_survives_raw_cleanup(tmp_path):
    module = _load()
    table = _table(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "job_sample_Docking_000001_lib.maegz").write_bytes(b"sample-library")
    payload = module.audit(scratch, table)
    assert payload["candidates"][1]["highest_observed_milestone"] == "sampling_library"
    assert payload["candidates"][1]["scientifically_validated"] is False


def test_partial_audit_rejects_position_outside_frozen_denominator(tmp_path):
    module = _load()
    table = _table(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "job_score_Docking_000003_lib.maegz").write_bytes(b"score")
    with pytest.raises(ValueError, match="outside denominator"):
        module.audit(scratch, table)


def test_partial_audit_rejects_out_of_denominator_strain_subtask(tmp_path):
    module = _load()
    table = _table(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / "job_LigandStrain_000003_00000_min.out").write_bytes(b"evidence")
    with pytest.raises(ValueError, match="outside denominator"):
        module.audit(scratch, table)
