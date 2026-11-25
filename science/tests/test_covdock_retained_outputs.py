import importlib.util
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("covdock_retained", SCRIPTS / "run_covdock_lo_registered.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_retained_pose_files_must_all_exist_and_process_must_succeed(tmp_path):
    first, second = tmp_path / "a.maegz", tmp_path / "b.maegz"
    first.write_text("pose")
    manifest = tmp_path / "manifest.json"
    payload = {"status": "SUCCESS", "return_code": 0,
               "parsed_outputs": {"pose_files": [str(first), str(second)]}}
    manifest.write_text(json.dumps(payload))
    assert module._successful_covdock(manifest) == []
    second.write_text("pose")
    assert module._successful_covdock(manifest) == [first, second]
    payload["return_code"] = 1
    manifest.write_text(json.dumps(payload))
    assert module._successful_covdock(manifest) == []
