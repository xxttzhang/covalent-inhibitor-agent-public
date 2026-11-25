from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import Chem

from .research import CovalentAssessment, RoutePlan, TargetPassport, utc_now


class WarheadHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    attachment_smarts: str
    compatible_residues: list[str]
    reaction_class: str
    covalent_mode: Literal["reversible", "irreversible"]
    intrinsic_reactivity_tier: Literal["low", "moderate", "high", "unknown"]
    evidence_note: str


DEFAULT_WARHEADS = {
    "acrylamide": WarheadHypothesis(
        name="acrylamide",
        attachment_smarts="[*:1]NC(=O)C=C",
        compatible_residues=["CYS"],
        reaction_class="thiol Michael addition",
        covalent_mode="irreversible",
        intrinsic_reactivity_tier="moderate",
        evidence_note="Common targeted-covalent hypothesis; scaffold-specific kinetics are required.",
    ),
    "propiolamide": WarheadHypothesis(
        name="propiolamide",
        attachment_smarts="[*:1]NC(=O)C#C",
        compatible_residues=["CYS"],
        reaction_class="thiol addition to activated alkyne",
        covalent_mode="irreversible",
        intrinsic_reactivity_tier="moderate",
        evidence_note="Alternative geometry hypothesis; comparative reactivity controls are required.",
    ),
    "chloroacetamide": WarheadHypothesis(
        name="chloroacetamide",
        attachment_smarts="[*:1]NC(=O)CCl",
        compatible_residues=["CYS"],
        reaction_class="nucleophilic substitution",
        covalent_mode="irreversible",
        intrinsic_reactivity_tier="high",
        evidence_note="Higher-reactivity probe hypothesis; not a default lead-optimization choice.",
    ),
    "cyanoacrylamide": WarheadHypothesis(
        name="cyanoacrylamide",
        attachment_smarts="[*:1]NC(=O)C(C#N)=C",
        compatible_residues=["CYS"],
        reaction_class="reversible thiol Michael addition",
        covalent_mode="reversible",
        intrinsic_reactivity_tier="moderate",
        evidence_note="Reversible-covalent hypothesis; residence time and reversibility require direct measurement.",
    ),
    "nitrile": WarheadHypothesis(
        name="nitrile",
        attachment_smarts="[*:1]C#N",
        compatible_residues=["CYS", "SER"],
        reaction_class="reversible nucleophilic addition to nitrile",
        covalent_mode="reversible",
        intrinsic_reactivity_tier="low",
        evidence_note="Low-reactivity reversible hypothesis; activation is strongly pocket-dependent.",
    ),
    "vinyl_sulfone": WarheadHypothesis(
        name="vinyl_sulfone",
        attachment_smarts="[*:1]S(=O)(=O)C=C",
        compatible_residues=["CYS"],
        reaction_class="thiol Michael addition to vinyl sulfone",
        covalent_mode="irreversible",
        intrinsic_reactivity_tier="high",
        evidence_note="High-reactivity hypothesis requiring stringent selectivity and intrinsic-reactivity controls.",
    ),
    "aldehyde": WarheadHypothesis(
        name="aldehyde",
        attachment_smarts="[*:1]C=O",
        compatible_residues=["CYS", "SER", "LYS"],
        reaction_class="reversible nucleophilic addition to aldehyde",
        covalent_mode="reversible",
        intrinsic_reactivity_tier="high",
        evidence_note="Reversible but intrinsically reactive hypothesis; chemical stability must be established.",
    ),
    "alpha_ketoamide": WarheadHypothesis(
        name="alpha_ketoamide",
        attachment_smarts="[*:1]NC(=O)C(=O)C",
        compatible_residues=["CYS", "SER"],
        reaction_class="reversible nucleophilic addition to alpha-ketoamide",
        covalent_mode="reversible",
        intrinsic_reactivity_tier="moderate",
        evidence_note="Reversible-covalent protease-oriented hypothesis; target-specific geometry is required.",
    ),
}


class AttachmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    project_id: str
    ligand_id: str
    ligand_smiles: str
    covalent_mode: Literal["reversible", "irreversible", "either"] = "either"
    pose_source: Literal[
        "experimental",
        "carsidock",
        "karmadock",
        "vina",
        "glide",
        "alphafold3",
        "other",
        "none",
    ] = "none"
    pose_reference: str | None = None
    scaffold_core_atom_indices: list[int] = Field(default_factory=list)
    key_interaction_atom_indices: list[int] = Field(default_factory=list)
    attachment_atom_indices: list[int] = Field(default_factory=list)
    protected_atom_indices: list[int] = Field(default_factory=list)
    solvent_exposed_atom_indices: list[int] = Field(default_factory=list)
    distance_to_target_angstrom: dict[int, float] = Field(default_factory=dict)
    vector_to_target_angle_degrees: dict[int, float] = Field(default_factory=dict)
    steric_clash_fraction: dict[int, float] = Field(default_factory=dict)
    interaction_retention_fraction: dict[int, float] = Field(default_factory=dict)
    synthetic_accessibility_score: dict[int, float] = Field(default_factory=dict)
    linker_environment: Literal["polar", "hydrophobic", "mixed", "unknown"] = "unknown"
    linker_topology_preferences: list[Literal["chain", "cyclic"]] = Field(
        default_factory=lambda: ["chain"]
    )
    warhead_names: list[str] = Field(default_factory=lambda: ["acrylamide", "propiolamide"])
    max_plans: int = Field(default=12, ge=1, le=100)
    route_override_approved: bool = False

    @model_validator(mode="after")
    def indices_nonnegative(self) -> "AttachmentRequest":
        groups = (
            self.attachment_atom_indices,
            self.protected_atom_indices,
            self.solvent_exposed_atom_indices,
            self.scaffold_core_atom_indices,
            self.key_interaction_atom_indices,
            list(self.distance_to_target_angstrom),
            list(self.vector_to_target_angle_degrees),
            list(self.steric_clash_fraction),
            list(self.interaction_retention_fraction),
            list(self.synthetic_accessibility_score),
        )
        if any(index < 0 for group in groups for index in group):
            raise ValueError("atom indices must be non-negative")
        if any(not 0 <= value <= 180 for value in self.vector_to_target_angle_degrees.values()):
            raise ValueError("vector angles must be in [0, 180] degrees")
        for name, values in (
            ("steric_clash_fraction", self.steric_clash_fraction),
            ("interaction_retention_fraction", self.interaction_retention_fraction),
            ("synthetic_accessibility_score", self.synthetic_accessibility_score),
        ):
            if any(not 0 <= value <= 1 for value in values.values()):
                raise ValueError(f"{name} values must be in [0, 1]")
        if self.pose_source == "none" and self.pose_reference is not None:
            raise ValueError("pose_reference requires a non-none pose_source")
        return self


class AttachmentCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = Field(ge=1)
    ligand_atom_index: int = Field(ge=0)
    atom_symbol: str
    exit_vector_class: str
    modifiable_probability: float = Field(ge=0, le=1)
    distance_to_residue_angstrom: float | None = Field(default=None, gt=0)
    vector_to_target_angle_degrees: float | None = Field(default=None, ge=0, le=180)
    steric_clash_fraction: float | None = Field(default=None, ge=0, le=1)
    interaction_loss_fraction: float | None = Field(default=None, ge=0, le=1)
    synthetic_accessibility_score: float | None = Field(default=None, ge=0, le=1)
    vector_score: float = Field(ge=0, le=1)
    evidence_complete: bool
    warhead_name: str
    reaction_class: str
    covalent_mode: Literal["reversible", "irreversible"]
    linker_necessity: Literal["direct_attachment", "short_spacer", "linker_required"]
    linker_heavy_atom_range: tuple[int, int]
    linker_flexible_bond_range: tuple[int, int]
    linker_topology_options: list[Literal["chain", "cyclic"]]
    linker_environment: Literal["polar", "hydrophobic", "mixed", "unknown"]
    heuristic_score: float = Field(ge=0, le=1)
    confidence: Literal["low", "medium", "high"]
    rationale: list[str]
    liabilities: list[str]
    required_controls: list[str]


class AttachmentPlanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "1.0"
    project_id: str
    ligand_id: str
    canonical_smiles: str | None
    status: Literal["READY", "NEEDS_STRUCTURE", "BLOCKED", "STOP"]
    target_site: str
    candidates: list[AttachmentCandidate]
    warnings: list[str]
    rejected_atom_indices: dict[int, str]
    generated_at: str = Field(default_factory=utc_now)
    disclaimer: str = (
        "Attachment rankings are medicinal-chemistry hypotheses. They are not reaction "
        "predictions, synthetic routes, or evidence of selective covalent engagement."
    )


