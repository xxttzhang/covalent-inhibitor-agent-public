"""Bounded observed-atom repair policy and independent displacement limits.

These are conservative trial limits, not experimentally established tolerances.
No function here grants execution permission or receptor/docking admission.
"""
from copy import deepcopy
import hashlib
import json
import math
import re

BACKBONE = {'N', 'CA', 'C', 'O', 'OXT'}
IDENTITY = ('entity_position', 'name', 'chain', 'author_residue', 'resname', 'original_serial')
LOCAL_SCHEMA = 'receptor-local-observation-mask-v1'

BOUNDED_OPTIMIZER = {
    'schema': 'receptor-projected-ball-optimizer-v1',
    'max_iterations': 1000, 'max_evaluations': 3000,
    'tolerance': 10.0, 'initial_step': 1e-4, 'max_step': 1e-2,
    'residual_step': 1e-3, 'max_backtracks': 40,
    'nonconvergence': 'fail_without_retry',
}
BLOCK_OPTIMIZER = {**BOUNDED_OPTIMIZER,
    'schema': 'receptor-free-then-projected-ball-optimizer-v1',
    'warmup_iterations': 100}


def make_block_policy(bounded_policy):
    validate_policy_version(bounded_policy, 'v2-local-bounded')
    return {**deepcopy(bounded_policy), 'bounded_optimizer': deepcopy(BLOCK_OPTIMIZER)}


def make_bounded_policy(local_policy):
    validate_policy(local_policy)
    if 'bounded_optimizer' in local_policy:
        raise ValueError('A bounded policy cannot be selected again automatically')
    return {**deepcopy(local_policy), 'bounded_optimizer': deepcopy(BOUNDED_OPTIMIZER)}


def validate_policy_version(policy, version):
    validate_policy(policy)
    actual = 'v2-local-bounded' if 'bounded_optimizer' in policy else 'v2-local'
    if policy.get('bounded_optimizer') == BLOCK_OPTIMIZER:
        actual = 'v2-local-block'
    if version != actual:
        raise ValueError('Local optimizer policy differs from its explicit version')
    return policy


def digest(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def make_policy(source_sha256, observed_atoms, movable_positions, pocket_positions, target_position):
    """Compile exact source atom identities; caller owns evidence/selection validation."""
    from .receptor_relaxation import RELAXATION_POLICY_V2
    positions, pocket = set(movable_positions), set(pocket_positions)
    if target_position in positions or not positions or target_position not in pocket:
        raise ValueError('Local repair must retain the target site outside the movable mask')
    selected = []
    for atom in observed_atoms:
        if atom['original_serial'] is None or atom['entity_position'] not in positions:
            continue
        tight = atom['name'] in BACKBONE or atom['entity_position'] in pocket
        selected.append({key: atom[key] for key in IDENTITY} | {
            'reference_angstrom': list(atom['xyz_angstrom']),
            'k_kj_mol_nm2': 10000.0 if tight else 1000.0,
            'max_displacement_angstrom': 0.2 if tight else 0.5})
    policy = deepcopy(RELAXATION_POLICY_V2)
    policy['local_observation_mask'] = {'schema': LOCAL_SCHEMA,
        'source_sha256': source_sha256, 'target_entity_position': target_position,
        'movable_entity_positions': sorted(positions), 'pocket_entity_positions': sorted(pocket),
        'fixed_observed_tolerance_angstrom': 1e-10,
        'atoms': sorted(selected, key=lambda a: (a['entity_position'], a['name']))}
    validate_policy(policy)
    bind_policy(policy, observed_atoms, source_sha256)
    return policy


def validate_policy(policy):
    if isinstance(policy, dict) and 'bounded_optimizer' in policy:
        if policy['bounded_optimizer'] not in (BOUNDED_OPTIMIZER, BLOCK_OPTIMIZER):
            raise ValueError('Bounded optimizer configuration differs from the registered policy')
        return validate_policy({k: v for k, v in policy.items() if k != 'bounded_optimizer'})
    from .receptor_relaxation import RELAXATION_POLICY_V2
    if not isinstance(policy, dict) or set(policy) != set(RELAXATION_POLICY_V2) | {'local_observation_mask'}:
        raise ValueError('Local policy must extend the exact registered restrained policy')
    if {k: v for k, v in policy.items() if k != 'local_observation_mask'} != RELAXATION_POLICY_V2:
        raise ValueError('Local policy cannot change forcefield, stages or global preparation settings')
    mask = policy['local_observation_mask']
    if not isinstance(mask, dict) or set(mask) != {'schema', 'source_sha256', 'target_entity_position',
            'movable_entity_positions', 'pocket_entity_positions', 'fixed_observed_tolerance_angstrom', 'atoms'}:
        raise ValueError('Invalid local observation mask fields')
    if mask['schema'] != LOCAL_SCHEMA or not isinstance(mask['source_sha256'], str) or not re.fullmatch('[a-f0-9]{64}', mask['source_sha256']):
        raise ValueError('Local mask requires its original source checksum')
    for name in ('movable_entity_positions', 'pocket_entity_positions'):
        rows = mask[name]
        if not isinstance(rows, list) or not rows or any(type(x) is not int or x < 1 for x in rows) or rows != sorted(set(rows)):
            raise ValueError('Local residue lists must be sorted unique positive entity positions')
    target = mask['target_entity_position']
    if type(target) is not int or target not in mask['pocket_entity_positions'] or target in mask['movable_entity_positions']:
        raise ValueError('Local repair must fix the reactive site')
    if mask['fixed_observed_tolerance_angstrom'] != 1e-10:
        raise ValueError('Fixed observed tolerance cannot be relaxed')
    atoms = mask['atoms']
    if not isinstance(atoms, list) or not 1 <= len(atoms) <= 1000:
        raise ValueError('Local repair requires a bounded explicit atom list')
    seen = set()
    for atom in atoms:
        if set(atom) != set(IDENTITY) | {'reference_angstrom', 'k_kj_mol_nm2', 'max_displacement_angstrom'}:
            raise ValueError('Local atom fields differ from the registered schema')
        if (type(atom['entity_position']) is not int or atom['entity_position'] not in mask['movable_entity_positions']
                or type(atom['original_serial']) is not int or atom['original_serial'] < 1
                or any(not isinstance(atom[k], str) or not atom[k] for k in ('name', 'chain', 'author_residue', 'resname'))):
            raise ValueError('Local atom identity must be explicit and observed')
        key = (atom['entity_position'], atom['name'])
        if key in seen:
            raise ValueError('Duplicate local atom identity')
        seen.add(key)
        xyz = atom['reference_angstrom']
        if not isinstance(xyz, list) or len(xyz) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in xyz):
            raise ValueError('Local atom reference must be three finite angstrom coordinates')
        tight = atom['name'] in BACKBONE or atom['entity_position'] in mask['pocket_entity_positions']
        if (atom['k_kj_mol_nm2'] != (10000.0 if tight else 1000.0)
                or atom['max_displacement_angstrom'] != (0.2 if tight else 0.5)):
            raise ValueError('Local restraint or displacement limit differs from the registered trial policy')
    if set(mask['movable_entity_positions']) != {a['entity_position'] for a in atoms}:
        raise ValueError('Local residue list differs from selected atoms')
    return mask


