"""CovDock-VS prepared-input contract, independent of LO and CarsiDock lineage.

This module compiles and revalidates data only. It neither grants native approval
nor submits work. Workflow adapters must bind source receipts before exposing it.
"""
from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from rdkit import Chem

from .adapters.covdock import CovDockAdapter
from .covdock_receptor import inspect_receptor, runtime_paths
from .jobs import sha256_file
from .schrodinger import validate_license_config


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Source(Record):
    route: Literal["ligand-first", "library-first", "de-novo"]
    workflow_id: str = Field(min_length=1, max_length=160)
    revision: int = Field(ge=0)
    # Actual source manifests are hashed, not a claimed upstream engine/run ID.
    manifests: dict[str, str] = Field(min_length=1)


class Budget(Record):
    gpu_count: Literal[0] = 0
    cpu_count: int = Field(ge=1, le=128)
    memory_gb: int = Field(ge=1, le=1024)
    max_runtime_minutes: int = Field(ge=1, le=1440)
    max_candidates: int = Field(ge=1, le=100)

    @field_validator("gpu_count", mode="before")
    @classmethod
    def cpu_only(cls, value):
        if type(value) is not int or value != 0:
            raise ValueError("VS requires an explicit integer zero GPU budget")
        return value


class Inputs(Record):
    schema_version: Literal["covdock-vs-input-v1"] = "covdock-vs-input-v1"
    engine: Literal["schrodinger-covdock-vs"] = "schrodinger-covdock-vs"
    mode: Literal["enrichment"] = "enrichment"
    reaction_type: Literal["Michael Addition"] = "Michael Addition"
    source: Source
    original_ligands: str
    prepared_ligands: str
    prepared_receptor: str
    attachment_residue: str
    budget: Budget
    njobs: int = Field(ge=1, le=100)


def bound_file(root: Path, name: str) -> Path:
    root = root.resolve(strict=True)
    path = Path(name)
    if (not name or path.is_absolute() or ".." in path.parts
            or path.as_posix() != name or name == "."):
        raise ValueError("VS inputs require canonical project-relative file paths")
    location = root / path
    if any((root / Path(*path.parts[:index])).is_symlink()
           for index in range(1, len(path.parts) + 1)):
        raise ValueError("VS inputs cannot traverse symlinks")
    if not location.is_file() or not location.resolve().is_relative_to(root.resolve()):
        raise ValueError("VS input is missing or outside the data root")
    return location


def molecules(path: Path, maestro: bool):
    if maestro:
        if path.suffix.lower() != ".mae":
            raise ValueError("VS prepared inputs require uncompressed Maestro files")
        supplier = Chem.MaeMolSupplier(str(path), removeHs=False)
        result = []
        while not supplier.atEnd():
            result.append(next(supplier))
    else:
        if path.suffix.lower() != ".sdf":
            raise ValueError("VS original candidates require mapped SDF")
        result = list(Chem.SDMolSupplier(str(path), removeHs=False))
    if not result or any(mol is None for mol in result):
        raise ValueError("VS input contains an unreadable or empty molecule record")
    return result


def identity(mol):
    value = Chem.RemoveHs(Chem.Mol(mol))
    for atom in value.GetAtoms():
        atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(value, isomericSmiles=True)


def unresolved_stereo(mol):
    """Locate unspecified potential stereochemistry before vendor preparation.

    A vendor-selected stereoisomer is not the same approved chemical identity
    as an unspecified input. Do not silently choose, enumerate or accept it.
    """
    value = Chem.RemoveHs(Chem.Mol(mol))
    return [{'type': str(info.type), 'centered_on': int(info.centeredOn),
             'specified': str(info.specified)}
            for info in Chem.FindPotentialStereo(value)
            if info.specified != Chem.StereoSpecified.Specified]


