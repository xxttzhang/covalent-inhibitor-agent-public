import hashlib
import json
from pathlib import Path
import shutil
import sys

import pytest

from covscreen.tool_api import library_first_prepare_receptor_tool
from covscreen.native_approval import needs_approval


ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "reports/library-first-target-identity-20261014"
ACQUISITION_ID = "3fe7754be1b14546a7141aa26cebde6b"
TARGET_ID = "b2da731ddc46453da2b996bebdfae971"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def project(tmp_path):
    openmm_python = ROOT / ".tooling/md-interoperability-20261002/bin/python"
    packages = ROOT / ".tooling/research-receptor-prep-v2/site-packages"
    if not openmm_python.is_file() or not packages.is_dir():
        pytest.skip("registered local OpenMM/PDBFixer runtime is unavailable")
    acquisition = tmp_path / "state/target_acquisition" / ACQUISITION_ID
    target = tmp_path / "state/target_identity" / TARGET_ID
    shutil.copytree(LIVE / "state/target_acquisition" / ACQUISITION_ID, acquisition)
    shutil.copytree(LIVE / "state/target_identity" / TARGET_ID, target)
    config = tmp_path / "configs/local.inspire.yaml"
    config.parent.mkdir()
    config.write_text(
        "receptor_preparation:\n"
        "  enabled: true\n"
        f"  audit_python: {sys.executable}\n"
        f"  openmm_python: {openmm_python}\n"
        f"  openmm_pythonpath: {ROOT / '.tooling/md-interoperability-20261002'}\n"
        "  pdbfixer_source: bundled\n"
        "runtime:\n  worker_backend: local\n"
    )
    return acquisition


def arguments(acquisition):
    return {
        "action": "plan",
        "receptor_input_artifact_id": f"receptor-input-{ACQUISITION_ID}",
        "manifest_sha256": sha(acquisition / "target_acquisition.json"),
        "altlocs": {"751": "A", "831": "A"},
        "heavy_seed": 20261013,
        "max_missing_residues": 30,
    }


def test_plan_freezes_full_parent_chain_and_uses_one_job_output_subdirectory(tmp_path):
    acquisition = project(tmp_path)
    result = library_first_prepare_receptor_tool(tmp_path, arguments(acquisition))
    spec = result["spec"]
    assert result["status"] == "PLANNED"
    assert result["receptor_repair"]["selected_backend"] == "pdbfixer-openmm"
    assert result["receptor_repair"]["prime"]["implemented"] is False
    assert result["receptor_repair"]["multiple_repair_backends_in_one_lineage_allowed"] is False
    assert not (tmp_path / "vendor").exists()
    vendor = Path(spec["command"][spec["command"].index("--pdbfixer-site-packages") + 1])
    assert vendor == ROOT / "science/vendor/PDBFixer"
    assert spec["engine"] == "library-first-receptor-preparation"
    assert spec["route"] == "library-first"
    assert spec["resource_budget"] == {
        "max_runtime_minutes": 15, "gpu_count": 0, "cpu_count": 1,
        "memory_gb": 4.0, "max_candidates": 1,
    }
    assert len(spec["input_sha256"]) == 10
    output, = spec["expected_output_paths"]
    assert output.endswith("/artifacts/receptor-preparation")
    assert spec["command"][spec["command"].index("--output-dir") + 1] == output
    assert not Path(output).exists()


def test_plan_returns_deposited_altloc_options_before_compiling_a_job(tmp_path):
    acquisition = project(tmp_path)
    values = arguments(acquisition)
    values.pop("altlocs")
    result = library_first_prepare_receptor_tool(tmp_path, values)
    assert result["status"] == "ALTERNATE_LOCATION_SELECTION_REQUIRED"
    assert result["receptor_repair"]["prime"]["status"] == "capability_not_implemented"
    plan = result["alternate_location_plan"]
    assert plan["required_author_residue_ids"] == ["751", "831"]
    assert all({option["label"] for option in row["options"]} == {"A", "B"}
               for row in plan["residues"])
    assert plan["automatic_selection_allowed"] is False
    assert not list((tmp_path / "state/jobs").glob("*/spec.json"))


def test_plan_replays_raw_parent_evidence_instead_of_trusting_rehashed_manifest(tmp_path):
    acquisition = project(tmp_path)
    source = acquisition / "structure.pdb"
    source.write_bytes(source.read_bytes() + b"REMARK tampered\n")
    with pytest.raises(ValueError, match="source checksum mismatch"):
        library_first_prepare_receptor_tool(tmp_path, arguments(acquisition))


def test_plan_rejects_non_cpu_or_multi_candidate_budget(tmp_path):
    acquisition = project(tmp_path)
    values = arguments(acquisition)
    values["resource_budget"] = {
        "max_runtime_minutes": 15, "gpu_count": 1, "cpu_count": 1,
        "memory_gb": 4, "max_candidates": 1,
    }
    with pytest.raises(ValueError, match="CPU-only"):
        library_first_prepare_receptor_tool(tmp_path, values)


def test_only_start_crosses_the_native_execution_approval_boundary():
    name = "covalent_library_first_prepare_receptor"
    assert needs_approval(name, {"action": "start"}) is True
    assert needs_approval(name, {"action": "plan"}) is False
    assert needs_approval(name, {"action": "status"}) is False
