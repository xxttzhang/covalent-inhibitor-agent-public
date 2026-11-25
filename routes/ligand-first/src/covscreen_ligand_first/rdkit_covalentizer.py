from __future__ import annotations

import hashlib
from collections import deque
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import Chem

from .ligand_first_inputs import ExitVectorSpec, LigandFirstCampaignSpec


PARENT_ATOM_MAP_BASE = 100
WARHEAD_ATOM_MAPS = {
    "attachment_nitrogen": 902,
    "carbonyl_carbon": 903,
    "carbonyl_oxygen": 904,
    "electrophile_alpha_carbon": 905,
    "reaction_beta_carbon": 906,
    "linker_atom_1": 907,
    "linker_atom_2": 908,
    "leaving_group": 909,
}


class CovalentizationRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    warhead_name: str
    transformation_name: str
    fragment_smiles: str
    reaction_class: str
    covalent_mode: Literal["reversible", "irreversible"]
    linker_name: Literal["direct", "methylene", "ethylene"]
    linker_heavy_atom_count: int = Field(ge=0, le=2)
    compatible_anchor_elements: list[str]
    compatible_leaving_elements: list[str]
    evidence_note: str


ACRYLAMIDE_REPLACEMENT_RULE = CovalentizationRule(
    rule_id="rdkit-replace-aryl-ether-with-acrylamide-v1",
    warhead_name="acrylamide",
    transformation_name="replace_aryl_ether_with_acrylamide",
    fragment_smiles="[*:901][NH:902][C:903](=[O:904])[CH:905]=[CH2:906]",
    reaction_class="thiol Michael addition",
    covalent_mode="irreversible",
    linker_name="direct",
    linker_heavy_atom_count=0,
    compatible_anchor_elements=["C"],
    compatible_leaving_elements=["O"],
    evidence_note=(
        "Implements the campaign-required Covalentizer subset as an atom-mapped RDKit graph "
        "transformation. It replaces the nominated aryl-ether branch; it does not predict "
        "reaction kinetics or synthetic yield."
    ),
)


_WARHEAD_FRAGMENTS = {
    "acrylamide": {
        "tail": "[NH:902][C:903](=[O:904])[CH:905]=[CH2:906]",
        "reaction_class": "thiol Michael addition",
        "covalent_mode": "irreversible",
    },
    "chloroacetamide": {
        "tail": "[NH:902][C:903](=[O:904])[CH2:906][Cl:909]",
        "reaction_class": "thiol nucleophilic substitution",
        "covalent_mode": "irreversible",
    },
    "vinyl_sulfone": {
        "tail": "[S:903](=[O:904])(=[O:909])[CH:905]=[CH2:906]",
        "reaction_class": "thiol Michael addition to vinyl sulfone",
        "covalent_mode": "irreversible",
    },
    "propiolamide": {
        "tail": "[NH:902][C:903](=[O:904])[C:905]#[CH:906]",
        "reaction_class": "thiol addition to activated alkyne",
        "covalent_mode": "irreversible",
    },
}

_LINKER_PREFIXES = {
    "direct": "[*:901]",
    "methylene": "[*:901][CH2:907]",
    "ethylene": "[*:901][CH2:907][CH2:908]",
}


def _formal_rules() -> dict[str, CovalentizationRule]:
    rules = {
        ACRYLAMIDE_REPLACEMENT_RULE.transformation_name: ACRYLAMIDE_REPLACEMENT_RULE
    }
    for warhead_name, warhead in _WARHEAD_FRAGMENTS.items():
        for linker_name, prefix in _LINKER_PREFIXES.items():
            if warhead_name == "acrylamide" and linker_name == "direct":
                continue
            transformation = f"replace_branch_with_{warhead_name}_{linker_name}"
            rules[transformation] = CovalentizationRule(
                rule_id=f"rdkit-{transformation}-v1",
                warhead_name=warhead_name,
                transformation_name=transformation,
                fragment_smiles=prefix + str(warhead["tail"]),
                reaction_class=str(warhead["reaction_class"]),
                covalent_mode=str(warhead["covalent_mode"]),
                linker_name=linker_name,
                linker_heavy_atom_count={"direct": 0, "methylene": 1, "ethylene": 2}[
                    linker_name
                ],
                compatible_anchor_elements=["C"],
                compatible_leaving_elements=["O", "N", "C", "S"],
                evidence_note=(
                    "Deterministic Covalentizer-style branch replacement with a curated "
                    f"{warhead_name} warhead and {linker_name} linker. Geometry, reactivity, "
                    "and synthesis remain downstream hypotheses."
                ),
            )
    return rules


