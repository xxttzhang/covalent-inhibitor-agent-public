"""One frozen hydrogen-rebuild hypothesis test; final Amber is unrestrained.

The v3 restrained intermediate is an input proposal, never an accepted result.
All original observed heavy atoms remain fixed; no new conformer/seed search.
"""
from __future__ import annotations
import os
for key in ('OPENMM_CPU_THREADS', 'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[key] = '1'
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import resource
import signal
import time
import traceback

ROOT = Path(__file__).resolve().parents[5]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def bound(ref):
    filename = (ROOT / ref['path']).resolve()
    if not filename.is_relative_to(ROOT) or sha(filename) != ref['sha256']:
        raise ValueError('Frozen input changed: ' + ref['path'])
    return filename


def atom_key(atom):
    return (atom.residue.chain.id, int(atom.residue.id), atom.residue.name, atom.name)


def prepare(policy_path, directory):
    import numpy as np
    import openmm as mm
    from openmm import app, unit
    policy = json.loads(policy_path.read_text())
    if policy['schema'] != 'drug-design-receptor-preparation-policy-v4' or sha(__file__) != policy['preparation_script_sha256']:
        raise ValueError('Unregistered preparation policy/script')
    if mm.version.full_version != policy['runtime']['openmm_full_version']:
        raise ValueError('OpenMM changed')
    for ref in policy['runtime_files'].values():
        bound(ref)
    source = bound(policy['source'])
    initial = {name: bound(ref) for name, ref in policy['initial'].items()}
    for key in ('receipt', 'parent_v3_receipt'):
        receipt = json.loads(initial[key].read_text())
        for name, digest in receipt['artifacts'].items():
            if sha(initial[key].parent / name) != digest:
                raise ValueError('Parent preparation artifact changed')
    previous = json.loads(initial['receipt'].read_text())
    parent = json.loads(initial['parent_v3_receipt'].read_text())
    if parent['artifacts']['amber_chiral-coordinates.json'] != sha(initial['parent_coordinates']):
        raise ValueError('Initial coordinates are not the frozen v3 intermediate')
    directory.mkdir(parents=True, exist_ok=False)
    limit = policy['compute']['preparation_cpu_seconds_limit']
    resource.setrlimit(resource.RLIMIT_CPU, (limit - 5, limit))
    def deadline(signum, frame):
        raise TimeoutError('Frozen preparation CPU/wall limit reached')
    signal.signal(signal.SIGXCPU, deadline)
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(limit)
    start = time.monotonic()
    ledger = {'schema': 'drug-design-receptor-preparation-receipt-v4', 'method_revision': '4.0', 'status': 'running',
              'source_sha256': sha(source), 'policy_sha256': sha(policy_path), 'script_sha256': sha(__file__),
              'initial_inputs': policy['initial'], 'initial_artifacts': previous['artifacts'],
              'parent_v3_artifacts': parent['artifacts'], 'platform': 'CPU', 'threads': 1,
              'new_gpu_compute': False, 'remote_calls': 0, 'model_calls': 0,
              'modeled_coordinates_are_experimental': False, 'stages': [], 'artifacts': {}}
    def progress(stage, **details):
        value = {'stage': stage, 'process_cpu_seconds': time.process_time(), 'elapsed_wall_seconds': time.monotonic() - start, **details}
        ledger['stages'].append(value)
        (directory / 'progress.json').write_text(json.dumps(ledger, indent=2) + '\n')
        print(json.dumps(value, allow_nan=False), flush=True)
    try:
        top = json.loads(initial['topology'].read_text())
        oldmap = json.loads(initial['atom_mapping'].read_text())
        mapped = {tuple(atom['identity']): atom for atom in oldmap['atoms']}
        keys = [(a['chain'], a['number'], a['residue'], a['name']) for a in top['atoms']]
        coordinate_file = json.loads(initial['parent_coordinates'].read_text())
        if coordinate_file['units'] != 'nanometer':
            raise ValueError('Unsupported initial coordinate unit')
        xyz = np.asarray(coordinate_file['positions'], dtype=float)
        if xyz.shape != (len(keys), 3) or not np.isfinite(xyz).all() or len(set(keys)) != len(keys):
            raise ValueError('Invalid atom denominator or coordinates')
        topology = app.Topology()
        chain = topology.addChain('A')
        residues, added = {}, []
        for a in top['atoms']:
            key = (a['number'], a['residue'])
            if key not in residues:
                residues[key] = topology.addResidue(a['residue'], chain, str(a['number']))
            added.append(topology.addAtom(a['name'], app.element.Element.getBySymbol(a['element']), residues[key]))
        for first, second in top['bonds']:
            topology.addBond(added[first], added[second])
        heavy = [i for i, a in enumerate(top['atoms']) if a['element'] != 'H']
        fixed = [i for i, key in enumerate(keys) if mapped[key]['coordinate_origin'] == 'retained_observed_coordinate']
        if len(keys) != 5327 or len(residues) != 333 or len(fixed) != 2460:
            raise ValueError('Frozen full construct or observed atom denominator changed')
        originals = np.asarray([mapped[key]['original_xyz'] if mapped[key]['original_xyz'] else [0, 0, 0] for key in keys]) / 10
        if not np.allclose(xyz[fixed], originals[fixed], atol=1e-14, rtol=0):
            raise ValueError('Initial proposal moved observed atoms')
        forcefield = app.ForceField('amber14/protein.ff14SB.xml')
        platform = mm.Platform.getPlatformByName('CPU')
        platform.setPropertyDefaultValue('Threads', '1')
        # Explicit hydrogen identities preserve the previous protonation state,
        # including CYS773-HG. No pH-driven variant reselection is performed.
        variants = []
        neighbors = {i: [] for i in range(len(keys))}
        for first, second in top['bonds']:
            neighbors[first].append(second)
            neighbors[second].append(first)
        for residue in topology.residues():
            row = []
            for atom in residue.atoms():
                if atom.element.symbol != 'H':
                    continue
                attached = neighbors[atom.index]
                if len(attached) != 1 or added[attached[0]].element.symbol == 'H':
                    raise ValueError('Ambiguous original hydrogen connectivity')
                row.append((atom.name, added[attached[0]].name))
            variants.append(row)
        modeller = app.Modeller(topology, xyz * unit.nanometer)
        modeller.delete([a for a in modeller.topology.atoms() if a.element.symbol == 'H'])
        random.seed(policy['hydrogens']['single_fixed_seed'])
        selected = modeller.addHydrogens(forcefield, pH=7.0, variants=variants, platform=platform)
        rebuilt = {atom_key(atom): atom.index for atom in modeller.topology.atoms()}
        if set(rebuilt) != set(keys) or len(rebuilt) != len(keys):
            raise ValueError('Hydrogen rebuild changed atom identity or protonation')
        rebuilt_bonds = {tuple(sorted((atom_key(first), atom_key(second)))) for first, second in modeller.topology.bonds()}
        if rebuilt_bonds != {tuple(sorted((keys[first], keys[second]))) for first, second in top['bonds']}:
            raise ValueError('Hydrogen rebuild changed chemical graph')
        positions = np.asarray(modeller.positions.value_in_unit(unit.nanometer))
        rebuilt_xyz = np.asarray([positions[rebuilt[key]] for key in keys])
        if not np.array_equal(rebuilt_xyz[heavy], xyz[heavy]):
            raise ValueError('Hydrogen construction moved heavy atoms')
        xyz = rebuilt_xyz
        write(directory / 'hydrogen-rebuild.json', {'hydrogen_count': len(keys) - len(heavy), 'heavy_atom_displacement_nm': 0,
              'explicit_variants': selected, 'seed': policy['hydrogens']['single_fixed_seed'], 'seed_attempts': 1,
              'source_intermediate_is_accepted': False, 'positions_nm': xyz.tolist()})
        progress('hydrogens_rebuilt', atoms=len(keys), rebuilt_hydrogens=len(keys)-len(heavy), fixed_observed_heavy_atoms=len(fixed))
        system = forcefield.createSystem(topology, nonbondedMethod=app.NoCutoff, constraints=None, rigidWater=False, removeCMMotion=False)
        for index in fixed:
            system.setParticleMass(index, 0)
        integrator = mm.VerletIntegrator(0.001)
        context = mm.Context(system, integrator, platform, {'Threads': '1'})
        context.setPositions(xyz)
        before = context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        settings = policy['minimization']
        mm.LocalEnergyMinimizer.minimize(context, settings['tolerance_kj_mol_nm'], settings['max_iterations'])
        state = context.getState(getEnergy=True, getPositions=True, getForces=True)
        xyz = np.asarray(state.getPositions(asNumpy=True).value_in_unit(unit.nanometer))
        energy = state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
        forces = np.asarray(state.getForces(asNumpy=True).value_in_unit(unit.kilojoule_per_mole/unit.nanometer))
        movable = sorted(set(range(len(keys))) - set(fixed))
        rms = float(np.sqrt(np.mean(forces[movable]**2)))
        if not math.isfinite(energy) or not np.isfinite(xyz).all() or not np.allclose(xyz[fixed], originals[fixed], atol=1e-14, rtol=0):
            raise ValueError('Nonfinite state or changed observed coordinates')
        progress('amber_unrestrained', initial_energy_kj_mol=before, final_energy_kj_mol=energy,
                 movable_force_rms_component_kj_mol_nm=rms, tolerance_reached=rms <= settings['tolerance_kj_mol_nm'],
                 artificial_chirality_restraint=False, soft_nonbonded=False, max_iterations=settings['max_iterations'])
        initial_lines = initial['prepared'].read_text().splitlines()
        atom_lines = [line for line in initial_lines if line.startswith('ATOM  ')]
        if len(atom_lines) != len(keys):
            raise ValueError('Original serialized atom denominator changed')
        lines = [line for line in initial_lines if not line.startswith(('ATOM  ', 'TER', 'END', 'REMARK 900'))]
        lines += ['REMARK 900 V4 HYDROGEN REBUILD; OBSERVED HEAVY ATOMS FIXED', 'REMARK 900 MODELED OCCUPANCY ZERO; NOT EXPERIMENTAL COORDINATES']
        changes = []
        for index, (key, line) in enumerate(zip(keys, atom_lines)):
            point = xyz[index] * 10
            updated = line[:30] + ''.join(f'{value:8.3f}' for value in point) + line[54:]
            output_xyz = [float(updated[i:i+8]) for i in (30, 38, 46)]
            lines.append(updated)
            previous_atom = mapped[key]
            changes.append({**previous_atom, 'output_xyz': output_xyz,
                'displacement_angstrom': float(np.linalg.norm(np.asarray(output_xyz)-previous_atom['original_xyz'])) if previous_atom['original_xyz'] else None,
                'v4_preparation_origin': 'rebuilt_hydrogen' if top['atoms'][index]['element']=='H' else 'observed_fixed' if index in fixed else 'modeled_heavy_optimized'})
        final = directory / '1M17-prepared-model-v4.pdb'
        final.write_text('\n'.join(lines + ['TER', 'END']) + '\n')
        write(directory / 'atom-mapping.json', {**oldmap, 'schema': 'drug-design-prepared-receptor-atom-map-v4', 'policy_sha256': sha(policy_path), 'prepared_sha256': sha(final), 'atoms': changes})
        write(directory / 'topology.json', top)
        drift = [a for a in changes if a['original_xyz']]
        if max(a['displacement_angstrom'] for a in drift) != 0:
            raise ValueError('Serialized observed atom moved')
        write(directory / 'observed-atom-drift.json', {'maximum_angstrom': 0.0, 'atoms': [{'identity': a['identity'], 'displacement_angstrom': a['displacement_angstrom']} for a in drift]})
        ledger['artifacts'] = {p.name: sha(p) for p in directory.iterdir() if p.is_file() and p.name != 'progress.json'}
        ledger['status'] = 'prepared_pending_independent_audit'
        progress('prepared_proposal_written', prepared_sha256=sha(final))
    except BaseException as error:
        ledger['status'] = 'preparation_failed'
        ledger['error'] = {'type': type(error).__name__, 'message': str(error), 'traceback': traceback.format_exc()}
        raise
    finally:
        signal.alarm(0)
        ledger['elapsed_wall_seconds'] = time.monotonic()-start
        ledger['process_cpu_seconds'] = time.process_time()
        ledger['constraint_cpu_seconds'] = 0
        ledger['cpu_core_hours'] = ledger['process_cpu_seconds']/3600
        ledger['source_unchanged'] = sha(source) == policy['source']['sha256']
        write(directory / 'preparation-receipt.json', ledger)
        print(json.dumps({key: ledger[key] for key in ('status', 'process_cpu_seconds', 'cpu_core_hours', 'source_unchanged')}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.policy, args.output_dir)