def _existing_warhead_atoms(mol: Chem.Mol) -> set[int]:
    patterns = (
        "C=CC(=O)N",
        "C#CC(=O)N",
        "ClCC(=O)N",
    )
    atoms: set[int] = set()
    for smarts in patterns:
        query = Chem.MolFromSmarts(smarts)
        if query is not None:
            for match in mol.GetSubstructMatches(query):
                atoms.update(match)
    return atoms


def _exit_vector_class(atom: Chem.Atom) -> str:
    if atom.GetIsAromatic() and atom.GetAtomicNum() == 6:
        return "aryl_exit_vector"
    if atom.GetAtomicNum() in {7, 8, 16}:
        return "heteroatom_exit_vector"
    if atom.GetDegree() <= 1:
        return "terminal_exit_vector"
    return "aliphatic_exit_vector"


def _auto_attachment_atoms(mol: Chem.Mol) -> list[int]:
    return [
        atom.GetIdx()
        for atom in mol.GetAtoms()
        if atom.GetAtomicNum() in {6, 7, 8, 16}
        and atom.GetTotalNumHs() > 0
        and atom.GetFormalCharge() == 0
    ]


def _linker_range(distance: float | None) -> tuple[int, int]:
    if distance is None:
        return (0, 4)
    center = max(0, round((distance - 2.5) / 1.45))
    return (max(0, center - 1), min(10, center + 1))


def _linker_necessity(distance: float | None) -> Literal[
    "direct_attachment", "short_spacer", "linker_required"
]:
    if distance is None or distance > 6.0:
        return "linker_required"
    if distance <= 3.8:
        return "direct_attachment"
    return "short_spacer"


