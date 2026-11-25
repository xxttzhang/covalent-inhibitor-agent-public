"""Explicitly normalize the already selected registered receptor conformer.

The registered protein_A contains only conformer A of Lys970. Preserve every
coordinate and atom; remove its altloc annotation only after binding that choice.
This is not missing-atom repair, protonation, relaxation or MD admission.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile

REGISTRY = Path(__file__).resolve().parents[2] / 'registry/egfr_c797_v1'
CHOICES = {'A:970': 'A'}


def freeze_receptor(source, destination):
    source, destination = Path(source), Path(destination)
    original = source.read_bytes()
    if original != (REGISTRY/'assets/protein_A.pdb').read_bytes():
        raise ValueError('CovDock conformer source differs from registered receptor')
    lines, keys, selected = [], set(), []
    for line in original.decode('ascii').splitlines(keepends=True):
        if line.startswith(('ATOM  ', 'HETATM')):
            key = (line[21:27], line[12:16])
            if key in keys:
                raise ValueError('CovDock conformer contains duplicate atom identity')
            keys.add(key)
            if line[16:17].strip():
                residue = line[21]+':'+str(int(line[22:26]))
                if CHOICES.get(residue) != line[16] or line[17:20] != 'LYS':
                    raise ValueError('Unregistered alternate conformer selection')
                selected.append({'residue': residue, 'atom': line[12:16].strip(), 'altloc': line[16]})
                line = line[:16]+' '+line[17:]
        lines.append(line)
    if len(selected) != 9:
        raise ValueError('Registered Lys970 conformer inventory changed')
    output = ''.join(lines).encode('ascii')
    receipt = {'schema': 'denovo-covdock-receptor-conformer-v1',
        'source_sha256': hashlib.sha256(original).hexdigest(),
        'output_sha256': hashlib.sha256(output).hexdigest(),
        'selection': CHOICES, 'normalized_atoms': selected, 'atom_count': len(keys),
        'atoms_removed': 0, 'coordinates_changed': False, 'only_altloc_annotations_cleared': True,
        'receptor_prepared_for_md': False}
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('CovDock conformer destination traverses a symlink')
    destination.mkdir(parents=True, exist_ok=True)
    payloads = {'receptor.pdb': output,
        'receipt.json': (json.dumps(receipt, sort_keys=True, indent=2)+'\n').encode()}
    for name, raw in payloads.items():
        target = destination/name
        with tempfile.NamedTemporaryFile(dir=destination, prefix='.pending-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        try:
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
            if target.is_symlink() or not target.is_file() or target.read_bytes() != raw:
                raise ValueError('Frozen CovDock receptor derivation changed')
        finally:
            temporary.unlink()
    return receipt
