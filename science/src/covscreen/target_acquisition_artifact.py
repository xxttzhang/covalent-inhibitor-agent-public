"""Reload and recompute a target coordinate-acquisition artifact."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .construct_audit import audit_construct
from .rcsb_mapping import map_cysteine
from .target_site_audit import audit_target_site


def load_target_acquisition(directory, expected_sha256, canonical_sequence, taxon):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Target acquisition must be a regular directory')
    manifest = directory / 'target_acquisition.json'
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError('Target acquisition manifest is missing')
    raw = manifest.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Target acquisition manifest checksum mismatch')
    record = json.loads(raw)
    if record.get('schema') != 'target-acquisition-v1':
        raise ValueError('Unsupported target acquisition schema')
    acquisition = record['acquisition']
    if acquisition.get('status') != 'acquired_coordinate_audit_completed_review_pending':
        raise ValueError('Target coordinates did not complete acquisition audit')
    sources = acquisition.get('sources')
    if not isinstance(sources, list) or not sources:
        raise ValueError('Target acquisition lacks source evidence')
    by_name = {}
    for source in sources:
        name = source.get('path')
        if (not isinstance(name, str) or Path(name).name != name or name in by_name
                or not isinstance(source.get('sha256'), str)):
            raise ValueError('Invalid target acquisition source record')
        path = directory / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('Target acquisition source checksum mismatch: ' + name)
        by_name[name] = path
    mapping_rows = acquisition.get('mappings')
    if not isinstance(mapping_rows, list) or len(mapping_rows) != 1:
        raise ValueError('Exactly one verified target mapping is required')
    stored = mapping_rows[0]
    entity_name = f"entity_{stored['entity_id']}.json"
    chain_name = f"chain_{stored['label_asym_id']}.json"
    if any(name not in by_name for name in (entity_name, chain_name, 'structure.pdb')):
        raise ValueError('Target acquisition is missing mapping or coordinate sources')
    entity = json.loads(by_name[entity_name].read_text())
    instance = json.loads(by_name[chain_name].read_text())
    plan = acquisition['plan']
    recomputed = map_cysteine(entity, instance, plan['uniprot'], plan['cys'])
    if recomputed != stored:
        raise ValueError('Stored target mapping differs from raw RCSB evidence')
    selection = record['selection']
    coordinates = audit_target_site(by_name['structure.pdb'], recomputed,
                                    model=selection.get('model'), altloc=selection.get('altloc'))
    coordinates = json.loads(json.dumps(coordinates, allow_nan=False))
    if coordinates != acquisition.get('coordinate_audit'):
        raise ValueError('Stored coordinate audit differs from raw PDB evidence')
    construct = audit_construct(entity, canonical_sequence, plan['uniprot'], taxon)
    construct = json.loads(json.dumps(construct, allow_nan=False))
    if record.get('construct_audit') is not None and construct != record['construct_audit']:
        raise ValueError('Stored construct audit differs from target evidence')
    if record.get('construct_audit') is None:
        record = {**record, 'construct_audit': construct,
                  'construct_audit_recomputed_from_raw': True}
    if record.get('receptor_prepared') is not False or record.get('campaign_ready') is not False:
        raise ValueError('Coordinate acquisition cannot claim receptor or campaign readiness')
    return record
