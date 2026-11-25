"""Plan an explicitly selected domain without repairing or admitting a receptor.

The original deposited entity and its numbering remain intact. Scoped indices
are additional identifiers, never replacements for source entity positions.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .peptide_templates import peptide_reference
from .receptor_preparation import AMINO_ACIDS, project_receptor


def plan_domain_scope(source: Path, source_sha256: str, entity: dict, instance: dict,
                      accession: str, cys: int, canonical_sequence: str, taxon: int,
                      *, canonical_start: int, canonical_end: int, reason: str,
                      altlocs: dict | None = None, model: int | None = None) -> dict:
    """Return a coordinate-preserving inventory; no output structure or task."""
    if (type(canonical_start) is not int or type(canonical_end) is not int
            or not 1 <= canonical_start <= cys <= canonical_end <= len(canonical_sequence)):
        raise ValueError('Domain range must contain the target cysteine')
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000:
        raise ValueError('Record the reason for changing construct scope')
    _, parent = project_receptor(source, source_sha256, entity, instance,
        accession, cys, canonical_sequence, taxon, altlocs=altlocs, model=model)
    construct = parent['construct_audit']
    if not construct['source_species_verified']:
        raise ValueError('Domain source species is not verified')
    mapping = {}
    for alignment in entity.get('rcsb_polymer_entity_align', []):
        if (alignment.get('reference_database_name') != 'UniProt'
                or alignment.get('reference_database_accession') != accession):
            continue
        for region in alignment['aligned_regions']:
            for offset in range(region['length']):
                canonical = region['ref_beg_seq_id'] + offset
                position = region['entity_beg_seq_id'] + offset
                if canonical in mapping and mapping[canonical] != position:
                    raise ValueError('Ambiguous domain alignment')
                mapping[canonical] = position
    canonical_positions = list(range(canonical_start, canonical_end + 1))
    if any(p not in mapping for p in canonical_positions):
        raise ValueError('Domain range is not completely mapped to the source')
    positions = [mapping[p] for p in canonical_positions]
    if positions != list(range(positions[0], positions[0] + len(positions))):
        raise ValueError('Domain range is not contiguous in the deposited entity')
    selected = set(positions)
    if any(row['entity_position'] in selected for key in ('sequence_differences', 'uncertain_residues')
           for row in construct[key]):
        raise ValueError('Selected domain differs from the canonical sequence')
    sequence = canonical_sequence[canonical_start - 1:canonical_end]
    scoped = {p: i + 1 for i,p in enumerate(positions)}
    retained = [{**a, 'scoped_position': scoped[a['entity_position']]}
                for a in parent['retained_atoms'] if a['entity_position'] in selected]
    if not retained:
        raise ValueError('Selected domain has no retained observed atoms')
    excluded = [*parent['excluded_atoms'],
        *[{'atom': a, 'reason': 'outside_selected_domain'} for a in parent['retained_atoms']
          if a['entity_position'] not in selected]]
    reference, _ = peptide_reference(sequence)
    expected = {(a.GetPDBResidueInfo().GetResidueNumber(),
                 a.GetPDBResidueInfo().GetName().strip()) for a in reference.GetAtoms()}
    actual = {(a['scoped_position'], a['name']) for a in retained}
    if not actual <= expected:
        raise ValueError('Observed domain contains nonstandard heavy atoms')
    rows = [{'scoped_position': i+1, 'source_entity_position': position,
             'canonical_position': canonical_positions[i],
             'author_residue': parent['author_residue_by_entity_position'][position-1],
             'resname': AMINO_ACIDS[sequence[i]]} for i,position in enumerate(positions)]
    missing = [{**rows[p-1], 'name': name,
                'kind': 'new_terminal_atom' if name == 'OXT' else 'unobserved_heavy_atom'}
               for p,name in sorted(expected-actual)]
    present = {p for p,_ in actual}
    return {'schema': 'receptor-domain-scope-v1', 'status': 'preparation_policy_required',
        'reason': reason.strip(), 'source_sha256': source_sha256,
        'source_inventory_sha256': hashlib.sha256(json.dumps(parent, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
        'source_input_object_sha256': parent['input_object_sha256'],
        'canonical_range': [canonical_start, canonical_end], 'sequence': sequence,
        'residue_mapping': rows, 'source_site_mapping': parent['mapping'],
        'target_scoped_position': scoped[parent['mapping']['entity_seq_id']],
        'altlocs': parent['altlocs'], 'model': parent['model'],
        'retained_atoms': retained, 'excluded_atoms': excluded,
        'missing_residues': [row for row in rows if row['scoped_position'] not in present],
        'missing_heavy_atoms': missing, 'terminal_treatment_required': True,
        'coordinates_modified': False, 'calculation_submitted': False,
        'receptor_prepared': False, 'campaign_ready': False, 'md_ready': False}
