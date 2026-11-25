"""Deterministic all-atom relaxation of a verified heavy-atom proposal."""
from __future__ import annotations

import hashlib
import io
import json
import math
from pathlib import Path
import re
import time

from .peptide_templates import chiral_force


RELAXATION_POLICY_V1 = {
    'schema': 'receptor-relaxation-policy-v1',
    'forcefield': 'amber14/protein.ff14SB.xml',
    'pH_assumption': 7.0,
    'target_cysteine_variant': 'CYS',
    'hydrogen_seed': 0,
    'rebuild_hydrogens_before_unrestrained': True,
    'soft_repulsion_k_kj_mol_nm2': 10000.0,
    'chirality_k_kj_mol_nm6': 100000000.0,
    'minimum_signed_volume_nm3': 0.0015,
    'stages': [
        {'name': 'bonded_soft_chiral', 'soft_nonbonded': True,
         'chirality_restraint': True, 'max_iterations': 500, 'tolerance_kj_mol_nm': 10.0},
        {'name': 'amber_chiral', 'soft_nonbonded': False,
         'chirality_restraint': True, 'max_iterations': 1000, 'tolerance_kj_mol_nm': 10.0},
        {'name': 'amber_unrestrained', 'soft_nonbonded': False,
         'chirality_restraint': False, 'max_iterations': 1000, 'tolerance_kj_mol_nm': 10.0},
    ],
}


# Development-only opt-in: production planners still compile v1.
# This is a preparation proposal, not an admission policy.
RELAXATION_POLICY_V2 = json.loads(json.dumps(RELAXATION_POLICY_V1))
RELAXATION_POLICY_V2.update(schema='receptor-relaxation-policy-v2',
    observed_position_k_kj_mol_nm2=1000.0,
    observed_motion_policy='harmonic_reference_in_final_stage')
RELAXATION_POLICY_V2['stages'][-1]['name'] = 'amber_position_restrained'


def observed_position_force(indices, reference_nm, spring_constant):
    """Nonperiodic harmonic restraint; k uses the 0.5*k*r^2 convention."""
    import openmm as mm
    if not math.isfinite(spring_constant) or spring_constant <= 0:
        raise ValueError('Position restraint requires positive finite stiffness')
    if len(set(indices)) != len(indices):
        raise ValueError('Duplicate position-restraint atom')
    force = mm.CustomExternalForce('0.5*k_observed*((x-x0)^2+(y-y0)^2+(z-z0)^2)')
    force.addGlobalParameter('k_observed', spring_constant)
    for name in ('x0', 'y0', 'z0'):
        force.addPerParticleParameter(name)
    for index in indices:
        if type(index) is not int or not 0 <= index < len(reference_nm):
            raise ValueError('Invalid position-restraint atom index')
        point = reference_nm[index]
        if len(point) != 3 or not all(math.isfinite(v) for v in point):
            raise ValueError('Invalid position-restraint reference')
        force.addParticle(index, [float(v) for v in point])
    return force


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def _topology_bonds(topology):
    """Canonical connectivity; bond enumeration and endpoint order are irrelevant."""
    return sorted(tuple(sorted((a.index, b.index))) for a, b in topology.bonds())


def _write_stage_identity(output, atoms, topology):
    """Persist identity before minimization so even a failed stage is auditable."""
    output = Path(output)
    fields = ('index', 'entity_position', 'chain', 'author_residue', 'resname', 'name', 'element')
    if ([a['index'] for a in atoms] != list(range(len(atoms)))
            or len({(a['entity_position'], a['name']) for a in atoms}) != len(atoms)):
        raise ValueError('Stage identity requires unique contiguous atom identities')
    current = list(topology.atoms())
    if len(current) != len(atoms) or any(
            a['index'] != t.index or a['name'] != t.name or a['element'] != t.element.symbol
            or a['chain'] != t.residue.chain.id or a['resname'] != t.residue.name
            or a['author_residue'] != str(t.residue.id) + str(t.residue.insertionCode).strip()
            for a, t in zip(atoms, current)):
        raise ValueError('Stage identity does not match the actual OpenMM topology')
    _write(output / 'stage-atom-mapping.json', {'schema': 'receptor-stage-atom-mapping-v1',
        'coordinate_reference': 'before_relaxation', 'atoms': atoms})
    _write(output / 'stage-topology.json', {'schema': 'receptor-stage-topology-v1',
        'atoms': [{k: a[k] for k in fields} for a in atoms],
        'bonds': [[a.index, b.index] for a, b in topology.bonds()]})
    return {'atom_mapping_sha256': _sha(output / 'stage-atom-mapping.json'),
            'topology_sha256': _sha(output / 'stage-topology.json')}


