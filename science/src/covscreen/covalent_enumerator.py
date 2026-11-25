from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import Chem

from .attachment import DEFAULT_WARHEADS
from .research import utc_now


class CovalentEnumerationRequest(BaseModel):
    """Deterministic, traceable warhead enumeration for a 2D scaffold.

    This is deliberately not presented as a learned covalent-generation model.
    It attaches a curated warhead through the dummy atom in DEFAULT_WARHEADS and
    records the requested scaffold atom and chemistry checks for every result.
    """

    model_config = ConfigDict(extra="forbid")
    request_id: str
    scaffold_smiles: str
    attachment_atom_indices: list[int] = Field(default_factory=list)
    protected_atom_indices: list[int] = Field(default_factory=list)
    allowed_warheads: list[str] = Field(min_length=1)
    allowed_linkers: list[Literal["direct", "methylene", "ethylene"]] = Field(
        default_factory=lambda: ["direct"]
    )
    max_candidates: int = Field(default=100, ge=1, le=2000)

    @model_validator(mode="after")
    def valid_indices(self) -> "CovalentEnumerationRequest":
        if any(
            index < 0
            for index in (*self.attachment_atom_indices, *self.protected_atom_indices)
        ):
            raise ValueError("atom indices must be non-negative")
        if len(set(self.allowed_linkers)) != len(self.allowed_linkers):
            raise ValueError("allowed_linkers must not contain duplicates")
        return self


class CovalentEnumerationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = Field(ge=1)
    candidate_id: str
    canonical_smiles: str
    scaffold_attachment_atom_index: int = Field(ge=0)
    warhead_name: str
    linker_name: Literal["direct", "methylene", "ethylene"]
    linker_heavy_atom_count: int = Field(ge=0, le=2)
    warhead_attachment_smarts: str
    reaction_class: str
    covalent_mode: Literal["reversible", "irreversible"]
    intrinsic_reactivity_tier: Literal["low", "moderate", "high", "unknown"]
    chemistry_gate_pass: bool
    scaffold_retained: bool
    single_component: bool
    formal_charge: int
    radical_electron_count: int = Field(ge=0)
    warhead_match_count: int = Field(ge=0)
    provenance: Literal["deterministic-rdkit-enumeration"] = "deterministic-rdkit-enumeration"


class CovalentEnumerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str
    status: Literal["PASS", "FAIL"]
    candidates: list[CovalentEnumerationCandidate]
    rejected: list[str]
    generated_at: str = Field(default_factory=utc_now)
    disclaimer: str = (
        "This deterministic RDKit enumerator validates attachment chemistry only. It does not "
        "predict binding, covalent reaction rate, selectivity, synthesis, or biological activity."
    )


LINKER_ATOMIC_NUMBERS = {
    "direct": (),
    "methylene": (6,),
    "ethylene": (6, 6),
}

WARHEAD_VALIDATION_SMARTS = {
    "acrylamide": "NC(=O)C=C",
    "propiolamide": "NC(=O)C#C",
    "chloroacetamide": "NC(=O)CCl",
    "vinyl_sulfone": "S(=O)(=O)C=C",
}


def _auto_attachment_atom_indices(scaffold: Chem.Mol) -> list[int]:
    """Return deterministic, rule-based decoration sites.

    The rule deliberately stays conservative: a neutral C/N/O/S atom must have a
    replaceable hydrogen.  It is an enumeration rule, not a learned attachment
    prediction.
    """

    return [
        atom.GetIdx()
        for atom in scaffold.GetAtoms()
        if atom.GetAtomicNum() in {6, 7, 8, 16}
        and atom.GetFormalCharge() == 0
        and atom.GetTotalNumHs() > 0
    ]


def _attach_warhead(
    scaffold: Chem.Mol,
    attachment_index: int,
    warhead_smarts: str,
    linker_name: str,
) -> Chem.Mol | None:
    warhead = Chem.MolFromSmiles(warhead_smarts)
    linker_atoms = LINKER_ATOMIC_NUMBERS.get(linker_name)
    if warhead is None or linker_atoms is None or attachment_index >= scaffold.GetNumAtoms():
        return None
    dummy = next((atom for atom in warhead.GetAtoms() if atom.GetAtomicNum() == 0), None)
    if dummy is None or dummy.GetDegree() != 1:
        return None
    warhead_neighbor = dummy.GetNeighbors()[0].GetIdx()
    decorated = Chem.RWMol(scaffold)
    previous = attachment_index
    for atomic_number in linker_atoms:
        linker_index = decorated.AddAtom(Chem.Atom(atomic_number))
        decorated.AddBond(previous, linker_index, Chem.BondType.SINGLE)
        previous = linker_index

    decorated_mol = decorated.GetMol()
    merged = Chem.RWMol(Chem.CombineMols(decorated_mol, warhead))
    dummy_index = decorated_mol.GetNumAtoms() + dummy.GetIdx()
    merged.AddBond(
        previous,
        decorated_mol.GetNumAtoms() + warhead_neighbor,
        Chem.BondType.SINGLE,
    )
    merged.RemoveAtom(dummy_index)
    product = merged.GetMol()
    try:
        Chem.SanitizeMol(product)
    except Exception:
        return None
    return product


