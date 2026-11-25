import importlib.util
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "validate_btk_workflow.py"
spec = importlib.util.spec_from_file_location("validate_btk_workflow", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_audit_table_rejects_unknown_identity(tmp_path):
    library = pd.DataFrame(
        {"compound_id": ["a"], "input_sdf_index": [1], "label": ["active"]}
    )
    result = tmp_path / "engine.csv"
    pd.DataFrame(
        {
            "compound_id": ["x"],
            "input_sdf_index": [99],
            "raw_score": [1.0],
            "engine_status": ["SUCCESS"],
            "score_direction": ["lower_is_better"],
        }
    ).to_csv(result, index=False)
    report = module.audit_table(result, library)
    assert report["status"] == "INVALID"
    assert report["unknown_input_sdf_index"] == [99]


def test_audit_table_accepts_partial_matched_set(tmp_path):
    library = pd.DataFrame(
        {
            "compound_id": ["a", "b"],
            "input_sdf_index": [1, 2],
            "label": ["active", "decoy"],
        }
    )
    result = tmp_path / "engine.csv"
    pd.DataFrame(
        {
            "compound_id": ["a"],
            "input_sdf_index": [1],
            "raw_score": [1.0],
            "engine_status": ["SUCCESS"],
            "score_direction": ["lower_is_better"],
        }
    ).to_csv(result, index=False)
    assert module.audit_table(result, library)["status"] == "PASS"