RULES = _formal_rules()

FORMAL_COVALENTIZER_TRANSFORMATIONS = tuple(RULES)

_WARHEAD_VALIDATION = {
    "acrylamide": ("[NX3][CX3](=[OX1])[CX3]=[CX3]", 4),
    "chloroacetamide": ("[NX3][CX3](=[OX1])[CH2][Cl]", 3),
    "vinyl_sulfone": ("[SX4](=[OX1])(=[OX1])[CX3]=[CX3]", 4),
    "propiolamide": ("[NX3][CX3](=[OX1])[CX2]#[CX2H1]", 4),
}


class ChemistryQualityGate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["PASS", "FAIL"]
    sanitization_ok: bool
    valence_ok: bool
    radical_electron_count: int = Field(ge=0)
    formal_charge: int
    formal_charge_allowed: bool
    fragment_count: int = Field(ge=0)
    single_component: bool
    chemistry_problem_types: list[str]
    protected_core_retained: bool
    protected_pharmacophore_retained: bool
    protected_substituent_bonds_retained: bool
    warhead_match_count: int = Field(ge=0)
    exactly_one_allowed_warhead: bool
    reaction_atom_index: int | None = Field(default=None, ge=0)
    reaction_atom_map_number: int | None = Field(default=None, ge=0)
    reaction_atom_mapping_valid: bool
    failure_reasons: list[str]


Sha256 = Annotated[str, Field(pattern=r'^[a-f0-9]{64}$')]


class UpstreamCovalentizerSource(BaseModel):
    """Trace to sealed enumeration evidence; hashes alone do not verify files."""
    model_config = ConfigDict(extra='forbid')
    enumeration_manifest_sha256: Sha256
    source_product_sha256: Sha256
    structure_sha256: Sha256
    input_sha256: dict[str, Sha256] = Field(min_length=1)
    enumeration_candidate_id: str = Field(min_length=1)
    upstream_id: str = Field(min_length=1)
    input_id: str = Field(min_length=1)
    mapping_method: Literal['unique chirality-aware full-parent graph correspondence']
    reaction_ancestry_verified: Literal[False] = False


class CovalentizedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: str
    campaign_id: str
    parent_ligand_id: str
    parent_pose_id: str
    exit_vector_id: str
    transformation_name: str
    rule_id: str
    warhead_name: str
    linker_name: Literal["direct", "methylene", "ethylene"]
    linker_heavy_atom_count: int = Field(ge=0, le=2)
    reaction_class: str
    covalent_mode: Literal["reversible", "irreversible"]
    canonical_smiles: str
    parent_atom_index_to_product_index: dict[int, int]
    warhead_atom_map_to_product_index: dict[int, int]
    removed_parent_atom_indices: list[int]
    quality_gate: ChemistryQualityGate
    provenance: Literal["deterministic-rdkit-covalentizer", "upstream-covalentizer-full-parent-v1"] = "deterministic-rdkit-covalentizer"
    upstream_source: UpstreamCovalentizerSource | None = None
    disclaimer: str = (
        "A passing graph-chemistry gate proves identity and mapping invariants only; it does not "
        "establish binding, covalent kinetics, selectivity, synthesis, safety, or biological activity."
    )

    @model_validator(mode='after')
    def consistent_generation_source(self):
        upstream = self.provenance == 'upstream-covalentizer-full-parent-v1'
        if upstream != (self.upstream_source is not None):
            raise ValueError('Upstream provenance requires its source evidence and cannot use the legacy label')
        if upstream and self.removed_parent_atom_indices:
            raise ValueError('Full-parent provenance cannot remove parent atoms')
        return self


def _atom_name_map(mol: Chem.Mol) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for atom in mol.GetAtoms():
        if atom.HasProp("pdb_atom_name"):
            mapping[atom.GetProp("pdb_atom_name")] = atom.GetIdx()
    if not mapping and mol.HasProp("atom_name_to_index_json"):
        import json

        mapping = {str(name): int(index) for name, index in json.loads(mol.GetProp("atom_name_to_index_json")).items()}
    return mapping


def _branch_atoms(mol: Chem.Mol, anchor_index: int, leaving_index: int) -> set[int]:
    bond = mol.GetBondBetweenAtoms(anchor_index, leaving_index)
    if bond is None:
        raise ValueError("nominated anchor/leaving atoms are not bonded")
    if bond.IsInRing():
        raise ValueError("ring bonds cannot be removed by this covalentization rule")
    visited = {anchor_index}
    branch: set[int] = set()
    queue: deque[int] = deque([leaving_index])
    while queue:
        index = queue.popleft()
        if index in visited:
            continue
        visited.add(index)
        branch.add(index)
        for neighbor in mol.GetAtomWithIdx(index).GetNeighbors():
            neighbor_index = neighbor.GetIdx()
            if neighbor_index not in visited:
                queue.append(neighbor_index)
    if anchor_index in branch:
        raise ValueError("leaving branch traversal returned to the anchor")
    return branch


