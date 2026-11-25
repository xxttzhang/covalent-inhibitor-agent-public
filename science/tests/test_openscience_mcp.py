import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def exchange(messages):
    payload = "".join(json.dumps(item) + "\n" for item in messages)
    result = subprocess.run(
        [sys.executable, "-u", str(ROOT / "scripts/openscience_mcp_server.py")],
        input=payload,
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=30,
        check=True,
    )
    assert result.stderr == ""
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_mcp_initializes_lists_tools_and_calls_project_validation():
    responses = exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            },
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "covalent_validate_project",
                    "arguments": {"project_id": "egfr-c797"},
                },
            },
        ]
    )
    assert responses[0]["result"]["serverInfo"]["name"] == "covalent-discovery-tools"
    names = {item["name"] for item in responses[1]["result"]["tools"]}
    assert "covalent_plan_library_cascade" in names
    assert "covalent_plan_attachments" in names
    assert "covalent_plan_ligand_first" in names
    assert "covalent_evaluate_ligand_first_candidate" in names
    assert "covalent_plan_de_novo" in names
    assert "covalent_predict_cysteine_sites" in names
    assert "covalent_evaluate_covalent_sites" in names
    assert "covalent_select_covalent_sites" in names
    assert "covalent_enumerate_warheads" in names
    assert "covalent_platform_health" in names
    assert "covalent_submit_job" in names
    assert "covalent_library_first_intake" in names
    assert "covalent_library_first_chain" in names
    assert "covalent_ligand_first_intake" in names
    assert "covalent_ligand_first_discover_structures" in names
    assert "covalent_ligand_first_resolve_names" in names
    assert "covalent_ligand_first_prepare_inputs" in names
    assert "covalent_ligand_first_audit_structure_site" in names
    assert "covalent_ligand_first_chain" in names
    assert "covalent_ligand_first_campaign" in names
    assert "covalent_list_jobs" in names
    assert "covalent_get_job" in names
    assert "covalent_get_job_log" in names
    assert "covalent_get_candidates" in names
    assert "covalent_cancel_job" in names
    assert "covalent_retry_job" in names
    submit = next(item for item in responses[1]["result"]["tools"] if item["name"] == "covalent_submit_job")
    assert "covindb-prescreen" in submit["inputSchema"]["properties"]["engine"]["enum"]
    assert "ligand-first-structure-intake" in submit["inputSchema"]["properties"]["engine"]["enum"]
    assert "carsidock-pool-selection" in submit["inputSchema"]["properties"]["engine"]["enum"]
    assert "alphafold3-mpae" in submit["inputSchema"]["properties"]["engine"]["enum"]
    assert "schrodinger-covdock-lo" in submit["inputSchema"]["properties"]["engine"]["enum"]
    assert "library-first-pareto" in submit["inputSchema"]["properties"]["engine"]["enum"]
    assert "library-first-adduct-parameterization" in submit["inputSchema"]["properties"]["engine"]["enum"]
    assert responses[2]["result"]["structuredContent"]["status"] == "PASS"
    assert responses[2]["result"]["structuredContent"]["route"] == "ligand-first"


def test_mcp_structure_discovery_plan_is_nonexecuting():
    result = exchange([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "covalent_ligand_first_discover_structures",
                   "arguments": {"uniprot": "P00533", "component": "AQ4"}}}])[0]["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["network_request_sent"] is False
    assert result["structuredContent"]["campaign_registered"] is False


def test_mcp_name_resolution_plan_requires_explicit_species_and_no_network():
    result = exchange([{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "covalent_ligand_first_resolve_names",
            "arguments": {"gene": "EGFR", "taxon": 9606, "cys": 797, "ligand": "erlotinib"}}}])[0]["result"]
    assert result["isError"] is False
    payload = result["structuredContent"]
    assert payload["network_request_sent"] is False
    assert payload["campaign_registered"] is False
    assert payload["plan"]["max_calls"] == 3


def test_mcp_intake_resolves_a_natural_language_library_first_request():
    result = exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "covalent_library_first_intake",
                    "arguments": {
                        "query": "针对 EGFR C797，从 CovalentInDB 中筛选共价抑制剂",
                        "scope": "pilot",
                    },
                },
            }
        ]
    )[0]["result"]
    assert result["isError"] is False
    intake = result["structuredContent"]["intake"]
    assert intake["target"]["site_id"] == "EGFR_C797"
    assert intake["campaign_config_path"] == (
        "configs/library_first/EGFR_C797_covindb_pilot_v1.yaml"
    )


