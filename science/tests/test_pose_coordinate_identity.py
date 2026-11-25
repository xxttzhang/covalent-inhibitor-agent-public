import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "compute_rmsd", Path(__file__).resolve().parents[1] / "scripts/compute_af3_covdock_rmsd.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_file_local_pose_indices_do_not_collide(tmp_path):
    files = []
    for name in ("Michael", "Substitution"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps({"input": str(tmp_path / f"{name}.maegz"),
                                    "rows": [{"pose_index": 0, "title": name}]}))
        files.append(path)
    coordinates = module.load_coordinate_sources(files)
    assert len(coordinates) == 2
    row = SimpleNamespace(covdock_pose_index=0, covdock_title="Substitution",
                          covdock_pose_file=str(tmp_path / "Substitution.maegz"))
    assert module.select_coordinates(row, coordinates)["title"] == "Substitution"
    del row.covdock_pose_file
    with pytest.raises(ValueError, match="pose_source_required"):
        module.select_coordinates(row, coordinates)
    with pytest.raises(ValueError, match="duplicate_coordinate_pose"):
        module.load_coordinate_sources(files + files[:1])


def test_title_and_integer_index_are_checked(tmp_path):
    source = str(tmp_path / "a.maegz")
    coordinates = {(source, 0): {"title": "expected"}}
    row = SimpleNamespace(covdock_pose_index=0, covdock_pose_file=source,
                          covdock_title="wrong")
    with pytest.raises(ValueError, match="title_mismatch"):
        module.select_coordinates(row, coordinates)
    row.covdock_pose_index = 0.5
    with pytest.raises(ValueError, match="invalid_candidate_pose_index"):
        module.select_coordinates(row, coordinates)