def _clear_atom_maps_for_smiles(mol: Chem.Mol) -> Chem.Mol:
    copy = Chem.Mol(mol)
    for atom in copy.GetAtoms():
        atom.SetAtomMapNum(0)
    return copy


def _mapped_indices(mol: Chem.Mol) -> tuple[dict[int, int], dict[int, int]]:
    parent: dict[int, int] = {}
    warhead: dict[int, int] = {}
    for atom in mol.GetAtoms():
        atom_map = atom.GetAtomMapNum()
        if PARENT_ATOM_MAP_BASE <= atom_map < 900:
            parent[atom_map - PARENT_ATOM_MAP_BASE] = atom.GetIdx()
        elif atom_map >= 900:
            warhead[atom_map] = atom.GetIdx()
    return parent, warhead


def _protected_bonds_retained(
    parent_mol: Chem.Mol,
    product_mol: Chem.Mol,
    campaign: LigandFirstCampaignSpec,
    parent_mapping: dict[int, int],
) -> bool:
    parent_names = _atom_name_map(parent_mol)
    for protected in campaign.protection.protected_substituent_bonds:
        first_parent = parent_names[protected.anchor_atom_name]
        second_parent = parent_names[protected.substituent_atom_name]
        if first_parent not in parent_mapping or second_parent not in parent_mapping:
            return False
        original = parent_mol.GetBondBetweenAtoms(first_parent, second_parent)
        product = product_mol.GetBondBetweenAtoms(
            parent_mapping[first_parent], parent_mapping[second_parent]
        )
        if original is None or product is None or original.GetBondType() != product.GetBondType():
            return False
    return True


