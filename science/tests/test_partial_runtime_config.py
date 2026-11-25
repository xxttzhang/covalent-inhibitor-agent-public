from pathlib import Path
import yaml
from covscreen.tool_api import infer_engine_availability


def test_empty_runtime_does_not_raise(tmp_path):
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/local.inspire.yaml").write_text("{}")
    result = infer_engine_availability(tmp_path)
    assert not result.carsidock_cov
    assert not result.rtmscore
    assert not result.covdock_lo


def test_missing_rtmscore_does_not_hide_other_assets(tmp_path):
    (tmp_path / "configs").mkdir()
    for name in ("python", "checkpoint", "covalent_docking"):
        (tmp_path / name).touch()
    config = {"carsidock": {"repo": str(tmp_path), "python": str(tmp_path / "python"),
                           "checkpoint": str(tmp_path / "checkpoint")},
              "schrodinger": {"root": str(tmp_path)}}
    (tmp_path / "configs/local.inspire.yaml").write_text(yaml.safe_dump(config))
    result = infer_engine_availability(tmp_path)
    assert not result.carsidock_cov  # Installed assets do not override the suspended-engine policy.
    assert result.covdock_lo
    assert not result.rtmscore
