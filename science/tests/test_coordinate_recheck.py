import json
from pathlib import Path

import pandas as pd

from covscreen.coordinate_recheck import recheck_coordinates


def test_raw_coordinate_recheck_preserves_identity_and_hashes(tmp_path, monkeypatch):
    source = tmp_path / "poses.maegz"
    source.write_bytes(b"mock raw structure; no real Schrodinger validation")
    calls = []

    def inspector(command, **kwargs):
        calls.append(command)
        prefix = Path(command[command.index("--output-prefix") + 1])
        prefix.with_suffix(".json").write_text(json.dumps({
            "input": str(source), "geometry_requested": True,
            "rows": [{"pose_index": 0, "title": "x__p0__i7",
                      "geometry_inspection_status": "SUCCESS",
                      "covalent_link_present": True, "severe_clash": False,
                      "covalent_bond_length_angstrom": 1.8}],
        }))

    monkeypatch.setattr("covscreen.coordinate_recheck.subprocess.run", inspector)
    candidates = pd.DataFrame([
        {"input_sdf_index": i, "compound_id": f"compound_{i}", "covdock_pose_file": str(source),
         "covdock_pose_index": 0, "covdock_title": "x__p0__i7"}
        for i in (7, 8)
    ])
    result = recheck_coordinates(candidates, schrodinger_root=tmp_path,
        inspector=tmp_path / "inspector.py", output_dir=tmp_path / "audit",
        attachment_chain="A", attachment_residue=797)
    assert len(calls) == 1
    assert result.input_sdf_index.tolist() == [7, 8]
    assert result.iloc[0].coordinate_recheck_status == "SUCCESS"
    assert len(result.iloc[0].coordinate_recheck_source_sha256) == 64
    assert result.iloc[1].coordinate_recheck_status == "FAILED"
    assert "pose_identity_mismatch" in result.iloc[1].coordinate_recheck_failure


def test_missing_raw_source_is_not_a_success(tmp_path):
    result = recheck_coordinates(pd.DataFrame([{"input_sdf_index": 1, "compound_id": "compound_1"}]),
        schrodinger_root=tmp_path, inspector=tmp_path / "inspector.py",
        output_dir=tmp_path / "audit", attachment_chain="A", attachment_residue=797)
    assert result.iloc[0].coordinate_recheck_status == "FAILED"
    assert "raw_pose_source_missing" in result.iloc[0].coordinate_recheck_failure
