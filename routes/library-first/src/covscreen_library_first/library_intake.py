from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict


IntakeState = Literal[
    "READY",
    "AMBIGUOUS_TARGET",
    "TARGET_NOT_REGISTERED",
    "SITE_MISMATCH",
    "CAMPAIGN_NOT_CONFIGURED",
    "SCOPE_NOT_CONFIGURED",
    "INPUTS_MISSING",
    "UNSUPPORTED_LIBRARY",
]


class LibraryFirstIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    intent: Literal["library-first"] = "library-first"
    state: IntakeState
    source_library: str
    requested_scope: Literal["pilot", "full"]
    query: str
    target: dict[str, Any] | None
    target_inferred: bool
    campaign_config_path: str | None
    missing_inputs: list[str]
    supported_targets: list[dict[str, Any]]
    approval_required: bool
    next_action: str


def _read_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text()) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML document must be an object: {path}")
    return payload


def _scope(query: str, requested: str) -> Literal["pilot", "full"]:
    if requested == "pilot":
        return "pilot"
    if requested == "full":
        return "full"
    return "full" if re.search(r"(?i)全库|完整库|full[ -]?library|production", query) else "pilot"


def _site_number(query: str) -> int | None:
    patterns = (
        r"(?i)\bG\s*(\d+)\s*C\b",
        r"(?i)\b(?:CYS|C)\s*[-_:]?\s*(\d+)\b",
        r"(?i)半胱氨酸\s*(\d+)",
    )
    values = {
        int(match.group(1))
        for pattern in patterns
        for match in re.finditer(pattern, query)
    }
    if len(values) > 1:
        raise ValueError(f"multiple reactive-site numbers were supplied: {sorted(values)}")
    return next(iter(values), None)


