"""Provenance and transport tests, NOT a licensed SDK/scientific acceptance run."""
import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import test_refinement_output as fixtures
from covscreen.refinement_output import (Files, SPECS, collect_refinement_outputs,
    compare_covdock_inspections, verify_covdock_structures, _read_covdock_response)


@pytest.fixture
def source():
    fixture = fixtures.RefinementOutputTest()
    fixture.setUp()
    fixture.fixture("covdock")
    audit = fixture.root / "audit.json"
    contents = json.loads(audit.read_text())
    contents["geometry_requested"] = True
    for row in contents["rows"]:
        row.update(covalent_link_present=False, geometry_inspection_status="COVALENT_LINK_MISSING")
    audit.write_text(json.dumps(contents))
    for row in fixture.rows:
        row.update(covdock_inspection_sha256=fixtures.sha(audit), covalent_link_present=False,
                   geometry_inspection_status="COVALENT_LINK_MISSING")
    fixture.persist()
    yield fixture
    fixture.tearDown()


def comparison(source):
    evidence = collect_refinement_outputs(source.root, "covdock", source.contract)
    request = {"version": "covdock-structure-readback-v1", "attachment_residue": "A:797",
               "files": [str(source.root / "combined.maegz")]}
    observed = json.loads((source.root / "audit.json").read_text())
    observed["pose_count"] = len(observed["rows"])
    result = {"version": request["version"], "attachment_residue": request["attachment_residue"],
              "audits": [observed], "scientific_quality_verified": False}
    def verify(value):
        return compare_covdock_inspections(Files(source.root), evidence["artifacts"], source.manifest,
            source.rows, request, value)
    return evidence, result, verify


def test_comparison_preserves_negative_geometry_and_does_not_write(source):
    before = {p.name: (fixtures.sha(p), p.stat().st_mtime_ns) for p in source.root.iterdir()}
    evidence, result, verify = comparison(source)
    assert verify(result) is None
    assert evidence["scientific_quality_verified"] is False
    assert result["audits"][0]["rows"][0]["covalent_link_present"] is False
    assert before == {p.name: (fixtures.sha(p), p.stat().st_mtime_ns) for p in source.root.iterdir()}


@pytest.mark.parametrize("field,value", [("title", "another__i1"), ("pose_index", 9),
    ("r_test_score", -99.), ("covalent_link_present", True), ("covalent_link_present", 0),
    ("geometry_inspection_status", "SUCCESS"), ("covalent_bond_length_angstrom", 1.8),
    ("r_test_score", float("nan"))])
def test_source_observations_cannot_disagree_with_saved_audit(source, field, value):
    _, result, verify = comparison(source)
    result["audits"][0]["rows"][0][field] = value
    with pytest.raises(ValueError):
        verify(result)


def test_missing_duplicate_foreign_container_or_unrequested_quality_claim_rejected(source):
    _, result, verify = comparison(source)
    variants = [{**result, "scientific_quality_verified": True}, {**result, "attachment_residue": "A:773"},
                {**result, "audits": []}, {**result, "audits": result["audits"] * 2}]
    for field, value in (("input", "/inspire/old.maegz"), ("pose_count", 1), ("geometry_requested", False)):
        changed = copy.deepcopy(result)
        changed["audits"][0][field] = value
        variants.append(changed)
    for value in variants:
        with pytest.raises(ValueError):
            verify(value)


def test_full_verifier_requires_hash_bound_vendor_reader_before_any_launch(source):
    evidence, _, _ = comparison(source)
    runtime = {"name": "covdock", "attachment_residue": "A:797", "schrodinger_root": str(source.root / "unavailable"),
               "license_file": str(source.root / "license.lic"), "runtime_lib_dir": str(source.root / "libs")}
    with pytest.raises(ValueError, match="hash-bound"):
        verify_covdock_structures(source.root, evidence, runtime=runtime,
            script=Path(__file__).parents[1] / "scripts/inspect_covdock_output.py", assets={})


def run_response(tmp_path, code, timeout=3):
    return _read_covdock_response([sys.executable, "-I", "-B", "-c", code], dict(os.environ),
        str(tmp_path), b'{"fixture_only":true}', timeout)


def test_real_subprocess_transport_reads_json_but_grants_no_sdk_or_science_claim(tmp_path):
    assert run_response(tmp_path, 'import sys,json; print(json.dumps({"received":json.load(sys.stdin)}))') == {
        "received": {"fixture_only": True}}


@pytest.mark.parametrize("code,reason", [
    ('import sys; print("SENSITIVE_VENDOR_ERROR",file=sys.stderr); sys.exit(17)', "failed (exit 17)"),
    ('print("not json")', "valid readback JSON"),
    ('import sys; sys.stdout.write("x" * (4*1024*1024+1))', "bounds"),
    ('import sys; sys.stderr.write("SENSITIVE_VENDOR_ERROR" * 10000)', "bounds"),
])
def test_real_subprocess_errors_overflow_and_invalid_json_fail_without_raw_output(tmp_path, code, reason):
    with pytest.raises(ValueError) as error:
        run_response(tmp_path, code)
    assert reason in str(error.value)
    assert "SENSITIVE_VENDOR_ERROR" not in str(error.value)


def test_real_subprocess_timeout_does_not_wait_for_orphan_pipe_holder(tmp_path):
    start = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        run_response(tmp_path, 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time;time.sleep(10)"])', .2)
    assert time.monotonic() - start < 3


def test_actual_readonly_cli_rejects_invalid_request_before_sdk_import_or_any_write(tmp_path):
    script = Path(__file__).parents[1] / "scripts/inspect_covdock_output.py"
    result = subprocess.run([sys.executable, "-I", "-B", str(script), "--readback-stdin"],
        input='{"command":"must-not-run"}', text=True, capture_output=True, cwd=tmp_path, timeout=3)
    assert result.returncode != 0
    assert "Invalid CovDock structure readback request" in result.stderr
    assert list(tmp_path.iterdir()) == []
