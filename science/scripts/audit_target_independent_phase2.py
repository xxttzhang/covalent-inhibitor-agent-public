from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from covscreen.research import SCHEMA_MODELS, validate_project


EXPECTED = {
    "egfr-c797": ("GO", "ligand-first"),
    "kras-g12c": ("GO", "ligand-first"),
    "kras-wt-g12-control": ("STOP", "evidence-first"),
}


def check(name: str, passed: bool, evidence: object) -> dict:
    return {"name": name, "status": "PASS" if passed else "FAIL", "evidence": evidence}


def audit(root: Path) -> dict:
    checks: list[dict] = []
    generic_files = (
        root / "src/covscreen/research.py",
        root / "scripts/covalent_project.py",
    )
    hits = {
        str(path.relative_to(root)): [
            token for token in ("BTK", "Cys481", "BTK_C481") if token in path.read_text()
        ]
        for path in generic_files
    }
    checks.append(check("generic_core.no_btk_defaults", not any(hits.values()), hits))

    schema_status = {}
    for name in SCHEMA_MODELS:
        path = root / "schemas" / f"{name}.schema.json"
        try:
            payload = json.loads(path.read_text())
            schema_status[name] = bool(payload.get("$defs") is not None or payload.get("type"))
        except (OSError, json.JSONDecodeError):
            schema_status[name] = False
    checks.append(
        check("contracts.schemas", all(schema_status.values()), schema_status)
    )

    for project_id, expected in EXPECTED.items():
        project_dir = root / "projects" / project_id
        validation = validate_project(project_dir)
        assessment = json.loads(
            (project_dir / "assessment/covalent_assessment.json").read_text()
        )
        route = json.loads((project_dir / "plans/route_plan.json").read_text())
        sources = json.loads(
            (project_dir / "evidence/evidence_register.json").read_text()
        )
        state_db = project_dir / "state/science_state.sqlite3"
        foreign_key_errors = []
        if state_db.is_file():
            with sqlite3.connect(state_db) as connection:
                foreign_key_errors = list(connection.execute("PRAGMA foreign_key_check"))
        evidence = {
            "validation": validation["status"],
            "decision": assessment["decision"],
            "route": route["selected_route"],
            "citations": len(sources),
            "source_types": sorted({item["source_type"] for item in sources}),
            "state_db": state_db.is_file(),
            "foreign_key_errors": len(foreign_key_errors),
        }
        passed = (
            validation["status"] == "PASS"
            and (assessment["decision"], route["selected_route"]) == expected
            and len(sources) >= 3
            and {"uniprot", "pdb", "publication"}.issubset(evidence["source_types"])
            and state_db.is_file()
            and not foreign_key_errors
        )
        checks.append(check(f"project.{project_id}", passed, evidence))

    frozen_manifest = root / "benchmark/frozen/BTK-mini-v1/manifest.json"
    frozen = json.loads(frozen_manifest.read_text())
    checks.append(
        check(
            "benchmark.btk_isolated_and_frozen",
            frozen.get("frozen") is True and frozen.get("status") == "PASS",
            {
                "path": str(frozen_manifest.relative_to(root)),
                "freeze_id": frozen.get("freeze_id"),
                "frozen": frozen.get("frozen"),
                "status": frozen.get("status"),
            },
        )
    )
    passed = sum(item["status"] == "PASS" for item in checks)
    return {
        "phase": "target-independent-covalent-scientist-v2",
        "checks_total": len(checks),
        "checks_passed": passed,
        "checks_failed": len(checks) - passed,
        "status": "PASS" if passed == len(checks) else "FAIL",
        "checks": checks,
    }


def markdown(report: dict) -> str:
    lines = [
        "# 靶点无关共价抑制剂智能体 Phase 2 验收",
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
            "## 结论边界",
            "",
            "该验收证明项目模型、证据链、共价评估、假设卡、路线决策、科学状态库和",
            "Library-first 通用契约可以在非 BTK 分子上下文工作。它不表示已经完成",
            "新的对接筛选、化合物合成、质谱、动力学、细胞或动物实验。",
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
        default=ROOT / "reports/target_independent_phase2_audit.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT / "reports/TARGET_INDEPENDENT_PHASE2_ACCEPTANCE.md",
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
