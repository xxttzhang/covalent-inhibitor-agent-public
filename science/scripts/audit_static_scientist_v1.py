from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
import urllib.request
from pathlib import Path
from typing import Any


EXPECTED_AGENTS = {
    "covalent-scientist.md": "primary",
    "library-first-operator.md": "subagent",
    "evidence-curator.md": "subagent",
    "benchmark-auditor.md": "subagent",
}
EXPECTED_SKILLS = {
    "library-first-benchmark",
    "scientific-state-graph",
}
EXPECTED_ENGINES = {
    "carsidock": (420, 318, 102, "lower_is_better"),
    "rtmscore": (420, 318, 102, "higher_is_better"),
    "af3": (420, 417, 3, "lower_is_better"),
    "covdock_vs": (420, 412, 8, "lower_is_better"),
    "covdock_lo": (420, 413, 7, "lower_is_better"),
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def frontmatter_value(text: str, key: str) -> str | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 4)
    if end < 0:
        return None
    for line in text[4:end].splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() == key:
            return value.strip().strip("\"'")
    return None


def check(
    checks: list[dict[str, Any]],
    name: str,
    condition: bool,
    evidence: Any,
) -> None:
    checks.append(
        {
            "name": name,
            "status": "PASS" if condition else "FAIL",
            "evidence": evidence,
        }
    )


def audit_config(root: Path, checks: list[dict[str, Any]]) -> None:
    config = load_json(root / "openscience.json")
    permissions = config.get("permission", {})
    sandbox = config.get("sandbox", {})
    provider = config.get("provider", {}).get("boyue-kimi", {})
    provider_options = provider.get("options", {})
    check(
        checks,
        "openscience.default_agent",
        config.get("default_agent") == "covalent-scientist",
        config.get("default_agent"),
    )
    check(checks, "permission.edit_denied", permissions.get("edit") == "deny", permissions)
    check(checks, "permission.bash_requires_approval", permissions.get("bash") == "ask", permissions)
    check(
        checks,
        "permission.external_directory_denied",
        permissions.get("external_directory") == "deny",
        permissions,
    )
    check(
        checks,
        "sandbox.fail_closed",
        sandbox.get("enabled") is True and sandbox.get("onUnavailable") == "error",
        sandbox,
    )
    check(
        checks,
        "model.provider_configured_without_embedded_key",
        config.get("model") == "boyue-kimi/kimi-k3"
        and provider.get("npm") == "@ai-sdk/openai-compatible"
        and provider_options.get("baseURL") == "https://apicz.boyuerichdata.com/v1/"
        and "kimi-k3" in provider.get("models", {})
        and "apiKey" not in provider_options,
        {
            "default_model": config.get("model"),
            "provider": provider.get("name"),
            "baseURL": provider_options.get("baseURL"),
            "api_key_embedded": "apiKey" in provider_options,
        },
    )


def audit_agents(root: Path, checks: list[dict[str, Any]]) -> None:
    agent_dir = root / ".openscience" / "agent"
    for filename, expected_mode in EXPECTED_AGENTS.items():
        path = agent_dir / filename
        exists = path.is_file()
        text = path.read_text() if exists else ""
        mode = frontmatter_value(text, "mode")
        check(
            checks,
            f"agent.{filename}",
            exists
            and mode == expected_mode
            and frontmatter_value(text, "model") == "boyue-kimi/kimi-k3"
            and "  edit: deny" in text
            and "automatic" not in text.lower(),
            {
                "exists": exists,
                "mode": mode,
                "model": frontmatter_value(text, "model"),
                "edit_denied": "  edit: deny" in text,
            },
        )


def audit_skills(root: Path, checks: list[dict[str, Any]]) -> None:
    skill_root = root / ".claude" / "skills"
    observed: set[str] = set()
    for path in skill_root.glob("*/SKILL.md"):
        name = frontmatter_value(path.read_text(), "name")
        if name:
            observed.add(name)
    check(
        checks,
        "skills.project_skills_present",
        EXPECTED_SKILLS.issubset(observed),
        sorted(observed),
    )


