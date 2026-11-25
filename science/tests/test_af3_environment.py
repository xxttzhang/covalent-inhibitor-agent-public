import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import numpy

from covscreen.af3_environment import af3_environment, af3_site_packages


def test_native_process_does_not_inherit_orchestrator_packages(tmp_path):
    poison = tmp_path / "python311-packages"
    poison.mkdir()
    (poison / "wrong_orchestrator_runtime.py").write_text("raise RuntimeError('wrong interpreter packages')\n")
    inherited = dict(os.environ, PYTHONPATH=str(poison), PYTHONHOME=str(poison),
                     PYTHONUSERBASE=str(poison), LD_PRELOAD=str(poison / "wrong.so"),
                     LD_LIBRARY_PATH=str(poison), CUDA_VISIBLE_DEVICES="0")
    original = dict(inherited)
    python = Path(sys.executable)
    source = Path(__file__).resolve().parents[1] / "src"
    env = af3_environment(python, source, inherited)
    result = subprocess.run([str(python), "-c",
        "import importlib.util,json,sys,numpy,covscreen.af3_environment; "
        "print(json.dumps({'numpy':numpy.__file__,'path':sys.path,"
        "'wrong':importlib.util.find_spec('wrong_orchestrator_runtime') is not None}))"],
        env=env, cwd=tmp_path, text=True, capture_output=True, check=True)
    observed = json.loads(result.stdout)
    assert observed["wrong"] is False
    assert str(poison) not in observed["path"]
    # The local test venv legitimately uses its base interpreter's packages;
    # compare the actual clean interpreter dependency, not a guessed venv path.
    assert Path(observed["numpy"]).resolve() == Path(numpy.__file__).resolve()
    assert env["CUDA_VISIBLE_DEVICES"] == "0"
    assert env["PYTHONNOUSERSITE"] == "1"
    assert "LD_PRELOAD" not in env
    assert inherited == original


def test_package_directories_do_not_substitute_for_an_interpreter(tmp_path):
    python = tmp_path / "bin/python"
    for version in ("3.11", "3.12"):
        (tmp_path / f"lib/python{version}/site-packages").mkdir(parents=True)
    with pytest.raises(ValueError, match="Cannot query"):
        af3_site_packages(python)


def test_missing_native_packages_fail_before_launch(tmp_path):
    with pytest.raises(ValueError, match="Cannot query"):
        af3_environment(tmp_path / "bin/python", tmp_path / "src", {})
