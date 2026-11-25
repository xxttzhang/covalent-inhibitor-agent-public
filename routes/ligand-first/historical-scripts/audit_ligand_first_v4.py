from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = _bootstrap.PROJECT
sys.path.insert(0, str(ROOT / "src"))

from covscreen.ligand_first import LigandFirstRequest, plan_ligand_first  # noqa: E402
from covscreen.research import CovalentAssessment, RoutePlan, TargetPassport  # noqa: E402
from covscreen.tool_api import plan_ligand_first_tool  # noqa: E402


EXPECTED_STAGES = [
    "project-and-route-gate",
    "noncovalent-pose-acquisition",
    "scaffold-modification-map",
    "attachment-vector-linker-warhead-plan",
    "warhead-linker-generation",
    "covalent-pose-geometry-screen",
    "candidate-pool-handoff",
]


def check(name: str, passed: bool, evidence: Any) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "evidence": evidence}


def load_state(root: Path, project_id: str):
    project = root / "projects" / project_id
    passport = TargetPassport.model_validate(json.loads((project / "target_passport.json").read_text()))
    assessment = CovalentAssessment.model_validate(
        json.loads((project / "assessment/covalent_assessment.json").read_text())
    )
    route = RoutePlan.model_validate(json.loads((project / "plans/route_plan.json").read_text()))
    return passport, assessment, route


def mcp_tool_names(root: Path) -> set[str]:
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}) + "\n"
    result = subprocess.run(
        ["bash", str(root / "ops/run_covalent_tools.sh")],
        cwd=root,
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    response = json.loads(result.stdout.strip())
    return {tool["name"] for tool in response["result"]["tools"]}


def audit(root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    request_path = root / "projects/egfr-c797/plans/ligand_first_request.example.json"
    request = LigandFirstRequest.model_validate(json.loads(request_path.read_text()))
    passport, assessment, route = load_state(root, "egfr-c797")
    plan = plan_ligand_first(request, passport, assessment, route)

    checks.append(
        check(
            "route.documented_stage_order",
            [stage.stage_id for stage in plan.stages] == EXPECTED_STAGES,
            [stage.stage_id for stage in plan.stages],
        )
    )
    checks.append(
        check(
            "route.ready_for_controlled_generation",
            plan.decision == "READY_FOR_GENERATION"
            and bool(plan.eligible_attachment_ranks)
            and plan.stages[4].requires_human_approval
            and plan.stages[4].status == "planned",
            {
                "decision": plan.decision,
                "eligible_attachment_ranks": plan.eligible_attachment_ranks,
                "generation_stage": plan.stages[4].model_dump(mode="json"),
            },
        )
    )
    candidate = plan.attachment_plan.candidates[0]
    checks.append(
        check(
            "attachment.complete_joint_evidence",
            candidate.evidence_complete
            and candidate.distance_to_residue_angstrom is not None
            and candidate.vector_to_target_angle_degrees is not None
            and candidate.steric_clash_fraction is not None
            and candidate.interaction_loss_fraction is not None
            and candidate.synthetic_accessibility_score is not None
            and candidate.linker_necessity in {"direct_attachment", "short_spacer", "linker_required"},
            candidate.model_dump(mode="json"),
        )
    )
    incomplete = request.model_copy(
        update={
            "attachment": request.attachment.model_copy(
                update={"pose_source": "none", "pose_reference": None}
            )
        }
    )
    incomplete_plan = plan_ligand_first(incomplete, passport, assessment, route)
    checks.append(
        check(
            "gates.missing_pose",
            incomplete_plan.decision == "NEEDS_NONCOVALENT_POSE"
            and incomplete_plan.stages[1].status == "ready"
            and incomplete_plan.stages[2].status == "blocked",
            {
                "decision": incomplete_plan.decision,
                "pose_stage": incomplete_plan.stages[1].status,
                "scaffold_stage": incomplete_plan.stages[2].status,
            },
        )
    )
    geometry_evidence = set(plan.stages[5].completion_evidence)
    checks.append(
        check(
            "screen.carsidock_geometry_contract",
            plan.stages[5].tools == ["CarsiDock-Cov"]
            and {
                "scaffold core RMSD",
                "parent-interaction retention",
                "warhead distance and attack angle",
                "linker and ligand strain",
                "protein clash and pose-ensemble consistency",
            }.issubset(geometry_evidence),
            plan.stages[5].model_dump(mode="json"),
        )
    )
    tool_result = plan_ligand_first_tool(
        root,
        "projects/egfr-c797/plans/ligand_first_request.example.json",
    )
    names = mcp_tool_names(root)
    checks.append(
        check(
            "openscience.ligand_first_tool",
            tool_result.get("status") == "PASS"
            and tool_result.get("plan", {}).get("decision") == "READY_FOR_GENERATION"
            and "covalent_plan_ligand_first" in names,
            {"tool_result": tool_result.get("plan", {}).get("decision"), "mcp_tools": sorted(names)},
        )
    )
    agent_text = (root / ".openscience/agent/ligand-first-attachment-planner.md").read_text()
    command_text = (root / ".openscience/command/attachment-plan.md").read_text()
    checks.append(
        check(
            "openscience.agent_follows_full_route",
            "covalent_plan_ligand_first" in agent_text
            and "CarsiDock-Cov" in agent_text
            and "controlled generation" in command_text
            and "attachment ranking alone" in (
                root / ".openscience/agent/covalent-scientist.md"
            ).read_text(),
            {
                "agent": ".openscience/agent/ligand-first-attachment-planner.md",
                "command": ".openscience/command/attachment-plan.md",
            },
        )
    )
    expected_schemas = {
        "ligand-first-generation-policy.schema.json",
        "covalent-geometry-policy.schema.json",
        "ligand-first-request.schema.json",
        "ligand-first-stage.schema.json",
        "ligand-first-plan.schema.json",
    }
    observed_schemas = {path.name for path in (root / "schemas").glob("*.schema.json")}
    checks.append(
        check(
            "contracts.ligand_first_schemas",
            expected_schemas.issubset(observed_schemas),
            sorted(expected_schemas & observed_schemas),
        )
    )

    passed = sum(item["status"] == "PASS" for item in checks)
    return {
        "phase": "documented-ligand-first-covalentization-v4",
        "source_specification": "共价抑制剂AIDD智能体三路线工具流程.md sections 4 and 6-9",
        "checks_total": len(checks),
        "checks_passed": passed,
        "checks_failed": len(checks) - passed,
        "status": "PASS" if passed == len(checks) else "FAIL",
        "checks": checks,
        "boundary": (
            "The route planner and evidence gates are implemented. Molecular enumeration, "
            "DiffLinker/REINVENT4 generation, CarsiDock-Cov execution, common downstream "
            "evaluation, and experiments remain separate human-approved execution work."
        ),
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# 文档化 Ligand-first Covalentization V4 验收",
        "",
        f"- 总状态：**{report['status']}**",
        f"- 检查：{report['checks_passed']}/{report['checks_total']} PASS",
        f"- 路线来源：`{report['source_specification']}`",
        "",
        "| 检查 | 状态 |",
        "|---|---|",
    ]
    lines.extend(f"| `{item['name']}` | {item['status']} |" for item in report["checks"])
    lines.extend(["", "## 当前边界", "", report["boundary"], ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT / "reports/LIGAND_FIRST_V4_AUDIT.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT / "reports/LIGAND_FIRST_V4_ACCEPTANCE.md",
    )
    args = parser.parse_args()
    report = audit(args.root.resolve())
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    args.markdown_output.write_text(markdown(report))
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
