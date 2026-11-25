"""Conservative pre-enumeration screening; not a covalent reaction prediction."""
import math
import json


POLICY = {'version': 'ligand-parent-cys-proximity-v1',
          'maximum_sg_distance_angstrom': 6.0,
          'short_contact_review_angstrom': 2.1,
          'sulfur_contact_review_angstrom': 2.5,
          'metal_contact_review_angstrom': 3.0}


def report_summary(review):
    labels = {'ELIGIBLE_FOR_ENUMERATION_REVIEW': '可尝试枚举评审',
              'EVIDENCE_INSUFFICIENT': '证据不足，暂缓',
              'OUTSIDE_CURRENT_SCOPE': '超出当前自动方案范围'}
    lines = ['## 母体—Cys 配对预审', '',
             '邻近与身份核验不等于弹头可达、共价反应成立或实验活性。', '']
    for row in review.get('candidates', []):
        assessment = row.get('geometry', {}).get('parent_cys_assessment')
        if not assessment:
            continue
        text = labels.get(assessment['status'], '未评价')
        distance = assessment.get('sg_ligand_min_distance_angstrom')
        lines.append(f"- {row['molecule_id']}：{text}；最近 SG 距离 {distance} Å。")
        if assessment.get('reasons'):
            lines.append('  原因：' + '、'.join(assessment['reasons']))
    return '\n'.join(lines) + '\n'


def review_acquisition(directory):
    """Replay source coordinates/CCD before preparing a new design."""
    from .structure_site_audit import audit_structure_site
    from .parent_chemical_identity import audit_parent_chemical_identity
    acquisition = json.loads((directory / 'structure_acquisition.json').read_text())
    saved = acquisition['coordinate_audit']
    mapping = json.loads((directory / 'cys_mapping.json').read_text())
    audit = audit_structure_site(directory / 'structure.pdb', mapping, saved['component'],
        ligand_chain=saved['ligand_author_chain'], ligand_residue=saved['ligand_author_residue'],
        model=saved['model'], altloc=saved.get('altloc'))
    chemical = audit_parent_chemical_identity(directory / 'component.cif', audit,
        acquisition['parent_chemical_identity']['expected_inchikey'])
    return assess(audit, chemical)


def assess(audit, chemical_identity):
    reasons = []
    distance = audit.get('sg_ligand_min_distance_angstrom')
    if (audit.get('status') != 'coordinate_checks_passed_linkage_review_pending'
            or chemical_identity.get('status') != 'declared_parent_identity_and_heavy_atoms_verified'
            or not audit.get('structure_sha256')
            or audit.get('structure_sha256') != chemical_identity.get('structure_sha256')):
        reasons.append('verified_parent_coordinates_and_identity_required')
    if type(distance) not in (int, float) or not math.isfinite(distance) or distance < 0:
        reasons.append('valid_sg_distance_required')
    environment = audit.get('cys_environment')
    if not environment:
        reasons.append('cys_environment_audit_required')
    elif environment.get('connections'):
        reasons.append('cys_connection_requires_review')
    if environment and environment.get('nearby_sulfur'):
        reasons.append('close_sulfur_contact_requires_review')
    if environment and environment.get('nearby_metals'):
        reasons.append('possible_metal_coordination_requires_review')
    if not reasons and distance <= POLICY['short_contact_review_angstrom']:
        reasons.append('short_contact_requires_covalent_or_structure_review')
    status = 'EVIDENCE_INSUFFICIENT' if reasons else 'ELIGIBLE_FOR_ENUMERATION_REVIEW'
    if not reasons and distance >= POLICY['maximum_sg_distance_angstrom']:
        status = 'OUTSIDE_CURRENT_SCOPE'
        reasons.append('outside_default_proximity_window')
    return {'schema': 'ligand-parent-cys-assessment-v1', 'policy': dict(POLICY),
        'status': status, 'reasons': reasons, 'structure_sha256': audit.get('structure_sha256'),
        'sg_ligand_min_distance_angstrom': distance,
        'reaction_feasibility': 'not_established', 'execution_ready': False,
        'remaining_checks': ['noncovalent mechanism, target species and mutation evidence',
            'modifiable parent positions and protected interactions',
            'enumerated warhead geometry and independent docking'],
        'limitations': ['No recorded connection is not proof of a chemically free thiol.',
            'Proximity does not establish a reachable exit vector or covalent reactivity.']}


def cys_connections(lines, atoms, site):
    """Retain explicit links to the chosen Cys; do not infer bond chemistry."""
    selected = (site['chain'], site['residue'], 'CYS')
    connections = []
    by_serial = {atom['serial']: atom for atom in atoms}
    for line in lines:
        if line.startswith('SSBOND'):
            partners = [(line[15:16].strip(), line[17:21].strip() + line[21:22].strip(), 'CYS'),
                        (line[29:30].strip(), line[31:35].strip() + line[35:36].strip(), 'CYS')]
            if selected in partners:
                connections.append({'record': 'SSBOND', 'partners': partners})
        elif line.startswith('LINK  '):
            partners = [(line[21:22].strip(), line[22:26].strip() + line[26:27].strip(), line[17:20].strip()),
                        (line[51:52].strip(), line[52:56].strip() + line[56:57].strip(), line[47:50].strip())]
            if selected in partners:
                connections.append({'record': 'LINK', 'partners': partners})
        elif line.startswith('CONECT'):
            serials = [int(line[i:i+5]) for i in range(6, len(line), 5) if line[i:i+5].strip()]
            for other in serials[1:]:
                if site['serial'] not in (serials[0], other):
                    continue
                partner = by_serial.get(other if serials[0] == site['serial'] else serials[0], {})
                if (partner.get('chain'), partner.get('residue')) != (site['chain'], site['residue']):
                    connections.append({'record': 'CONECT', 'serials': [serials[0], other]})
    sulfurs, metals = [], []
    metal_elements = {'LI', 'NA', 'K', 'RB', 'CS', 'MG', 'CA', 'SR', 'BA',
                      'MN', 'FE', 'CO', 'NI', 'CU', 'ZN', 'CD', 'HG', 'AG', 'AU', 'PT'}
    for atom in atoms:
        if atom['serial'] == site['serial'] or atom.get('occupancy', 0) <= 0:
            continue
        if site.get('altloc') and atom.get('altloc') not in ('', site['altloc']):
            continue
        if 'xyz' not in atom or 'xyz' not in site:
            continue
        distance = math.dist(site['xyz'], atom['xyz'])
        entry = {key: atom.get(key) for key in ('serial', 'chain', 'residue', 'resname', 'name', 'element', 'altloc')}
        entry['distance_angstrom'] = distance
        if (atom.get('element') == 'S' and atom.get('name') == 'SG'
                and (atom['chain'], atom['residue']) != (site['chain'], site['residue'])
                and distance <= POLICY['sulfur_contact_review_angstrom']):
            sulfurs.append(entry)
        # A conservative review trigger, not a bond assignment or a calibrated metal cutoff.
        if atom.get('element', '').upper() in metal_elements and distance <= POLICY['metal_contact_review_angstrom']:
            metals.append(entry)
    return {'schema': 'cys-environment-review-v1', 'connections': connections,
            'nearby_sulfur': sulfurs, 'nearby_metals': metals,
            'free_thiol_verified': False,
            'limitations': ['Distance contacts trigger review, not bond assignment.',
                           'Only deposited selected-model atoms are assessed; missing atoms are not reconstructed.']}