def audit_library_first(root: Path, checks: list[dict[str, Any]]) -> None:
    report = load_json(root / "reports" / "BTK-mini" / "five_engine_validation.json")
    check(checks, "library_first.overall", report.get("status") == "PASS", report.get("status"))
    library = report.get("library", {})
    check(
        checks,
        "library_first.library",
        library.get("rows") == 420
        and library.get("active") == 20
        and library.get("decoy") == 400
        and library.get("duplicate_compound_id") == 0
        and library.get("duplicate_input_sdf_index") == 0,
        library,
    )
    engines = report.get("engines", {})
    for engine, expected in EXPECTED_ENGINES.items():
        result = engines.get(engine, {})
        rows, success, failure, direction = expected
        condition = (
            result.get("status") == "PASS"
            and result.get("rows") == rows
            and result.get("successful_rows") == success
            and result.get("failed_rows") == failure
            and result.get("score_directions") == [direction]
        )
        check(checks, f"library_first.engine.{engine}", condition, result)


def audit_freeze(root: Path, checks: list[dict[str, Any]]) -> None:
    manifest = load_json(
        root / "benchmark" / "frozen" / "BTK-mini-v1" / "manifest.json"
    )
    verification = load_json(
        root / "reports" / "BTK-mini" / "frozen_benchmark_validation.json"
    )
    check(
        checks,
        "benchmark.manifest",
        manifest.get("frozen") is True
        and manifest.get("status") == "PASS"
        and len(manifest.get("artifacts", [])) == 15
        and len(manifest.get("engine_audits", [])) == 5,
        {
            "freeze_id": manifest.get("freeze_id"),
            "artifacts": len(manifest.get("artifacts", [])),
            "engines": len(manifest.get("engine_audits", [])),
        },
    )
    artifact_checks = verification.get("artifact_checks", [])
    engine_checks = verification.get("engine_audits", [])
    check(
        checks,
        "benchmark.verification",
        verification.get("status") == "PASS"
        and len(artifact_checks) == 15
        and all(item.get("status") == "PASS" for item in artifact_checks)
        and len(engine_checks) == 5
        and all(item.get("status") == "PASS" for item in engine_checks),
        {
            "status": verification.get("status"),
            "artifact_pass": sum(item.get("status") == "PASS" for item in artifact_checks),
            "engine_pass": sum(item.get("status") == "PASS" for item in engine_checks),
        },
    )


