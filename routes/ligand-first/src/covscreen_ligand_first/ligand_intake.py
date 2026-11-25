from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from .ligand_chain import load_ligand_chain_config


IntakeState = Literal[
    "READY",
    "AMBIGUOUS_CAMPAIGN",
    "CAMPAIGN_NOT_REGISTERED",
    "TARGET_NOT_REGISTERED",
    "LIGAND_NOT_REGISTERED",
    "SITE_MISMATCH",
    "SITE_REQUIRED",
    "INPUTS_MISSING",
]


class LigandFirstIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    intent: Literal["ligand-first"] = "ligand-first"
    state: IntakeState
    query: str
    target: dict[str, Any] | None
    parent_ligand: dict[str, Any] | None
    campaign_config_path: str | None
    missing_inputs: list[str]
    recoverable_source_assets: list[dict[str, str]]
    deferred_inputs: list[str]
    supported_campaigns: list[dict[str, Any]]
    approval_required: bool
    next_action: str


def _records(root: Path) -> list[tuple[Path, dict[str, Any], dict[str, Any]]]:
    records = []
    for path in sorted((root / "configs" / "ligand_first").glob("*.yaml")):
        payload = yaml.safe_load(path.read_text()) or {}
        campaign_value = payload.get("campaign_path")
        campaign_path = root / str(campaign_value or "")
        if not campaign_value or not campaign_path.is_file():
            continue
        campaign = json.loads(campaign_path.read_text())
        records.append((path, payload, campaign))
    return records


def _site_numbers(query: str) -> set[int]:
    patterns = (
        # Unicode word boundaries treat adjacent Chinese as part of C797 and
        # used to silently discard explicit user sites. Do not parse C797S
        # mutation notation or embedded identifiers as a reactive residue.
        r"(?i)(?<![A-Za-z0-9])(?:CYS|C)\s*[-_:]?\s*(\d+)(?![A-Za-z0-9])",
        r"半胱氨酸\s*[-_:]?\s*(\d+)(?![A-Za-z0-9])",
    )
    return {
        int(match.group(1))
        for pattern in patterns
        for match in re.finditer(pattern, query)
    }


def _contains_alias(query: str, aliases: list[str]) -> bool:
    return any(re.search(rf"(?i)(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", query) for alias in aliases)


def _missing_named_paths(
    root: Path, required: dict[str, object]
) -> list[str]:
    missing: list[str] = []
    for label, value in required.items():
        path = Path(str(value or ""))
        resolved = path if path.is_absolute() else root / path
        if not value or not resolved.is_file():
            missing.append(f"{label}:{value or 'not_declared'}")
    return missing


def _recoverable_assets(root: Path, payload: dict[str, Any], campaign: dict[str, Any]) -> list[dict[str, str]]:
    campaign_path = root / str(payload["campaign_path"])
    base = campaign_path.parent
    recoverable = []
    for asset in campaign.get("source_assets", []):
        target = base / str(asset.get("path", ""))
        if not target.is_file() and asset.get("source_url") and asset.get("sha256"):
            recoverable.append(
                {
                    "path": str(target.relative_to(root)),
                    "source_url": str(asset["source_url"]),
                    "sha256": str(asset["sha256"]),
                }
            )
    return recoverable


