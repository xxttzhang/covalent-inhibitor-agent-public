from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import Chem
from rdkit.Geometry import Point3D


class SourceAsset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_url: str


class NucleophileSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    residue_name: str
    uniprot_residue_number: int = Field(gt=0)
    atom_name: str


class TargetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    uniprot_id: str
    organism: str
    nucleophile: NucleophileSpec


class ParentPoseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pdb_id: str
    structure_path: str
    ligand_component_id: str
    ligand_name: str
    ligand_chain_id: str
    ligand_author_residue_number: int
    ligand_template_path: str
    protein_chain_id: str
    target_author_residue_number: int
    target_uniprot_residue_number: int
    residue_number_offset: int
    experimental_method: str
    resolution_angstrom: float = Field(gt=0)
    pdb_doi: str
    primary_citation_doi: str

    @model_validator(mode="after")
    def residue_mapping_is_consistent(self) -> "ParentPoseSpec":
        if self.target_author_residue_number + self.residue_number_offset != self.target_uniprot_residue_number:
            raise ValueError("target author-to-UniProt residue mapping is inconsistent")
        return self


class CovalentReferenceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pdb_id: str
    structure_path: str
    component_id: str
    component_template_path: str
    role: str
    pdb_doi: str
    primary_citation_doi: str
    interpretation_note: str | None = None


class ProtectedInteractionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interaction_id: str
    ligand_atom_name: str
    ligand_role: str
    protein_chain_id: str
    protein_residue_name: str
    protein_author_residue_number: int
    protein_uniprot_residue_number: int | None = None
    protein_atom_name: str
    maximum_distance_angstrom: float = Field(gt=0)