def _candidate_id(smiles: str, attachment_index: int, warhead_name: str, linker_name: str) -> str:
    payload = f"{smiles}|{attachment_index}|{warhead_name}|{linker_name}".encode()
    return f"lf-{hashlib.sha256(payload).hexdigest()[:16]}"


def _quality_measurements(
    scaffold: Chem.Mol, product: Chem.Mol, warhead_name: str
) -> dict[str, int | bool]:
    scaffold_retained = product.HasSubstructMatch(scaffold)
    single_component = len(Chem.GetMolFrags(product)) == 1
    formal_charge = sum(atom.GetFormalCharge() for atom in product.GetAtoms())
    radical_count = sum(atom.GetNumRadicalElectrons() for atom in product.GetAtoms())
    query = Chem.MolFromSmarts(WARHEAD_VALIDATION_SMARTS.get(warhead_name, ""))
    warhead_count = len(product.GetSubstructMatches(query)) if query is not None else 0
    chemistry_pass = bool(
        scaffold_retained
        and single_component
        and abs(formal_charge) <= 1
        and radical_count == 0
        and warhead_count == 1
    )
    return {
        "chemistry_gate_pass": chemistry_pass,
        "scaffold_retained": scaffold_retained,
        "single_component": single_component,
        "formal_charge": formal_charge,
        "radical_electron_count": radical_count,
        "warhead_match_count": warhead_count,
    }


def enumerate_covalent_candidates(request: CovalentEnumerationRequest) -> CovalentEnumerationResult:
    scaffold = Chem.MolFromSmiles(request.scaffold_smiles)
    if scaffold is None:
        return CovalentEnumerationResult(
            request_id=request.request_id,
            status="FAIL",
            candidates=[],
            rejected=["scaffold_smiles is not RDKit-parseable"],
        )

    candidates: list[CovalentEnumerationCandidate] = []
    rejected: list[str] = []
    seen: set[str] = set()
    protected = set(request.protected_atom_indices)
    requested_indices = request.attachment_atom_indices or _auto_attachment_atom_indices(scaffold)
    if not requested_indices:
        rejected.append("no neutral C/N/O/S atom with a replaceable hydrogen was found")
    for atom_index in requested_indices:
        if atom_index >= scaffold.GetNumAtoms():
            rejected.append(f"attachment atom {atom_index} is outside scaffold atom range")
            continue
        if atom_index in protected:
            rejected.append(f"attachment atom {atom_index} is protected")
            continue
        for name in request.allowed_warheads:
            warhead = DEFAULT_WARHEADS.get(name)
            if warhead is None:
                rejected.append(f"unsupported warhead: {name}")
                continue
            for linker_name in request.allowed_linkers:
                product = _attach_warhead(
                    scaffold,
                    atom_index,
                    warhead.attachment_smarts,
                    linker_name,
                )
                if product is None:
                    rejected.append(
                        f"{name}/{linker_name} attachment at atom {atom_index} "
                        "failed RDKit valence/sanitization"
                    )
                    continue
                smiles = Chem.MolToSmiles(product, canonical=True)
                if smiles in seen:
                    continue
                quality = _quality_measurements(scaffold, product, name)
                if not quality["chemistry_gate_pass"]:
                    rejected.append(
                        f"{name}/{linker_name} attachment at atom {atom_index} "
                        "failed chemistry quality gates"
                    )
                    continue
                seen.add(smiles)
                candidates.append(
                    CovalentEnumerationCandidate(
                        rank=len(candidates) + 1,
                        candidate_id=_candidate_id(
                            smiles, atom_index, name, linker_name
                        ),
                        canonical_smiles=smiles,
                        scaffold_attachment_atom_index=atom_index,
                        warhead_name=name,
                        linker_name=linker_name,
                        linker_heavy_atom_count=len(LINKER_ATOMIC_NUMBERS[linker_name]),
                        warhead_attachment_smarts=warhead.attachment_smarts,
                        reaction_class=warhead.reaction_class,
                        covalent_mode=warhead.covalent_mode,
                        intrinsic_reactivity_tier=warhead.intrinsic_reactivity_tier,
                        **quality,
                    )
                )
                if len(candidates) >= request.max_candidates:
                    break
            if len(candidates) >= request.max_candidates:
                break
        if len(candidates) >= request.max_candidates:
            break
    return CovalentEnumerationResult(
        request_id=request.request_id,
        status="PASS" if candidates else "FAIL",
        candidates=candidates,
        rejected=rejected,
    )
