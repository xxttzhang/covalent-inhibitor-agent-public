"""Verify DeepCoSI identities against the exact normalized input, never offsets."""
import csv
import hashlib
from pathlib import Path
import re


def _atoms(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('DeepCoSI identity input must be a regular file')
    return [line for line in path.read_text().splitlines() if line.startswith('ATOM  ')]


def verified_result_keys(raw: Path, normalized: Path, residue_map: dict, validation: dict) -> dict[str, int]:
    """The registered runner always names its single-chain input input.pdb."""
    if validation.get('input_sha256') != hashlib.sha256(normalized.read_bytes()).hexdigest():
        raise ValueError('DeepCoSI validation input checksum mismatch')
    atoms = _atoms(normalized)
    if atoms != _atoms(raw / 'input.pdb') or atoms != _atoms(raw / 'build/input_process.pdb'):
        raise ValueError('DeepCoSI processed ATOM identities or coordinates changed')
    keys = {}
    with (raw / 'build/prediction_cysteines.csv').open(newline='') as handle:
        for row in csv.reader(handle):
            if len(row) != 4 or row[1:3] != ['input', 'A'] or not re.fullmatch(r'[1-9][0-9]*', row[3]):
                raise ValueError('Unexpected DeepCoSI detected-site identity')
            number = int(row[3])
            source = residue_map.get(number)
            if not source or source['residue_name'] != 'CYS':
                raise ValueError('Detected DeepCoSI site is not a normalized CYS')
            key = f'input_A_{number}'
            if key in keys:
                raise ValueError('Duplicate DeepCoSI detected-site identity')
            keys[key] = number
    return keys


def result_number(key: str, keys: dict[str, int]) -> int:
    # Exact membership rejects wrong chains, prefixes, extra suffixes and paths.
    if key not in keys:
        raise ValueError('Unexpected DeepCoSI result identity: ' + str(key))
    return keys[key]
