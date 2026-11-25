"""Source-bound alternative coordinates; no automatic conformer selection."""
import hashlib
import json
from pathlib import Path

from .pdb_coordinates import read_pdb_coordinates


def digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def inventory(source: Path, source_sha256: str, mapping: dict, *, model: int,
              target_altloc: str | None = None) -> dict:
    raw, _, atoms, selected_model = read_pdb_coordinates(source, mapping['entry_id'], model)
    if hashlib.sha256(raw).hexdigest() != source_sha256:
        raise ValueError('Conformer source checksum mismatch')
    residues = {}
    for atom in atoms:
        if atom['record'] != 'ATOM' or atom['chain'] != mapping['auth_asym_id'] or not atom['altloc']:
            continue
        residue = residues.setdefault(atom['residue'], {})
        residue.setdefault(atom['altloc'], []).append({key: atom[key] for key in
            ('serial', 'name', 'resname', 'occupancy', 'element', 'xyz')})
    payload = {'schema': 'receptor-conformer-evidence-v1', 'source_sha256': source_sha256,
        'source_site_mapping': mapping, 'model': selected_model,
        'target_altloc': target_altloc,
        'residues': [{'author_residue_id': residue, 'alternatives': alternatives}
                     for residue, alternatives in sorted(residues.items())],
        'observation': 'Occupancy is deposited evidence, not a scientific ranking. No conformer selected.',
        'receptor_prepared': False, 'compute_submitted': False}
    return {**payload, 'evidence_sha256': digest(payload)}


def validate_selection(evidence: dict, selection: dict) -> dict:
    required = {'evidence_sha256', 'choices', 'rationale', 'limitations'}
    if not isinstance(selection, dict) or set(selection) != required:
        raise ValueError('Conformer selection requires evidence_sha256, choices, rationale and limitations')
    payload = {key: value for key, value in evidence.items() if key != 'evidence_sha256'}
    if selection['evidence_sha256'] != digest(payload) or evidence['evidence_sha256'] != digest(payload):
        raise ValueError('Conformer evidence changed; read current evidence before selecting')
    if not isinstance(selection['rationale'], str) or not selection['rationale'].strip():
        raise ValueError('Conformer selection requires nonempty rationale')
    limitations = selection['limitations']
    if isinstance(limitations, list):
        if not limitations or any(not isinstance(item, str) or not item.strip() for item in limitations):
            raise ValueError('Conformer selection requires nonempty limitations')
        # Preserve the v1 stored contract as a string while accepting the same
        # structured limitation list already used by construct review.
        selection = {**selection, 'limitations': '\n'.join(item.strip() for item in limitations)}
    elif not isinstance(limitations, str) or not limitations.strip():
        raise ValueError('Conformer selection requires nonempty limitations')
    allowed = {row['author_residue_id']: row['alternatives'] for row in evidence['residues']}
    choices = selection['choices']
    if (not isinstance(choices, dict) or set(choices) != set(allowed)
            or any(not isinstance(value, str) or value not in allowed[key] for key, value in choices.items())):
        raise ValueError('Select an existing alternate location for every ambiguous residue, and no others')
    target = evidence['source_site_mapping']['author_residue_id']
    if evidence['target_altloc'] and choices.get(target) != evidence['target_altloc']:
        raise ValueError('Conformer selection conflicts with the acquired target selection')
    return {'schema': 'receptor-conformer-selection-v1', 'selection': selection,
            'scientific_suitability_verified': False, 'grants_resource_approval': False}