def inspect_inputs(root: Path, value: Inputs | dict) -> dict:
    """Re-read all source and vendor input bytes; no cached PASS is accepted."""
    plan = Inputs.model_validate(value)
    if not re.fullmatch(r"(?:ligand|library|de)-flow-[a-f0-9]{24}", plan.source.workflow_id):
        raise ValueError("VS source must identify a native workflow")
    if not plan.source.workflow_id.startswith(plan.source.route.split("-")[0] + "-flow-"):
        raise ValueError("VS source route and workflow identity disagree")
    if plan.source.route == 'de-novo':
        from .layout import enable_de_novo
        enable_de_novo()
        from covscreen_de_novo.batch_vs import verify_source
        verify_source(root, plan.source, plan.original_ligands)
    if plan.njobs > min(plan.budget.cpu_count, plan.budget.max_candidates):
        raise ValueError("VS local subjobs exceed the approved CPU/candidate budget")
    filenames = [plan.original_ligands, plan.prepared_ligands, plan.prepared_receptor,
                 *plan.source.manifests]
    paths = {name: bound_file(root, name) for name in filenames}
    hashes = {name: sha256_file(path) for name, path in paths.items()}
    for name, checksum in plan.source.manifests.items():
        if not re.fullmatch(r"[a-f0-9]{64}", checksum) or hashes[name] != checksum:
            raise ValueError("VS source manifest changed")
    originals = molecules(paths[plan.original_ligands], False)
    prepared = molecules(paths[plan.prepared_ligands], True)
    if len(originals) > plan.budget.max_candidates or len(originals) != len(prepared):
        raise ValueError("VS candidate denominator differs from the bounded prepared input")
    if plan.njobs > len(originals):
        raise ValueError("VS subjobs exceed the actual candidate denominator")
    names, candidates = set(), []
    # Retain input order. Expanding protomers, tautomers or stereoisomers requires
    # a different explicitly approved contract, not an implicit LigPrep default.
    for index, (original, ligand) in enumerate(zip(originals, prepared)):
        name = original.GetProp("_Name") if original.HasProp("_Name") else ""
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", name)
                or name in names or not ligand.HasProp("_Name")
                or ligand.GetProp("_Name") != name):
            raise ValueError("VS candidate identity/order is missing, duplicated or changed")
        names.add(name)
        if identity(original) != identity(ligand):
            raise ValueError("VS preparation changed candidate stereochemistry, charge or connectivity")
        from .covdock_vs_output import ordered_identity
        ordered_identity(original, ligand)
        maps = {}
        for atom in original.GetAtoms():
            number = atom.GetAtomMapNum()
            if number and number in maps:
                raise ValueError("VS original atom maps must be unique")
            if number:
                maps[number] = atom.GetIdx()
        if not {905, 906}.issubset(maps):
            raise ValueError("VS Michael input needs explicit alpha/beta maps 905/906")
        alpha, beta = maps[905], maps[906]
        bond = original.GetBondBetweenAtoms(alpha, beta)
        if (bond is None or bond.GetBondType() != Chem.BondType.DOUBLE
                or any(original.GetAtomWithIdx(i).GetAtomicNum() != 6 for i in (alpha, beta))):
            raise ValueError("VS Michael reactive atoms must identify the carbon double bond")
        if ligand.GetNumConformers() != 1 or not ligand.GetConformer().Is3D():
            raise ValueError("VS prepared ligand must have one 3D conformer")
        if not all(math.isfinite(v) for xyz in ligand.GetConformer().GetPositions() for v in xyz):
            raise ValueError("VS prepared coordinates must be finite")
        candidates.append({"input_index": index, "candidate_id": name,
            "canonical_isomeric_smiles": identity(original),
            "alpha_atom_map": 905, "beta_atom_map": 906,
            "original_alpha_index": alpha, "original_beta_index": beta})
    receptor = paths[plan.prepared_receptor]
    if receptor.suffix.lower() != ".mae":
        raise ValueError("VS receptor must be prepared Maestro, not raw PDB")
    receptor_check = inspect_receptor(receptor, plan.attachment_residue)
    if any(sha256_file(path) != hashes[name] for name, path in paths.items()):
        raise ValueError("VS input changed during inspection")
    return {"schema": "covdock-vs-input-witness-v1", "plan": plan.model_dump(mode="json"),
        "input_sha256": hashes, "candidates": candidates, "receptor": receptor_check,
        "scientific_acceptance": False, "md_ready": False, "grants_resource_approval": False}


def revalidate(root: Path, witness: dict) -> dict:
    actual = inspect_inputs(root, witness["plan"])
    if actual != witness:
        raise ValueError("VS frozen input witness differs from current source or prepared files")
    return actual


def command(root: Path, witness: dict, configuration: dict, jobname: str) -> list[str]:
    """Compile only VS argv; execution/approval belongs to the registered host."""
    actual = revalidate(root, witness)
    plan = Inputs.model_validate(actual["plan"])
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", jobname):
        raise ValueError("VS job name must be a bounded safe identifier")
    runtime = runtime_paths(configuration)
    permitted, _ = validate_license_config(str(runtime["license_file"]))
    if not permitted:
        raise ValueError("VS requires a legitimate configured vendor license")
    return CovDockAdapter(str(runtime["root"].resolve())).build_command(
        str(bound_file(root, plan.prepared_receptor)), str(bound_file(root, plan.prepared_ligands)),
        plan.attachment_residue, jobname, reaction_type=plan.reaction_type,
        mode="enrichment", njobs=plan.njobs,
        extra_options=["-HOST", f"localhost:{plan.njobs}", "-RETRIES", "0"], wait=True)


def freeze_execution(root: Path, witness: dict, configuration: dict, jobname: str) -> dict:
    """Bind the installed runtime and exact argv for a future native approval.

    Keep this private on the host: it contains a license path/hash, never license
    contents. The public approval view should expose only the configured status.
    """
    argv = command(root, witness, configuration, jobname)
    runtime = runtime_paths(configuration)
    assets = [runtime["root"] / "covalent_docking", runtime["root"] / "run", runtime["license_file"]]
    sources = [Path(__file__), Path(__file__).parent / "adapters/covdock.py",
               Path(__file__).parent / "covdock_receptor.py", Path(__file__).parent / "schrodinger.py"]
    return {"schema": "covdock-vs-execution-plan-v1", "inputs": witness,
        "command": argv, "jobname": jobname, "engine": "schrodinger-covdock-vs",
        "mode": "enrichment", "resource_budget": witness["plan"]["budget"],
        "timeout_seconds": witness["plan"]["budget"]["max_runtime_minutes"] * 60,
        "runtime": {name: str(path.resolve()) if path else None for name, path in runtime.items()},
        "runtime_assets": {str(path.resolve()): sha256_file(path) for path in assets},
        "sources": {str(path.resolve()): sha256_file(path) for path in sources},
        "native_approval_required": True, "compute_submitted": False,
        "scientific_acceptance": False, "md_ready": False}


def validate_execution(root: Path, packet: dict, configuration: dict) -> dict:
    actual = freeze_execution(root, packet["inputs"], configuration, packet["jobname"])
    if actual != packet:
        raise ValueError("VS executable, license configuration, sources, inputs or exact command changed")
    return actual