def audit_state(root: Path, checks: list[dict[str, Any]]) -> None:
    db_path = root / "state" / "science_state.sqlite3"
    expected_counts = {
        "entities": 9,
        "evidence": 16,
        "relations": 8,
        "artifacts": 15,
        "benchmark_runs": 5,
    }
    observed: dict[str, int] = {}
    foreign_key_errors: list[Any] = []
    with sqlite3.connect(db_path) as connection:
        for table in expected_counts:
            observed[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    check(
        checks,
        "state_graph.database",
        observed == expected_counts and not foreign_key_errors,
        {"counts": observed, "foreign_key_errors": len(foreign_key_errors)},
    )
    export = load_json(root / "state" / "exports" / "evidence_graph.json")
    export_tables = export.get("tables", {})
    check(
        checks,
        "state_graph.export",
        all(
            len(export_tables.get(table, [])) == count
            for table, count in expected_counts.items()
        ),
        {table: len(export_tables.get(table, [])) for table in expected_counts},
    )


def audit_static_boundary(root: Path, checks: list[dict[str, Any]]) -> None:
    schema = load_json(root / "schemas" / "evolution-record.schema.json")
    human_approval = schema.get("properties", {}).get("human_approval", {})
    check(
        checks,
        "static_phase.no_auto_evolution",
        human_approval.get("const") is False,
        human_approval,
    )


def audit_credential(auth_file: Path | None, checks: list[dict[str, Any]]) -> None:
    if auth_file is None:
        return
    auth = load_json(auth_file)
    file_mode = stat.S_IMODE(auth_file.stat().st_mode)
    credential = auth.get("boyue-kimi", {})
    check(
        checks,
        "model.credential_secured",
        file_mode == 0o600
        and credential.get("type") == "api"
        and bool(credential.get("key")),
        {
            "path": str(auth_file),
            "mode": oct(file_mode),
            "providers": sorted(auth),
            "credential_type": credential.get("type"),
            "key_present": bool(credential.get("key")),
        },
    )


def audit_model_smoke(root: Path, checks: list[dict[str, Any]]) -> None:
    path = root / "reports" / "BTK-mini" / "kimi_readonly_smoke.jsonl"
    events = [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    tools = [
        event.get("part", {}).get("tool")
        for event in events
        if event.get("type") == "tool_use"
    ]
    texts = [
        event.get("part", {}).get("text", "")
        for event in events
        if event.get("type") == "text"
    ]
    final_text = "\n".join(texts)
    expected_fragments = (
        "BTK-mini-v1.0.0-4724d9a707a2",
        "420",
        "active=20",
        "decoy=400",
        "成功 318，失败 102",
        "成功 417，失败 3",
        "成功 412，失败 8",
        "成功 413，失败 7",
        "未用 bash 重算",
    )
    check(
        checks,
        "model.readonly_smoke",
        tools == ["read", "read"]
        and all(fragment in final_text for fragment in expected_fragments),
        {
            "trace": str(path.relative_to(root)),
            "tools": tools,
            "final_text_present": bool(final_text),
            "expected_fragments_present": sum(
                fragment in final_text for fragment in expected_fragments
            ),
            "expected_fragments_total": len(expected_fragments),
        },
    )
    default_path = (
        root / "reports" / "BTK-mini" / "kimi_default_model_smoke.jsonl"
    )
    default_events = [
        json.loads(line)
        for line in default_path.read_text().splitlines()
        if line.strip()
    ]
    default_tools = [
        event.get("part", {}).get("tool")
        for event in default_events
        if event.get("type") == "tool_use"
    ]
    default_texts = [
        event.get("part", {}).get("text", "")
        for event in default_events
        if event.get("type") == "text"
    ]
    check(
        checks,
        "model.default_agent_and_model_smoke",
        not default_tools
        and default_texts
        and default_texts[-1].strip() == "默认智能体与 Kimi K3 调用正常。",
        {
            "trace": str(default_path.relative_to(root)),
            "tools": default_tools,
            "final_text": default_texts[-1].strip() if default_texts else None,
        },
    )


def audit_server(url: str, checks: list[dict[str, Any]]) -> None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read(4096).decode(errors="replace")
            status = response.status
        check(
            checks,
            "openscience.http",
            status == 200 and "<title>OpenScience</title>" in body,
            {"url": url, "http_status": status},
        )
    except Exception as exc:
        check(
            checks,
            "openscience.http",
            False,
            {"url": url, "error": f"{type(exc).__name__}: {exc}"},
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit all static trusted scientist V1 acceptance gates."
    )
    parser.add_argument("--project-root", type=Path, default=Path("."))
    parser.add_argument("--server-url", default="http://127.0.0.1:4096/")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--auth-file",
        type=Path,
        help="Optional OpenScience auth.json; reports presence and mode, never the key.",
    )
    args = parser.parse_args()

    root = args.project_root.resolve()
    checks: list[dict[str, Any]] = []
    try:
        audit_config(root, checks)
        audit_agents(root, checks)
        audit_skills(root, checks)
        audit_library_first(root, checks)
        audit_freeze(root, checks)
        audit_state(root, checks)
        audit_static_boundary(root, checks)
        audit_credential(args.auth_file, checks)
        audit_model_smoke(root, checks)
        audit_server(args.server_url, checks)
    except Exception as exc:
        checks.append(
            {
                "name": "audit.execution",
                "status": "FAIL",
                "evidence": f"{type(exc).__name__}: {exc}",
            }
        )

    failures = [item for item in checks if item["status"] != "PASS"]
    report = {
        "phase": "static-trusted-scientist-v1",
        "project_root": str(root),
        "checks_total": len(checks),
        "checks_passed": len(checks) - len(failures),
        "checks_failed": len(failures),
        "status": "PASS" if not failures else "FAIL",
        "checks": checks,
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