def test_mcp_chain_tool_reports_the_formal_next_stage_without_submitting():
    response = exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "covalent_library_first_chain",
                    "arguments": {
                        "config_path": "configs/library_first/EGFR_C797_covindb_pilot_v1.yaml",
                        "execution_mode": "plan",
                        "submit": False,
                    },
                },
            }
        ]
    )[0]["result"]
    result = response["structuredContent"]
    assert result["status"] == "PASS"
    assert result["submitted"] is False
    assert result["chain"]["next_stage"] in {
        "prescreen",
        "carsidock",
        "rtmscore",
        "recall_pool",
        "af3",
        "covdock_lo",
        "pareto",
        None,
    }


def test_mcp_ligand_first_intake_and_chain_are_natural_language_driven():
    responses = exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "covalent_ligand_first_intake",
                    "arguments": {
                        "query": "针对 EGFR C797，将 erlotinib 设计为共价抑制剂"
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "covalent_ligand_first_chain",
                    "arguments": {
                        "config_path": "configs/ligand_first/EGFR_C797_erlotinib_v1.yaml",
                        "execution_mode": "plan",
                        "submit": False,
                    },
                },
            },
        ]
    )
    intake = responses[0]["result"]["structuredContent"]["intake"]
    assert intake["state"] == "READY"
    assert intake["target"]["residue"] == "CYS797"
    assert intake["parent_ligand"]["name"] == "erlotinib"
    assert intake["campaign_config_path"] == (
        "configs/ligand_first/EGFR_C797_erlotinib_v1.yaml"
    )
    assert intake["missing_inputs"] == []
    import yaml

    config = yaml.safe_load((ROOT / intake["campaign_config_path"]).read_text())
    expected_deferred = {
        f"{label}:{config[key]}"
        for label, key in (
            ("af3_target_config", "target_config"),
            ("af3_protein_seed", "af3_source_data_json"),
            ("covdock_receptor", "covdock_receptor"),
        )
        if not (ROOT / config[key]).is_file()
    }
    assert set(intake["deferred_inputs"]) == expected_deferred
    chain = responses[1]["result"]["structuredContent"]
    assert chain["submitted"] is False
    assert chain["chain"]["next_stage"] == "structure_intake"
    assert chain["next_request"]["engine"] == "ligand-first-structure-intake"
    assert chain["next_request"]["resource_budget"]["max_candidates"] == 1


def test_mcp_unknown_tool_is_explicit_error():
    response = exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "does_not_exist", "arguments": {}},
            }
        ]
    )[0]
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["status"] == "FAIL"


def test_formal_ligand_first_commands_do_not_delegate_to_attachment_planner():
    scientist = (ROOT / ".openscience/agent/covalent-scientist.md").read_text()
    formal_agent = (ROOT / ".openscience/agent/ligand-first-agent.md").read_text()
    start = (ROOT / ".openscience/command/ligand-first-start.md").read_text()
    execute = (ROOT / ".openscience/command/ligand-first-execute.md").read_text()

    assert '"ligand-first-agent": allow' in scientist
    assert '"ligand-first-attachment-planner": allow' not in scientist
    assert "agent: ligand-first-agent" in start
    assert "agent: ligand-first-agent" in execute
    assert "Do not use the legacy Attachment Planner" in formal_agent
    assert "covalent_ligand_first_chain" in execute


def test_mcp_independently_recomputes_real_ligand_first_candidate():
    result = exchange(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "covalent_evaluate_ligand_first_candidate",
                    "arguments": {
                        "project_id": "egfr-c797",
                        "campaign_path": "ligand_first/campaign.json",
                        "candidate_json_path": "ligand_first/generated/candidates.json",
                        "candidate_sdf_path": "ligand_first/generated/candidates.sdf",
                        "geometry_json_path": "ligand_first/geometry/geometry_evaluation.json",
                        "raw_poses_path": "ligand_first/geometry/carsidock_20260817T135020/carsidock.sdf",
                        "no_dummy_poses_path": "ligand_first/geometry/carsidock_20260817T135020/carsidock_nodumat.sdf",
                        "expected_carsidock_run_id": "20260817T135020-carsidock-cov-be42799293",
                    },
                },
            }
        ]
    )[0]["result"]
    assert result["isError"] is False
    evaluation = result["structuredContent"]["independent_evaluation"]
    assert evaluation["status"] == "PASS"
    assert evaluation["decision"] == "ADVANCE"
    assert evaluation["recomputed_chemistry_gate"]["status"] == "PASS"
    assert evaluation["recomputed_geometry"]["passing_pose_count"] == 6