def evaluate_chemistry_quality(
    product_mol: Chem.Mol,
    parent_mol: Chem.Mol,
    campaign: LigandFirstCampaignSpec,
    *,
    max_absolute_formal_charge: int = 1,
) -> ChemistryQualityGate:
    failures: list[str] = []
    sanitization_ok = True
    valence_ok = True
    chemistry_problem_types: list[str] = []
    try:
        product_mol.UpdatePropertyCache(strict=True)
        Chem.SanitizeMol(product_mol)
    except Exception as exc:
        sanitization_ok = False
        valence_ok = False
        chemistry_problem_types.append(type(exc).__name__)
        failures.append(f"RDKit sanitization/valence failure: {exc}")
    try:
        problems = list(Chem.DetectChemistryProblems(product_mol))
    except Exception as exc:
        problems = []
        sanitization_ok = False
        failures.append(f"RDKit chemistry-problem detection failed: {exc}")
    for problem in problems:
        problem_type = problem.GetType()
        chemistry_problem_types.append(problem_type)
        if "Valence" in problem_type:
            valence_ok = False
    if chemistry_problem_types:
        failures.append(f"RDKit chemistry problems: {sorted(set(chemistry_problem_types))}")

    radical_count = sum(atom.GetNumRadicalElectrons() for atom in product_mol.GetAtoms())
    if radical_count:
        failures.append(f"radical electrons present: {radical_count}")
    formal_charge = sum(atom.GetFormalCharge() for atom in product_mol.GetAtoms())
    charge_allowed = abs(formal_charge) <= max_absolute_formal_charge
    if not charge_allowed:
        failures.append(
            f"formal charge {formal_charge} exceeds allowed absolute value {max_absolute_formal_charge}"
        )
    fragment_count = len(Chem.GetMolFrags(product_mol))
    single_component = fragment_count == 1
    if not single_component:
        failures.append(f"expected one molecular component, found {fragment_count}")

    parent_mapping, warhead_mapping = _mapped_indices(product_mol)
    parent_names = _atom_name_map(parent_mol)
    core_parent_indices = {parent_names[name] for name in campaign.protection.scaffold_core_atom_names}
    pharmacophore_parent_indices = {
        parent_names[name] for name in campaign.protection.protected_pharmacophore_atom_names
    }
    core_retained = core_parent_indices.issubset(parent_mapping)
    pharmacophore_retained = pharmacophore_parent_indices.issubset(parent_mapping)
    if not core_retained:
        failures.append("one or more protected scaffold-core atoms were removed")
    if not pharmacophore_retained:
        failures.append("one or more protected pharmacophore atoms were removed")

    if core_retained:
        for bond in parent_mol.GetBonds():
            begin = bond.GetBeginAtomIdx()
            end = bond.GetEndAtomIdx()
            if begin not in core_parent_indices or end not in core_parent_indices:
                continue
            product_bond = product_mol.GetBondBetweenAtoms(parent_mapping[begin], parent_mapping[end])
            if product_bond is None or product_bond.GetBondType() != bond.GetBondType():
                core_retained = False
                failures.append("protected scaffold-core connectivity or bond order changed")
                break
    protected_bonds_retained = _protected_bonds_retained(
        parent_mol, product_mol, campaign, parent_mapping
    )
    if not protected_bonds_retained:
        failures.append("a protected substituent bond was removed or changed")

    allowed_matches: list[tuple[str, tuple[int, ...], int]] = []
    for name in campaign.allowed_warheads:
        validation = _WARHEAD_VALIDATION.get(name)
        if validation is None:
            continue
        smarts, reaction_position = validation
        query = Chem.MolFromSmarts(smarts)
        if query is None:
            continue
        allowed_matches.extend(
            (name, match, reaction_position)
            for match in product_mol.GetSubstructMatches(query)
        )
    warhead_match_count = len(allowed_matches)
    exactly_one = warhead_match_count == 1
    if not exactly_one:
        failures.append(
            "expected exactly one campaign-allowed warhead, "
            f"found {warhead_match_count}"
        )
    reaction_map = WARHEAD_ATOM_MAPS["reaction_beta_carbon"]
    reaction_index = warhead_mapping.get(reaction_map)
    reaction_map_count = sum(
        atom.GetAtomMapNum() == reaction_map for atom in product_mol.GetAtoms()
    )
    mapping_valid = (
        exactly_one
        and reaction_index is not None
        and reaction_index == allowed_matches[0][1][allowed_matches[0][2]]
        and product_mol.GetAtomWithIdx(reaction_index).GetSymbol() == "C"
        and reaction_map_count == 1
    )
    if not mapping_valid:
        failures.append("reaction beta-carbon atom mapping is missing, duplicated, or inconsistent")

    return ChemistryQualityGate(
        status="FAIL" if failures else "PASS",
        sanitization_ok=sanitization_ok,
        valence_ok=valence_ok,
        radical_electron_count=radical_count,
        formal_charge=formal_charge,
        formal_charge_allowed=charge_allowed,
        fragment_count=fragment_count,
        single_component=single_component,
        chemistry_problem_types=sorted(set(chemistry_problem_types)),
        protected_core_retained=core_retained,
        protected_pharmacophore_retained=pharmacophore_retained,
        protected_substituent_bonds_retained=protected_bonds_retained,
        warhead_match_count=warhead_match_count,
        exactly_one_allowed_warhead=exactly_one,
        reaction_atom_index=reaction_index,
        reaction_atom_map_number=reaction_map if reaction_index is not None else None,
        reaction_atom_mapping_valid=mapping_valid,
        failure_reasons=failures,
    )


