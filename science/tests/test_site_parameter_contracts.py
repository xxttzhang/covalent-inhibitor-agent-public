"""Reject malformed calls before touching jobs; exercise the published MCP schema too."""
import math

import pytest
from jsonschema import Draft202012Validator

from covscreen.covalent_site_discovery import (
    _prediction_request, predict_cysteine_sites_tool,
    evaluate_covalent_sites_tool, select_covalent_sites_tool,
)
from covscreen.mcp.site_discovery import TOOLS
from covscreen.site_contracts import validate_prediction

BASE = {"receptor_input_artifact_id": "receptor-input-" + "a" * 32,
        "manifest_sha256": "b" * 64}
SCHEMAS = {tool["name"]: tool["inputSchema"] for tool in TOOLS}


@pytest.mark.parametrize("arguments", [
    {"receptor_input_artifact_id": BASE["receptor_input_artifact_id"]},
    {**BASE, "run_id": "unsubmitted-plan"},
    {**BASE, "manifest_sha256": "wrong"},
    {**BASE, "manifest_sha256": None},
    {**BASE, "human_approved": True},
    {**BASE, "action": "start", "human_approved": "true"},
    {**BASE, "native_timeout_seconds": "900"},
    {**BASE, "native_timeout_seconds": True},
    {**BASE, "resource_budget": None},
    {**BASE, "resource_budget": {"gpu_count": 1}},
    {**BASE, "resource_budget": {"cpu_count": 5}},
    {**BASE, "resource_budget": {"cpu_count": True}},
    {**BASE, "resource_budget": {"memory_gb": 17}},
    {**BASE, "resource_budget": {"max_candidates": 1001}},
    {**BASE, "resource_budget": {"max_runtime_minutes": 61}},
    {**BASE, "resource_budget": {"unknown": 1}},
    {"action": "status"},
    {"action": "status", "run_id": "../other-job"},
    {"action": "status", "run_id": ""},
    {"action": "status", "run_id": "job-1", "manifest_sha256": "b" * 64},
    {**BASE, "unknown": 1},
    {**BASE, "action": "execute"},
])
def test_schema_and_runtime_reject_bad_prediction_before_side_effects(tmp_path, arguments):
    assert list(Draft202012Validator(SCHEMAS["covalent_predict_cysteine_sites"]).iter_errors(arguments))
    with pytest.raises(ValueError):
        predict_cysteine_sites_tool(tmp_path, arguments)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("arguments", [BASE, {**BASE, "action": "plan"},
    {**BASE, "action": "start", "human_approved": True},
    {**BASE, "resource_budget": {"cpu_count": 1}},
    {"action": "status", "run_id": "deepcosi-test-run"}])
def test_published_schema_accepts_valid_action_shapes(arguments):
    validator = Draft202012Validator(SCHEMAS["covalent_predict_cysteine_sites"])
    validator.check_schema(validator.schema)
    validator.validate(arguments)
    validate_prediction(arguments)


def test_defaults_and_cross_field_resource_limits(tmp_path):
    request = _prediction_request(BASE, execution_mode="plan")
    assert request.resource_budget.model_dump() == {
        "max_runtime_minutes": 20, "gpu_count": 0, "cpu_count": 2,
        "memory_gb": 8, "max_candidates": 100}
    assert request.parameters["native_timeout_seconds"] == 900
    for update in ({"resource_budget": {"max_runtime_minutes": 1}},
                   {"resource_budget": {"memory_gb": math.inf}},
                   {"resource_budget": {"memory_gb": math.nan}}):
        with pytest.raises(ValueError):
            predict_cysteine_sites_tool(tmp_path, {**BASE, **update})
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("updates", [
    {"canonical_positions": []}, {"canonical_positions": [797, 797], "max_sites": 2},
    {"canonical_positions": [797, 775]}, {"canonical_positions": [True]},
    {"max_sites": "2"}, {"routes": ["library-first", "library-first"]},
    {"routes": []}, {"unknown": True},
])
def test_selection_rejects_ambiguous_or_invalid_input_before_loading(tmp_path, updates):
    with pytest.raises(ValueError):
        select_covalent_sites_tool(tmp_path, {
            "evaluation_artifact_id": "site-evaluation-" + "a" * 32,
            "evaluation_manifest_sha256": "b" * 64, **updates})
    assert not list(tmp_path.iterdir())


def test_evaluation_rejects_duplicate_evidence_before_loading(tmp_path):
    with pytest.raises(ValueError, match="unique"):
        evaluate_covalent_sites_tool(tmp_path, {
            "prediction_run_id": "job-1", "prediction_manifest_sha256": "b" * 64,
            "hypothesis_evidence_ids": ["hyp-" + "a" * 24] * 2})
    assert not list(tmp_path.iterdir())


def test_boolean_cannot_bypass_native_approval(tmp_path):
    from covscreen.mcp.server import dispatch
    with pytest.raises(ValueError, match="receipt required"):
        dispatch(tmp_path, "covalent_predict_cysteine_sites",
                 {**BASE, "action": "start", "human_approved": True})
    assert not list(tmp_path.rglob("spec.json"))


def test_missing_hash_stops_session_without_submitting(tmp_path):
    from covscreen.mcp.server import dispatch
    from covscreen.mcp.task_guard import guarded_call
    result = guarded_call(tmp_path, "covalent_predict_cysteine_sites",
        {"action": "plan", "receptor_input_artifact_id": BASE["receptor_input_artifact_id"]},
        dispatch, environment={"COVALENT_STOP_ON_TOOL_FAILURE": "1"})
    assert result["status"] == "FAIL"
    assert "manifest_sha256" in result["error"]
    assert (tmp_path / "state/task_guard/stopped.json").is_file()
    assert not list(tmp_path.rglob("spec.json"))
