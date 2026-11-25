"""Read receptor identity without a vendor runtime or a preparation claim.

Numbered observed residues are not a canonical/full protein sequence. A file
or vendor preparation flag alone does not bind the receptor to a study.
"""
from __future__ import annotations

import gzip
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import re

from rdkit import Chem, rdBase


class CovDockRuntimeUnavailable(ValueError):
    """Missing registered runtime assets; no claim about license availability."""


def runtime_paths(configuration):
    if configuration.get('enabled') is not True:
        raise CovDockRuntimeUnavailable('Configured Schrödinger execution is disabled')
    paths = {}
    for name in ('root', 'license_file'):
        value = configuration.get(name)
        if not isinstance(value, str) or not value.strip():
            raise CovDockRuntimeUnavailable('Schrödinger runtime is not configured: ' + name)
        paths[name] = Path(value).expanduser()
    if any(not (paths['root'] / name).is_file() for name in ('run', 'covalent_docking')):
        raise CovDockRuntimeUnavailable('Configured Schrödinger CovDock installation is unavailable')
    if not paths['license_file'].is_file():
        raise CovDockRuntimeUnavailable('Configured vendor-issued Schrödinger license file is unavailable')
    runtime = configuration.get('runtime_lib_dir')
    paths['runtime_lib_dir'] = None
    if runtime is not None:
        if not isinstance(runtime, str) or not runtime.strip() or not Path(runtime).expanduser().is_dir():
            raise CovDockRuntimeUnavailable('Configured Schrödinger runtime_lib_dir is unavailable')
        paths['runtime_lib_dir'] = Path(runtime).expanduser()
    return paths


def attachment_site(value: str) -> tuple[str, int]:
    match = re.fullmatch(r"([^:\s]+):(-?(?:0|[1-9][0-9]*))", value) if isinstance(value, str) else None
    if match is None:
        raise ValueError("CovDock attachment residue must be explicit CHAIN:integer; insertion codes require an adapter")
    return match[1], int(match[2])


def _residue(atom):
    info = atom.GetPDBResidueInfo()
    if info is None:
        raise ValueError("CovDock receptor lacks explicit atom/residue metadata")
    if info.GetAltLoc().strip():
        raise ValueError("CovDock receptor contains unresolved alternate coordinates")
    return (info.GetChainId().strip(), info.GetResidueNumber(),
            info.GetInsertionCode().strip(), info.GetResidueName().strip().upper())