def _find_exit(campaign: LigandFirstCampaignSpec, exit_vector_id: str) -> ExitVectorSpec:
    matches = [vector for vector in campaign.exit_vectors if vector.exit_vector_id == exit_vector_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one exit vector named {exit_vector_id}")
    return matches[0]


def covalentize_parent(
    parent_mol: Chem.Mol,
    campaign: LigandFirstCampaignSpec,
    exit_vector_id: str,
    transformation_name: str,
) -> tuple[CovalentizedCandidate, Chem.Mol]:
    if campaign.planning_mode not in {"curated-v1", "rule-derived-v1"}:
        raise ValueError("Native campaign requires upstream-native enumeration, not curated RDKit transformations")
    exit_vector = _find_exit(campaign, exit_vector_id)
    if not exit_vector.advance:
        raise ValueError(f"exit vector is protected/non-advancing: {exit_vector_id}")
    if transformation_name not in exit_vector.allowed_transformations:
        raise ValueError(
            f"transformation {transformation_name} is not allowed for exit vector {exit_vector_id}"
        )
    rule = RULES.get(transformation_name)
    if rule is None:
        raise ValueError(f"unsupported RDKit covalentization transformation: {transformation_name}")
    if rule.warhead_name not in campaign.allowed_warheads:
        raise ValueError(f"warhead {rule.warhead_name} is not allowed by the campaign")

    name_map = _atom_name_map(parent_mol)
    anchor_index = name_map[exit_vector.anchor_atom_name]
    leaving_index = name_map[exit_vector.leaving_atom_name]
    anchor = parent_mol.GetAtomWithIdx(anchor_index)
    leaving = parent_mol.GetAtomWithIdx(leaving_index)
    if anchor.GetSymbol() not in rule.compatible_anchor_elements:
        raise ValueError(f"incompatible anchor element: {anchor.GetSymbol()}")
    if leaving.GetSymbol() not in rule.compatible_leaving_elements:
        raise ValueError(f"incompatible leaving element: {leaving.GetSymbol()}")

    protected_parent = {
        name_map[name] for name in campaign.protection.scaffold_core_atom_names
    } | {name_map[name] for name in campaign.protection.protected_pharmacophore_atom_names}
    removed = _branch_atoms(parent_mol, anchor_index, leaving_index)
    protected_removed = sorted(removed & protected_parent)
    if protected_removed:
        raise ValueError(f"transformation would remove protected parent atoms: {protected_removed}")

    parent_copy = Chem.Mol(parent_mol)
    for atom in parent_copy.GetAtoms():
        atom.SetAtomMapNum(PARENT_ATOM_MAP_BASE + atom.GetIdx())
        atom.SetIntProp("parent_atom_index", atom.GetIdx())
        atom.SetProp("lineage_role", "parent")
    editable = Chem.RWMol(parent_copy)
    for index in sorted(removed, reverse=True):
        editable.RemoveAtom(index)
    truncated = editable.GetMol()
    parent_mapping_after_removal, _ = _mapped_indices(truncated)
    new_anchor_index = parent_mapping_after_removal[anchor_index]

    fragment = Chem.MolFromSmiles(rule.fragment_smiles, sanitize=True)
    if fragment is None:
        raise ValueError(f"RDKit could not parse rule fragment: {rule.fragment_smiles}")
    dummy_atoms = [atom for atom in fragment.GetAtoms() if atom.GetAtomicNum() == 0]
    if len(dummy_atoms) != 1 or dummy_atoms[0].GetDegree() != 1:
        raise ValueError("rule fragment must contain one singly connected dummy atom")
    for atom in fragment.GetAtoms():
        atom.SetProp("lineage_role", "warhead")
    combined = Chem.RWMol(Chem.CombineMols(truncated, fragment))
    offset = truncated.GetNumAtoms()
    dummy_index = offset + dummy_atoms[0].GetIdx()
    neighbor_index = offset + dummy_atoms[0].GetNeighbors()[0].GetIdx()
    combined.AddBond(new_anchor_index, neighbor_index, Chem.BondType.SINGLE)
    combined.RemoveAtom(dummy_index)
    product = combined.GetMol()
    Chem.SanitizeMol(product)

    parent_product_mapping, warhead_product_mapping = _mapped_indices(product)
    canonical_smiles = Chem.MolToSmiles(_clear_atom_maps_for_smiles(product), canonical=True)
    quality = evaluate_chemistry_quality(product, parent_mol, campaign)
    identity = hashlib.sha256(
        f"{campaign.campaign_id}|{exit_vector_id}|{transformation_name}|{canonical_smiles}".encode()
    ).hexdigest()[:16]
    candidate = CovalentizedCandidate(
        candidate_id=f"{campaign.target.symbol.lower()}-{identity}",
        campaign_id=campaign.campaign_id,
        parent_ligand_id=campaign.parent_pose.ligand_component_id,
        parent_pose_id=campaign.parent_pose.pdb_id,
        exit_vector_id=exit_vector_id,
        transformation_name=transformation_name,
        rule_id=rule.rule_id,
        warhead_name=rule.warhead_name,
        linker_name=rule.linker_name,
        linker_heavy_atom_count=rule.linker_heavy_atom_count,
        reaction_class=rule.reaction_class,
        covalent_mode=rule.covalent_mode,
        canonical_smiles=canonical_smiles,
        parent_atom_index_to_product_index=parent_product_mapping,
        warhead_atom_map_to_product_index=warhead_product_mapping,
        removed_parent_atom_indices=sorted(removed),
        quality_gate=quality,
    )
    product.SetProp("candidate_id", candidate.candidate_id)
    product.SetProp("campaign_id", campaign.campaign_id)
    product.SetProp("parent_ligand_id", campaign.parent_pose.ligand_component_id)
    product.SetProp("exit_vector_id", exit_vector_id)
    product.SetProp("transformation_name", transformation_name)
    product.SetProp("warhead_name", rule.warhead_name)
    product.SetProp("linker_name", rule.linker_name)
    product.SetProp("reaction_atom_index", str(quality.reaction_atom_index))
    product.SetProp("chemistry_gate_status", quality.status)
    return candidate, product
