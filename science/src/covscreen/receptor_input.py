"""Reload the complete verified parent chain for receptor preparation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

from .target_acquisition_artifact import load_target_acquisition
from .target_identity import load_target_identity


class ReceptorRuntimeUnavailable(ValueError):
    """Missing or failing configured CPU runtime; not receptor unsuitability."""


def load_receptor_preparation_input(root, artifact_id, manifest_sha256):
    root = Path(root).resolve()
    if not isinstance(artifact_id, str) or not re.fullmatch(
        r"receptor-input-[a-f0-9]{32}", artifact_id
    ):
        raise ValueError("Use a receptor-input artifact ID returned by structure acquisition")
    acquisition = root / "state/target_acquisition" / artifact_id[15:]
    acquisition.relative_to(root)
    if any(path.is_symlink() for path in (acquisition, *acquisition.parents)) or not acquisition.is_dir():
        raise ValueError("Receptor input artifact is unavailable")
    manifest = acquisition / "target_acquisition.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError('Receptor input manifest must be a regular file')
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != manifest_sha256:
        raise ValueError("Target acquisition manifest checksum mismatch")
    record = json.loads(manifest.read_text())
    parents = record.get("parents", {})
    if parents.get('source_kind') in {'sealed-ligand-study-v1', 'sealed-ligand-vs-receptor-v1'}:
        from .ligand_receptor_input import load
        return load(root, acquisition, record)
    target_id = parents.get("target_artifact_id")
    if not isinstance(target_id, str) or not re.fullmatch(r"target-[a-f0-9]{32}", target_id):
        raise ValueError("Target acquisition has no valid target identity parent")
    target = root / "state/target_identity" / target_id[7:]
    target.relative_to(root)
    if any(path.is_symlink() for path in (target, *target.parents)) or not target.is_dir():
        raise ValueError("Target identity parent is unavailable")
    identity = load_target_identity(target, parents.get("target_manifest_sha256"))
    candidate, = identity["identity"]["candidates"]
    fasta = target / candidate["sequence_path"]
    if hashlib.sha256(fasta.read_bytes()).hexdigest() != candidate["fasta_sha256"]:
        raise ValueError("Canonical FASTA changed after target verification")
    sequence = "".join(fasta.read_text().splitlines()[1:])
    acquired = load_target_acquisition(
        acquisition, manifest_sha256, sequence, identity["plan"]["taxon"]
    )
    evidence = sorted(
        path for folder in (acquisition, target)
        for path in folder.iterdir() if path.is_file() and not path.is_symlink()
    )
    if not evidence:
        raise ValueError("Receptor preparation evidence inventory is empty")
    return {
        "acquisition_directory": acquisition,
        "target_directory": target,
        "identity": identity,
        "acquisition": acquired,
        "canonical_fasta": fasta,
        "canonical_sequence": sequence,
        "taxon": identity["plan"]["taxon"],
        "target_artifact_id": target_id,
        "source_kind": "target-identity-v1",
        "source_route": "library-first",
        "evidence_files": evidence,
    }


def load_receptor_preparation_directory(root, directory, manifest_sha256):
    """Use the same parent validation at plan compilation and worker startup."""
    root, directory = Path(root).resolve(), Path(directory)
    if directory.is_symlink() or not re.fullmatch(r'[a-f0-9]{32}', directory.name):
        raise ValueError('Invalid receptor input directory')
    expected = root / 'state/target_acquisition' / directory.name
    if directory != expected:
        raise ValueError('Receptor input directory differs from its registered location')
    return load_receptor_preparation_input(root, 'receptor-input-' + directory.name, manifest_sha256)