def bind_policy(policy, atoms, source_sha256):
    """Bind by identity, never by assumed atom ordering; return movable indices."""
    mask = validate_policy(policy)
    if mask['source_sha256'] != source_sha256:
        raise ValueError('Local mask belongs to another source')
    original = {(a['entity_position'], a['name']): a for a in atoms if a['original_serial'] is not None}
    if len(original) != sum(a['original_serial'] is not None for a in atoms):
        raise ValueError('Duplicate observed source atoms')
    target = [a for a in original.values() if a['entity_position'] == mask['target_entity_position']]
    if not target or any(a['resname'] != 'CYS' for a in target) or 'SG' not in {a['name'] for a in target}:
        raise ValueError('Local mask requires an observed prereaction CYS target')
    movable = {}
    for selected in mask['atoms']:
        atom = original.get((selected['entity_position'], selected['name']))
        if atom is None or any(atom[k] != selected[k] for k in IDENTITY) or math.dist(atom['xyz_angstrom'], selected['reference_angstrom']) > 1e-10:
            raise ValueError('Local atom identity or original coordinate changed')
        movable[atom['index']] = selected
    expected = {a['index'] for a in original.values() if a['entity_position'] in mask['movable_entity_positions']}
    if set(movable) != expected:
        raise ValueError('Local selection must include every observed heavy atom in each selected residue')
    return movable


def position_force(movable):
    """One force with per-particle stiffness; no shared global parameter collision."""
    import openmm as mm
    force = mm.CustomExternalForce('0.5*k_local*((x-x0)^2+(y-y0)^2+(z-z0)^2)')
    for name in ('k_local', 'x0', 'y0', 'z0'):
        force.addPerParticleParameter(name)
    for index, atom in sorted(movable.items()):
        force.addParticle(index, [atom['k_kj_mol_nm2'], *[v / 10 for v in atom['reference_angstrom']]])
    return force


def audit_motion(policy, original_atoms, final_atoms, source_sha256):
    """Independent full observed-atom comparison, including atoms outside the mask."""
    movable = bind_policy(policy, original_atoms, source_sha256)
    mask = validate_policy(policy)
    final = {(a['entity_position'], a['name']): a for a in final_atoms}
    if len(final) != len(final_atoms):
        raise ValueError('Duplicate final atom identity')
    rows = []
    for before in original_atoms:
        if before['original_serial'] is None:
            continue
        after = final.get((before['entity_position'], before['name']))
        if after is None or any(before[k] != after[k] for k in IDENTITY):
            raise ValueError('Local audit observed identity changed')
        xyz = after['xyz_angstrom']
        if len(xyz) != 3 or any(not math.isfinite(v) for v in xyz):
            raise ValueError('Nonfinite local audit coordinate')
        displacement = math.dist(before['xyz_angstrom'], xyz)
        selected = movable.get(before['index'])
        limit = selected['max_displacement_angstrom'] if selected else mask['fixed_observed_tolerance_angstrom']
        rows.append({k: before[k] for k in IDENTITY} | {'movable': selected is not None,
            'displacement_angstrom': displacement, 'limit_angstrom': limit, 'passed': displacement <= limit})
    violations = [r for r in rows if not r['passed']]
    return {'schema': 'local-receptor-motion-audit-v1', 'policy_sha256': digest(policy),
        'status': 'local_motion_limits_passed' if not violations else 'local_motion_limits_failed',
        'observed_atom_count': len(rows), 'movable_observed_atom_count': len(movable),
        'fixed_observed_atom_count': len(rows) - len(movable), 'atoms': rows,
        'violations': violations, 'receptor_prepared': False, 'environment_suitability': 'not_established'}
