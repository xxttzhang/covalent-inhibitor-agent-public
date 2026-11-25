import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "run_ligprep_job.py"
spec = importlib.util.spec_from_file_location("run_ligprep_job", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


def test_ligprep_manifest_rejects_missing_license(tmp_path, monkeypatch):
    input_sdf = tmp_path / "input.sdf"
    input_sdf.write_text("dummy")
    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_ligprep_job.py",
            "--schrodinger-root",
            str(tmp_path / "schrodinger"),
            "--license-file",
            str(tmp_path / "missing.lic"),
            "--input-sdf",
            str(input_sdf),
            "--output-mae",
            str(output_dir / "out.mae"),
            "--output-dir",
            str(output_dir),
            "--jobname",
            "job",
        ],
    )
    assert module.main() == 3
    manifest = json.loads(
        (output_dir / "job.run_manifest.json").read_text()
    )
    assert manifest["status"] == "LICENSE_MISSING"


def test_vendor_subprocess_does_not_inherit_project_python(tmp_path, monkeypatch):
    import subprocess
    root = tmp_path / "vendor"
    root.mkdir()
    (root / "ligprep").touch()
    license_path = tmp_path / "official.lic"
    license_path.write_text("test-license-not-used")
    source = tmp_path / "input.sdf"
    source.write_text("test-input-not-used")
    output = tmp_path / "out"
    monkeypatch.setenv("PYTHONPATH", "/wrong/python311/site-packages")
    monkeypatch.setenv("LD_AUDIT", "/unwanted/injection")
    def vendor_check(command, **kwargs):
        assert "PYTHONPATH" not in kwargs["env"]
        assert "LD_AUDIT" not in kwargs["env"]
        assert "LD_PRELOAD" not in kwargs["env"]
        return subprocess.CompletedProcess(command, 1)
    monkeypatch.setattr(module.subprocess, "run", vendor_check)
    monkeypatch.setattr(sys, "argv", ["run_ligprep_job.py", "--schrodinger-root", str(root),
        "--license-file", str(license_path), "--input-sdf", str(source),
        "--output-mae", str(output / "result.mae"), "--output-dir", str(output), "--jobname", "probe"])
    assert module.main() == 1


def test_ligprep_script_has_explicit_no_reionization_flags():
    text = SCRIPT.read_text()
    assert '"-i",\n        "0"' in text
    assert '"-s",\n        "1"' in text
