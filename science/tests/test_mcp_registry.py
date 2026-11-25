"""Guard the existing MCP contract while separating transport and route ownership."""
import json
import os
from pathlib import Path
import subprocess
import sys

from covscreen.mcp import registry
from covscreen.tool_api import capabilities_tool


PROJECT = Path(__file__).resolve().parents[1]
ENTRYPOINT = PROJECT / "scripts" / "openscience_mcp_server.py"

# These are externally callable names, not an implementation snapshot. Changing
# this set requires an intentional protocol change rather than a module move.
EXISTING_TOOL_NAMES = {
    "covalent_library_workflow",
    "covalent_library_research",
    "covalent_library_receptor",
    "covalent_ligand_workflow",
    "covalent_ligand_evidence",
    "covalent_ligand_design_step",
    "covalent_ligand_design",
    "covalent_lookup_submission",
    "covalent_wait_job",
    "covalent_verify_job_result",
    "covalent_list_workflows",
    "covalent_get_guard_status",
    "covalent_verify_report_claims",
    "covalent_resume_workflow",
    "covalent_library_first_prepare_docking",
    "covalent_library_first_prepare_pilot",
    "covalent_library_first_plan_from_sites",
    "covalent_predict_cysteine_sites",
    "covalent_evaluate_covalent_sites",
    "covalent_select_covalent_sites",
    "covalent_library_first_next_md_action",
    "covalent_library_first_admit_md_candidate",
    "covalent_library_first_select_md_candidate",
    "covalent_library_first_deliver_md_candidates",
    "covalent_library_first_build_md_report",
    "covalent_library_first_analyze_openmm_replicas",
    "covalent_library_first_audit_openmm_trajectory",
    "covalent_library_first_run_openmm_production",
    "covalent_library_first_equilibrate_openmm_system",
    "covalent_library_first_run_openmm_short_qc",
    "covalent_library_first_prepare_openmm_system",
    "covalent_library_first_parameterize_adduct",
    "covalent_library_first_assess_receptor_context",
    "covalent_library_first_prepare_receptor",
    "covalent_library_first_acquire_structure",
    "covalent_library_first_discover_structures",
    "covalent_library_first_resolve_target",
    "covalent_target_hypothesis",
    "covalent_prepare_execution",
    "covalent_ligand_first_reuse_result",
    "covalent_capabilities",
    "covalent_validate_project",
    "covalent_runtime_preflight",
    "covalent_platform_health",
    "covalent_submit_job",
    "covalent_library_first_intake",
    "covalent_library_first_chain",
    "covalent_ligand_first_intake",
    "covalent_ligand_first_resolve_names",
    "covalent_ligand_first_prepare_inputs",
    "covalent_ligand_first_discover_structures",
    "covalent_ligand_first_audit_structure_site",
    "covalent_ligand_first_chain",
    "covalent_ligand_first_campaign",
    "covalent_list_jobs",
    "covalent_get_job",
    "covalent_get_job_log",
    "covalent_get_candidates",
    "covalent_cancel_job",
    "covalent_retry_job",
    "covalent_plan_library_cascade",
    "covalent_next_library_action",
    "covalent_plan_attachments",
    "covalent_enumerate_warheads",
    "covalent_plan_ligand_first",
    "covalent_evaluate_ligand_first_candidate",
    "covalent_plan_de_novo",
    "covalent_de_novo_review",
}


def test_registered_schemas_and_callable_handlers_preserve_the_public_contract(tmp_path):
    names = [tool["name"] for tool in registry.TOOLS]
    assert len(names) == len(set(names)) == 68
    assert set(names) == EXISTING_TOOL_NAMES
    handlers = registry.handlers(tmp_path, {})
    assert set(handlers) == set(names)
    assert all(callable(handler) for handler in handlers.values())

    # Discovery must contain complete JSON-serializable schemas; handlers remain
    # uncalled so registry construction cannot launch scientific work.
    assert json.loads(json.dumps(registry.TOOLS)) == registry.TOOLS
    for tool in registry.TOOLS:
        assert tool["description"].strip()
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert all(
            isinstance(tool["annotations"][hint], bool)
            for hint in ("readOnlyHint", "destructiveHint", "idempotentHint")
        )
    assert list(tmp_path.iterdir()) == []


