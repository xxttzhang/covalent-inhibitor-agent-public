"""Build a coordinate-independent chirality repair plan from verified evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .peptide_templates import chirality_terms
from .receptor_quality import audit_receptor_heavy_atoms


def build_chirality_plan(parent, parent_receipt_sha256, source):
    parent, source = Path(parent), Path(source)
    audit = audit_receptor_heavy_atoms(parent, parent_receipt_sha256, source)
    for field in ('provenance_violations', 'missing_atoms', 'extra_atoms',
                  'missing_bonds', 'extra_bonds', 'element_mismatches'):
        if audit[field]:
            raise ValueError('Heavy proposal requires repair beyond stereochemistry: ' + field)
    mapping = parent / 'atom-mapping.json'
    atoms = json.loads(mapping.read_text())['atoms']
    terms = chirality_terms(atoms)
    template = Path(__file__).with_name('peptide_templates.py')
    return {'schema': 'receptor-chirality-plan-v1',
        'parent_receipt_sha256': parent_receipt_sha256,
        'atom_mapping_sha256': hashlib.sha256(mapping.read_bytes()).hexdigest(),
        'template_source_sha256': hashlib.sha256(template.read_bytes()).hexdigest(),
        'terms': terms, 'pre_repair_quality': audit,
        'sign_source': 'Standard tripeptide stereotags and independent orthogonal test coordinates',
        'strength_kj_mol_nm6': 100000000.0, 'minimum_signed_volume_nm3': 0.0015,
        'acceptance_requires_restraint_removal': True, 'receptor_prepared': False}
