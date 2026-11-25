from __future__ import annotations

import math
from collections import deque
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from rdkit import Chem

from .ligand_first_inputs import (
    ExitVectorSpec,
    LigandFirstCampaignSpec,
    build_parent_pose_mol,
    read_pdb_atoms,
)
from .rdkit_covalentizer import FORMAL_COVALENTIZER_TRANSFORMATIONS


class RuleBasedExitVector(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = Field(ge=1)
    exit_vector_id: str
    operation: Literal["replace_branch"] = "replace_branch"
    anchor_atom_name: str
    anchor_atom_index: int = Field(ge=0)
    leaving_atom_name: str
    leaving_atom_index: int = Field(ge=0)
    removed_parent_atom_indices: list[int]
    distance_to_target_angstrom: float = Field(gt=0)
    vector_to_target_angle_degrees: float = Field(ge=0, le=180)
    removed_heavy_atom_count: int = Field(ge=1)
    rule_score: float = Field(ge=0, le=1)
    advance: bool
    rejection_reasons: list[str]
    provenance: Literal["covalentizer-rdkit-rule-v1"] = "covalentizer-rdkit-rule-v1"


class SiteDiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    campaign_id: str
    target_site: str
    status: Literal["PASS", "FAIL"]
    candidates: list[RuleBasedExitVector]
    advancing_count: int = Field(ge=0)
    warnings: list[str]
    disclaimer: str = (
        "These are deterministic Covalentizer-style branch-replacement hypotheses. "
        "They are not learned attachment predictions or evidence of synthesis, binding, "
        "covalent kinetics, or selectivity."
    )


def _distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _angle(
    origin: tuple[float, float, float],
    vector_end: tuple[float, float, float],
    target: tuple[float, float, float],
) -> float:
    left = tuple(vector_end[i] - origin[i] for i in range(3))
    right = tuple(target[i] - origin[i] for i in range(3))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 180.0
    cosine = sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _point(molecule: Chem.Mol, atom_index: int) -> tuple[float, float, float]:
    position = molecule.GetConformer().GetAtomPosition(atom_index)
    return position.x, position.y, position.z


def _branch_atoms(molecule: Chem.Mol, anchor: int, leaving: int) -> set[int]:
    visited = {anchor}
    branch: set[int] = set()
    queue: deque[int] = deque([leaving])
    while queue:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)
        branch.add(current)
        for neighbor in molecule.GetAtomWithIdx(current).GetNeighbors():
            if neighbor.GetIdx() not in visited:
                queue.append(neighbor.GetIdx())
    return branch


def _target_xyz(
    campaign: LigandFirstCampaignSpec, campaign_dir: Path
) -> tuple[float, float, float]:
    parent = campaign.parent_pose
    atoms = read_pdb_atoms(campaign_dir / parent.structure_path)
    matches = [
        atom
        for atom in atoms
        if atom.record == "ATOM"
        and atom.chain_id == parent.protein_chain_id
        and atom.residue_name == campaign.target.nucleophile.residue_name
        and atom.residue_number == parent.target_author_residue_number
        and atom.atom_name == campaign.target.nucleophile.atom_name
    ]
    if len(matches) != 1:
        raise ValueError(f"expected one target nucleophile atom, found {len(matches)}")
    return matches[0].xyz