def plan_attachments(
    request: AttachmentRequest,
    passport: TargetPassport,
    assessment: CovalentAssessment,
    route: RoutePlan,
) -> AttachmentPlanResult:
    target_site = f"{passport.symbol}:{passport.site.chain_id}:{passport.site.site_id}"
    if assessment.decision == "STOP" or not passport.site.nucleophile_present:
        return AttachmentPlanResult(
            project_id=request.project_id,
            ligand_id=request.ligand_id,
            canonical_smiles=None,
            status="STOP",
            target_site=target_site,
            candidates=[],
            warnings=["The project assessment does not support the nominated covalent site."],
            rejected_atom_indices={},
        )
    if route.selected_route != "ligand-first" and not request.route_override_approved:
        return AttachmentPlanResult(
            project_id=request.project_id,
            ligand_id=request.ligand_id,
            canonical_smiles=None,
            status="BLOCKED",
            target_site=target_site,
            candidates=[],
            warnings=["The approved project route is not ligand-first."],
            rejected_atom_indices={},
        )
    mol = Chem.MolFromSmiles(request.ligand_smiles)
    if mol is None:
        raise ValueError("invalid ligand_smiles")
    atom_count = mol.GetNumAtoms()
    all_indices = set(range(atom_count))
    provided = set(request.attachment_atom_indices)
    referenced = (
        provided
        | set(request.protected_atom_indices)
        | set(request.solvent_exposed_atom_indices)
        | set(request.scaffold_core_atom_indices)
        | set(request.key_interaction_atom_indices)
        | set(request.distance_to_target_angstrom)
        | set(request.vector_to_target_angle_degrees)
        | set(request.steric_clash_fraction)
        | set(request.interaction_retention_fraction)
        | set(request.synthetic_accessibility_score)
    )
    invalid = sorted(referenced - all_indices)
    if invalid:
        raise ValueError(f"atom indices outside ligand range 0..{atom_count - 1}: {invalid}")

    candidate_indices = sorted(provided or set(_auto_attachment_atoms(mol)))
    protected = set(request.protected_atom_indices)
    exposed = set(request.solvent_exposed_atom_indices)
    existing_warhead = _existing_warhead_atoms(mol)
    rejected: dict[int, str] = {}
    warheads: list[WarheadHypothesis] = []
    for name in request.warhead_names:
        if name not in DEFAULT_WARHEADS:
            raise ValueError(f"unknown warhead hypothesis: {name}")
        hypothesis = DEFAULT_WARHEADS[name]
        if passport.site.residue_name.upper() in hypothesis.compatible_residues:
            if request.covalent_mode == "either" or hypothesis.covalent_mode == request.covalent_mode:
                warheads.append(hypothesis)
    if not warheads:
        return AttachmentPlanResult(
            project_id=request.project_id,
            ligand_id=request.ligand_id,
            canonical_smiles=Chem.MolToSmiles(mol, canonical=True),
            status="BLOCKED",
            target_site=target_site,
            candidates=[],
            warnings=["No selected warhead hypothesis is compatible with the nominated residue."],
            rejected_atom_indices={},
        )

    raw_plans: list[dict] = []
    for atom_index in candidate_indices:
        atom = mol.GetAtomWithIdx(atom_index)
        if atom_index in protected:
            rejected[atom_index] = "explicitly protected pharmacophore atom"
            continue
        if atom_index in existing_warhead:
            rejected[atom_index] = "part of an existing electrophilic motif"
            continue
        if atom.GetTotalNumHs() <= 0:
            rejected[atom_index] = "no replaceable hydrogen for the proposed exit vector"
            continue
        if atom.GetAtomicNum() == 7 and any(
            bond.GetBondType() == Chem.BondType.DOUBLE
            and bond.GetOtherAtom(atom).GetAtomicNum() == 6
            for bond in atom.GetBonds()
        ):
            rejected[atom_index] = "imine-like nitrogen is not a default attachment handle"
            continue

        vector_class = _exit_vector_class(atom)
        distance = request.distance_to_target_angstrom.get(atom_index)
        angle = request.vector_to_target_angle_degrees.get(atom_index)
        clash = request.steric_clash_fraction.get(atom_index)
        retention = request.interaction_retention_fraction.get(atom_index)
        synthetic = request.synthetic_accessibility_score.get(atom_index)
        modifiable = 0.75
        if atom.GetIsAromatic():
            modifiable -= 0.05
        if atom.GetAtomicNum() in {7, 8, 16}:
            modifiable -= 0.15
        if any(neighbor.GetIdx() in protected for neighbor in atom.GetNeighbors()):
            modifiable -= 0.20
        modifiable = max(0.0, min(1.0, modifiable))
        base_score = 0.25 + 0.25 * modifiable
        rationale = [f"Atom has a replaceable hydrogen and is classified as {vector_class}."]
        liabilities: list[str] = []
        if atom_index in exposed:
            base_score += 0.20
            rationale.append("User supplied solvent-exposure evidence for this atom.")
        else:
            liabilities.append("Solvent exposure is not established.")
        if distance is not None:
            if 3.0 <= distance <= 8.0:
                base_score += 0.20
                rationale.append(f"Provided target distance ({distance:.2f} Å) is linker-accessible.")
            elif 8.0 < distance <= 12.0:
                base_score += 0.08
                rationale.append(f"Provided target distance ({distance:.2f} Å) may require a longer linker.")
            else:
                base_score -= 0.15
                liabilities.append(f"Provided target distance ({distance:.2f} Å) is geometrically strained or remote.")
        else:
            liabilities.append("No 3D distance to the target nucleophile was supplied.")
        if angle is None:
            liabilities.append("No attachment-vector angle to the target nucleophile was supplied.")
        elif angle <= 30:
            base_score += 0.15
            rationale.append(f"Provided exit-vector angle ({angle:.1f}°) points toward the target site.")
        elif angle <= 60:
            base_score += 0.05
            rationale.append(f"Provided exit-vector angle ({angle:.1f}°) is moderately aligned.")
        else:
            base_score -= 0.15
            liabilities.append(f"Provided exit-vector angle ({angle:.1f}°) points away from the target site.")
        if clash is None:
            liabilities.append("No local steric-clash estimate was supplied.")
        elif clash <= 0.15:
            base_score += 0.10
            rationale.append(f"Provided local clash fraction ({clash:.2f}) is low.")
        elif clash > 0.35:
            base_score -= 0.15
            liabilities.append(f"Provided local clash fraction ({clash:.2f}) is high.")
        if retention is None:
            liabilities.append("Retention of the parent ligand's key interactions is not quantified.")
        elif retention >= 0.80:
            base_score += 0.10
            rationale.append(f"Predicted parent-interaction retention is {retention:.2f}.")
        elif retention < 0.60:
            base_score -= 0.15
            liabilities.append(f"Predicted parent-interaction retention ({retention:.2f}) is low.")
        if synthetic is None:
            liabilities.append("Attachment-site synthetic accessibility is not supported by a route assessment.")
        elif synthetic >= 0.60:
            base_score += 0.05
            rationale.append(f"Provided synthetic-accessibility score is {synthetic:.2f}.")
        else:
            base_score -= 0.10
            liabilities.append(f"Provided synthetic-accessibility score ({synthetic:.2f}) is low.")
        if atom.GetIsAromatic():
            base_score += 0.05
            liabilities.append("Aryl functionalization feasibility and regioselectivity require route review.")
        if atom.GetAtomicNum() in {7, 8, 16}:
            liabilities.append("Attachment may disrupt a ligand hydrogen-bond donor or acceptor pattern.")
        if any(neighbor.GetIdx() in protected for neighbor in atom.GetNeighbors()):
            base_score -= 0.15
            liabilities.append("The exit vector is adjacent to a protected pharmacophore atom.")

        for warhead in warheads:
            score = base_score
            if warhead.intrinsic_reactivity_tier == "high":
                score -= 0.15
                warhead_liabilities = [
                    "Higher intrinsic reactivity increases nonspecific-labeling risk."
                ]
            else:
                warhead_liabilities = []
            raw_plans.append(
                {
                    "ligand_atom_index": atom_index,
                    "atom_symbol": atom.GetSymbol(),
                    "exit_vector_class": vector_class,
                    "modifiable_probability": round(modifiable, 4),
                    "distance_to_residue_angstrom": distance,
                    "vector_to_target_angle_degrees": angle,
                    "steric_clash_fraction": clash,
                    "interaction_loss_fraction": None if retention is None else round(1 - retention, 4),
                    "synthetic_accessibility_score": synthetic,
                    "vector_score": round(max(0.0, min(1.0, base_score)), 4),
                    "evidence_complete": all(
                        value is not None for value in (distance, angle, clash, retention, synthetic)
                    )
                    and atom_index in exposed
                    and request.pose_source != "none"
                    and request.pose_reference is not None,
                    "warhead_name": warhead.name,
                    "reaction_class": warhead.reaction_class,
                    "covalent_mode": warhead.covalent_mode,
                    "linker_necessity": _linker_necessity(distance),
                    "linker_heavy_atom_range": _linker_range(distance),
                    "linker_flexible_bond_range": (
                        max(0, _linker_range(distance)[0] - 1),
                        max(0, _linker_range(distance)[1] - 1),
                    ),
                    "linker_topology_options": request.linker_topology_preferences,
                    "linker_environment": request.linker_environment,
                    "heuristic_score": round(max(0.0, min(1.0, score)), 4),
                    "confidence": (
                        "high"
                        if all(value is not None for value in (distance, angle, clash, retention, synthetic))
                        and atom_index in exposed
                        and request.pose_source == "experimental"
                        and request.pose_reference is not None
                        else "medium"
                        if all(value is not None for value in (distance, angle, clash, retention))
                        and atom_index in exposed
                        and request.pose_source != "none"
                        and request.pose_reference is not None
                        else "low"
                    ),
                    "rationale": [*rationale, warhead.evidence_note],
                    "liabilities": [*liabilities, *warhead_liabilities],
                    "required_controls": [
                        "Matched parent ligand to measure loss of reversible affinity.",
                        "Matched nonreactive warhead analog.",
                        "Time-dependent inhibition with kinact/KI where applicable.",
                        "Intact-protein and peptide-level mass spectrometry.",
                        "Target-residue mutant or orthogonal site-dependence control.",
                        "Glutathione or comparable intrinsic-reactivity assay.",
                    ],
                }
            )
    raw_plans.sort(
        key=lambda item: (-item["heuristic_score"], item["ligand_atom_index"], item["warhead_name"])
    )
    candidates = [
        AttachmentCandidate(rank=rank, **item)
        for rank, item in enumerate(raw_plans[: request.max_plans], start=1)
    ]
    warnings = [
        "Medicinal-chemistry filters and warhead classes are context-dependent guidelines.",
        "Protect conserved ligand interactions before accepting an exit vector.",
    ]
    status: Literal["READY", "NEEDS_STRUCTURE", "BLOCKED", "STOP"]
    if not candidates:
        status = "BLOCKED"
        warnings.append("No acceptable attachment atom remained after exclusions.")
    elif not any(candidate.evidence_complete for candidate in candidates):
        status = "NEEDS_STRUCTURE"
        warnings.append(
            "No candidate has the complete noncovalent-pose, exposure, distance, vector-angle, "
            "steric-clash, interaction-retention, and synthetic-accessibility evidence required "
            "to advance into warhead/linker generation."
        )
    else:
        status = "READY"
    return AttachmentPlanResult(
        project_id=request.project_id,
        ligand_id=request.ligand_id,
        canonical_smiles=Chem.MolToSmiles(mol, canonical=True),
        status=status,
        target_site=target_site,
        candidates=candidates,
        warnings=warnings,
        rejected_atom_indices=rejected,
    )
