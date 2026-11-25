import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest


ROOT = Path(__file__).parents[1]


def load_script(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_af3_batch_keeps_missing_outputs_as_failures(tmp_path):
    module = load_script("parse_af3_batch.py")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "job.manifest.json").write_text(
        json.dumps(
            {
                "job_name": "job",
                "compound_id": "B1852",
                "protomer_id": "1",
                "input_sdf_index": 27,
                "standardized_smiles": "C=CC(=O)N",
                "label": "active",
            }
        )
    )
    result = module.parse_batch(
        manifests, tmp_path / "results", tmp_path / "target.yaml"
    )
    assert len(result) == 1
    assert result.loc[0, "engine_status"] == "FAILED"
    assert result.loc[0, "failure_reason"] == "missing_af3_output"


def test_af3_batch_serializes_nested_manifest_metadata(tmp_path):
    module = load_script("parse_af3_batch.py")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "job.manifest.json").write_text(
        json.dumps(
            {
                "job_name": "job",
                "compound_id": "B1852",
                "protomer_id": "1",
                "input_sdf_index": 27,
                "standardized_smiles": "C=CC(=O)N",
                "bonded_atom_pairs": [["A", 88, "SG"], ["L", 1, "C1"]],
                "label": "active",
            }
        )
    )
    result = module.parse_batch(
        manifests, tmp_path / "results", tmp_path / "target.yaml"
    )
    assert result.loc[0, "engine_status"] == "FAILED"
    assert json.loads(result.loc[0, "bonded_atom_pairs"])[0][1] == 88


def test_af3_batch_preserves_preparation_failure_reason(tmp_path):
    module = load_script("parse_af3_batch.py")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    (manifests / "job.manifest.json").write_text(
        json.dumps(
            {
                "job_name": "job",
                "compound_id": "B1852",
                "protomer_id": "1",
                "input_sdf_index": "270",
                "standardized_smiles": "C=CC(=O)N",
                "status": "PREPARATION_FAILED",
                "failure_reason": "ETKDGv3 embedding failed",
            }
        )
    )
    result = module.parse_batch(
        manifests, tmp_path / "results", tmp_path / "target.yaml"
    )
    assert result.loc[0, "input_sdf_index"] == 270
    assert result.loc[0, "preparation_status"] == "PREPARATION_FAILED"
    assert result.loc[0, "preparation_failure_reason"] == "ETKDGv3 embedding failed"
    assert result.loc[0, "failure_reason"] == "preparation_failed:ETKDGv3 embedding failed"


def test_covdock_parser_maps_identity_and_selects_best_pose(tmp_path):
    module = load_script("parse_covdock_scores.py")
    inspection = tmp_path / "inspection.json"
    inspection.write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "pose_index": 0,
                        "title": "B1852__p1__i27",
                        "r_i_glide_gscore": -7.0,
                    },
                    {
                        "pose_index": 1,
                        "title": "B1852__p1__i27_pose2",
                        "r_i_glide_gscore": -8.0,
                    },
                ]
            }
        )
    )
    table = tmp_path / "identity.csv"
    pd.DataFrame(
        {
            "compound_id": ["B1852"],
            "protomer_id": ["1"],
            "input_sdf_index": [27],
            "label": ["active"],
        }
    ).to_csv(table, index=False)
    result = module.parse_inspection(
        inspection,
        table,
        "r_i_glide_gscore",
        "lower_is_better",
    )
    assert result.loc[0, "raw_score"] == -8.0
    assert result.loc[0, "engine_status"] == "SUCCESS"


def test_covdock_parser_refuses_order_only_identity(tmp_path):
    module = load_script("parse_covdock_scores.py")
    inspection = tmp_path / "inspection.json"
    inspection.write_text(
        json.dumps({"rows": [{"pose_index": 0, "title": "pose", "score": -1}]})
    )
    table = tmp_path / "identity.csv"
    pd.DataFrame(
        {"compound_id": ["x"], "input_sdf_index": [1], "label": ["decoy"]}
    ).to_csv(table, index=False)
    with pytest.raises(ValueError, match="cannot be resolved safely"):
        module.parse_inspection(
            inspection, table, "score", "lower_is_better"
        )


def test_covdock_parser_preserves_selected_pose_file_across_chunks(tmp_path):
    module = load_script("parse_covdock_scores.py")
    paths = []
    for name, score in [("first", -3.0), ("second", -8.0)]:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({
            "input": str(tmp_path / f"{name}.maegz"),
            "rows": [{"pose_index": 0, "title": "x__p0__i1", "score": score}],
        }))
        paths.append(path)
    table = tmp_path / "identities.csv"
    pd.DataFrame({"compound_id": ["x"], "input_sdf_index": [1]}).to_csv(table, index=False)
    result = module.parse_inspection(paths, table, "score", "lower_is_better")
    assert result.loc[0, "covdock_pose_index"] == 0
    assert result.loc[0, "covdock_pose_file"] == str(tmp_path / "second.maegz")
    assert result.loc[0, "covdock_inspection_file"] == str(paths[1].resolve())
    import hashlib
    assert result.loc[0, "covdock_inspection_sha256"] == hashlib.sha256(paths[1].read_bytes()).hexdigest()
