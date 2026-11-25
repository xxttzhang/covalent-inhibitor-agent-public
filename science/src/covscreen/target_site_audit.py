"""Audit mapped Cys coordinates without assuming a parent ligand or reactive site."""
import hashlib
import math
import re
from pathlib import Path

from .pdb_coordinates import read_pdb_coordinates


def audit_target_site(pdb_path: Path, mapping: dict, *, model=None, altloc=None):
    if mapping.get('status') != 'sequence_mapping_verified_coordinates_pending':
        raise ValueError('A verified sequence-mapping result is required')
    if altloc is not None and (not isinstance(altloc, str) or not re.fullmatch('[A-Za-z0-9]', altloc)):
        raise ValueError('Select one explicit alternate location')
    raw, lines, atoms, selected_model = read_pdb_coordinates(pdb_path, mapping['entry_id'], model)
    site = [a for a in atoms if a['chain'] == mapping['auth_asym_id']
            and a['residue'] == str(mapping['author_residue_id'])]
    if not site or any(a['resname'] != 'CYS' or a['record'] != 'ATOM' for a in site):
        raise ValueError('Mapped cysteine is absent or has a different residue identity')
    alternatives = {a['altloc'] for a in site if a['altloc']}
    if alternatives and altloc is None:
        raise ValueError('Alternate site coordinates require explicit selection')
    if altloc is not None and altloc not in alternatives:
        raise ValueError('Requested alternate location is absent')
    selected = [a for a in site if not a['altloc'] or a['altloc'] == altloc]
    expected = {'N': 'N', 'CA': 'C', 'C': 'C', 'O': 'O', 'CB': 'C', 'SG': 'S'}
    if len({a['name'] for a in selected}) != len(selected):
        raise ValueError('Duplicate site atom names after conformer selection')
    named = {a['name']: a for a in selected}
    if any(name not in named or named[name]['element'] != element for name, element in expected.items()):
        raise ValueError('Mapped cysteine lacks the complete heavy-atom identity')
    if any(a['occupancy'] <= 0 for a in selected):
        raise ValueError('Selected site has zero-occupancy atoms')
    # Coordinate evidence of chemical occupancy is kept separate from absence
    # of a record: no LINK/SSBOND/CONECT does not prove a free reactive thiol.
    sulfur = named['SG']
    by_serial = {a['serial']: a for a in atoms}
    links = []
    for line in lines:
        if line.startswith('SSBOND'):
            partners = [(line[15:16].strip(), str(int(line[17:21])) + line[21:22].strip()),
                        (line[29:30].strip(), str(int(line[31:35])) + line[35:36].strip())]
            if (sulfur['chain'], sulfur['residue']) in partners:
                links.append({'record': 'SSBOND', 'partners': partners})
        elif line.startswith('LINK  '):
            partners = [(line[21:22].strip(), str(int(line[22:26])) + line[26:27].strip(), line[12:16].strip()),
                        (line[51:52].strip(), str(int(line[52:56])) + line[56:57].strip(), line[42:46].strip())]
            if (sulfur['chain'], sulfur['residue'], 'SG') in partners:
                links.append({'record': 'LINK', 'partners': partners})
        elif line.startswith('CONECT'):
            serials = [int(line[i:i+5]) for i in range(6, len(line), 5) if line[i:i+5].strip()]
            if serials:
                for other in serials[1:]:
                    pair = {serials[0], other}
                    if sulfur['serial'] in pair:
                        partner = by_serial.get(other if serials[0] == sulfur['serial'] else serials[0])
                        if partner is None or (partner['chain'], partner['residue']) != (sulfur['chain'], sulfur['residue']):
                            links.append({'record': 'CONECT', 'serials': sorted(pair)})
    distance = math.dist(named['CB']['xyz'], sulfur['xyz'])
    if not 1.5 < distance < 2.2:
        raise ValueError('Cys CB-SG geometry lies outside the supported preparation range')
    return {'schema': 'target-site-coordinate-audit-v1',
            'status': 'site_linkage_review_required' if links else 'site_coordinates_verified_preparation_pending',
            'entry_id': mapping['entry_id'], 'uniprot': mapping['uniprot'],
            'canonical_residue': mapping['uniprot_residue'], 'author_chain': sulfur['chain'],
            'author_residue': sulfur['residue'], 'model': selected_model, 'altloc': altloc,
            'structure_sha256': hashlib.sha256(raw).hexdigest(),
            'site_heavy_atoms': [{k: named[n][k] for k in ('serial', 'name', 'element', 'xyz', 'occupancy')}
                                for n in expected],
            'cb_sg_distance_angstrom': distance, 'explicit_site_connections': links,
            'minimum_site_occupancy': min(a['occupancy'] for a in selected),
            'receptor_prepared': False, 'reactivity_validated': False, 'campaign_ready': False,
            'remaining_checks': ['species and construct mutations', 'site chemical state and accessibility',
                                 'missing residues and receptor preparation', 'supported covalent reaction chemistry']}
