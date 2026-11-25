from __future__ import annotations

import math
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import Chem
from rdkit.Chem import AllChem

from .ligand_first_inputs import LigandFirstCampaignSpec, PdbAtom, read_pdb_atoms
from .rdkit_covalentizer import PARENT_ATOM_MAP_BASE, WARHEAD_ATOM_MAPS


class LigandFirstGeometryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_scaffold_rmsd_angstrom: float = Field(default=2.0, gt=0)
    min_interaction_retention_fraction: float = Field(default=0.70, ge=0, le=1)
    covalent_cs_bond_window_angstrom: tuple[float, float] = (1.65, 2.15)
    attack_angle_window_degrees: tuple[float, float] = (80.0, 140.0)
    max_dummy_to_target_sg_distance_angstrom: float = Field(default=0.50, ge=0)
    max_strain_excess_over_parent_kcal_mol: float = Field(default=15.0, gt=0)
    clash_vdw_scale: float = Field(default=0.75, gt=0, lt=1)
    max_clash_fraction: float = Field(default=0.20, ge=0, le=1)
    max_severe_clash_pair_count: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def ordered_windows(self) -> "LigandFirstGeometryPolicy":
        for name, window in (
            ("covalent_cs_bond_window_angstrom", self.covalent_cs_bond_window_angstrom),
            ("attack_angle_window_degrees", self.attack_angle_window_degrees),
        ):
            if window[0] >= window[1]:
                raise ValueError(f"{name} must be increasing")
        return self


class RetainedInteraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interaction_id: str
    ligand_atom_map_number: int = Field(ge=0)
    protein_residue: str
    protein_atom_name: str
    distance_angstrom: float = Field(gt=0)
    maximum_distance_angstrom: float = Field(gt=0)
    retained: bool


class PoseGeometryEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pose_index: int = Field(ge=0)
    status: Literal["PASS", "FAIL"]
    carsidock_score: float | None = None
    pose_loss: float | None = None
    pose_mdn_score: float | None = None
    scaffold_rmsd_angstrom: float = Field(ge=0)
    interaction_retention_fraction: float = Field(ge=0, le=1)
    protected_interactions: list[RetainedInteraction]
    covalent_cs_bond_distance_angstrom: float = Field(gt=0)
    actual_sg_cbeta_distance_angstrom: float | None = Field(default=None, ge=0)
    actual_sg_cbeta_calpha_angle_degrees: float | None = Field(default=None, ge=0, le=180)
    attack_angle_s_cbeta_calpha_degrees: float = Field(ge=0, le=180)
    dummy_to_target_sg_distance_angstrom: float = Field(ge=0)
    ligand_strain_kcal_mol: float | None = None
    parent_ligand_strain_kcal_mol: float | None = None
    strain_excess_over_parent_kcal_mol: float | None = None
    strain_forcefield: str | None = None
    strain_estimator: Literal["legacy_unchecked", "local_relaxation_convergence_checked_v1"] = "legacy_unchecked"
    severe_clash_pair_count: int = Field(ge=0)
    clashing_ligand_atom_count: int = Field(ge=0)
    ligand_heavy_atom_count: int = Field(gt=0)
    clash_fraction: float = Field(ge=0, le=1)
    clash_method: str = "legacy_whole_target_residue_excluded"
    gate_flags: dict[str, bool]
    failure_reasons: list[str]


class LigandFirstGeometryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    campaign_id: str
    candidate_id: str
    carsidock_run_id: str
    status: Literal["PASS", "FAIL"]
    pose_count: int = Field(ge=0)
    passing_pose_count: int = Field(ge=0)
    ensemble_pass_fraction: float = Field(ge=0, le=1)
    selected_pose_index: int | None = Field(default=None, ge=0)
    selected_pose: PoseGeometryEvaluation | None = None
    poses: list[PoseGeometryEvaluation]
    policy: LigandFirstGeometryPolicy
    errors: list[str]
    disclaimer: str = (
        "This evaluates pose geometry and internal strain. A PASS is not evidence of kinact, "
        "affinity, selectivity, synthetic accessibility, safety, or cellular activity."
    )


