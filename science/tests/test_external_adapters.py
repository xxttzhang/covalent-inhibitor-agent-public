import json

from covscreen.adapters.alphafold3 import (
    AF3_DATABASE_FILES,
    AlphaFold3Adapter,
)
from covscreen.adapters.covdock import CovDockAdapter


def test_af3_adapter_checks_all_assets_and_parses_outputs(tmp_path):
    runner = tmp_path / "run_alphafold.py"
    python = tmp_path / "python"
    models = tmp_path / "models"
    databases = tmp_path / "databases"
    runner.write_text("")
    python.write_text("")
    models.mkdir()
    databases.mkdir()
    (models / "af3.bin").write_bytes(b"x")
    for name in AF3_DATABASE_FILES:
        (databases / name).write_bytes(b"x")
    adapter = AlphaFold3Adapter(
        str(runner), str(python), str(models), str(databases)
    )
    assert adapter.check_availability()["ready"] is True
    one_seed_command = adapter.build_command("in.json", "out", num_seeds=1)
    assert not any(arg.startswith("--num_seeds=") for arg in one_seed_command)
    multi_seed_command = adapter.build_command("in.json", "out", num_seeds=3)
    assert "--num_seeds=3" in multi_seed_command
    processed_command = adapter.build_command(
        "in.json", "out", run_data_pipeline=False
    )
    assert "--run_data_pipeline=false" in processed_command
    input_dir = tmp_path / "inputs"
    input_dir.mkdir()
    directory_command = adapter.build_command(str(input_dir), "out")
    assert f"--input_dir={input_dir}" in directory_command

    output = tmp_path / "output"
    output.mkdir()
    (output / "job_model.cif").write_text("data")
    (output / "job_summary_confidences.json").write_text(json.dumps({}))
    parsed = adapter.parse_output(output)
    assert adapter.validate_output(parsed) is True


def test_af3_adapter_reports_missing_database(tmp_path):
    adapter = AlphaFold3Adapter(
        str(tmp_path / "missing.py"),
        str(tmp_path / "missing-python"),
        str(tmp_path / "models"),
        str(tmp_path / "databases"),
    )
    report = adapter.check_availability()
    assert report["ready"] is False
    assert report["missing_databases"]


def test_covdock_requires_explicit_legitimate_license(monkeypatch, tmp_path):
    root = tmp_path / "schrodinger"
    root.mkdir()
    (root / "covalent_docking").write_text("")
    monkeypatch.delenv("SCHROD_LICENSE_FILE", raising=False)
    adapter = CovDockAdapter(str(root))
    report = adapter.check_availability()
    assert report["ready"] is False
    assert "bypass" in report["reason"]

    monkeypatch.setenv("SCHROD_LICENSE_FILE", "27008@license.example.edu")
    assert adapter.check_availability()["ready"] is True
    assert "27008" not in str(adapter.check_availability())

    command = adapter.build_command(
        "receptor.mae",
        "ligand.mae",
        "A:481",
        "BTK_B1852_covdock",
    )
    assert command[:4] == [
        str(root / "covalent_docking"),
        "receptor.mae",
        "ligand.mae",
        "A:481",
    ]
    assert command[command.index("-mode") + 1] == "enrichment"
    assert command[command.index("-rxn_type") + 1] == "Michael Addition"
    assert command[-1] == "-WAIT"

    grid_command = adapter.build_command(
        "receptor.mae",
        "ligand.mae",
        "A:481",
        "BTK_B1852_covdock_grid",
        grid_file="grid.zip",
    )
    assert grid_command[grid_command.index("-grid_file") + 1] == "grid.zip"


def test_covdock_rejects_explicit_bypass_marker(monkeypatch, tmp_path):
    root = tmp_path / "schrodinger"
    root.mkdir()
    (root / "covalent_docking").write_text("")
    monkeypatch.setenv("SCHROD_LICENSE_FILE", "/tmp/crack/license")
    report = CovDockAdapter(str(root)).check_availability()
    assert report["ready"] is False
    assert report["unsafe_license_marker"] is True


def test_covdock_output_validation_requires_pose_and_no_failed_log(tmp_path):
    adapter = CovDockAdapter(str(tmp_path))
    (tmp_path / "receptor_prepared.mae").write_text("receptor")
    parsed = adapter.parse_output(tmp_path)
    assert adapter.validate_output(parsed) is False

    (tmp_path / "job-out.maegz").write_text("pose")
    parsed = adapter.parse_output(tmp_path)
    assert adapter.validate_output(parsed) is True

    (tmp_path / "job.log").write_text("Error: All Entries Failed")
    parsed = adapter.parse_output(tmp_path)
    assert adapter.validate_output(parsed) is False
