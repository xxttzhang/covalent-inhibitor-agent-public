import importlib.util
import json
from pathlib import Path

import pytest

from covscreen.jobs import sha256_file


def test_retained_entry_checks_gate_input_and_output_hashes(tmp_path):
    source = Path(__file__).resolve().parents[1] / "scripts/register_ligand_covdock_reparse.py"
    spec = importlib.util.spec_from_file_location("covdock_reparse_entry", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    candidate = tmp_path / "input.parquet"
    candidate.write_bytes(b"synthetic input; not a real molecule table")
    retained = tmp_path / "retained"
    retained.mkdir()
    output = retained / "stage5_gated_candidates.parquet"
    output.write_bytes(b"synthetic output")
    manifest = {"status": "completed", "gate_quality_gates": {"complete": True},
        "input_candidate_table_sha256": sha256_file(candidate),
        "outputs": {output.name: {"sha256": sha256_file(output)}}}
    manifest_path = retained / "covdock_lo_registered_manifest.json"
    manifest_path.write_text(json.dumps(manifest))
    assert module.validate_retained(tmp_path, retained, candidate) == manifest
    output.write_bytes(b"changed")
    with pytest.raises(ValueError, match="missing or changed"):
        module.validate_retained(tmp_path, retained, candidate)
    candidate.write_bytes(b"changed input")
    with pytest.raises(ValueError, match="input hash differs"):
        module.validate_retained(tmp_path, retained, candidate)
    manifest["status"] = "quality_gate_failed"
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="quality gates"):
        module.validate_retained(tmp_path, retained, candidate)