def discover_covalentizer_sites(
    campaign: LigandFirstCampaignSpec,
    campaign_dir: Path,
    *,
    max_sites: int = 8,
    max_branch_heavy_atoms: int = 12,
    max_distance_angstrom: float = 12.0,
    max_vector_angle_degrees: float = 100.0,
) -> SiteDiscoveryResult:
    """Find auditable branch-replacement exit vectors from a bound parent pose."""

    if max_sites < 1:
        raise ValueError("max_sites must be positive")
    molecule = build_parent_pose_mol(campaign, campaign_dir)
    target = _target_xyz(campaign, campaign_dir)
    name_to_index = {
        atom.GetProp("pdb_atom_name"): atom.GetIdx()
        for atom in molecule.GetAtoms()
        if atom.HasProp("pdb_atom_name")
    }
    index_to_name = {index: name for name, index in name_to_index.items()}
    core = {
        name_to_index[name] for name in campaign.protection.scaffold_core_atom_names
    }
    protected = core | {
        name_to_index[name]
        for name in campaign.protection.protected_pharmacophore_atom_names
    }
    protected_bonds = {
        frozenset(
            (
                name_to_index[item.anchor_atom_name],
                name_to_index[item.substituent_atom_name],
            )
        )
        for item in campaign.protection.protected_substituent_bonds
    }

    rows: list[dict] = []
    seen: set[tuple[int, int]] = set()
    for bond in molecule.GetBonds():
        if bond.GetBondType() != Chem.BondType.SINGLE or bond.IsInRing():
            continue
        for anchor, leaving in (
            (bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
            (bond.GetEndAtomIdx(), bond.GetBeginAtomIdx()),
        ):
            if (anchor, leaving) in seen:
                continue
            seen.add((anchor, leaving))
            anchor_atom = molecule.GetAtomWithIdx(anchor)
            leaving_atom = molecule.GetAtomWithIdx(leaving)
            if anchor_atom.GetSymbol() != "C" or leaving_atom.GetSymbol() not in {
                "C",
                "N",
                "O",
                "S",
            }:
                continue
            if anchor not in core and not anchor_atom.IsInRing():
                continue
            branch = _branch_atoms(molecule, anchor, leaving)
            if not branch or len(branch) > max_branch_heavy_atoms:
                continue
            reasons: list[str] = []
            protected_removed = sorted(branch & protected)
            if protected_removed:
                reasons.append(f"would remove protected atoms {protected_removed}")
            if frozenset((anchor, leaving)) in protected_bonds:
                reasons.append("bond is explicitly protected by the campaign")
            distance = _distance(_point(molecule, anchor), target)
            angle = _angle(
                _point(molecule, anchor), _point(molecule, leaving), target
            )
            if distance > max_distance_angstrom:
                reasons.append(
                    f"anchor-target distance {distance:.2f} A exceeds {max_distance_angstrom:.2f} A"
                )
            if angle > max_vector_angle_degrees:
                reasons.append(
                    f"exit-vector angle {angle:.1f} degrees exceeds {max_vector_angle_degrees:.1f} degrees"
                )
            distance_score = max(0.0, 1.0 - abs(distance - 6.0) / 8.0)
            angle_score = max(0.0, 1.0 - angle / 180.0)
            branch_score = max(0.0, 1.0 - (len(branch) - 1) / max_branch_heavy_atoms)
            score = 0.45 * angle_score + 0.35 * distance_score + 0.20 * branch_score
            rows.append(
                {
                    "exit_vector_id": f"auto-{index_to_name[anchor]}-{index_to_name[leaving]}",
                    "anchor_atom_name": index_to_name[anchor],
                    "anchor_atom_index": anchor,
                    "leaving_atom_name": index_to_name[leaving],
                    "leaving_atom_index": leaving,
                    "removed_parent_atom_indices": sorted(branch),
                    "distance_to_target_angstrom": distance,
                    "vector_to_target_angle_degrees": angle,
                    "removed_heavy_atom_count": len(branch),
                    "rule_score": max(0.0, min(1.0, score)),
                    "advance": not reasons,
                    "rejection_reasons": reasons,
                }
            )

    rows.sort(
        key=lambda item: (
            not item["advance"],
            -item["rule_score"],
            item["anchor_atom_index"],
            item["leaving_atom_index"],
        )
    )
    candidates = [
        RuleBasedExitVector(rank=index, **row)
        for index, row in enumerate(rows[:max_sites], start=1)
    ]
    advancing = sum(item.advance for item in candidates)
    warnings = [
        "Only non-ring single-bond branch replacements are enumerated in V1.",
        "Rule scores prioritize geometry and protected-core retention; they do not predict synthesis.",
    ]
    if not advancing:
        warnings.append("No advancing rule-based exit vector was found.")
    return SiteDiscoveryResult(
        campaign_id=campaign.campaign_id,
        target_site=(
            f"{campaign.target.symbol}:{campaign.parent_pose.protein_chain_id}:"
            f"{campaign.target.nucleophile.residue_name}"
            f"{campaign.target.nucleophile.uniprot_residue_number}"
        ),
        status="PASS" if advancing else "FAIL",
        candidates=candidates,
        advancing_count=advancing,
        warnings=warnings,
    )


def exit_vector_specs(result: SiteDiscoveryResult) -> list[ExitVectorSpec]:
    return [
        ExitVectorSpec(
            exit_vector_id=item.exit_vector_id,
            anchor_atom_name=item.anchor_atom_name,
            leaving_atom_name=item.leaving_atom_name,
            priority=item.rank,
            advance=item.advance,
            allowed_transformations=(
                list(FORMAL_COVALENTIZER_TRANSFORMATIONS) if item.advance else []
            ),
            reference_mapping=(
                "Automatically enumerated by covalentizer-rdkit-rule-v1; "
                + ("passed declared rule gates" if item.advance else "; ".join(item.rejection_reasons))
            ),
        )
        for item in result.candidates
    ]
