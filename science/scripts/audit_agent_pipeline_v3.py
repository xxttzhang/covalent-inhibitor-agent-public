from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from covscreen.tool_api import (  # noqa: E402
    next_library_action_tool,
    plan_attachments_tool,
    plan_library_cascade_tool,
    runtime_preflight,
)


EXPECTED_MCP_TOOLS = {
    "covalent_capabilities",
    "covalent_validate_project",
    "covalent_runtime_preflight",
    "covalent_plan_library_cascade",
    "covalent_next_library_action",
    "covalent_plan_attachments",
    "covalent_plan_ligand_first",
    "covalent_plan_de_novo",
}


def check(name: str, passed: bool, evidence: Any) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "evidence": evidence}


def mcp_exchange(root: Path, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload = "".join(json.dumps(message) + "\n" for message in messages)
    result = subprocess.run(
        ["bash", str(root / "ops" / "run_covalent_tools.sh")],
        cwd=root,
        input=payload,
        text=True,
        capture_output=True,
        timeout=60,
        check=True,
    )
    if result.stderr.strip():
        raise RuntimeError(f"MCP server wrote to stderr: {result.stderr.strip()}")
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def audit(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    config = json.loads((root / "openscience.json").read_text())
    mcp = config.get("mcp", {}).get("covalent-discovery", {})
    checks.append(
        check(
            "openscience.mcp_config",
            mcp.get("type") == "local"
            and mcp.get("command") == ["bash", "ops/run_covalent_tools.sh"],
            mcp,
        )
    )

    agent_results: dict[str, Any] = {}
    for filename in (
        "library-first-agent.md",
        "ligand-first-attachment-planner.md",
        "covalent-scientist.md",
    ):
        path = root / ".openscience" / "agent" / filename
        text = path.read_text() if path.is_file() else ""
        agent_results[filename] = {
            "exists": path.is_file(),
            "mcp_allowed": "  mcp: allow" in text,
            "model": "model: boyue-kimi/kimi-k3" in text,
        }
    checks.append(
        check(
            "openscience.agent_mcp_permissions",
            all(
                item["exists"] and item["mcp_allowed"] and item["model"]
                for item in agent_results.values()
            ),
            agent_results,
        )
    )

    responses = mcp_exchange(
        root,
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
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "covalent_runtime_preflight", "arguments": {}},
            },
        ],
    )
    listed = {item["name"] for item in responses[1]["result"]["tools"]}
    validation = responses[2]["result"]["structuredContent"]
    mcp_preflight = responses[3]["result"]["structuredContent"]
    checks.append(
        check(
            "mcp.protocol_and_tools",
            responses[0]["result"]["serverInfo"]["name"] == "covalent-discovery-tools"
            and listed == EXPECTED_MCP_TOOLS,
            {"server": responses[0]["result"]["serverInfo"], "tools": sorted(listed)},
        )
    )
    checks.append(
        check(
            "mcp.project_validation_call",
            validation.get("status") == "PASS"
            and validation.get("site_id") == "EGFR_C797"
            and validation.get("route") == "ligand-first",
            validation,
        )
    )
    checks.append(
        check(
            "mcp.runtime_preflight_call",
            mcp_preflight.get("status") == "PASS"
            and mcp_preflight.get("cuda", {}).get("device_count") == 2
            and mcp_preflight.get("safety", {}).get("ld_preload_present") is False
            and mcp_preflight.get("safety", {}).get("schrodinger_license_inherited") is False
            and mcp_preflight.get("safety", {}).get("expensive_execution_enabled") is False,
            mcp_preflight,
        )
    )

    smoke_path = root / "reports" / "OPENSCIENCE_LIVE_AGENT_SMOKE_2026-08-17.jsonl"
    smoke_events = [
        json.loads(line) for line in smoke_path.read_text().splitlines() if line.strip()
    ]
    tool_events = [event for event in smoke_events if event.get("type") == "tool_use"]
    live_tool = tool_events[0] if tool_events else {}
    live_state = live_tool.get("part", {}).get("state", {})
    try:
        live_output = json.loads(live_state.get("output", "{}"))
    except json.JSONDecodeError:
        live_output = {}
    checks.append(
        check(
            "openscience.live_agent_mcp_call",
            live_tool.get("part", {}).get("tool")
            == "covalent-discovery_covalent_capabilities"
            and live_state.get("status") == "completed"
            and live_output.get("status") == "PASS"
            and live_output.get("server") == "covalent-discovery-tools",
            {
                "trace": str(smoke_path.relative_to(root)),
                "session_id": live_tool.get("sessionID"),
                "tool": live_tool.get("part", {}).get("tool"),
                "state": live_state.get("status"),
                "server": live_output.get("server"),
            },
        )
    )

    preflight = runtime_preflight(root)
    engines = preflight["engines"]
    checks.append(
        check(
            "runtime.two_gpu_and_five_engine_assets",
            preflight["status"] == "PASS"
            and preflight["cuda"]["device_count"] == 2
            and all(
                engines[name]
                for name in (
                    "carsidock_cov",
                    "rtmscore",
                    "alphafold3_mpae",
                    "covdock_vs",
                    "covdock_lo",
                    "schrodinger_license_ok",
                )
            ),
            preflight,
        )
    )

    cascade = plan_library_cascade_tool(
        root,
        "projects/egfr-c797/plans/library_first_request.example.json",
        library_size=5000,
        route_override_approved=True,
        allow_proprietary=True,
        require_inputs=False,
    )
    plan = cascade["plan"]
    stages = {item["stage_id"]: item for item in plan["stages"]}
    expected_stages = {
        "intake",
        "primary-screen",
        "pose-rescore",
        "uncertainty-adjudication",
        "precision-refinement",
    }
    checks.append(
        check(
            "library_first.adaptive_cascade",
            cascade["status"] == "PASS"
            and plan["decision"] in {"READY", "CONDITIONAL"}
            and set(stages) == expected_stages
            and stages["uncertainty-adjudication"]["max_candidates"] <= 100
            and stages["precision-refinement"]["max_candidates"] <= 50
            and stages["pose-rescore"]["input_scope"].startswith("successful CarsiDock poses"),
            {
                "decision": plan["decision"],
                "stages": stages,
                "invariants": plan["invariants"],
            },
        )
    )

    next_action = next_library_action_tool(
        plan,
        {
            "completed_stage_id": "pose-rescore",
            "input_count": 5000,
            "success_count": 4500,
            "failed_count": 500,
            "rank_disagreement": 0.50,
            "geometry_pass_fraction": 0.40,
            "unresolved_candidate_count": 200,
            "remaining_gpu_hours": 100,
            "explicit_failure_table": True,
        },
    )["next_action"]
    checks.append(
        check(
            "library_first.uncertainty_escalation",
            next_action["action"] == "run"
            and next_action["stage_id"] == "uncertainty-adjudication"
            and next_action["max_candidates"] <= 100,
            next_action,
        )
    )

    missing_geometry = plan_attachments_tool(
        root,
        {
            "project_id": "egfr-c797",
            "ligand_id": "acceptance-fixture",
            "ligand_smiles": "COc1ccc(NC(C)=O)cc1",
            "max_plans": 5,
        },
    )["attachment_plan"]
    with_geometry = plan_attachments_tool(
        root,
        {
            "project_id": "egfr-c797",
            "ligand_id": "acceptance-fixture",
            "ligand_smiles": "CCOc1ccccc1",
            "pose_source": "experimental",
            "pose_reference": "PDB:test-fixture",
            "scaffold_core_atom_indices": [3, 4, 5, 6, 7, 8],
            "key_interaction_atom_indices": [2],
            "attachment_atom_indices": [4],
            "protected_atom_indices": [2],
            "solvent_exposed_atom_indices": [4],
            "distance_to_target_angstrom": {"4": 5.5},
            "vector_to_target_angle_degrees": {"4": 20.0},
            "steric_clash_fraction": {"4": 0.1},
            "interaction_retention_fraction": {"4": 0.85},
            "synthetic_accessibility_score": {"4": 0.75},
            "warhead_names": ["acrylamide"],
        },
    )["attachment_plan"]
    negative_control = plan_attachments_tool(
        root,
        {
            "project_id": "kras-wt-g12-control",
            "ligand_id": "negative-control",
            "ligand_smiles": "CCO",
            "route_override_approved": True,
        },
    )["attachment_plan"]
    checks.append(
        check(
            "ligand_first.attachment_gates",
            missing_geometry["status"] == "NEEDS_STRUCTURE"
            and bool(missing_geometry["candidates"])
            and with_geometry["status"] == "READY"
            and with_geometry["candidates"][0]["confidence"] == "high"
            and with_geometry["candidates"][0]["evidence_complete"] is True
            and negative_control["status"] == "STOP"
            and not negative_control["candidates"],
            {
                "missing_geometry": missing_geometry,
                "with_geometry": with_geometry,
                "negative_control": negative_control,
            },
        )
    )

    wrappers = {
        path.name: path.read_text()
        for path in (root / "ops" / "run_covalent_tools.sh", root / "ops" / "start_openscience.sh")
    }
    checks.append(
        check(
            "safety.clean_process_boundary",
            all("unset LD_PRELOAD" in text for text in wrappers.values())
            and all("SCHROD_LICENSE_FILE" in text for text in wrappers.values()),
            {name: "explicit environment cleanup present" for name in wrappers},
        )
    )

    old_reports = {
        "phase2": json.loads((root / "reports/target_independent_phase2_audit.json").read_text()).get("status"),
        "static": json.loads(
            (root / "reports/BTK-mini/static_scientist_v1_audit.json").read_text()
        ).get("status"),
        "benchmark": json.loads((root / "reports/BTK-mini/five_engine_validation.json").read_text()).get("status"),
        "frozen": json.loads((root / "reports/BTK-mini/frozen_benchmark_validation.json").read_text()).get("status"),
    }
    checks.append(
        check(
            "regression.existing_acceptance_reports",
            all(status == "PASS" for status in old_reports.values()),
            old_reports,
        )
    )

    passed = sum(item["status"] == "PASS" for item in checks)
    return {
        "phase": "openscience-agent-pipeline-v3",
        "checks_total": len(checks),
        "checks_passed": passed,
        "checks_failed": len(checks) - passed,
        "status": "PASS" if passed == len(checks) else "FAIL",
        "checks": checks,
        "scientific_boundary": (
            "This acceptance proves migration, read-only planning tools, agent configuration, "
            "adaptive allocation, and attachment-hypothesis gates. It does not claim a new "
            "screen, designed compound, synthesis, potency, selectivity, or wet-lab validation."
        ),
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# OpenScience 共价抑制剂智能体 Pipeline V3 验收",
        "",
        f"- 总状态：**{report['status']}**",
        f"- 检查：{report['checks_passed']}/{report['checks_total']} PASS",
        "",
        "| 检查 | 状态 |",
        "|---|---|",
    ]
    lines.extend(f"| `{item['name']}` | {item['status']} |" for item in report["checks"])
    lines.extend(
        [
            "",
            "## 科学边界",
            "",
            report["scientific_boundary"],
            "",
            "## 关键结论",
            "",
            "- OpenScience 通过本地 MCP 接入 8 个只读共价研发工具。",
            "- 真实 Kimi/OpenScience 会话已完成一次 `covalent-discovery_covalent_capabilities` 调用并返回 `PASS`。",
            "- Library-first 已实现按失败、分歧、几何证据和 GPU 预算自适应的五阶段级联。",
            "- Ligand-first 已按项目文档扩展为非共价 pose、scaffold map、attachment/vector/linker/warhead、受控生成、CarsiDock-Cov 精筛和候选池交接；缺少证据时停在相应 NEEDS_* 门控。",
            "- BTK-mini 仍只是五引擎回归基准，不作为新智能体的默认靶点或发现成果。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT / "reports/OPENSCIENCE_AGENT_PIPELINE_V3_AUDIT.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT / "reports/OPENSCIENCE_AGENT_PIPELINE_V3_ACCEPTANCE.md",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    report = audit(root)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    args.markdown_output.write_text(markdown(report))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
