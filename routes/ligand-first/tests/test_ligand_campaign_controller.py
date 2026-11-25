import importlib.util
from pathlib import Path
from types import SimpleNamespace
import hashlib

import pytest


def controller():
    path = (Path(__file__).resolve().parents[3] / "science") / "scripts/run_ligand_first_campaign.py"
    spec = importlib.util.spec_from_file_location("campaign_controller_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_controller_does_not_adopt_unapproved_existing_run():
    status = SimpleNamespace(terminal_state="active", stages=[
        SimpleNamespace(stage="af3", run_id="other-run", state="RUNNING")])
    assert controller().stop_reason(status, {}) == "unapproved_existing_run"


def test_controller_never_automatically_retries_failed_stage():
    status = SimpleNamespace(terminal_state="ready", stages=[
        SimpleNamespace(stage="af3", run_id="approved-run", state="FAILED")])
    assert controller().stop_reason(status, {"af3": "approved-run"}) == "stage_failed_requires_explicit_retry"


def test_controller_preserves_quality_gate_stop():
    status = SimpleNamespace(terminal_state="quality_gate_failed", stages=[
        SimpleNamespace(stage="af3", run_id="approved-run", state="COMPLETED")])
    assert controller().stop_reason(status, {"af3": "approved-run"}) == "quality_gate_failed"


def test_controller_waits_for_approved_active_run():
    status = SimpleNamespace(terminal_state="active", stages=[
        SimpleNamespace(stage="af3", run_id="approved-run", state="RUNNING")])
    assert controller().stop_reason(status, {"af3": "approved-run"}) is None


@pytest.mark.parametrize("changed", [None, "config", "campaign"])
def test_controller_binds_both_approved_input_files(tmp_path, changed):
    config, campaign = tmp_path / "config.yaml", tmp_path / "campaign.json"
    config.write_text("project_id: example\n")
    campaign.write_text('{"campaign_id":"example"}')
    hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in (config, campaign)]
    if changed:
        (config if changed == "config" else campaign).write_text("changed after approval")
        with pytest.raises(ValueError, match="Approved"):
            controller().verify_approved_inputs(config, campaign, *hashes)
    else:
        controller().verify_approved_inputs(config, campaign, *hashes)