def parse_geometry_policy(value):
    """Missing version means the exact historical policy; never upgrade it."""
    from .ligand_geometry_assessment import ProvisionalGeometryPolicy, VERSION
    if "version" not in value:
        return LigandFirstGeometryPolicy.model_validate(value)
    if value["version"] == VERSION:
        return ProvisionalGeometryPolicy.model_validate(value)
    raise ValueError("Unsupported frozen ligand geometry policy version")


def provisional_geometry_policy():
    from .ligand_geometry_assessment import ProvisionalGeometryPolicy
    return ProvisionalGeometryPolicy()


def _point(mol: Chem.Mol, atom_index: int) -> tuple[float, float, float]:
    position = mol.GetConformer().GetAtomPosition(atom_index)
    return (position.x, position.y, position.z)


def _distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def _angle(
    first: tuple[float, float, float],
    vertex: tuple[float, float, float],
    third: tuple[float, float, float],
) -> float:
    left = tuple(first[i] - vertex[i] for i in range(3))
    right = tuple(third[i] - vertex[i] for i in range(3))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("cannot calculate angle from zero-length vector")
    cosine = sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


def _atom_map_indices(mol: Chem.Mol) -> dict[int, int]:
    mapping: dict[int, int] = {}
    duplicates: list[int] = []
    for atom in mol.GetAtoms():
        atom_map = atom.GetAtomMapNum()
        if not atom_map:
            continue
        if atom_map in mapping:
            duplicates.append(atom_map)
        mapping[atom_map] = atom.GetIdx()
    if duplicates:
        raise ValueError(f"duplicate atom-map numbers in pose: {sorted(set(duplicates))}")
    return mapping


