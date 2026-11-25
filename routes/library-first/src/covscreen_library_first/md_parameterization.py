"""Native OPLS_2005 assignment for a hash-bound, QC-reviewed Cys adduct.

This consumes a prepared intact adduct. It does not repair a receptor, invent
reaction chemistry, optimize protonation, or certify MD readiness. The native
vendor runtime is imported only after the input receipt has been checked.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import re
import time
import traceback


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    with path.open('x') as stream: json.dump(value, stream, indent=2, allow_nan=False)


def graph(st):
    return sorted((min(b.atom1.index, b.atom2.index), max(b.atom1.index, b.atom2.index), b.order) for b in st.bond)


def command(argv, folder, log, seconds):
    with (folder / log).open('x') as output:
        child = subprocess.Popen(argv, cwd=folder, stdout=output, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            if not (child.wait(timeout=seconds) == 0):
                raise ValueError('Native program failed: ' + log)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try: child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL); child.wait()


def read_input(source, qc_path, expected_qc_sha):
    """Bind QC to exact source bytes before loading native libraries or writing."""
    source, qc_path = Path(source), Path(qc_path)
    if not isinstance(expected_qc_sha, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_qc_sha):
        raise ValueError('Explicit QC SHA256 required')
    if any(p.is_symlink() or not p.is_file() for p in (source, qc_path)):
        raise ValueError('Input and QC must be regular files')
    if sha(qc_path) != expected_qc_sha:
        raise ValueError('QC checksum differs')
    qc = json.loads(qc_path.read_text())
    if not isinstance(qc, dict):
        raise ValueError('QC must be an object')
    sid = qc.get('system_id')
    if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', sid):
        raise ValueError('Invalid system identity')
    if (qc.get('status') != 'LOOP_GRAPH_QC_PASSED_PENDING_RELAXATION'
            or qc.get('simulation_state') != 'covalent_adduct'
            or qc.get('heavy_graph_preserved') is not True
            or qc.get('warnings') != []):
        raise ValueError('Prepared-adduct graph QC has not passed')
    if sha(source) != qc.get('loop_qc_input_sha256'):
        raise ValueError('Adduct checksum differs')
    pair = qc.get('covalent_atom_indices_1based')
    ligand = qc.get('ligand_heavy_atom_indices_1based')
    for name, indices in [('covalent pair', pair), ('ligand selection', ligand)]:
        if (not isinstance(indices, list) or not indices
                or any(type(i) is not int or i < 1 for i in indices)
                or len(set(indices)) != len(indices)):
            raise ValueError('Invalid ' + name)
    if len(pair) != 2 or len(ligand) < 5 or pair[0] not in ligand or pair[1] in ligand:
        raise ValueError('Expected ordered ligand-carbon / protein-sulfur pair')
    return qc


def validate_adduct(st, qc):
    """Audit the loaded native graph; no mutation, parameterization or MD."""
    pair = set(qc['covalent_atom_indices_1based'])
    if not (len(pair) == 2 and any(({a, b} == pair and o == 1 for (a, b, o) in graph(st)))):
        raise ValueError('Prepared structure lacks the declared single covalent bond')
    ligand_index, sulfur_index = qc['covalent_atom_indices_1based']
    if max([*pair, *qc['ligand_heavy_atom_indices_1based']]) > st.atom_total:
        raise ValueError('QC atom index exceeds native structure')
    sulfur, carbon = st.atom[sulfur_index], st.atom[ligand_index]
    if (sulfur.atomic_number != 16 or sulfur.pdbname.strip() != 'SG'
            or sulfur.pdbres.strip() not in {'CYS', 'CYX', 'CYM'}
            or carbon.atomic_number != 6):
        raise ValueError('Only prepared cysteine sulfur–carbon adducts are supported')
    if any(a.atomic_number == 1 for a in sulfur.bonded_atoms):
        raise ValueError('Covalent sulfur retains a thiol hydrogen')
    if not 1.5 < math.dist(sulfur.xyz, carbon.xyz) < 2.2:
        raise ValueError('Covalent sulfur–carbon geometry outside admission range')
    residue = (carbon.chain, carbon.resnum, carbon.inscode)
    actual_ligand = {a.index for a in st.atom if a.atomic_number > 1
                     and (a.chain, a.resnum, a.inscode) == residue}
    if set(qc['ligand_heavy_atom_indices_1based']) != actual_ligand:
        raise ValueError('Ligand heavy-atom selection differs from native residue')
    if (sulfur.chain, sulfur.resnum, sulfur.inscode) == residue:
        raise ValueError('Covalent pair must cross protein and ligand residues')
    boundary = {tuple(sorted((b.atom1.index, b.atom2.index))) for b in st.bond
                if (b.atom1.index in actual_ligand) != (b.atom2.index in actual_ligand)
                and b.atom1.atomic_number > 1 and b.atom2.atomic_number > 1}
    if boundary != {tuple(sorted(pair))}:
        raise ValueError('Expected one heavy-atom bond between candidate and environment')
    neighbors = list(sulfur.bonded_atoms)
    if len(neighbors) != 2 or any(a.atomic_number != 6 for a in neighbors):
        raise ValueError('Expected a two-coordinate cysteine thioether sulfur')
    if not all(math.isfinite(v) for a in st.atom for v in a.xyz):
        raise ValueError('Nonfinite input coordinates')
    return sulfur


def audit_covalent_terms(pair, edges, bonds, angles, dihedrals):
    """Check local OPLS coverage using native indices and normalized FFIO terms.

    A term has atoms, function and parameters. This checks parameter presence
    and integrity, not parameter accuracy, reactivity or binding affinity.
    Explicit zero torsion coefficients are valid; a missing term is not.
    """
    if len(pair) != 2 or len(set(pair)) != 2 or any(type(i) is not int or i < 1 for i in pair):
        raise ValueError('Invalid covalent pair')
    adjacency = {}
    for edge in edges:
        if len(edge) != 2 or len(set(edge)) != 2 or any(type(i) is not int or i < 1 for i in edge):
            raise ValueError('Invalid structure graph edge')
        a, b = edge
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)
    a, b = pair
    if b not in adjacency.get(a, set()):
        raise ValueError('Declared covalent pair is absent from structure graph')
    key = lambda atoms: min(tuple(atoms), tuple(reversed(atoms)))
    edge = set(pair)
    left, right = adjacency[a] - {b}, adjacency[b] - {a}
    required_angles = {key((n, a, b)) for n in left} | {key((a, b, n)) for n in right}
    required_central = {key((n, a, b, m)) for n in left for m in right if n != m}
    required_torsions = (required_central
        | {key((k, n, a, b)) for n in left for k in adjacency[n] - {a, b}}
        | {key((a, b, n, k)) for n in right for k in adjacency[n] - {a, b}})
    if not required_angles or not required_torsions:
        raise ValueError('Covalent link lacks neighbors required for the supported thioether model')
    found = {'bond': {}, 'angle': {}, 'dihedral': {}}
    central = set()
    proper = set()
    for kind, terms, size in [('bond', bonds, 2), ('angle', angles, 3), ('dihedral', dihedrals, 4)]:
        for term in terms:
            atoms = term['atoms']
            if not edge <= set(atoms):
                continue
            if (len(atoms) != size or len(set(atoms)) != size
                    or any(type(i) is not int or i < 1 for i in atoms)):
                raise ValueError('Invalid covalent ' + kind + ' atom indices')
            function = term['function'].upper()
            parameters = term['parameters']
            count = 8 if kind == 'dihedral' else 2
            if (len(parameters) != count or any(type(v) not in (int, float)
                    or not math.isfinite(v) for v in parameters)):
                raise ValueError('Invalid or nonfinite covalent ' + kind + ' parameters')
            if kind == 'dihedral':
                if function not in {'OPLS_PROPER', 'OPLS_IMPROPER'}:
                    raise ValueError('Unsupported covalent dihedral function')
            elif function not in ({'HARM'} if kind == 'bond' else {'HARM', 'HARM_CONSTRAINED'}):
                raise ValueError('Unsupported covalent ' + kind + ' function')
            if kind == 'bond' and not (1.5 < parameters[0] < 2.2 and parameters[1] > 0):
                raise ValueError('Invalid covalent bond equilibrium length or force constant')
            if kind == 'angle' and not (0 < parameters[0] < 180 and parameters[1] > 0):
                raise ValueError('Invalid covalent angle equilibrium or force constant')
            if function != 'OPLS_IMPROPER':
                if any(y not in adjacency.get(x, set()) for x, y in zip(atoms, atoms[1:])):
                    raise ValueError('Covalent ' + kind + ' does not follow structure graph')
            identity = (function if kind == 'dihedral' else kind, key(atoms))
            if identity in found[kind]:
                raise ValueError('Duplicate covalent ' + kind + ' term')
            found[kind][identity] = term
            if kind == 'dihedral' and function == 'OPLS_PROPER':
                proper.add(key(atoms))
                if set(atoms[1:3]) == edge:
                    central.add(key(atoms))
    if len(found['bond']) != 1:
        raise ValueError('Expected exactly one covalent bond parameter term')
    actual_angles = {identity[1] for identity in found['angle']}
    if actual_angles != required_angles:
        raise ValueError('Covalent angle coverage differs from structure graph')
    if proper != required_torsions:
        raise ValueError('Covalent proper-torsion coverage differs from structure graph')
    bond = next(iter(found['bond'].values()))
    return {'atom_indices_1based': list(pair), 'equilibrium_length_A': bond['parameters'][0],
            'cross_link_angle_terms': len(found['angle']), 'cross_link_dihedral_terms': len(found['dihedral']),
            'central_proper_torsions': len(central), 'local_graph_coverage_complete': True}


def parameterize(source, qc_path, expected_qc_sha, folder, vendor, expected_system_id=None):
    source, qc_path, folder, vendor = map(Path, (source, qc_path, folder, vendor))
    qc = read_input(source, qc_path, expected_qc_sha)
    if folder.exists() or folder.is_symlink():
        raise ValueError('Output must be a new directory')
    source, qc_path, folder, vendor = (p.resolve() for p in (source, qc_path, folder, vendor))
    sid = qc['system_id']
    if expected_system_id is not None and sid != expected_system_id:
        raise ValueError('Requested system identity differs from QC')
    from schrodinger.structure import StructureReader
    from schrodinger.application.desmond.system_builder_inp import SystemBuilderInput
    from schrodinger.application.desmond import cms
    from schrodinger.infra import mm
    st, = list(StructureReader(str(source)))
    sulfur = validate_adduct(st, qc)
    pair = set(qc['covalent_atom_indices_1based'])
    if sha(source) != qc['loop_qc_input_sha256'] or sha(qc_path) != expected_qc_sha:
        raise ValueError('Inputs changed during native structure loading')
    folder.mkdir(parents=True, exist_ok=False)
    st.write(str(folder / 'input.mae'))
    record = {'system_id': sid, 'started_epoch': time.time(), 'source_qc_sha256': expected_qc_sha,
              'source_input_sha256': sha(source), 'native_input_sha256': sha(folder / 'input.mae'),
              'forcefield': 'OPLS_2005', 'water': 'TIP3P', 'salt_M': .15, 'buffer_A': 12,
              'simulation_state': 'covalent_adduct', 'parameters_audited': False, 'simulation_started': False,
              'script_sha256': sha(__file__), 'cpu_threads': 2,
              'native_vendor_root': str(vendor.resolve()),
              'covalent_site': {'chain': sulfur.chain.strip(), 'residue_number': sulfur.resnum,
                                'insertion_code': sulfur.inscode.strip(), 'atom_name': sulfur.pdbname.strip()}}
    save(folder / 'started.json', record)
    try:
        builder = SystemBuilderInput()
        builder.setSolute(str(folder / 'input.mae'))
        builder.setOplsaaVersion('OPLS_2005')
        builder.setBoundaryCondition(use_buffer=1, boundary_condition='orthorhombic', a=12., b=12., c=12.)
        builder.setSolvent(str(vendor / 'mmshare-v5.4/data/system_builder/tip3p.box.mae'))
        for setter in [builder.setPositiveIon, builder.setSaltPositiveIon]:
            setter(str(vendor / 'mmshare-v5.4/data/system_builder/Na.mae'))
        for setter in [builder.setNegativeIon, builder.setSaltNegativeIon]:
            setter(str(vendor / 'mmshare-v5.4/data/system_builder/Cl.mae'))
        builder.salt_concentration = .15
        builder.setNeutralize(1)
        builder.setWriteMaeFile(str(folder / 'geometry.mae'))
        builder.write(str(folder / 'system.csb'))
        command([str(vendor / 'utilities/system_builder'), '-NOJOBID', 'system.csb'], folder, 'builder.log', 300)
        if not ('error:' not in (folder / 'builder.log').read_text().lower()):
            raise ValueError('Native system builder reported an error')
        workflow = '''task { task = "desmond:auto" }
assign_forcefield {
  forcefield = OPLS_2005
  water = TIP3P
  hydrogen_mass_repartition = off
  make_alchemical_water = off
  fail_on_lewis_failure = on
}
'''
        (folder / 'forcefield.msj').write_text(workflow)
        job = sid + '_forcefield'
        command([str(vendor / 'utilities/multisim'), '-m', 'forcefield.msj', 'geometry.mae',
                 '-o', 'parameterized.cms', '-JOBNAME', job, '-HOST', 'localhost', '-maxjob', '1',
                 '-mode', 'umbrella', '-LOCAL', '-WAIT'], folder, 'forcefield-launcher.log', 600)
        log = (folder / (job + '_multisim.log')).read_text()
        if not ('Stage 2 completed successfully.' in log and 'Multisim completed.' in log):
            raise ValueError('Native force-field assignment did not complete')
        output = folder / 'parameterized.cms'
        model = cms.Cms(file=str(output))
        matches = [c for c in model.comp_ct if c.atom_total == st.atom_total and graph(c) == graph(st)]
        solute, = matches
        if not (solute.atom_total == st.atom_total and graph(solute) == graph(st)):
            raise ValueError('Solute graph changed during parameter assignment')
        for a, b in zip(st.atom, solute.atom):
            if not ((a.index, a.atomic_number, a.chain, a.resnum, a.pdbres, a.pdbname, a.inscode, a.formal_charge) == (b.index, b.atomic_number, b.chain, b.resnum, b.pdbres, b.pdbname, b.inscode, b.formal_charge)):
                raise ValueError('Solute atom identity or formal charge changed')
            if not (math.dist(a.xyz, b.xyz) < 0.001):
                raise ValueError('Solute coordinates changed during parameter assignment')
        # Require solute-first CMS indexing; downstream selection must never guess.
        for a, b in zip(solute.atom, model.fsys_ct.atom):
            if not (a.atomic_number == b.atomic_number and math.dist(a.xyz, b.xyz) < 0.001):
                raise ValueError('CMS full-system atom ordering differs from solute-first mapping')
        def native_terms(rows, names, coefficients):
            for row in rows:
                atoms = [getattr(row, name) for name in names]
                if pair <= set(atoms):
                    yield {'atoms': atoms, 'function': row.funct,
                           'parameters': [getattr(row, 'c' + str(n)) for n in coefficients]}
        link = audit_covalent_terms(qc['covalent_atom_indices_1based'],
            [(a, b) for a, b, _ in graph(solute)],
            native_terms(solute.ffio.bond, ('ai', 'aj'), (1, 2)),
            native_terms(solute.ffio.angle, ('ai', 'aj', 'ak'), (1, 2)),
            native_terms(solute.ffio.dihedral, ('ai', 'aj', 'ak', 'al'), range(8)))
        if not ({(a, b) for (a, b, _) in graph(solute)} <= {tuple(sorted((b.ai, b.aj))) for b in solute.ffio.bond}):
            raise ValueError('Solute graph contains bonds without force-field terms')
        components = []
        for component in model.comp_ct:
            sites = list(component.ffio.site)
            if not (sites and component.atom_total % len(sites) == 0):
                raise ValueError('Component force-field site count does not match atom count')
            if not (all((math.isfinite(s.mass) and s.mass > 0 and math.isfinite(s.charge) and s.vdwtype for s in sites))):
                raise ValueError('Force-field site lacks finite positive mass, charge or van der Waals type')
            components.append({'atoms': component.atom_total, 'forcefield_sites': len(sites),
                               'charge_e': sum(s.charge for s in sites) * (component.atom_total // len(sites))})
        charge = sum(c['charge_e'] for c in components)
        if not (abs(charge) < 0.001 and sum((c['atoms'] for c in components)) == model.fsys_ct.atom_total):
            raise ValueError('System is not neutral or component atom counts are inconsistent')
        mapping = [{'index_0based': a.index-1, 'chain': a.chain.strip(), 'residue_number': a.resnum,
                    'insertion_code': a.inscode.strip(), 'residue_name': a.pdbres.strip(), 'atom_name': a.pdbname.strip(),
                    'atomic_number': a.atomic_number, 'formal_charge': a.formal_charge,
                    'bonded_indices_0based': [b.index-1 for b in a.bonded_atoms],
                    'hbond_donor': bool(mm.mmct_hbond_is_donor(solute, a)),
                    'hbond_acceptor': bool(mm.mmct_hbond_is_acceptor(solute, a))} for a in solute.atom]
        save(folder / 'atom-mapping.json', mapping)
        record.update(status='COVALENT_PARAMETERIZATION_AUDITED', parameters_audited=True,
                      total_atoms=model.fsys_ct.atom_total, solute_atoms=solute.atom_total,
                      parameterized_cms_sha256=sha(output), solute_graph_preserved=True,
                      covalent_bond=link,
                      ligand_heavy_atom_indices_1based=qc['ligand_heavy_atom_indices_1based'],
                      total_charge_e=charge, component_forcefield_coverage=components,
                      atom_mapping_sha256=sha(folder / 'atom-mapping.json'),
                      protonation_scope='Source microstate retained; Lewis/valence checked, pKa optimality not established',
                      minimization_validated=False, equilibration_validated=False)
        save(folder / 'parameterization-audit.json', record)
    except Exception:
        record.update(status='PARAMETERIZATION_BLOCKED', parameters_audited=False,
                      error=traceback.format_exc())
    record['finished_epoch'] = time.time()
    save(folder / 'finished.json', record)
    print(json.dumps(record), flush=True)
    return 0 if record['parameters_audited'] else 1