def _bind_chirality_terms(terms, atoms):
    """Bind an independently generated template plan to one exact atom order."""
    indices = {(a['entity_position'], a['name']): a for a in atoms}
    if len(indices) != len(atoms):
        raise ValueError('Chirality binding requires unique atom identities')
    bound = []
    for term in terms:
        selected = [indices[term['entity_position'], name]
                    for name in (term['center'], *term['neighbor_names'])]
        current = {**term, 'particles': [a['index'] for a in selected],
                   'movable': any(a['original_serial'] is None for a in selected)}
        bound.append(current)
    return bound


def _chirality_summary(terms, xyz):
    """Report scalar triple products in nm^3 for every planned center."""
    import numpy as np
    values, wrong = [], []
    for term in terms:
        points = xyz[term['particles']]
        vectors = points[1:] - points[0]
        volume = float(vectors[0] @ np.cross(vectors[1], vectors[2]))
        row = {'entity_position': term['entity_position'], 'center': term['center'],
               'sign': term['sign'], 'signed_volume_nm3': term['sign'] * volume,
               'movable': term['movable']}
        values.append(row)
        if row['signed_volume_nm3'] <= 0:
            wrong.append(row)
    return {'checked': len(values), 'violations': wrong,
            'minimum_signed_volume_nm3': min(row['signed_volume_nm3'] for row in values),
            'minimum_movable_signed_volume_nm3': min(row['signed_volume_nm3'] for row in values if row['movable'])}