def _find_protein_atom(
    atoms: list[PdbAtom],
    *,
    chain_id: str,
    residue_name: str,
    residue_number: int,
    atom_name: str,
) -> PdbAtom:
    matches = [
        atom
        for atom in atoms
        if atom.record == "ATOM"
        and atom.chain_id == chain_id
        and atom.residue_name == residue_name
        and atom.residue_number == residue_number
        and atom.atom_name == atom_name
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one protein atom {chain_id}:{residue_name}:{residue_number}:{atom_name}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _scaffold_rmsd(
    parent_pose: Chem.Mol,
    pose: Chem.Mol,
    campaign: LigandFirstCampaignSpec,
    pose_maps: dict[int, int],
) -> float:
    parent_names = {
        atom.GetProp("pdb_atom_name"): atom.GetIdx()
        for atom in parent_pose.GetAtoms()
        if atom.HasProp("pdb_atom_name")
    }
    squared: list[float] = []
    for name in campaign.protection.scaffold_core_atom_names:
        parent_index = parent_names[name]
        atom_map = PARENT_ATOM_MAP_BASE + parent_index
        if atom_map not in pose_maps:
            raise ValueError(f"pose is missing protected parent atom map {atom_map}/{name}")
        distance = _distance(_point(parent_pose, parent_index), _point(pose, pose_maps[atom_map]))
        squared.append(distance * distance)
    return math.sqrt(sum(squared) / len(squared))


def _interaction_measurements(
    pose: Chem.Mol,
    pose_maps: dict[int, int],
    parent_pose: Chem.Mol,
    protein_atoms: list[PdbAtom],
    campaign: LigandFirstCampaignSpec,
) -> list[RetainedInteraction]:
    parent_names = {
        atom.GetProp("pdb_atom_name"): atom.GetIdx()
        for atom in parent_pose.GetAtoms()
        if atom.HasProp("pdb_atom_name")
    }
    measurements: list[RetainedInteraction] = []
    for spec in campaign.protection.protected_interactions:
        parent_index = parent_names[spec.ligand_atom_name]
        atom_map = PARENT_ATOM_MAP_BASE + parent_index
        if atom_map not in pose_maps:
            raise ValueError(f"pose is missing protected interaction atom map {atom_map}")
        protein_atom = _find_protein_atom(
            protein_atoms,
            chain_id=spec.protein_chain_id,
            residue_name=spec.protein_residue_name,
            residue_number=spec.protein_author_residue_number,
            atom_name=spec.protein_atom_name,
        )
        distance = _distance(_point(pose, pose_maps[atom_map]), protein_atom.xyz)
        measurements.append(
            RetainedInteraction(
                interaction_id=spec.interaction_id,
                ligand_atom_map_number=atom_map,
                protein_residue=(f"{spec.protein_residue_name}{spec.protein_uniprot_residue_number}"
                    if spec.protein_uniprot_residue_number is not None else
                    f"{spec.protein_chain_id}:{spec.protein_residue_name}{spec.protein_author_residue_number} (author)"),
                protein_atom_name=spec.protein_atom_name,
                distance_angstrom=distance,
                maximum_distance_angstrom=spec.maximum_distance_angstrom,
                retained=distance <= spec.maximum_distance_angstrom,
            )
        )
    return measurements


def _local_relaxation_energy(pose_field, minimum_field, *, max_iterations=1000) -> float | None:
    """Energy release to a converged local minimum, not global conformational strain."""
    initial = float(pose_field.CalcEnergy())
    if not math.isfinite(initial) or minimum_field.Minimize(maxIts=max_iterations) != 0:
        return None
    final = float(minimum_field.CalcEnergy())
    if not math.isfinite(final):
        return None
    return max(0.0, initial - final)


def _strain_energy(pose_without_dummy: Chem.Mol) -> tuple[float | None, str | None]:
    molecule = Chem.Mol(pose_without_dummy)
    try:
        Chem.SanitizeMol(molecule)
        molecule = Chem.AddHs(molecule, addCoords=True)
        properties = AllChem.MMFFGetMoleculeProperties(molecule, mmffVariant="MMFF94s")
        if properties is not None:
            pose_field = AllChem.MMFFGetMoleculeForceField(molecule, properties)
            if pose_field is not None:
                minimum = Chem.Mol(molecule)
                minimum_properties = AllChem.MMFFGetMoleculeProperties(
                    minimum, mmffVariant="MMFF94s"
                )
                minimum_field = AllChem.MMFFGetMoleculeForceField(minimum, minimum_properties)
                if minimum_field is not None:
                    return _local_relaxation_energy(pose_field, minimum_field), "MMFF94s"
        pose_field = AllChem.UFFGetMoleculeForceField(molecule)
        if pose_field is None:
            return None, None
        minimum = Chem.Mol(molecule)
        minimum_field = AllChem.UFFGetMoleculeForceField(minimum)
        if minimum_field is None:
            return None, None
        return _local_relaxation_energy(pose_field, minimum_field), "UFF"
    except Exception:
        return None, None


def _clashes(
    pose_without_dummy: Chem.Mol,
    protein_atoms: list[PdbAtom],
    campaign: LigandFirstCampaignSpec,
    policy: LigandFirstGeometryPolicy,
) -> tuple[int, int, int, float]:
    table = Chem.GetPeriodicTable()
    target_author_number = campaign.parent_pose.target_author_residue_number
    target_chain = campaign.parent_pose.protein_chain_id
    if (campaign.target.nucleophile.residue_name != "CYS"
            or campaign.target.nucleophile.atom_name != "SG"):
        raise ValueError("Product clash exclusions require the supported CYS SG topology")
    # Explicit CYS side-chain topology joined to the mapped product beta carbon.
    # Cross-interface 1-2, 1-3 and 1-4 pairs are not ordinary nonbonded pairs.
    # Their geometry/energetics remain a separate assessment, not an all-residue exemption.
    receptor_bond_depth = {"SG": 0, "CB": 1, "CA": 2}
    for name, element in (("SG", "S"), ("CB", "C"), ("CA", "C")):
        atom = _find_protein_atom(protein_atoms, chain_id=target_chain,
            residue_name="CYS", residue_number=target_author_number, atom_name=name)
        if atom.element.upper() != element:
            raise ValueError("Target CYS topology atom has an unexpected element")
    maps = _atom_map_indices(pose_without_dummy)
    beta = maps[WARHEAD_ATOM_MAPS["reaction_beta_carbon"]]
    ligand_bond_distances = Chem.GetDistanceMatrix(pose_without_dummy)[beta]
    receptor = [
        atom
        for atom in protein_atoms
        if atom.record == "ATOM"
        and atom.element.upper() != "H"
    ]
    severe_pairs = 0
    clashing_ligand_atoms: set[int] = set()
    ligand_heavy = [atom for atom in pose_without_dummy.GetAtoms() if atom.GetAtomicNum() > 1]
    for ligand_atom in ligand_heavy:
        ligand_xyz = _point(pose_without_dummy, ligand_atom.GetIdx())
        ligand_radius = table.GetRvdw(ligand_atom.GetAtomicNum())
        for protein_atom in receptor:
            if (protein_atom.chain_id == target_chain
                    and protein_atom.residue_number == target_author_number
                    and protein_atom.residue_name == "CYS"
                    and protein_atom.atom_name in receptor_bond_depth
                    and receptor_bond_depth[protein_atom.atom_name] + 1
                        + ligand_bond_distances[ligand_atom.GetIdx()] <= 3):
                continue
            if not all(math.isfinite(value) for value in protein_atom.xyz):
                raise ValueError("Protein clash check requires finite receptor coordinates")
            try:
                protein_atomic_number = table.GetAtomicNumber(protein_atom.element)
            except Exception as error:
                raise ValueError("Unknown receptor element in clash check") from error
            if protein_atomic_number <= 1:
                continue
            threshold = policy.clash_vdw_scale * (
                ligand_radius + table.GetRvdw(protein_atomic_number)
            )
            if _distance(ligand_xyz, protein_atom.xyz) < threshold:
                severe_pairs += 1
                clashing_ligand_atoms.add(ligand_atom.GetIdx())
    heavy_count = len(ligand_heavy)
    fraction = len(clashing_ligand_atoms) / heavy_count if heavy_count else 1.0
    return severe_pairs, len(clashing_ligand_atoms), heavy_count, fraction


def _property_float(mol: Chem.Mol, name: str) -> float | None:
    if not mol.HasProp(name):
        return None
    try:
        return float(mol.GetProp(name))
    except ValueError:
        return None


def evaluate_carsidock_geometry(
    *,
    campaign: LigandFirstCampaignSpec,
    parent_pose: Chem.Mol,
    candidate_id: str,
    carsidock_run_id: str,
    raw_poses: list[Chem.Mol],
    no_dummy_poses: list[Chem.Mol],
    receptor_pdb: Path,
    policy: LigandFirstGeometryPolicy | None = None,
) -> LigandFirstGeometryResult:
    from .ligand_geometry_assessment import (
        ProvisionalGeometryPolicy, GeometryResultV2, make_pose, ensemble_assessment, ensemble_status,
    )
    policy = policy or LigandFirstGeometryPolicy()
    strict = isinstance(policy, ProvisionalGeometryPolicy)
    errors: list[str] = []
    if len(raw_poses) != len(no_dummy_poses):
        errors.append(
            f"raw/no-dummy pose counts differ: {len(raw_poses)} != {len(no_dummy_poses)}"
        )
    protein_atoms = read_pdb_atoms(receptor_pdb)
    target = _find_protein_atom(
        protein_atoms,
        chain_id=campaign.parent_pose.protein_chain_id,
        residue_name=campaign.target.nucleophile.residue_name,
        residue_number=campaign.parent_pose.target_author_residue_number,
        atom_name=campaign.target.nucleophile.atom_name,
    )
    parent_strain, parent_forcefield = _strain_energy(parent_pose)
    evaluations: list[PoseGeometryEvaluation] = []
    for index, (raw_pose, no_dummy_pose) in enumerate(zip(raw_poses, no_dummy_poses)):
        try:
            if strict and any(molecule.GetNumConformers() != 1 or not all(
                    math.isfinite(float(value)) for point in molecule.GetConformer().GetPositions() for value in point)
                    for molecule in (raw_pose, no_dummy_pose)):
                raise ValueError("Pose geometry requires finite coordinates and one conformer")
            raw_maps = _atom_map_indices(raw_pose)
            no_dummy_maps = _atom_map_indices(no_dummy_pose)
            scaffold_rmsd = _scaffold_rmsd(parent_pose, no_dummy_pose, campaign, no_dummy_maps)
            interactions = _interaction_measurements(
                no_dummy_pose, no_dummy_maps, parent_pose, protein_atoms, campaign
            )
            retention = (
                sum(item.retained for item in interactions) / len(interactions)
                if interactions
                else 0.0
            )
            reaction_index = raw_maps[WARHEAD_ATOM_MAPS["reaction_beta_carbon"]]
            alpha_index = raw_maps[WARHEAD_ATOM_MAPS["electrophile_alpha_carbon"]]
            sulfur = [
                atom.GetIdx()
                for atom in raw_pose.GetAtoms()
                if atom.GetSymbol() == "S" and atom.GetAtomMapNum() == 0
            ]
            if len(sulfur) != 1:
                raise ValueError(f"expected one CarsiDock dummy sulfur, found {len(sulfur)}")
            sulfur_index = sulfur[0]
            if raw_pose.GetBondBetweenAtoms(sulfur_index, reaction_index) is None:
                raise ValueError("dummy sulfur is not bonded to the mapped reaction beta carbon")
            cs_distance = _distance(_point(raw_pose, sulfur_index), _point(raw_pose, reaction_index))
            attack_angle = _angle(
                _point(raw_pose, sulfur_index),
                _point(raw_pose, reaction_index),
                _point(raw_pose, alpha_index),
            )
            dummy_target_distance = _distance(_point(raw_pose, sulfur_index), target.xyz)
            actual_cs_distance = _distance(target.xyz, _point(raw_pose, reaction_index))
            actual_product_angle = _angle(target.xyz, _point(raw_pose, reaction_index),
                                          _point(raw_pose, alpha_index))
            strain, forcefield = _strain_energy(no_dummy_pose)
            strain_excess = (
                strain - parent_strain
                if strain is not None and parent_strain is not None and forcefield == parent_forcefield
                else None
            )
            severe_pairs, clashing_atoms, heavy_count, clash_fraction = _clashes(
                no_dummy_pose, protein_atoms, campaign, policy
            )
            gate_flags = {
                "scaffold_rmsd": scaffold_rmsd <= policy.max_scaffold_rmsd_angstrom,
                "interaction_retention": retention >= policy.min_interaction_retention_fraction,
                "covalent_cs_distance": (
                    policy.covalent_cs_bond_window_angstrom[0]
                    <= cs_distance
                    <= policy.covalent_cs_bond_window_angstrom[1]
                ),
                "attack_angle": (
                    policy.attack_angle_window_degrees[0]
                    <= attack_angle
                    <= policy.attack_angle_window_degrees[1]
                ),
                "actual_target_cs_distance": (
                    policy.covalent_cs_bond_window_angstrom[0]
                    <= actual_cs_distance
                    <= policy.covalent_cs_bond_window_angstrom[1]
                ),
                "actual_target_product_angle": (
                    policy.attack_angle_window_degrees[0]
                    <= actual_product_angle
                    <= policy.attack_angle_window_degrees[1]
                ),
                "dummy_target_alignment": (
                    dummy_target_distance <= policy.max_dummy_to_target_sg_distance_angstrom
                ),
                "ligand_strain": (
                    strain_excess is not None
                    and strain_excess <= policy.max_strain_excess_over_parent_kcal_mol
                ),
                "protein_clash": clash_fraction <= policy.max_clash_fraction,
                "severe_protein_clash_absent": severe_pairs <= policy.max_severe_clash_pair_count,
            }
            failures = [name for name, passed in gate_flags.items() if not passed]
            evaluations.append(
                PoseGeometryEvaluation(
                    pose_index=index,
                    status="FAIL" if failures else "PASS",
                    carsidock_score=_property_float(raw_pose, "CarsiScore"),
                    pose_loss=_property_float(raw_pose, "loss"),
                    pose_mdn_score=_property_float(raw_pose, "score"),
                    scaffold_rmsd_angstrom=scaffold_rmsd,
                    interaction_retention_fraction=retention,
                    protected_interactions=interactions,
                    covalent_cs_bond_distance_angstrom=cs_distance,
                    actual_sg_cbeta_distance_angstrom=actual_cs_distance,
                    actual_sg_cbeta_calpha_angle_degrees=actual_product_angle,
                    attack_angle_s_cbeta_calpha_degrees=attack_angle,
                    dummy_to_target_sg_distance_angstrom=dummy_target_distance,
                    ligand_strain_kcal_mol=strain,
                    parent_ligand_strain_kcal_mol=parent_strain,
                    strain_excess_over_parent_kcal_mol=strain_excess,
                    strain_forcefield=forcefield,
                    strain_estimator="local_relaxation_convergence_checked_v1",
                    severe_clash_pair_count=severe_pairs,
                    clashing_ligand_atom_count=clashing_atoms,
                    ligand_heavy_atom_count=heavy_count,
                    clash_fraction=clash_fraction,
                    clash_method="cys_product_cross_bond_exclusions_through_1_4_v2",
                    gate_flags=gate_flags,
                    failure_reasons=failures,
                )
            )
            if strict:
                evaluations[-1] = make_pose(evaluations[-1], policy, parent_forcefield)
        except Exception as exc:
            if evaluations and evaluations[-1].pose_index == index:
                evaluations.pop()
            errors.append(f"pose {index}: {type(exc).__name__}: {exc}")

    passing = [pose for pose in evaluations if pose.status == "PASS"]
    selected = min(
        passing,
        key=lambda pose: (
            float("inf") if pose.carsidock_score is None else pose.carsidock_score,
            pose.scaffold_rmsd_angstrom,
            pose.pose_index,
        ),
        default=None,
    )
    count = len(raw_poses)
    assessment = ensemble_assessment(evaluations, count, errors) if strict else None
    if strict and not assessment.review_eligible:
        selected = None
    result_type = GeometryResultV2 if strict else LigandFirstGeometryResult
    return result_type(
        **({"assessment": assessment} if strict else {}),
        campaign_id=campaign.campaign_id,
        candidate_id=candidate_id,
        carsidock_run_id=carsidock_run_id,
        status=ensemble_status(evaluations, assessment) if strict else ("PASS" if selected is not None and not errors else "FAIL"),
        pose_count=count,
        passing_pose_count=len(passing),
        ensemble_pass_fraction=len(passing) / count if count else 0.0,
        selected_pose_index=selected.pose_index if selected is not None else None,
        selected_pose=selected,
        poses=evaluations,
        policy=policy,
        errors=errors,
    )


def load_sdf_poses(path: Path) -> list[Chem.Mol]:
    return [
        molecule
        for molecule in Chem.SDMolSupplier(str(path), removeHs=False, sanitize=False)
        if molecule is not None
    ]
