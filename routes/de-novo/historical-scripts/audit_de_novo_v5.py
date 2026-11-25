from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = _bootstrap.PROJECT
sys.path.insert(0, str(ROOT / "src"))

from covscreen.de_novo import DeNovoGeneratorAvailability, DeNovoRequest, plan_de_novo  # noqa: E402
from covscreen.research import CovalentAssessment, RoutePlan, TargetPassport  # noqa: E402
from covscreen.tool_api import capabilities_tool, plan_de_novo_tool  # noqa: E402

EXPECTED_STAGES = [
    "project-and-route-gate", "pocket-definition",
    "pocket-conditioned-3d-scaffold-generation",
    "target-residue-warhead-constrained-generation",
    "linker-rgroup-multiobjective-optimization", "carsidock-cov-pose-screen",
    "candidate-pool-handoff",
]


def check(name: str, passed: bool, evidence: Any) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "evidence": evidence}


def audit(root: Path) -> dict[str, Any]:
    project = root / "projects/egfr-c797"
    passport = TargetPassport.model_validate(json.loads((project / "target_passport.json").read_text()))
    assessment = CovalentAssessment.model_validate(json.loads((project / "assessment/covalent_assessment.json").read_text()))
    route = RoutePlan.model_validate(json.loads((project / "plans/route_plan.json").read_text()))
    request = DeNovoRequest.model_validate(json.loads((project / "plans/de_novo_request.example.json").read_text()))
    availability = DeNovoGeneratorAvailability(
        pocketxmol=True, covalentizer_rdkit=True, rdkit=True, reinvent4=True,
        difflinker=True, carsidock_cov=True,
    )
    plan = plan_de_novo(request, passport, assessment, route, availability)
    checks = [
        check("route.documented_stage_order", [s.stage_id for s in plan.stages] == EXPECTED_STAGES, [s.stage_id for s in plan.stages]),
        check("pocket.complete_generation_region", request.pocket.evidence_complete(passport.site.site_id), request.pocket.model_dump(mode="json")),
        check("generation.one_primary_generator", plan.stages[2].tools == ["pocketxmol"] and plan.stages[2].requires_human_approval, plan.stages[2].model_dump(mode="json")),
        check(
            "chemistry.warhead_reaction_mapping",
            plan.stages[3].tools == ["covalentizer_rdkit", "RDKit SMARTS"]
            and "reaction atom mapping" in plan.stages[3].outputs
            and "not a learned CovaGEN substitute" in plan.stages[3].rationale[0],
            plan.stages[3].model_dump(mode="json"),
        ),
        check("optimization.anti_reward_hacking", request.objectives.require_score_component_logging and request.objectives.require_structure_alert_reasons and request.objectives.reactivity_risk_penalty > 0 and "no single-score winner" in plan.stages[4].completion_evidence, plan.stages[4].model_dump(mode="json")),
        check("screen.carsidock_and_pool_handoff", plan.stages[5].tools == ["CarsiDock-Cov"] and plan.stages[6].stage_id == "candidate-pool-handoff", {"screen": plan.stages[5].status, "handoff": plan.stages[6].status}),
    ]
    current = plan_de_novo_tool(root, "projects/egfr-c797/plans/de_novo_request.example.json")
    current_plan = current.get("plan", {})
    current_availability = current_plan.get("availability", {})
    checks.append(
        check(
            "assets.truthful_current_gate",
            current_plan.get("decision") == "READY_FOR_SCAFFOLD_GENERATION"
            and current_plan.get("missing_assets") == []
            and current_availability.get("covagen") is False
            and current_availability.get("covalentizer_rdkit") is True,
            current_plan,
        )
    )
    schemas = {p.name for p in (root / "schemas").glob("*.schema.json")}
    expected = {"pocket-definition.schema.json", "de-novo-generator-availability.schema.json", "de-novo-generation-policy.schema.json", "multi-objective-policy.schema.json", "de-novo-request.schema.json", "de-novo-stage.schema.json", "de-novo-plan.schema.json"}
    capabilities = capabilities_tool(root)
    checks.append(check("openscience.tool_agent_schemas", "covalent_plan_de_novo" in capabilities["tools"] and (root / ".openscience/agent/de-novo-generation-planner.md").is_file() and expected.issubset(schemas), {"tools": capabilities["tools"], "schemas": sorted(expected & schemas)}))
    passed = sum(item["status"] == "PASS" for item in checks)
    return {
        "phase": "documented-de-novo-generation-v5",
        "source_specification": "共价抑制剂AIDD智能体三路线工具流程.md sections 5-9",
        "checks_total": len(checks), "checks_passed": passed,
        "checks_failed": len(checks) - passed,
        "status": "PASS" if passed == len(checks) else "FAIL", "checks": checks,
        "boundary": "The route contracts and asset gates are operational. PocketXMol, deterministic RDKit covalent enumeration, REINVENT4, DiffLinker, and CarsiDock-Cov are configured; CovaGEN remains unavailable and is not claimed. Model generation and docking remain human-approved execution work.",
    }


def markdown(report: dict[str, Any]) -> str:
    lines = ["# 文档化 De novo Generation V5 验收", "", f"- 总状态：**{report['status']}**", f"- 检查：{report['checks_passed']}/{report['checks_total']} PASS", f"- 路线来源：`{report['source_specification']}`", "", "| 检查 | 状态 |", "|---|---|"]
    lines.extend(f"| `{item['name']}` | {item['status']} |" for item in report["checks"])
    lines.extend(["", "## 当前边界", "", report["boundary"], ""])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--json-output", type=Path, default=ROOT / "reports/DE_NOVO_V5_AUDIT.json")
    parser.add_argument("--markdown-output", type=Path, default=ROOT / "reports/DE_NOVO_V5_ACCEPTANCE.md")
    args = parser.parse_args()
    report = audit(args.root)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    args.markdown_output.write_text(markdown(report))
    print(json.dumps({k: report[k] for k in ("status", "checks_passed", "checks_total")}, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