def _targets(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    records = []
    for path in sorted((root / "configs" / "targets").glob("*.yaml")):
        payload = _read_yaml(path)
        required = {
            "target_id",
            "site_id",
            "target_regex",
            "protein_chain",
            "residue_name",
            "residue_number",
            "nucleophilic_atom",
            "reaction_type",
            "warhead_class",
        }
        if required.issubset(payload):
            records.append((path, payload))
    return records


def _campaigns(root: Path, target: dict[str, Any], scope: str) -> list[Path]:
    matched = []
    for path in sorted((root / "configs" / "library_first").glob("*.yaml")):
        payload = _read_yaml(path)
        target_path = root / str(payload.get("target_config", ""))
        linked = _read_yaml(target_path) if target_path.is_file() else {}
        same_target = str(linked.get("target_id", "")).upper() == str(target["target_id"]).upper()
        same_site = str(linked.get("site_id", "")).upper() == str(target["site_id"]).upper()
        campaign_id = str(payload.get("campaign_id", "")).lower()
        scope_match = (scope == "pilot" and "pilot" in campaign_id) or (
            scope == "full" and "pilot" not in campaign_id
        )
        if same_target and same_site and scope_match:
            matched.append(path)
    return matched


def _missing(root: Path, campaign: Path) -> list[str]:
    payload = _read_yaml(campaign)
    paths = {
        "target_config": payload.get("target_config"),
        "protein_structure": payload.get("pdb"),
        "reference_ligand": payload.get("reference_ligand"),
        "af3_protein_seed": payload.get("af3_source_data_json"),
        "covdock_receptor": payload.get("covdock_receptor"),
    }
    missing = []
    for label, value in paths.items():
        if not isinstance(value, str) or not value:
            missing.append(f"{label}:not_declared")
            continue
        path = Path(value)
        resolved = path if path.is_absolute() else root / path
        if not resolved.is_file():
            missing.append(f"{label}:{value}")
    local = root / "configs" / "local.inspire.yaml"
    runtime = _read_yaml(local) if local.is_file() else {}
    library = runtime.get("covindb", {}).get("virtual_screening_library")
    if not isinstance(library, str) or not Path(library).is_file():
        missing.append(f"covindb_library:{library or 'not_declared'}")
    return missing


def _supported(records: list[tuple[Path, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {
            "target_id": payload["target_id"],
            "site_id": payload["site_id"],
            "chain_id": payload["protein_chain"],
            "residue": f"{payload['residue_name']}{payload['residue_number']}",
            "warhead_class": payload["warhead_class"],
            "registry_path": str(Path("configs") / "targets" / path.name),
        }
        for path, payload in records
    ]


def resolve_library_first_intake(
    root: Path,
    query: str,
    *,
    source_library: str = "CovalentInDB 2.0",
    scope: Literal["auto", "pilot", "full"] = "auto",
) -> LibraryFirstIntake:
    root = root.resolve()
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    requested_scope = _scope(query, scope)
    records = _targets(root)
    supported = _supported(records)
    if source_library.strip().lower() not in {
        "covalentindb",
        "covalentindb 2.0",
        "covindb",
    }:
        return LibraryFirstIntake(
            state="UNSUPPORTED_LIBRARY",
            source_library=source_library,
            requested_scope=requested_scope,
            query=query,
            target=None,
            target_inferred=False,
            campaign_config_path=None,
            missing_inputs=[],
            supported_targets=supported,
            approval_required=False,
            next_action="当前正式闭环只接受 CovalentInDB 2.0；请切换数据库或先注册新的库适配器。",
        )
    matches = [
        (path, payload)
        for path, payload in records
        if re.search(str(payload["target_regex"]), query)
    ]
    if not matches:
        return LibraryFirstIntake(
            state="TARGET_NOT_REGISTERED",
            source_library="CovalentInDB 2.0",
            requested_scope=requested_scope,
            query=query,
            target=None,
            target_inferred=False,
            campaign_config_path=None,
            missing_inputs=[],
            supported_targets=supported,
            approval_required=False,
            next_action="先建立该靶点的 Target Passport，并确认共价残基、链、结构和反应类型。",
        )
    if len(matches) > 1:
        return LibraryFirstIntake(
            state="AMBIGUOUS_TARGET",
            source_library="CovalentInDB 2.0",
            requested_scope=requested_scope,
            query=query,
            target=None,
            target_inferred=False,
            campaign_config_path=None,
            missing_inputs=[],
            supported_targets=supported,
            approval_required=False,
            next_action="输入同时匹配多个已登记靶点；请明确靶点符号和共价残基。",
        )
    path, payload = matches[0]
    stated = _site_number(query)
    expected = int(payload["residue_number"])
    target = {
        "target_id": payload["target_id"],
        "site_id": payload["site_id"],
        "chain_id": payload["protein_chain"],
        "residue_name": payload["residue_name"],
        "residue_number": expected,
        "reactive_atom": payload["nucleophilic_atom"],
        "reaction_type": payload["reaction_type"],
        "warhead_class": payload["warhead_class"],
        "registry_path": str(path.relative_to(root)),
    }
    if stated is not None and stated != expected:
        return LibraryFirstIntake(
            state="SITE_MISMATCH",
            source_library="CovalentInDB 2.0",
            requested_scope=requested_scope,
            query=query,
            target=target,
            target_inferred=False,
            campaign_config_path=None,
            missing_inputs=[],
            supported_targets=supported,
            approval_required=False,
            next_action=f"输入位点与登记位点 {payload['site_id']} 不一致；必须人工核对后才能筛选。",
        )
    campaigns = _campaigns(root, payload, requested_scope)
    if not campaigns:
        alternate = _campaigns(root, payload, "full" if requested_scope == "pilot" else "pilot")
        state: IntakeState = "SCOPE_NOT_CONFIGURED" if alternate else "CAMPAIGN_NOT_CONFIGURED"
        action = (
            f"{payload['site_id']} 已登记，但尚未配置 {requested_scope} Campaign；请先冻结该规模的预算和输入。"
            if alternate
            else f"{payload['site_id']} 已登记，但缺少可执行的 Library-first Campaign 和结构资产映射。"
        )
        return LibraryFirstIntake(
            state=state,
            source_library="CovalentInDB 2.0",
            requested_scope=requested_scope,
            query=query,
            target=target,
            target_inferred=stated is None,
            campaign_config_path=None,
            missing_inputs=[],
            supported_targets=supported,
            approval_required=False,
            next_action=action,
        )
    campaign = campaigns[0]
    missing = _missing(root, campaign)
    relative = str(campaign.relative_to(root))
    if missing:
        return LibraryFirstIntake(
            state="INPUTS_MISSING",
            source_library="CovalentInDB 2.0",
            requested_scope=requested_scope,
            query=query,
            target=target,
            target_inferred=stated is None,
            campaign_config_path=relative,
            missing_inputs=missing,
            supported_targets=supported,
            approval_required=False,
            next_action="补齐列出的结构、AF3、CovDock 或 CovalentInDB 输入后再编译筛选计划。",
        )
    return LibraryFirstIntake(
        state="READY",
        source_library="CovalentInDB 2.0",
        requested_scope=requested_scope,
        query=query,
        target=target,
        target_inferred=stated is None,
        campaign_config_path=relative,
        missing_inputs=[],
        supported_targets=supported,
        approval_required=True,
        next_action="检查 parent-linked Campaign 状态，展示唯一合法的下一阶段及资源预算，并请求一次明确批准。",
    )