class ProtectedSubstituentBond(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor_atom_name: str
    substituent_atom_name: str
    reason: str


class ProtectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scaffold_core_atom_names: list[str] = Field(default_factory=list)
    protected_pharmacophore_atom_names: list[str] = Field(default_factory=list)
    protected_interactions: list[ProtectedInteractionSpec] = Field(default_factory=list)
    protected_substituent_bonds: list[ProtectedSubstituentBond] = Field(default_factory=list)


class ExitVectorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exit_vector_id: str
    anchor_atom_name: str
    leaving_atom_name: str
    priority: int = Field(ge=1)
    advance: bool
    allowed_transformations: list[str]
    reference_mapping: str


class LigandFirstCampaignSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    campaign_id: str
    planning_mode: Literal["curated-v1", "upstream-native", "rule-derived-v1"] = "curated-v1"
    target: TargetSpec
    parent_pose: ParentPoseSpec
    covalent_references: list[CovalentReferenceSpec] = Field(default_factory=list)
    ligand_atom_names_in_template_order: list[str] = Field(min_length=1)
    protection: ProtectionSpec = Field(default_factory=ProtectionSpec)
    exit_vectors: list[ExitVectorSpec] = Field(default_factory=list)
    allowed_warheads: list[str] = Field(min_length=1)
    source_assets: list[SourceAsset] = Field(min_length=1)

    @model_validator(mode="after")
    def atom_names_are_declared(self) -> "LigandFirstCampaignSpec":
        asset_paths = [asset.path for asset in self.source_assets]
        if len(asset_paths) != len(set(asset_paths)):
            raise ValueError("duplicate source asset paths")
        required_paths = {self.parent_pose.structure_path, self.parent_pose.ligand_template_path}
        for reference in self.covalent_references:
            required_paths.update((reference.structure_path, reference.component_template_path))
        if required_paths - set(asset_paths):
            raise ValueError("all consumed structures and templates require frozen source assets: "
                             + str(sorted(required_paths - set(asset_paths))))
        if self.planning_mode == "curated-v1":
            if not self.covalent_references or not self.exit_vectors:
                raise ValueError("curated-v1 requires covalent references and exit vectors")
            if not all((self.protection.scaffold_core_atom_names,
                        self.protection.protected_pharmacophore_atom_names,
                        self.protection.protected_interactions)):
                raise ValueError("curated-v1 requires scaffold, pharmacophore and interaction annotations")
        if self.planning_mode == "rule-derived-v1" and not self.protection.scaffold_core_atom_names:
            raise ValueError("rule-derived-v1 requires explicit protected scaffold atoms")
        names = self.ligand_atom_names_in_template_order
        if len(names) != len(set(names)):
            raise ValueError("ligand atom names must be unique")
        declared = set(names)
        referenced = set(self.protection.scaffold_core_atom_names)
        referenced.update(self.protection.protected_pharmacophore_atom_names)
        for interaction in self.protection.protected_interactions:
            referenced.add(interaction.ligand_atom_name)
        for bond in self.protection.protected_substituent_bonds:
            referenced.update((bond.anchor_atom_name, bond.substituent_atom_name))
        for vector in self.exit_vectors:
            referenced.update((vector.anchor_atom_name, vector.leaving_atom_name))
        missing = sorted(referenced - declared)
        if missing:
            raise ValueError(f"campaign references undeclared ligand atoms: {missing}")
        if self.parent_pose.target_uniprot_residue_number != self.target.nucleophile.uniprot_residue_number:
            raise ValueError("parent pose and target nucleophile residue numbers differ")
        return self


class InteractionMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interaction_id: str
    ligand_atom_name: str
    ligand_atom_index: int = Field(ge=0)
    protein_residue: str
    protein_atom_name: str
    distance_angstrom: float = Field(gt=0)
    maximum_distance_angstrom: float = Field(gt=0)
    retained: bool


class ExitVectorMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exit_vector_id: str
    anchor_atom_name: str
    anchor_atom_index: int = Field(ge=0)
    leaving_atom_name: str
    leaving_atom_index: int = Field(ge=0)
    distance_to_target_angstrom: float = Field(gt=0)
    vector_to_target_angle_degrees: float = Field(ge=0, le=180)
    advance: bool
    priority: int = Field(ge=1)
    reference_mapping: str


class LigandFirstInputAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    campaign_id: str
    status: Literal["PASS", "FAIL"]
    target_site: str
    parent_pose_id: str
    parent_ligand_id: str
    canonical_smiles: str | None
    asset_checks: dict[str, bool]
    atom_name_to_index: dict[str, int]
    scaffold_core_atom_indices: list[int]
    protected_pharmacophore_atom_indices: list[int]
    protected_interactions: list[InteractionMeasurement]
    exit_vectors: list[ExitVectorMeasurement]
    errors: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class PdbAtom:
    record: str
    serial: int
    atom_name: str
    altloc: str
    residue_name: str
    chain_id: str
    residue_number: int
    x: float
    y: float
    z: float
    element: str

    @property
    def xyz(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


def _infer_element(atom_name: str) -> str:
    stripped = atom_name.strip()
    if not stripped:
        raise ValueError("empty PDB atom name")
    if stripped[:2].upper() in {"CL", "BR"}:
        return stripped[:2].title()
    return stripped[0].upper()


def read_pdb_atoms(path: Path) -> list[PdbAtom]:
    atoms: list[PdbAtom] = []
    for line in path.read_text().splitlines():
        record = line[:6].strip()
        if record not in {"ATOM", "HETATM"}:
            continue
        altloc = line[16].strip()
        if altloc not in {"", "A"}:
            continue
        atom_name = line[12:16].strip()
        element = line[76:78].strip().title() if len(line) >= 78 else ""
        atoms.append(
            PdbAtom(
                record=record,
                serial=int(line[6:11]),
                atom_name=atom_name,
                altloc=altloc,
                residue_name=line[17:20].strip(),
                chain_id=line[21].strip(),
                residue_number=int(line[22:26]),
                x=float(line[30:38]),
                y=float(line[38:46]),
                z=float(line[46:54]),
                element=element or _infer_element(atom_name),
            )
        )
    return atoms


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_campaign(path: Path) -> LigandFirstCampaignSpec:
    return LigandFirstCampaignSpec.model_validate_json(path.read_text())


def verify_campaign_assets(campaign: LigandFirstCampaignSpec, campaign_dir: Path) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    for asset in campaign.source_assets:
        path = campaign_dir / asset.path
        checks[asset.path] = path.is_file() and _sha256(path) == asset.sha256
    return checks


def _distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def _angle_degrees(
    origin: tuple[float, float, float],
    vector_end: tuple[float, float, float],
    target: tuple[float, float, float],
) -> float:
    v1 = tuple(vector_end[i] - origin[i] for i in range(3))
    v2 = tuple(target[i] - origin[i] for i in range(3))
    norm1 = math.sqrt(sum(value * value for value in v1))
    norm2 = math.sqrt(sum(value * value for value in v2))
    if norm1 == 0 or norm2 == 0:
        raise ValueError("cannot calculate an angle from a zero-length vector")
    cosine = sum(x * y for x, y in zip(v1, v2)) / (norm1 * norm2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def build_parent_pose_mol(campaign: LigandFirstCampaignSpec, campaign_dir: Path) -> Chem.Mol:
    parent = campaign.parent_pose
    structure_path = campaign_dir / parent.structure_path
    template_path = campaign_dir / parent.ligand_template_path
    atoms = read_pdb_atoms(structure_path)
    ligand_atoms = [
        atom
        for atom in atoms
        if atom.record == "HETATM"
        and atom.residue_name == parent.ligand_component_id
        and atom.chain_id == parent.ligand_chain_id
        and atom.residue_number == parent.ligand_author_residue_number
        and atom.element != "H"
    ]
    expected_names = campaign.ligand_atom_names_in_template_order
    observed_names = [atom.atom_name for atom in ligand_atoms]
    if observed_names != expected_names:
        raise ValueError(
            "experimental ligand atom order differs from the audited CCD template order: "
            f"expected={expected_names}, observed={observed_names}"
        )
    template = Chem.MolFromMolFile(str(template_path), removeHs=True, sanitize=True)
    if template is None:
        raise ValueError(f"RDKit could not parse ligand template: {template_path}")
    if template.GetNumAtoms() != len(ligand_atoms):
        raise ValueError(
            f"template/experimental ligand atom counts differ: {template.GetNumAtoms()} != {len(ligand_atoms)}"
        )
    conformer = Chem.Conformer(template.GetNumAtoms())
    for index, (rd_atom, pdb_atom) in enumerate(zip(template.GetAtoms(), ligand_atoms)):
        if rd_atom.GetSymbol().upper() != pdb_atom.element.upper():
            raise ValueError(
                f"element mismatch at {index}/{pdb_atom.atom_name}: "
                f"template={rd_atom.GetSymbol()}, PDB={pdb_atom.element}"
            )
        rd_atom.SetProp("pdb_atom_name", pdb_atom.atom_name)
        rd_atom.SetIntProp("pdb_atom_serial", pdb_atom.serial)
        conformer.SetAtomPosition(index, Point3D(pdb_atom.x, pdb_atom.y, pdb_atom.z))
    template.RemoveAllConformers()
    template.AddConformer(conformer, assignId=True)
    template.SetProp("campaign_id", campaign.campaign_id)
    template.SetProp("source_pdb_id", parent.pdb_id)
    template.SetProp("source_component_id", parent.ligand_component_id)
    template.SetProp("atom_name_to_index_json", json.dumps({name: i for i, name in enumerate(expected_names)}))
    return template


def audit_ligand_first_inputs(
    campaign: LigandFirstCampaignSpec,
    campaign_dir: Path,
) -> tuple[LigandFirstInputAudit, Chem.Mol | None]:
    asset_checks = verify_campaign_assets(campaign, campaign_dir)
    errors = [f"asset missing or checksum mismatch: {path}" for path, ok in asset_checks.items() if not ok]
    warnings: list[str] = []
    if campaign.planning_mode == "rule-derived-v1":
        warnings.append("Rule-derived protection and exit vectors are geometric hypotheses; binding interaction retention is NOT_ASSESSED.")
    if campaign.planning_mode == "upstream-native":
        warnings.append(
            "Native enumeration does not use curated exit vectors or covalent references; "
            "input audit PASS does not establish attachment feasibility."
        )
        if not campaign.protection.protected_interactions:
            warnings.append("Binding interaction protection is NOT_ASSESSED: no curated annotations.")
        if not campaign.protection.scaffold_core_atom_names:
            warnings.append(
                "No curated scaffold annotation; native generation must require full-parent graph retention."
            )
    parent = campaign.parent_pose
    target_label = (
        f"{campaign.target.symbol}:{parent.protein_chain_id}:"
        f"{campaign.target.nucleophile.residue_name}{campaign.target.nucleophile.uniprot_residue_number}"
    )
    if errors:
        return (
            LigandFirstInputAudit(
                campaign_id=campaign.campaign_id,
                status="FAIL",
                target_site=target_label,
                parent_pose_id=parent.pdb_id,
                parent_ligand_id=parent.ligand_component_id,
                canonical_smiles=None,
                asset_checks=asset_checks,
                atom_name_to_index={},
                scaffold_core_atom_indices=[],
                protected_pharmacophore_atom_indices=[],
                protected_interactions=[],
                exit_vectors=[],
                errors=errors,
                warnings=warnings,
            ),
            None,
        )

    try:
        mol = build_parent_pose_mol(campaign, campaign_dir)
        atom_map = {atom.GetProp("pdb_atom_name"): atom.GetIdx() for atom in mol.GetAtoms()}
        pdb_atoms = read_pdb_atoms(campaign_dir / parent.structure_path)
        target_matches = [
            atom
            for atom in pdb_atoms
            if atom.record == "ATOM"
            and atom.chain_id == parent.protein_chain_id
            and atom.residue_name == campaign.target.nucleophile.residue_name
            and atom.residue_number == parent.target_author_residue_number
            and atom.atom_name == campaign.target.nucleophile.atom_name
        ]
        if len(target_matches) != 1:
            raise ValueError(f"expected one target nucleophile atom, found {len(target_matches)}")
        target_xyz = target_matches[0].xyz
        conformer = mol.GetConformer()
        interactions: list[InteractionMeasurement] = []
        for spec in campaign.protection.protected_interactions:
            protein_matches = [
                atom
                for atom in pdb_atoms
                if atom.record == "ATOM"
                and atom.chain_id == spec.protein_chain_id
                and atom.residue_name == spec.protein_residue_name
                and atom.residue_number == spec.protein_author_residue_number
                and atom.atom_name == spec.protein_atom_name
            ]
            if len(protein_matches) != 1:
                raise ValueError(
                    f"interaction {spec.interaction_id} expected one protein atom, found {len(protein_matches)}"
                )
            ligand_index = atom_map[spec.ligand_atom_name]
            point = conformer.GetAtomPosition(ligand_index)
            distance = _distance((point.x, point.y, point.z), protein_matches[0].xyz)
            retained = distance <= spec.maximum_distance_angstrom
            interactions.append(
                InteractionMeasurement(
                    interaction_id=spec.interaction_id,
                    ligand_atom_name=spec.ligand_atom_name,
                    ligand_atom_index=ligand_index,
                    protein_residue=f"{spec.protein_residue_name}{spec.protein_uniprot_residue_number}",
                    protein_atom_name=spec.protein_atom_name,
                    distance_angstrom=distance,
                    maximum_distance_angstrom=spec.maximum_distance_angstrom,
                    retained=retained,
                )
            )
            if not retained:
                errors.append(f"protected interaction is outside its distance limit: {spec.interaction_id}")

        vector_measurements: list[ExitVectorMeasurement] = []
        for spec in campaign.exit_vectors:
            anchor_index = atom_map[spec.anchor_atom_name]
            leaving_index = atom_map[spec.leaving_atom_name]
            if mol.GetBondBetweenAtoms(anchor_index, leaving_index) is None:
                raise ValueError(
                    f"exit vector atoms are not bonded: {spec.anchor_atom_name}-{spec.leaving_atom_name}"
                )
            anchor = conformer.GetAtomPosition(anchor_index)
            leaving = conformer.GetAtomPosition(leaving_index)
            anchor_xyz = (anchor.x, anchor.y, anchor.z)
            leaving_xyz = (leaving.x, leaving.y, leaving.z)
            vector_measurements.append(
                ExitVectorMeasurement(
                    exit_vector_id=spec.exit_vector_id,
                    anchor_atom_name=spec.anchor_atom_name,
                    anchor_atom_index=anchor_index,
                    leaving_atom_name=spec.leaving_atom_name,
                    leaving_atom_index=leaving_index,
                    distance_to_target_angstrom=_distance(anchor_xyz, target_xyz),
                    vector_to_target_angle_degrees=_angle_degrees(anchor_xyz, leaving_xyz, target_xyz),
                    advance=spec.advance,
                    priority=spec.priority,
                    reference_mapping=spec.reference_mapping,
                )
            )
        advancing = [item for item in vector_measurements if item.advance]
        if campaign.planning_mode == "curated-v1" and len(advancing) != 1:
            errors.append(f"V1 requires exactly one advancing exit vector, found {len(advancing)}")
        for reference in campaign.covalent_references:
            if reference.component_id == "0WN" and not reference.interpretation_note:
                errors.append("4G5J 0WN requires an explicit bound-form interpretation note")
        scaffold_indices = [atom_map[name] for name in campaign.protection.scaffold_core_atom_names]
        pharmacophore_indices = [
            atom_map[name] for name in campaign.protection.protected_pharmacophore_atom_names
        ]
        return (
            LigandFirstInputAudit(
                campaign_id=campaign.campaign_id,
                status="FAIL" if errors else "PASS",
                target_site=target_label,
                parent_pose_id=parent.pdb_id,
                parent_ligand_id=parent.ligand_component_id,
                canonical_smiles=Chem.MolToSmiles(mol, canonical=True),
                asset_checks=asset_checks,
                atom_name_to_index=atom_map,
                scaffold_core_atom_indices=scaffold_indices,
                protected_pharmacophore_atom_indices=pharmacophore_indices,
                protected_interactions=interactions,
                exit_vectors=sorted(vector_measurements, key=lambda item: item.priority),
                errors=errors,
                warnings=warnings,
            ),
            mol,
        )
    except Exception as exc:
        errors.append(str(exc))
        return (
            LigandFirstInputAudit(
                campaign_id=campaign.campaign_id,
                status="FAIL",
                target_site=target_label,
                parent_pose_id=parent.pdb_id,
                parent_ligand_id=parent.ligand_component_id,
                canonical_smiles=None,
                asset_checks=asset_checks,
                atom_name_to_index={},
                scaffold_core_atom_indices=[],
                protected_pharmacophore_atom_indices=[],
                protected_interactions=[],
                exit_vectors=[],
                errors=errors,
                warnings=warnings,
            ),
            None,
        )


def write_parent_pose_sdf(mol: Chem.Mol, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = Chem.SDWriter(str(output_path))
    try:
        writer.write(mol)
    finally:
        writer.close()