def test_groups_partition_tools_and_preserve_host_and_historical_roles():
    groups = registry.groups()
    assert len(groups) == 10
    assert len({group["group"] for group in groups}) == len(groups)
    assert {group["group"] for group in groups} == {
        "shared", "library-first", "ligand-first", "de-novo",
        "upstream", "site-discovery", "internal", "legacy", "ligand-workflow", "library-workflow",
    }
    members = [name for group in groups for name in group["tools"]]
    assert len(members) == len(set(members))
    assert set(members) == EXISTING_TOOL_NAMES
    for group in groups:
        assert isinstance(group["role"], str) and group["role"].strip()
        assert isinstance(group["routes"], list)

    owner = {name: group["group"] for group in groups for name in group["tools"]}
    assert owner["covalent_prepare_execution"] == "internal"
    assert owner["covalent_submit_job"] == "internal"
    assert owner["covalent_ligand_first_reuse_result"] == "legacy"
    assert owner["covalent_target_hypothesis"] == "upstream"
    assert owner["covalent_predict_cysteine_sites"] == "site-discovery"
    roles = {group["group"]: group["role"] for group in groups}
    assert roles["internal"] == "internal"
    assert roles["legacy"] == "historical"

    # Internal classification must not remove the preparer that the host finds
    # through tools/list before granting a signed execution approval.
    prepare = next(tool for tool in registry.TOOLS if tool["name"] == "covalent_prepare_execution")
    assert prepare["annotations"]["readOnlyHint"] is True
    assert set(prepare["inputSchema"]["required"]) == {"tool", "arguments"}


def test_capability_inventory_matches_discovery_and_does_not_create_state(tmp_path):
    result = capabilities_tool(tmp_path)
    names = [tool["name"] for tool in registry.TOOLS]
    assert result["status"] == "PASS"
    assert len(result["tools"]) == len(names)
    assert set(result["tools"]) == set(names)
    assert result["server_version"] == "1.27.23"
    repair = result["library_first"]["receptor_repair"]
    assert repair["selected_backend"] == "pdbfixer-openmm"
    assert repair["pdbfixer"]["version"] == "1.12.0"
    assert repair["prime"]["status"] == "capability_not_implemented"
    assert repair["multiple_repair_backends_in_one_lineage_allowed"] is False
    assert result["project_root"] == str(tmp_path.resolve())
    assert list(tmp_path.iterdir()) == []


def test_legacy_entrypoint_binds_explicit_root_from_unrelated_cwd(tmp_path):
    root = tmp_path / "campaign-data"
    root.mkdir()
    unrelated = tmp_path / "unrelated-cwd"
    unrelated.mkdir()
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "covalent_capabilities", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "covalent_md_run", "arguments": {}}},
    ]
    process = subprocess.run(
        [sys.executable, "-u", str(ENTRYPOINT)],
        input="".join(json.dumps(message) + "\n" for message in messages),
        cwd=unrelated,
        env={**os.environ, "COVSCREEN_PROJECT_ROOT": str(root),
             "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert process.stderr == ""
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert [response["id"] for response in responses] == [1, 2, 3, 4, 5]
    assert all(response["jsonrpc"] == "2.0" for response in responses)
    assert responses[0]["result"]["serverInfo"]["name"] == "covalent-discovery-tools"
    assert responses[0]["result"]["protocolVersion"] == "2024-11-05"
    assert responses[1]["result"] == {}
    discovered = responses[2]["result"]["tools"]
    assert discovered == registry.TOOLS
    assert {tool["name"] for tool in discovered} == EXISTING_TOOL_NAMES

    capability_result = responses[3]["result"]
    assert capability_result["isError"] is False
    capabilities = capability_result["structuredContent"]
    assert capabilities["project_root"] == str(root.resolve())
    assert set(capabilities["tools"]) == EXISTING_TOOL_NAMES
    assert json.loads(capability_result["content"][0]["text"]) == capabilities

    # MD is part of the product objective, but no general MD MCP tool exists in
    # this refactor. A speculative name must fail rather than claim execution.
    unknown = responses[4]["result"]
    assert unknown["isError"] is True
    assert unknown["structuredContent"]["status"] == "FAIL"
    assert "covalent_md_run" in unknown["structuredContent"]["error"]
    assert list(root.iterdir()) == []
    assert list(unrelated.iterdir()) == []