def relax_receptor(parent, parent_receipt_sha256, chirality_plan, chirality_plan_sha256,
                   source, output, *, policy=None):
    """Add hydrogens and run fixed-observation CPU minimization.

    This produces an independently auditable proposal. It does not grant receptor,
    docking, or MD admission.
    """
    import numpy as np
    import openmm as mm
    import random
    from openmm import app, unit

    parent, plan_path, source, out = map(Path, (parent, chirality_plan, source, output))
    if any(path.is_symlink() for path in (parent, plan_path, source)):
        raise ValueError('Relaxation inputs cannot be symbolic links')
    if out.exists():
        raise ValueError('Relaxation output must be a new directory')
    selected_policy = RELAXATION_POLICY_V1 if policy is None else policy
    local_mask = isinstance(selected_policy, dict) and 'local_observation_mask' in selected_policy
    if local_mask:
        from .receptor_local_policy import validate_policy, bind_policy, position_force
        validate_policy(selected_policy)
    elif selected_policy not in (RELAXATION_POLICY_V1, RELAXATION_POLICY_V2):
        raise ValueError('Only the registered receptor relaxation policy is supported')
    movable_observed = local_mask or selected_policy == RELAXATION_POLICY_V2
    receipt_path = parent / 'receipt.json'
    if _sha(receipt_path) != parent_receipt_sha256:
        raise ValueError('Parent heavy-atom receipt checksum mismatch')
    parent_receipt = json.loads(receipt_path.read_text())
    if (parent_receipt.get('schema') != 'receptor-heavy-atom-preparation-v1'
            or parent_receipt.get('status') != 'heavy_atoms_built_pending_independent_qc'):
        raise ValueError('A completed heavy-atom proposal is required')
    evidence = {}
    for name in ('inventory.json', 'atom-mapping.json', 'topology.json', 'heavy-atoms.pdb'):
        path = parent / name
        if path.is_symlink() or not path.is_file() or _sha(path) != parent_receipt['artifacts'][name]:
            raise ValueError('Parent heavy-atom evidence changed: ' + name)
        evidence[name] = json.loads(path.read_text()) if name.endswith('.json') else path.read_bytes()
    if _sha(plan_path) != chirality_plan_sha256:
        raise ValueError('Chirality plan checksum mismatch')
    plan = json.loads(plan_path.read_text())
    if (plan.get('schema') != 'receptor-chirality-plan-v1'
            or plan.get('parent_receipt_sha256') != parent_receipt_sha256
            or plan.get('atom_mapping_sha256') != parent_receipt['artifacts']['atom-mapping.json']):
        raise ValueError('Chirality plan does not bind the heavy-atom proposal')
    template_path = Path(__file__).with_name('peptide_templates.py')
    if plan.get('template_source_sha256') != _sha(template_path):
        raise ValueError('Chirality template implementation changed after plan construction')
    if _sha(source) != parent_receipt['source_sha256']:
        raise ValueError('Original receptor source changed')
    atoms = evidence['atom-mapping.json']['atoms']
    top = evidence['topology.json']
    if [a['index'] for a in atoms] != list(range(len(atoms))) or top['atoms'] != [
        {k: a[k] for k in ('index', 'entity_position', 'chain', 'author_residue', 'resname', 'name', 'element')}
        for a in atoms]:
        raise ValueError('Parent atom map and topology disagree')
    if _bind_chirality_terms(plan['terms'], atoms) != plan['terms']:
        raise ValueError('Chirality terms differ from the current standard template')
    if local_mask:
        if selected_policy['local_observation_mask']['target_entity_position'] != evidence['inventory.json']['mapping']['entity_seq_id']:
            raise ValueError('Local mask must protect the bound reactive site')
        bind_policy(selected_policy, atoms, parent_receipt['source_sha256'])
    out.mkdir(parents=True, exist_ok=False)
    start = time.monotonic()
    receipt = {'schema': 'receptor-relaxation-receipt-v2' if movable_observed else 'receptor-relaxation-receipt-v1', 'status': 'running',
        'parent_receipt_sha256': parent_receipt_sha256,
        'chirality_plan_sha256': chirality_plan_sha256,
        'source_sha256': parent_receipt['source_sha256'],
        'policy': selected_policy, 'policy_sha256': hashlib.sha256(json.dumps(
            selected_policy, sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'platform': 'CPU', 'threads': 1, 'remote_calls': 0, 'model_calls': 0,
        'receptor_prepared': False, 'campaign_ready': False, 'stages': [], 'artifacts': {}}
    try:
        topology = app.Topology()
        chain_ids = {a['chain'] for a in atoms}
        if len(chain_ids) != 1:
            raise ValueError('Registered relaxation supports one selected protein chain')
        chain = topology.addChain(chain_ids.pop())
        residues, added = {}, []
        for atom in atoms:
            position = atom['entity_position']
            if position not in residues:
                match = re.fullmatch(r'(-?\d+)([A-Za-z]?)', atom['author_residue'])
                if not match:
                    raise ValueError('Invalid author residue identifier')
                residues[position] = topology.addResidue(atom['resname'], chain, match.group(1), match.group(2))
            added.append(topology.addAtom(atom['name'], app.element.Element.getBySymbol(atom['element']), residues[position]))
        for first, second in top['bonds']:
            topology.addBond(added[first], added[second])
        xyz = np.asarray([a['xyz_angstrom'] for a in atoms], dtype=float) / 10
        if xyz.shape != (len(atoms), 3) or not np.isfinite(xyz).all():
            raise ValueError('Invalid parent heavy-atom coordinates')
        forcefield = app.ForceField(selected_policy['forcefield'])
        forcefield_path = Path(app.__file__).parent / 'data' / selected_policy['forcefield']
        platform = mm.Platform.getPlatformByName('CPU')
        platform.setPropertyDefaultValue('Threads', '1')
        modeller = app.Modeller(topology, xyz * unit.nanometer)
        target_position = evidence['inventory.json']['mapping']['entity_seq_id']
        variants = [selected_policy['target_cysteine_variant'] if residue.index + 1 == target_position else None
                    for residue in modeller.topology.residues()]
        random.seed(selected_policy['hydrogen_seed'])
        np.random.seed(selected_policy['hydrogen_seed'])
        selected_variants = modeller.addHydrogens(forcefield, pH=selected_policy['pH_assumption'],
                                                   variants=variants, platform=platform)
        author_to_position = {value: index + 1 for index, value in enumerate(
            evidence['inventory.json']['author_residue_by_entity_position'])}
        old = {(a['entity_position'], a['name']): a for a in atoms}
        positions = np.asarray(modeller.positions.value_in_unit(unit.nanometer))
        all_atoms, seen = [], set()
        for atom in modeller.topology.atoms():
            author = str(atom.residue.id) + str(atom.residue.insertionCode).strip()
            if author not in author_to_position:
                raise ValueError('Hydrogen addition changed residue numbering')
            position = author_to_position[author]
            key = (position, atom.name)
            if key in seen:
                raise ValueError('Duplicate all-atom identity after protonation')
            seen.add(key)
            previous = old.get(key)
            point = [float(v) for v in positions[atom.index] * 10]
            if previous and (atom.element.symbol != previous['element']
                    or math.dist(point, previous['xyz_angstrom']) > 1e-8):
                raise ValueError('Hydrogen addition changed a heavy atom')
            all_atoms.append({'index': atom.index, 'entity_position': position,
                'chain': atom.residue.chain.id, 'author_residue': author,
                'resname': atom.residue.name, 'name': atom.name, 'element': atom.element.symbol,
                'xyz_angstrom': point, 'original_serial': previous['original_serial'] if previous else None,
                'coordinate_origin': previous['coordinate_origin'] if previous else 'added_hydrogen',
                'occupancy': previous['occupancy'] if previous else 0.0})
        if set(old) != {k for k in seen if k in old}:
            raise ValueError('Hydrogen addition lost heavy atoms')
        terms = _bind_chirality_terms(plan['terms'], all_atoms)
        if [{k: t[k] for k in ('entity_position', 'author_residue', 'resname', 'center',
                'expected_cip', 'neighbor_names', 'sign', 'movable')} for t in terms] != [
            {k: t[k] for k in ('entity_position', 'author_residue', 'resname', 'center',
                'expected_cip', 'neighbor_names', 'sign', 'movable')} for t in plan['terms']]:
            raise ValueError('Protonation changed the heavy-atom chirality plan')
        fixed = [a['index'] for a in all_atoms if a['coordinate_origin'] == 'retained_observed_coordinate']
        if len(fixed) != sum(a['coordinate_origin'] == 'retained_observed_coordinate' for a in atoms):
            raise ValueError('Observed heavy-atom denominator changed')
        local_atoms = bind_policy(selected_policy, all_atoms, parent_receipt['source_sha256']) if local_mask else {}
        initial = np.asarray(modeller.positions.value_in_unit(unit.nanometer))
        xyz = initial.copy()
        receipt['protonation'] = {'pH_assumption': selected_policy['pH_assumption'],
            'target_entity_position': target_position,
            'target_variant': selected_policy['target_cysteine_variant'],
            'selected_variants': selected_variants,
            'hydrogen_count': sum(a['element'] == 'H' for a in all_atoms),
            'measured_pH_claim': False}
        stage_identity = _write_stage_identity(out, all_atoms, modeller.topology)
        receipt['stage_coordinate_identity'] = stage_identity
        for stage in selected_policy['stages']:
            if (stage['name'] in {'amber_unrestrained', 'amber_position_restrained'}
                    and selected_policy['rebuild_hydrogens_before_unrestrained']):
                current_atoms = list(modeller.topology.atoms())
                neighbors = {atom.index: [] for atom in current_atoms}
                for first, second in modeller.topology.bonds():
                    neighbors[first.index].append(second)
                    neighbors[second.index].append(first)
                explicit = []
                for residue in modeller.topology.residues():
                    row = []
                    for atom in residue.atoms():
                        if atom.element.symbol != 'H':
                            continue
                        attached = neighbors[atom.index]
                        if len(attached) != 1 or attached[0].element.symbol == 'H':
                            raise ValueError('Ambiguous hydrogen connectivity before rebuild')
                        row.append((atom.name, attached[0].name))
                    explicit.append(row)
                rebuilt = app.Modeller(modeller.topology, xyz * unit.nanometer)
                rebuilt.delete([atom for atom in rebuilt.topology.atoms() if atom.element.symbol == 'H'])
                random.seed(selected_policy['hydrogen_seed'])
                np.random.seed(selected_policy['hydrogen_seed'])
                rebuilt_selected = rebuilt.addHydrogens(forcefield,
                    pH=selected_policy['pH_assumption'], variants=explicit, platform=platform)
                def atom_key(atom):
                    author = str(atom.residue.id) + str(atom.residue.insertionCode).strip()
                    return (author_to_position[author], atom.name)
                old_keys = [atom_key(atom) for atom in modeller.topology.atoms()]
                new_keys = [atom_key(atom) for atom in rebuilt.topology.atoms()]
                if old_keys != new_keys or rebuilt_selected != explicit:
                    raise ValueError('Hydrogen rebuild changed atom identity or protonation')
                if _topology_bonds(modeller.topology) != _topology_bonds(rebuilt.topology):
                    raise ValueError('Hydrogen rebuild changed the registered stage connectivity')
                rebuilt_xyz = np.asarray(rebuilt.positions.value_in_unit(unit.nanometer))
                heavy_indices = [atom.index for atom in rebuilt.topology.atoms() if atom.element.symbol != 'H']
                if not np.array_equal(rebuilt_xyz[heavy_indices], xyz[heavy_indices]):
                    raise ValueError('Hydrogen rebuild moved heavy atoms')
                xyz = rebuilt_xyz
                modeller = rebuilt
                variants_record = {'variants': explicit, 'seed': selected_policy['hydrogen_seed']}
                _write(out / 'hydrogen-variants.json', variants_record)
                receipt['hydrogen_rebuild'] = {'stage': 'before_amber_unrestrained',
                    'hydrogen_count': sum(atom.element.symbol == 'H' for atom in modeller.topology.atoms()),
                    'heavy_atom_displacement_nm': 0.0,
                    'explicit_variants_sha256': _sha(out / 'hydrogen-variants.json'),
                    'seed': selected_policy['hydrogen_seed'], 'seed_attempts': 1,
                    'protonation_changed': False}
            system = forcefield.createSystem(modeller.topology, nonbondedMethod=app.NoCutoff,
                constraints=None, rigidWater=False, removeCMMotion=False)
            position_restrained = stage['name'] == 'amber_position_restrained'
            stage_fixed = ([i for i in fixed if i not in local_atoms] if local_mask else []) if position_restrained else fixed
            for index in stage_fixed:
                system.setParticleMass(index, 0)
            if position_restrained:
                system.addForce(position_force(local_atoms) if local_mask else observed_position_force(fixed, initial,
                    selected_policy['observed_position_k_kj_mol_nm2']))
            if stage['soft_nonbonded']:
                force_index = next(i for i, force in enumerate(system.getForces())
                                   if isinstance(force, mm.NonbondedForce))
                nonbonded = system.getForce(force_index)
                soft = mm.CustomNonbondedForce('0.5*ksoft*min(0,r-0.8*0.5*(sigma1+sigma2))^2')
                soft.addGlobalParameter('ksoft', selected_policy['soft_repulsion_k_kj_mol_nm2'])
                soft.addPerParticleParameter('sigma')
                for index in range(system.getNumParticles()):
                    soft.addParticle([nonbonded.getParticleParameters(index)[1].value_in_unit(unit.nanometer)])
                for index in range(nonbonded.getNumExceptions()):
                    first, second, *_ = nonbonded.getExceptionParameters(index)
                    soft.addExclusion(first, second)
                soft.setNonbondedMethod(mm.CustomNonbondedForce.CutoffNonPeriodic)
                soft.setCutoffDistance(1.0)
                soft.addInteractionGroup(set(range(len(all_atoms))) - set(fixed), set(range(len(all_atoms))))
                system.removeForce(force_index)
                system.addForce(soft)
            if stage['chirality_restraint']:
                system.addForce(chiral_force(terms, selected_policy['chirality_k_kj_mol_nm6'],
                                             selected_policy['minimum_signed_volume_nm3']))
            integrator = mm.VerletIntegrator(0.001)
            context = mm.Context(system, integrator, platform, {'Threads': '1'})
            context.setPositions(xyz)
            before = context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
            bounded_result = None
            if position_restrained and 'bounded_optimizer' in selected_policy:
                from .receptor_bounded_minimizer import minimize_openmm_bounded
                from .receptor_local_policy import BLOCK_OPTIMIZER
                minimizer = minimize_openmm_bounded
                if selected_policy['bounded_optimizer'] == BLOCK_OPTIMIZER:
                    from .receptor_block_minimizer import minimize_openmm_free_then_bounded
                    minimizer = minimize_openmm_free_then_bounded
                options = {k: v for k, v in selected_policy['bounded_optimizer'].items()
                           if k not in {'schema', 'nonconvergence'}}
                bounded_result = minimizer(context, initial,
                    {i: a['max_displacement_angstrom'] / 10 for i, a in local_atoms.items()},
                    stage_fixed, **options)
            else:
                mm.LocalEnergyMinimizer.minimize(context, stage['tolerance_kj_mol_nm'], stage['max_iterations'])
            state = context.getState(getEnergy=True, getPositions=True, getForces=True)
            xyz = np.asarray(state.getPositions(asNumpy=True).value_in_unit(unit.nanometer))
            energy = state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
            forces = np.asarray(state.getForces(asNumpy=True).value_in_unit(unit.kilojoule_per_mole / unit.nanometer))
            movable = sorted(set(range(len(all_atoms))) - set(stage_fixed))
            rms = float(np.sqrt(np.mean(forces[movable] ** 2)))
            drift = float(np.max(np.abs(xyz[stage_fixed] - initial[stage_fixed])) * 10) if stage_fixed else 0.0
            if not math.isfinite(before) or not math.isfinite(energy) or not np.isfinite(xyz).all() or (stage_fixed and drift > 1e-12):
                raise ValueError('Nonfinite relaxation or moved observed atom')
            receipt['stages'].append({'name': stage['name'], 'initial_energy_kj_mol': before,
                'final_energy_kj_mol': energy, 'movable_force_rms_component_kj_mol_nm': rms,
                'tolerance_reached': rms <= stage['tolerance_kj_mol_nm'],
                'iteration_cap': stage['max_iterations'], 'fixed_maximum_drift_angstrom': drift,
                'artificial_chirality_restraint': stage['chirality_restraint'],
                'soft_nonbonded': stage['soft_nonbonded'],
                'chirality': _chirality_summary(terms, xyz)})
            if movable_observed:
                receipt['stages'][-1].update(observed_maximum_displacement_angstrom=float(np.max(np.linalg.norm(xyz[fixed] - initial[fixed], axis=1)) * 10),
                    observed_atoms_fixed=not position_restrained,
                    artificial_position_restraint=position_restrained)
                if local_mask:
                    receipt['stages'][-1].update(fixed_observed_atom_count=len(stage_fixed),
                        movable_observed_atom_count=len(fixed) - len(stage_fixed))
                else:
                    receipt['stages'][-1].pop('fixed_maximum_drift_angstrom')
            if bounded_result is not None:
                if 'phases' in bounded_result:
                    from .receptor_block_minimizer import numerical_receipt
                    receipt['stages'][-1]['bounded_optimizer'] = numerical_receipt(bounded_result)
                else:
                    receipt['stages'][-1]['bounded_optimizer'] = {
                        k: v for k, v in bounded_result.items() if k != 'positions_nm'}
                receipt['stages'][-1]['tolerance_reached'] = bounded_result['status'] == 'converged'
                receipt['stages'][-1]['convergence_metric'] = 'projected_gradient_max_kj_mol_nm'
            _write(out / (stage['name'] + '-coordinates.json'),
                   {'units': 'nanometer', 'positions': xyz.tolist(), **stage_identity})
            del context, integrator, system
            if bounded_result is not None and bounded_result['status'] != 'converged':
                raise ValueError('Bounded receptor optimizer did not converge: ' + bounded_result['status'])
        for atom, point in zip(all_atoms, xyz * 10):
            if movable_observed and atom['original_serial'] is not None:
                atom['source_xyz_angstrom'] = list(atom['xyz_angstrom'])
                atom['coordinate_origin'] = 'relaxed_observed_atom'

            atom['xyz_angstrom'] = [float(v) for v in point]
        stream = io.StringIO()
        app.PDBFile.writeFile(modeller.topology, xyz * unit.nanometer, stream, keepIds=True)
        body, index = [], 0
        for line in stream.getvalue().splitlines():
            if line.startswith(('ATOM  ', 'HETATM')):
                line = line[:54] + f'{all_atoms[index]["occupancy"]:6.2f}{0:6.2f}' + line[66:]
                index += 1
            if not line.startswith(('HEADER', 'REMARK')):
                body.append(line)
        header = next(line for line in source.read_text().splitlines() if line.startswith('HEADER'))
        (out / 'relaxed.pdb').write_text('\n'.join([header,
            'REMARK 900 COMPUTATIONAL ALL-ATOM RELAXATION; NOT AN ACCEPTED RECEPTOR',
            'REMARK 900 MODELED ATOMS AND HYDROGENS HAVE ZERO OCCUPANCY'] + body) + '\n')
        bonds = [[a.index, b.index] for a, b in modeller.topology.bonds()]
        _write(out / 'atom-mapping.json', {'atoms': all_atoms})
        _write(out / 'topology.json', {'atoms': [{k: a[k] for k in ('index', 'entity_position',
            'chain', 'author_residue', 'resname', 'name', 'element')} for a in all_atoms], 'bonds': bonds})
        heavy_old = [a for a in all_atoms if a['element'] != 'H']
        heavy_index = {a['index']: index for index, a in enumerate(heavy_old)}
        heavy = [{**a, 'index': heavy_index[a['index']]} for a in heavy_old]
        heavy_bonds = [[heavy_index[a], heavy_index[b]] for a, b in bonds
                       if a in heavy_index and b in heavy_index]
        _write(out / 'heavy-atom-mapping.json', {'atoms': heavy})
        _write(out / 'heavy-topology.json', {'atoms': [{k: a[k] for k in ('index', 'entity_position',
            'chain', 'author_residue', 'resname', 'name', 'element')} for a in heavy], 'bonds': heavy_bonds})
        receipt.update(status=('position_restrained_relaxation_completed_quality_pending' if movable_observed
                              else 'unrestrained_relaxation_completed_quality_pending'),
            atom_count=len(all_atoms), heavy_atom_count=len(heavy), fixed_observed_heavy_atoms=len(fixed) - len(local_atoms) if local_mask else (0 if movable_observed else len(fixed)),
            artificial_forces_in_final_stage=movable_observed,
            remaining_checks=['independent identity, graph, stereochemistry and geometry',
                              'environment retention', 'receptor admission'])
    except Exception as exc:
        receipt.update(status='relaxation_failed', error=str(exc))
        raise
    finally:
        receipt['elapsed_wall_seconds'] = time.monotonic() - start
        receipt['artifacts'] = {p.name: _sha(p) for p in out.iterdir()
                                if p.is_file() and p.name not in {'progress.json', 'receipt.json'}}
        _write(out / 'receipt.json', receipt)
    return receipt
