"""One deterministic, CPU-only repair of frozen v2 modeled coordinates.

Observed heavy atoms are massless and never moved. Standard-template chirality
restraints are a preparation device, not an acceptance criterion. The last stage
uses only the unchanged Amber protein force field. No stochastic trajectories,
conformer selection, threshold changes, or scientific pass claims are made here.
"""
from __future__ import annotations
import os
for name in ('OPENMM_CPU_THREADS', 'OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS', 'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
    os.environ[name] = '1'
import argparse
import hashlib
import json
import math
from pathlib import Path
import resource
import signal
import time
import traceback

WORKSPACE = Path(__file__).resolve().parents[5]
RESIDUES = dict(zip('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split(), 'ARNDCQEGHILKMFPSTWYV'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def bound(root, ref):
    path = (root / ref['path']).resolve()
    if not path.is_relative_to(root.resolve()) or sha(path) != ref['sha256']:
        raise ValueError('Input provenance mismatch: ' + ref['path'])
    return path


def atoms(path):
    return [{'serial': int(line[6:11]), 'identity': (line[21], int(line[22:26]), line[17:20].strip(), line[12:16].strip()),
             'xyz': [float(line[i:i+8]) for i in (30, 38, 46)], 'line': line}
            for line in Path(path).read_text().splitlines() if line.startswith('ATOM  ')]


def template_sign(residue, center_name):
    """Derive orientation from standard sequence tags, never input coordinates.

    Place the center's three ordered heavy neighbors at +x,+y,+/-z on an
    independent standard tripeptide. RDKit derives the resulting CIP label;
    exactly one orientation must match the original sequence template label.
    """
    from rdkit import Chem
    mol = Chem.MolFromSequence('A' + RESIDUES[residue] + 'A')
    for atom in mol.GetAtoms():
        info = atom.GetPDBResidueInfo()
        if info.GetResidueName().strip() == 'ILE' and info.GetName().strip() in ('CG1', 'CG2'):
            info.SetName(' CG2' if info.GetName().strip() == 'CG1' else ' CG1')
    Chem.AssignStereochemistry(mol, cleanIt=True, force=True)
    center = next(atom for atom in mol.GetAtoms() if atom.GetPDBResidueInfo().GetResidueNumber() == 2
                  and atom.GetPDBResidueInfo().GetName().strip() == center_name)
    expected = center.GetProp('_CIPCode')
    neighbors = sorted(center.GetNeighbors(), key=lambda atom: atom.GetPDBResidueInfo().GetName().strip())
    if len(neighbors) != 3:
        raise ValueError('Unsupported standard heavy-atom chirality neighborhood')
    matches = []
    for sign in (-1, 1):
        trial = Chem.Mol(mol)
        conformer = Chem.Conformer(mol.GetNumAtoms())
        conformer.Set3D(True)
        for index in range(mol.GetNumAtoms()):
            conformer.SetAtomPosition(index, (7.0+index, 13.0+index*index, 19.0+index*index*index))
        conformer.SetAtomPosition(center.GetIdx(), (0, 0, 0))
        for atom, point in zip(neighbors, [(1, 0, 0), (0, 1, 0), (0, 0, sign)]):
            conformer.SetAtomPosition(atom.GetIdx(), point)
        trial.AddConformer(conformer)
        Chem.AssignStereochemistryFrom3D(trial, replaceExistingTags=True)
        Chem.AssignStereochemistry(trial, cleanIt=True, force=True)
        actual = trial.GetAtomWithIdx(center.GetIdx())
        if actual.HasProp('_CIPCode') and actual.GetProp('_CIPCode') == expected:
            matches.append(sign)
    if len(matches) != 1:
        raise ValueError('Standard template does not define a unique orientation')
    return {'residue': residue, 'center': center_name, 'expected_cip': expected,
            'neighbor_names': [atom.GetPDBResidueInfo().GetName().strip() for atom in neighbors], 'sign': matches[0]}


def constraints(policy_path, output):
    from rdkit import rdBase
    start = time.process_time()
    policy = json.loads(policy_path.read_text())
    if sha(__file__) != policy['preparation_script_sha256']:
        raise ValueError('Preparation script changed')
    limit = policy['compute']['constraint_cpu_seconds_limit']
    resource.setrlimit(resource.RLIMIT_CPU, (limit, limit))
    source = bound(WORKSPACE, policy['source'])
    mapping = json.loads(bound(WORKSPACE, policy['initial']['atom_mapping']).read_text())
    topology = json.loads(bound(WORKSPACE, policy['initial']['topology']).read_text())
    by_key = {tuple(atom['identity']): atom for atom in mapping['atoms']}
    indices = {(a['chain'], a['number'], a['residue'], a['name']): a['index'] for a in topology['atoms']}
    sequence = [word for line in source.read_text().splitlines() if line.startswith('SEQRES') and line[11] == 'A' for word in line[19:70].split()]
    if len(sequence) != 333:
        raise ValueError('Full deposited sequence required')
    templates = {(residue, name): template_sign(residue, name) for residue in sorted(set(sequence)) if residue != 'GLY'
                 for name in (('CA', 'CB') if residue in ('ILE', 'THR') else ('CA',))}
    for residue, center, expected in [('CYS', 'CA', 'R'), ('ILE', 'CA', 'S'), ('ILE', 'CB', 'S'), ('THR', 'CA', 'S'), ('THR', 'CB', 'R')]:
        if templates[(residue, center)]['expected_cip'] != expected:
            raise ValueError('Standard amino-acid CIP exception mismatch')
    terms = []
    for number, residue in enumerate(sequence, start=666):
        if residue == 'GLY':
            continue
        for name in (('CA', 'CB') if residue in ('ILE', 'THR') else ('CA',)):
            template = templates[(residue, name)]
            keys = [('A', number, residue, item) for item in [name] + template['neighbor_names']]
            terms.append({**template, 'identity': keys[0], 'particles': [indices[key] for key in keys],
                          'movable': any(by_key[key]['coordinate_origin'] != 'retained_observed_coordinate' for key in keys)})
    write(output, {'schema': 'drug-design-receptor-chirality-constraints-v3', 'synthetic_template_geometry_only': True,
                   'source_sha256': sha(source), 'policy_sha256': sha(policy_path), 'script_sha256': sha(__file__),
                   'initial_mapping_sha256': policy['initial']['atom_mapping']['sha256'],
                   'initial_topology_sha256': policy['initial']['topology']['sha256'],
                   'sign_source': 'standard tripeptide sequence stereotags and ordered artificial orthogonal neighbors; no proposed coordinates used',
                   'rdkit': rdBase.rdkitVersion, 'templates': list(templates.values()), 'terms': terms,
                   'process_cpu_seconds': time.process_time(), 'construction_cpu_seconds': time.process_time()-start})


def chiral_force(terms, strength, minimum):
    import openmm as mm
    force = mm.CustomCompoundBondForce(4, '0.5*k*min(0,s*v-vmin)^2;v=(x2-x1)*((y3-y1)*(z4-z1)-(z3-z1)*(y4-y1))-(y2-y1)*((x3-x1)*(z4-z1)-(z3-z1)*(x4-x1))+(z2-z1)*((x3-x1)*(y4-y1)-(y3-y1)*(x4-x1))')
    force.addGlobalParameter('k', strength)
    force.addGlobalParameter('vmin', minimum)
    force.addPerBondParameter('s')
    for term in terms:
        if term['movable']:
            force.addBond(term['particles'], [term['sign']])
    return force


def prepare(policy_path, constraints_path, directory):
    import numpy as np
    import openmm as mm
    from openmm import app, unit
    policy = json.loads(policy_path.read_text())
    if policy['schema'] != 'drug-design-receptor-preparation-policy-v3' or sha(__file__) != policy['preparation_script_sha256']:
        raise ValueError('Unregistered preparation policy/script')
    if mm.version.full_version != policy['runtime']['openmm_full_version']:
        raise ValueError('OpenMM version differs from policy')
    source = bound(WORKSPACE, policy['source'])
    initial = {name: bound(WORKSPACE, ref) for name, ref in policy['initial'].items()}
    frozen = json.loads(initial['receipt'].read_text())
    for name, expected in frozen['artifacts'].items():
        if sha(initial['receipt'].parent / name) != expected:
            raise ValueError('Frozen initial preparation lineage changed')
    proof = json.loads(constraints_path.read_text())
    if proof['policy_sha256'] != sha(policy_path) or proof['script_sha256'] != sha(__file__) or proof['source_sha256'] != sha(source):
        raise ValueError('Chirality constraint construction differs from frozen policy')
    if proof['initial_mapping_sha256'] != sha(initial['atom_mapping']) or proof['initial_topology_sha256'] != sha(initial['topology']):
        raise ValueError('Constraint atom order differs from initial topology')
    directory.mkdir(parents=True, exist_ok=False)
    start, cpu = time.monotonic(), time.process_time()
    limit = policy['compute']['preparation_cpu_seconds_limit']
    resource.setrlimit(resource.RLIMIT_CPU, (limit-5, limit))
    def deadline(signum, frame):
        raise TimeoutError('Single-attempt preparation CPU/wall budget reached')
    signal.signal(signal.SIGXCPU, deadline)
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(limit)
    ledger = {'schema': 'drug-design-receptor-preparation-receipt-v3', 'method_revision': '3.0', 'status': 'running',
              'source_sha256': sha(source), 'policy_sha256': sha(policy_path), 'script_sha256': sha(__file__),
              'initial_inputs': policy['initial'], 'initial_artifacts': frozen['artifacts'], 'constraints_sha256': sha(constraints_path),
              'platform': 'CPU', 'threads': 1, 'new_gpu_compute': False, 'remote_calls': 0,
              'modeled_coordinates_are_experimental': False, 'stages': [], 'artifacts': {}}
    def progress(name, **details):
        value = {'stage': name, 'process_cpu_seconds': time.process_time()-cpu, 'elapsed_wall_seconds': time.monotonic()-start, **details}
        ledger['stages'].append(value)
        (directory/'progress.json').write_text(json.dumps(ledger, indent=2, allow_nan=False)+'\n')
        print(json.dumps(value, allow_nan=False), flush=True)
    try:
        structure = atoms(initial['prepared'])
        oldmap = json.loads(initial['atom_mapping'].read_text())
        mapped = {tuple(a['identity']): a for a in oldmap['atoms']}
        top = json.loads(initial['topology'].read_text())
        if [a['identity'] for a in structure] != [(a['chain'], a['number'], a['residue'], a['name']) for a in top['atoms']]:
            raise ValueError('Initial PDB atom order differs from recorded topology')
        topology = app.Topology()
        chain = topology.addChain('A')
        residues, added = {}, []
        for atom in top['atoms']:
            key = (atom['number'], atom['residue'])
            if key not in residues:
                residues[key] = topology.addResidue(atom['residue'], chain, str(atom['number']))
            added.append(topology.addAtom(atom['name'], app.element.Element.getBySymbol(atom['element']), residues[key]))
        for first, second in top['bonds']:
            topology.addBond(added[first], added[second])
        if len(residues) != 333:
            raise ValueError('Truncation of deposited construct forbidden')
        fixed = [a['index'] for a in top['atoms'] if mapped[(a['chain'], a['number'], a['residue'], a['name'])]['coordinate_origin'] == 'retained_observed_coordinate']
        if len(fixed) != 2460:
            raise ValueError('Observed heavy atom denominator changed')
        xyz = np.asarray([a['xyz'] for a in structure], dtype=float) / 10
        initial_xyz = xyz.copy()
        forcefield = app.ForceField('amber14/protein.ff14SB.xml')
        forcepath = Path(app.__file__).parent/'data/amber14/protein.ff14SB.xml'
        if sha(forcepath) != policy['runtime']['forcefield_sha256']:
            raise ValueError('Amber forcefield changed')
        platform = mm.Platform.getPlatformByName('CPU')
        write(directory/'chirality-constraints.json', proof)
        progress('fixed_initial_state', atoms=len(added), residues=len(residues), fixed_observed_heavy_atoms=len(fixed),
                 chirality_constraints=sum(term['movable'] for term in proof['terms']))
        for stage in policy['minimization']['stages']:
            system = forcefield.createSystem(topology, nonbondedMethod=app.NoCutoff, constraints=None, rigidWater=False, removeCMMotion=False)
            for index in fixed:
                system.setParticleMass(index, 0)
            if stage['soft_nonbonded']:
                index = next(i for i,f in enumerate(system.getForces()) if isinstance(f, mm.NonbondedForce))
                nonbonded = system.getForce(index)
                soft = mm.CustomNonbondedForce('0.5*ksoft*min(0,r-0.8*0.5*(sigma1+sigma2))^2')
                soft.addGlobalParameter('ksoft', policy['minimization']['soft_repulsion_k_kj_mol_nm2'])
                soft.addPerParticleParameter('sigma')
                for i in range(system.getNumParticles()):
                    soft.addParticle([nonbonded.getParticleParameters(i)[1].value_in_unit(unit.nanometer)])
                for i in range(nonbonded.getNumExceptions()):
                    first,second,*_ = nonbonded.getExceptionParameters(i)
                    soft.addExclusion(first,second)
                soft.setNonbondedMethod(mm.CustomNonbondedForce.CutoffNonPeriodic)
                soft.setCutoffDistance(1.0)
                soft.addInteractionGroup(set(range(len(added)))-set(fixed), set(range(len(added))))
                system.removeForce(index)
                system.addForce(soft)
            if stage['chirality_restraint']:
                system.addForce(chiral_force(proof['terms'], policy['minimization']['chirality_k_kj_mol_nm6'], policy['minimization']['minimum_signed_volume_nm3']))
            integrator = mm.VerletIntegrator(0.001)
            context = mm.Context(system, integrator, platform, {'Threads':'1'})
            context.setPositions(xyz)
            before = context.getState(getEnergy=True).getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
            mm.LocalEnergyMinimizer.minimize(context, stage['tolerance_kj_mol_nm'], stage['max_iterations'])
            state = context.getState(getEnergy=True, getPositions=True, getForces=True)
            energy = state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole)
            xyz = np.asarray(state.getPositions(asNumpy=True).value_in_unit(unit.nanometer))
            force = np.asarray(state.getForces(asNumpy=True).value_in_unit(unit.kilojoule_per_mole/unit.nanometer))
            movable = sorted(set(range(len(added)))-set(fixed))
            rms = float(np.sqrt(np.mean(force[movable]**2)))
            drift = float(np.max(np.abs(xyz[fixed]-initial_xyz[fixed]))*10)
            if not math.isfinite(energy) or not np.isfinite(xyz).all() or drift != 0:
                raise ValueError('Nonfinite preparation or moved observed atom')
            write(directory/(stage['name']+'-coordinates.json'), {'units':'nanometer','positions':xyz.tolist()})
            progress(stage['name'], initial_energy_kj_mol=before, final_energy_kj_mol=energy,
                     movable_force_rms_component_kj_mol_nm=rms, tolerance_reached=rms <= stage['tolerance_kj_mol_nm'],
                     iteration_cap=stage['max_iterations'], fixed_maximum_drift_angstrom=drift,
                     artificial_chirality_restraint=stage['chirality_restraint'], soft_nonbonded=stage['soft_nonbonded'])
            del context, integrator, system
        lines = [line for line in initial['prepared'].read_text().splitlines() if not line.startswith(('ATOM  ','TER','END','REMARK 900'))]
        lines += ['REMARK 900 V3 COMPUTATIONAL REFINEMENT; OBSERVED HEAVY ATOMS FIXED', 'REMARK 900 MODELED OCCUPANCY ZERO; NOT EXPERIMENTAL COORDINATES']
        for index, atom in enumerate(structure):
            point = xyz[index]*10
            lines.append(atom['line'][:30]+''.join(f'{value:8.3f}' for value in point)+atom['line'][54:])
        final = directory/'1M17-prepared-model-v3.pdb'
        final.write_text('\n'.join(lines+['TER','END'])+'\n')
        output = atoms(final)
        changes = []
        for index, atom in enumerate(output):
            previous = mapped[atom['identity']]
            changes.append({**previous, 'output_xyz':atom['xyz'],
                'displacement_angstrom': float(np.linalg.norm(np.array(atom['xyz'])-previous['original_xyz'])) if previous['original_xyz'] else None,
                'v2_xyz':structure[index]['xyz'], 'displacement_from_v2_angstrom':float(np.linalg.norm(np.array(atom['xyz'])-structure[index]['xyz']))})
        write(directory/'atom-mapping.json', {**oldmap, 'schema':'drug-design-prepared-receptor-atom-map-v3', 'policy_sha256':sha(policy_path), 'prepared_sha256':sha(final), 'atoms':changes})
        write(directory/'topology.json', top)
        write(directory/'observed-atom-drift.json', {'maximum_angstrom':max(a['displacement_angstrom'] for a in changes if a['original_xyz']),
            'atoms':[{'identity':a['identity'],'displacement_angstrom':a['displacement_angstrom']} for a in changes if a['original_xyz']]})
        ledger['artifacts'] = {p.name:sha(p) for p in directory.iterdir() if p.is_file() and p.name!='progress.json'}
        ledger['status'] = 'prepared_pending_independent_audit'
        progress('prepared_proposal_written', prepared_sha256=sha(final))
    except BaseException as error:
        ledger['status']='preparation_failed'
        ledger['error']={'type':type(error).__name__,'message':str(error),'traceback':traceback.format_exc()}
        raise
    finally:
        signal.alarm(0)
        ledger['elapsed_wall_seconds']=time.monotonic()-start
        ledger['process_cpu_seconds']=time.process_time()
        ledger['constraint_cpu_seconds']=proof['process_cpu_seconds']
        ledger['cpu_core_hours']=(ledger['process_cpu_seconds']+proof['process_cpu_seconds'])/3600
        ledger['source_unchanged']=sha(source)==policy['source']['sha256']
        write(directory/'preparation-receipt.json',ledger)
        print(json.dumps({key:ledger[key] for key in ('status','process_cpu_seconds','cpu_core_hours','source_unchanged')}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy',type=Path,required=True)
    parser.add_argument('--constraints',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path)
    parser.add_argument('--build-constraints',action='store_true')
    args=parser.parse_args()
    constraints(args.policy,args.constraints) if args.build_constraints else prepare(args.policy,args.constraints,args.output_dir)