def inspect_receptor(path: Path, attachment: str, nucleophilic_atom: str = "SG",
                     *, expected_sha256: str | None = None,
                     include_residue_inventory: bool = False) -> dict:
    chain, number = attachment_site(attachment)
    if nucleophilic_atom != "SG":
        raise ValueError("Registered CovDock receptor check currently requires Cys SG")
    contents = path.read_bytes()
    checksum = hashlib.sha256(contents).hexdigest()
    if expected_sha256 is not None and (not isinstance(expected_sha256, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected_sha256) or checksum != expected_sha256):
        raise ValueError("CovDock receptor differs from the frozen plan")
    suffix = path.suffix.lower()
    if suffix in {".mae", ".maegz"} or path.name.lower().endswith('.mae.gz'):
        decoded = gzip.decompress(contents) if suffix in {".maegz", ".gz"} else contents
        # Keep the stream alive and read sequentially. list(supplier) invokes
        # RDKit's length hint, which rewinds this stream in some releases.
        stream = BytesIO(decoded)
        supplier = Chem.MaeMolSupplier(stream, sanitize=False, removeHs=False)
        structures = []
        while not supplier.atEnd() and len(structures) < 2:
            structures.append(next(supplier))
        file_format = "maestro"
    elif suffix == ".pdb":
        text = contents.decode('ascii')
        lines = text.splitlines()
        if sum(line.startswith('MODEL ') for line in lines) > 1:
            raise ValueError("CovDock receptor requires one explicitly selected coordinate model")
        atom_lines = [line for line in lines if line.startswith(('ATOM  ', 'HETATM'))]
        if any(line[16:17].strip() for line in atom_lines):
            raise ValueError("CovDock receptor contains unresolved alternate coordinates")
        # Do not invent covalent connectivity from distances in an input PDB.
        structures = [Chem.MolFromPDBBlock(text, sanitize=False, removeHs=False, proximityBonding=False)]
        file_format = "pdb"
    else:
        raise ValueError("CovDock receptor check supports PDB, MAE and compressed MAE")
    if len(structures) != 1 or structures[0] is None or structures[0].GetNumAtoms() == 0:
        raise ValueError("CovDock receptor must contain exactly one readable structure")
    mol = structures[0]
    if file_format == 'pdb' and mol.GetNumAtoms() != len(atom_lines):
        raise ValueError("CovDock PDB reader did not retain every explicit coordinate atom")
    if mol.GetNumConformers() != 1:
        raise ValueError("CovDock receptor must contain one coordinate conformer")
    conformer = mol.GetConformer()
    inventory = {}
    sites, atom_keys = [], set()
    for atom in mol.GetAtoms():
        residue = _residue(atom)
        name = atom.GetPDBResidueInfo().GetName().strip().upper()
        key = (*residue[:3], name)
        if not name or key in atom_keys:
            raise ValueError("CovDock receptor has ambiguous atom identity")
        atom_keys.add(key)
        # Point3D iteration ends through a native IndexError for every atom.
        # Explicit coordinates avoid that unwind cost during repeated audits.
        point = conformer.GetAtomPosition(atom.GetIdx())
        xyz = (point.x, point.y, point.z)
        if not all(math.isfinite(value) for value in xyz):
            raise ValueError("CovDock receptor coordinates must be finite")
        prior = inventory.setdefault(residue[:3], residue[3])
        if prior != residue[3]:
            raise ValueError("CovDock receptor has conflicting residue identities")
        if residue[0] == chain and residue[1] == number and name == nucleophilic_atom:
            sites.append(atom)
    if len(sites) != 1:
        raise ValueError("CovDock target atom must be unique in the receptor")
    atom = sites[0]
    residue = _residue(atom)
    if residue[2]:
        raise ValueError("CovDock target insertion code requires an explicit mapping adapter")
    if residue[3] not in {"CYS", "CYM"} or atom.GetAtomicNum() != 16:
        raise ValueError("CovDock target must be a free Cys sulfur, not a modified residue")
    neighbors = []
    for neighbor in atom.GetNeighbors():
        neighbor_residue = _residue(neighbor)
        if neighbor_residue != residue:
            raise ValueError("CovDock target sulfur is already bonded outside its residue")
        neighbors.append({'atom_name': neighbor.GetPDBResidueInfo().GetName().strip(),
            'element': neighbor.GetSymbol(), 'bond_order': mol.GetBondBetweenAtoms(
                atom.GetIdx(), neighbor.GetIdx()).GetBondTypeAsDouble()})
    if file_format == "maestro":
        heavy = [neighbor for neighbor in neighbors if neighbor['element'] != 'H']
        if heavy != [{'atom_name': 'CB', 'element': 'C', 'bond_order': 1.0}]:
            raise ValueError("CovDock target sulfur has an unsupported heavy-atom bond graph")
    observed = [{'chain': key[0], 'residue_number': key[1], 'insertion_code': key[2],
                 'residue_name': name} for key, name in sorted(inventory.items())]
    observed_sha = hashlib.sha256(json.dumps(observed, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    point = conformer.GetAtomPosition(atom.GetIdx())
    result = {'schema': 'covdock-receptor-input-check-v1', 'receptor_sha256': checksum,
        'reader': 'rdkit', 'reader_version': rdBase.rdkitVersion, 'format': file_format,
        'atom_count': mol.GetNumAtoms(), 'observed_residue_count': len(observed),
        'observed_residue_identity_sha256': observed_sha,
        'attachment_residue': attachment, 'nucleophilic_atom': nucleophilic_atom,
        'target': {'chain': chain, 'residue_number': number, 'insertion_code': '',
            'residue_name': residue[3], 'atom_name': nucleophilic_atom,
            'atom_index_1based': atom.GetIdx()+1, 'formal_charge': atom.GetFormalCharge(),
            'xyz': [point.x, point.y, point.z], 'bonded_atoms': neighbors},
        'target_coordinate_identity_verified': True,
        'target_bond_graph_checked': file_format == 'maestro',
        'vendor_prepared_flag': mol.GetProp('b_ppw_prepared') if mol.HasProp('b_ppw_prepared') else None,
        'receptor_preparation_quality_verified': False,
        'study_target_identity_verified': False, 'canonical_mapping_verified': False,
        'grants_resource_approval': False}
    if include_residue_inventory:
        result['observed_residues'] = observed
    return result


def verify_reused_inputs(manifest: dict, receptor_sha256: str, ligand_sha256: str,
                         attachment: str, reaction: str, mode: str, *,
                         prepare_receptor: bool = False, grid_sha256: str | None = None) -> None:
    """Use stored invocation hashes, never reread a historical mutable path."""
    for field, expected in {'input_receptor_sha256': receptor_sha256,
            'input_ligand_sha256': ligand_sha256, 'attachment_residue': attachment,
            'reaction_type': reaction, 'mode': mode,
            'prepare_receptor': prepare_receptor, 'grid_sha256': grid_sha256}.items():
        if manifest.get(field) != expected:
            raise ValueError(f"CovDock retained/restart input differs or lacks frozen provenance: {field}")


def read_effective_receptor(manifest_path: Path, source_receptor: Path,
                            attachment: str, nucleophilic_atom: str = 'SG') -> dict:
    """Read the receptor actually used, including a locally prepared product."""
    manifest = json.loads(manifest_path.read_text())
    if type(manifest.get('prepare_receptor')) is not bool:
        raise ValueError('CovDock effective receptor lacks preparation provenance')
    digest = manifest.get('effective_receptor_sha256')
    if not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
        raise ValueError('CovDock effective receptor lacks a frozen content hash')
    if manifest['prepare_receptor']:
        suffix = '.run_manifest.json'
        if not manifest_path.name.endswith(suffix):
            raise ValueError('CovDock effective receptor requires its named invocation manifest')
        # The producer writes this exact filename. Resolve in the retained job
        # directory so an archived absolute remote path is never dereferenced.
        receptor = manifest_path.with_name(manifest_path.name[:-len(suffix)]+'_receptor_prepared.mae')
    else:
        receptor = source_receptor
    return inspect_receptor(receptor, attachment, nucleophilic_atom, expected_sha256=digest)