def resolve_ligand_first_intake(root: Path, query: str) -> LigandFirstIntake:
    root = root.resolve()
    query = query.strip()
    if not query:
        raise ValueError("query must not be empty")
    all_records = _records(root)
    # Continuation/recovery configurations adopt immutable upstream runs via
    # seed_run_ids.  They are execution evidence for an existing campaign,
    # not additional campaigns that should compete in natural-language
    # discovery.  They remain addressable when the caller names the exact
    # config path, which keeps recovery reproducible without making ordinary
    # target/site/parent requests ambiguous.
    discoverable_records = [
        item for item in all_records if not item[1].get("seed_run_ids")
    ]
    supported = [
        {
            "campaign_id": payload.get("campaign_id"),
            "target": campaign.get("target", {}).get("symbol"),
            "site": f"C{campaign.get('target', {}).get('nucleophile', {}).get('uniprot_residue_number')}",
            "parent_ligand": campaign.get("parent_pose", {}).get("ligand_name"),
            "config_path": str(path.relative_to(root)),
        }
        for path, payload, campaign in discoverable_records
    ]
    explicit_paths = set(re.findall(
        r"(?<![A-Za-z0-9_./-])configs/ligand_first/[A-Za-z0-9_.-]+\.yaml(?![A-Za-z0-9_./-])",
        query))
    explicit = bool(explicit_paths)
    if explicit:
        selected = [
            item for item in all_records
            if str(item[0].relative_to(root)) in explicit_paths
        ]
        if len(explicit_paths) != 1 or len(selected) != 1:
            return LigandFirstIntake(
                state="AMBIGUOUS_CAMPAIGN" if len(explicit_paths) > 1 else "CAMPAIGN_NOT_REGISTERED",
                query=query, target=None, parent_ligand=None, campaign_config_path=None,
                missing_inputs=[], recoverable_source_assets=[], deferred_inputs=[],
                supported_campaigns=supported, approval_required=False,
                next_action="请指定唯一且已登记的配置；不会回退到其它枚举分支。")
        records = selected
    else:
        records = discoverable_records
    target_matches = [
        item
        for item in records
        if _contains_alias(
            query,
            [str(alias) for alias in (
                [item[2].get("target", {}).get("symbol", "")] if explicit else
                item[1].get("target_aliases", [item[2].get("target", {}).get("symbol", "")])) if alias],
        )
    ]
    if not target_matches:
        return LigandFirstIntake(
            state="TARGET_NOT_REGISTERED", query=query, target=None, parent_ligand=None,
            campaign_config_path=None, missing_inputs=[], recoverable_source_assets=[],
            deferred_inputs=[],
            supported_campaigns=supported, approval_required=False,
            next_action="先登记靶点、共价残基和带实验/可信构象的非共价母体配体。",
        )
    ligand_matches = [
        item
        for item in target_matches
        if _contains_alias(
            query,
            [str(alias) for alias in (
                [item[2].get("parent_pose", {}).get("ligand_name", "")] if explicit else
                item[1].get("ligand_aliases", [item[2].get("parent_pose", {}).get("ligand_name", "")])) if alias],
        )
    ]
    if not ligand_matches:
        item = target_matches[0]
        campaign = item[2]
        return LigandFirstIntake(
            state="LIGAND_NOT_REGISTERED", query=query,
            target={"symbol": campaign["target"]["symbol"]}, parent_ligand=None,
            campaign_config_path=None, missing_inputs=[], recoverable_source_assets=[],
            deferred_inputs=[],
            supported_campaigns=supported, approval_required=False,
            next_action="请明确已知非共价母体配体；若是新配体，先提供或获取其靶点结合构象。",
        )
    stated = _site_numbers(query)
    if not stated:
        return LigandFirstIntake(
            state="SITE_REQUIRED", query=query, target=None, parent_ligand=None,
            campaign_config_path=None, missing_inputs=["reactive_cys_residue"],
            recoverable_source_assets=[], deferred_inputs=[], supported_campaigns=supported,
            approval_required=False,
            next_action="请明确计划形成共价键的 Cys 位点（如 C797）；不会自动采用登记位点。",
        )
    # A named site can disambiguate multiple registered campaigns for one
    # target/ligand pair. Preserve mismatches for the explicit error below.
    site_matches = [item for item in ligand_matches
                    if stated == {int(item[2]["target"]["nucleophile"]["uniprot_residue_number"])}]
    if site_matches:
        ligand_matches = site_matches
    if len(ligand_matches) > 1:
        return LigandFirstIntake(
            state="AMBIGUOUS_CAMPAIGN", query=query, target=None, parent_ligand=None,
            campaign_config_path=None, missing_inputs=[], recoverable_source_assets=[],
            deferred_inputs=[],
            supported_campaigns=supported, approval_required=False,
            next_action="输入匹配多个 Ligand-first Campaign；请明确靶点、残基和母体配体。",
        )
    path, payload, campaign = ligand_matches[0]
    nucleophile = campaign["target"]["nucleophile"]
    expected = int(nucleophile["uniprot_residue_number"])
    target = {
        "symbol": campaign["target"]["symbol"],
        "uniprot_id": campaign["target"].get("uniprot_id"),
        "residue": f"{nucleophile['residue_name']}{expected}",
        "reactive_atom": nucleophile["atom_name"],
    }
    parent = {
        "name": campaign["parent_pose"]["ligand_name"],
        "pdb_id": campaign["parent_pose"]["pdb_id"],
        "component_id": campaign["parent_pose"]["ligand_component_id"],
    }
    if stated and stated != {expected}:
        return LigandFirstIntake(
            state="SITE_MISMATCH", query=query, target=target, parent_ligand=parent,
            campaign_config_path=str(path.relative_to(root)), missing_inputs=[],
            recoverable_source_assets=[], deferred_inputs=[], supported_campaigns=supported,
            approval_required=False,
            next_action=f"输入位点 {sorted(stated)} 与登记的 C{expected} 不一致；必须人工核对。",
        )
    # Validate the executable schema here, not only after a user approves it.
    load_ligand_chain_config(path)
    recoverable = _recoverable_assets(root, payload, campaign)
    recoverable_paths = {item["path"] for item in recoverable}
    recoverable_resolved = {(root / item["path"]).resolve() for item in recoverable}
    campaign_path = root / str(payload["campaign_path"])
    campaign_base = campaign_path.parent
    missing_sources = []
    for asset in campaign.get("source_assets", []):
        source_path = campaign_base / str(asset.get("path", ""))
        relative = str(source_path.relative_to(root))
        if not source_path.is_file() and relative not in recoverable_paths:
            missing_sources.append(f"source_asset:{relative}")
    protein_missing = _missing_named_paths(
        root, {"protein_structure": payload.get("pdb")}
    )
    protein_value = Path(str(payload.get("pdb", "")))
    protein_path = (
        protein_value if protein_value.is_absolute() else root / protein_value
    ).resolve()
    protein_recoverable = bool(
        protein_missing and protein_path in recoverable_resolved
    )
    blocking = [*missing_sources, *([] if protein_recoverable else protein_missing)]
    deferred = _missing_named_paths(
        root,
        {
            "af3_target_config": payload.get("target_config"),
            "af3_protein_seed": payload.get("af3_source_data_json"),
            "covdock_receptor": payload.get("covdock_receptor"),
        },
    )
    if blocking:
        return LigandFirstIntake(
            state="INPUTS_MISSING", query=query, target=target, parent_ligand=parent,
            campaign_config_path=str(path.relative_to(root)), missing_inputs=blocking,
            recoverable_source_assets=recoverable, deferred_inputs=deferred,
            supported_campaigns=supported,
            approval_required=False,
            next_action="补齐不可恢复的结构输入；仅带 RCSB HTTPS 来源和 SHA-256 的缺失资产可由结构接入阶段恢复。",
        )
    return LigandFirstIntake(
        state="READY", query=query, target=target, parent_ligand=parent,
        campaign_config_path=str(path.relative_to(root)), missing_inputs=[],
        recoverable_source_assets=recoverable, deferred_inputs=deferred,
        supported_campaigns=supported,
        approval_required=True,
        next_action=(
            "编译结构接入阶段；安全恢复缺失的冻结结构并重建母体构象。"
            if recoverable
            else "检查 parent-linked 闭环状态，展示唯一合法的下一阶段和资源预算，并请求明确批准。"
        ),
    )
