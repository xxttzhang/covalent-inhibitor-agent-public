"""Construct-indexed receptor preparation inputs and actual PDBFixer inventory.

The in-memory projection uses entity numbering solely for repair planning.
Original author numbering, coordinates and all exclusion decisions are retained.
No repaired structure or scientific acceptance is produced by inspection.
"""
import hashlib
import io
import json

from .construct_audit import audit_construct
from .pdb_coordinates import read_pdb_coordinates
from .rcsb_mapping import map_cysteine
from .target_site_audit import audit_target_site


AMINO_ACIDS = dict(zip('ACDEFGHIKLMNPQRSTVWY',
    'ALA CYS ASP GLU PHE GLY HIS ILE LYS LEU MET ASN PRO GLN ARG SER THR VAL TRP TYR'.split()))


def project_receptor(source, source_sha256, entity, instance, accession, cys,
                     canonical_sequence, taxon, *, altlocs=None, model=None):
    """Make an explicit single-chain analysis view, not a receptor-ready artifact."""
    mapping = map_cysteine(entity, instance, accession, cys)
    raw, lines, atoms, selected_model = read_pdb_coordinates(source, mapping['entry_id'], model)
    if hashlib.sha256(raw).hexdigest() != source_sha256:
        raise ValueError('Receptor source checksum mismatch')
    construct = audit_construct(entity, canonical_sequence, accession, taxon)
    sequence = ''.join(entity['entity_poly']['pdbx_seq_one_letter_code_can'].split())
    if any(residue not in AMINO_ACIDS for residue in sequence):
        raise ValueError('Nonstandard construct requires a separate preparation policy')
    authors = [str(value) for value in instance['rcsb_polymer_entity_instance_container_identifiers']
               ['auth_to_entity_poly_seq_mapping']]
    if len(authors) != len(sequence) or len(set(authors)) != len(authors):
        raise ValueError('Full construct author numbering must be unique and complete')
    # Check every identifier, not just the selected Cys validated by map_cysteine.
    import re
    if any(not re.fullmatch(r'-?\d+[A-Za-z]?', a) for a in authors):
        raise ValueError('Invalid construct author residue identifier')
    positions = {author: index + 1 for index, author in enumerate(authors)}
    chain = mapping['auth_asym_id']
    protein = [a for a in atoms if a['chain'] == chain and a['record'] == 'ATOM']
    alternatives = {}
    for atom in protein:
        if atom['altloc']:
            alternatives.setdefault(atom['residue'], set()).add(atom['altloc'])
    choices = dict(altlocs or {})
    if set(choices) != set(alternatives) or any(choices[k] not in v for k, v in alternatives.items()):
        raise ValueError('Select an existing alternate location for every ambiguous residue, and no others')
    site = audit_target_site(source, mapping, model=model, altloc=choices.get(mapping['author_residue_id']))
    retained, excluded, keys = [], [], set()
    for atom in atoms:
        reason = None
        if atom['chain'] != chain:
            reason = 'other_chain_requires_assembly_decision'
        elif atom['record'] != 'ATOM':
            reason = 'heterogen_requires_retention_decision'
        elif atom['altloc'] and atom['altloc'] != choices[atom['residue']]:
            reason = 'unselected_alternate_location'
        elif atom['occupancy'] == 0:
            reason = 'zero_occupancy_requires_rebuilding'
        elif atom['element'] in {'H', 'D'}:
            reason = 'hydrogen_requires_protonation_policy'
        if reason:
            excluded.append({'atom': atom, 'reason': reason})
            continue
        if atom['residue'] not in positions:
            raise ValueError('Observed residue is absent from the author numbering map')
        position = positions[atom['residue']]
        if atom['resname'] != AMINO_ACIDS[sequence[position - 1]]:
            raise ValueError('Observed residue differs from the deposited construct sequence')
        key = (position, atom['name'])
        if key in keys:
            raise ValueError('Duplicate atom identity in selected conformer')
        keys.add(key)
        retained.append({**atom, 'entity_position': position})
    if not retained or len(sequence) > 9999 or len(retained) > 99999:
        raise ValueError('Empty or PDB-incompatible receptor projection')
    # Generate SEQRES from the deposited construct, never from the full UniProt
    # sequence. Entity indices avoid inferring offsets or dropping insertion codes.
    headers = [line for line in lines if line.startswith('HEADER')]
    for start in range(0, len(sequence), 13):
        names = ' '.join(AMINO_ACIDS[r] for r in sequence[start:start + 13])
        headers.append(f'SEQRES {start // 13 + 1:3d} A {len(sequence):4d}  {names}')
    records = []
    for serial, atom in enumerate(retained, 1):
        name = atom['name'] if len(atom['name']) == 4 else ' ' + atom['name']
        x, y, z = atom['xyz']
        records.append(f'ATOM  {serial:5d} {name:<4s} {atom["resname"]:3s} A{atom["entity_position"]:4d}    '
                       f'{x:8.3f}{y:8.3f}{z:8.3f}{atom["occupancy"]:6.2f}{0:6.2f}          {atom["element"]:>2s}  ')
    projection = '\n'.join(headers + records + ['TER', 'END']) + '\n'
    present = {a['entity_position'] for a in retained}
    missing = [{'entity_position': p, 'author_residue': authors[p - 1],
                'resname': AMINO_ACIDS[sequence[p - 1]]}
               for p in range(1, len(sequence) + 1) if p not in present]
    report = {'schema': 'receptor-preparation-inventory-v1', 'status': 'projection_pending_engine_inventory',
              'source_sha256': source_sha256, 'entry_id': mapping['entry_id'],
              'input_object_sha256': {name: hashlib.sha256(json.dumps(value, sort_keys=True,
                  separators=(',', ':'), allow_nan=False).encode()).hexdigest()
                  for name, value in [('entity', entity), ('instance', instance)]},
              'model': selected_model, 'author_chain': chain, 'altlocs': choices,
              'mapping': mapping, 'construct_audit': construct, 'site_audit': site,
              'projection_sha256': hashlib.sha256(projection.encode()).hexdigest(),
              'retained_atoms': retained, 'excluded_atoms': excluded,
              'missing_construct_residues': missing,
              'author_residue_by_entity_position': authors,
              'receptor_prepared': False, 'campaign_ready': False}
    return projection, report


def inspect_receptor(*args, **kwargs):
    """Run installed PDBFixer against the validated projection without repair."""
    _, report = _inspect_receptor(*args, **kwargs)
    return report


def _inspect_receptor(*args, **kwargs):
    projection, report = project_receptor(*args, **kwargs)
    import openmm
    import pdbfixer
    from pathlib import Path
    fixer = pdbfixer.PDBFixer(pdbfile=io.StringIO(projection),
                            platform=openmm.Platform.getPlatformByName('CPU'))
    fixer.findMissingResidues()
    fixer.findMissingAtoms()
    residues = list(fixer.topology.residues())
    missing = []
    for (chain_index, index), names in sorted(fixer.missingResidues.items()):
        if chain_index != 0:
            raise ValueError('Engine introduced an unexpected chain')
        start = int(residues[index - 1].id) + 1 if index else 1
        missing.extend({'entity_position': start + n, 'resname': name} for n, name in enumerate(names))
    expected = [{k: row[k] for k in ('entity_position', 'resname')}
                for row in report['missing_construct_residues']]
    if missing != expected:
        raise ValueError('Engine missing-residue inventory differs from the deposited construct map')
    engine_ids = [(int(a.residue.id), a.name) for a in fixer.topology.atoms()]
    if engine_ids != [(a['entity_position'], a['name']) for a in report['retained_atoms']]:
        raise ValueError('Engine changed retained atom identities or order')
    xyz = fixer.positions.value_in_unit(openmm.unit.angstrom)
    if any(abs(xyz[i][j] - atom['xyz'][j]) > 1e-8
           for i, atom in enumerate(report['retained_atoms']) for j in range(3)):
        raise ValueError('Engine changed retained coordinates during inspection')
    def residue_row(residue, names):
        position = int(residue.id)
        return {'entity_position': position,
                'author_residue': report['author_residue_by_entity_position'][position - 1],
                'resname': residue.name, 'atoms': names}
    provenance = json.loads((Path(pdbfixer.__file__).resolve().parents[1]
                             / 'PROVENANCE.json').read_text())
    report.update(status='repair_plan_requires_policy',
        missing_heavy_atoms=[residue_row(r, [a.name for a in atoms]) for r, atoms in fixer.missingAtoms.items()],
        missing_terminal_atoms=[residue_row(r, names) for r, names in fixer.missingTerminals.items()],
        runtime={'openmm': openmm.version.full_version,
                 'pdbfixer': provenance['version'],
                 'pdbfixer_source': provenance['source'],
                 'preparation_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 'pdbfixer_source_sha256': hashlib.sha256(Path(pdbfixer.__file__).with_name('pdbfixer.py').read_bytes()).hexdigest()},
        coordinates_modified=False, simulation_submitted=False)
    return fixer, report


def build_receptor_heavy_atoms(source, *args, output, seed, max_missing_residues,
                               environment_policy, **kwargs):
    """Build a full deposited single-chain proposal in an isolated CPU worker.

    The caller must bound worker CPU/wall time. This is not a simulation-ready
    receptor: PDBFixer's soft potential does not establish stereochemical quality.
    """
    from pathlib import Path
    import math
    import random
    import re
    import time
    if type(seed) is not int or not 1 <= seed <= 2147483647:
        raise ValueError('Specify one positive 32-bit seed')
    if type(max_missing_residues) is not int or not 0 <= max_missing_residues <= 100:
        raise ValueError('Specify a missing-residue budget between zero and 100')
    if environment_policy != 'isolated_protein_proposal':
        raise ValueError('Explicit isolated-protein proposal policy is required')
    out = Path(output)
    if out.exists():
        raise ValueError('Preparation output must be a new directory')
    fixer, inventory = _inspect_receptor(source, *args, **kwargs)
    if len(inventory['missing_construct_residues']) > max_missing_residues:
        raise ValueError('Missing-residue count exceeds the preparation budget')
    construct = inventory['construct_audit']
    if not construct['source_species_verified'] or construct['uncertain_residues'] or construct['sequence_differences']:
        raise ValueError('Species or construct differences require a separate preparation policy')
    if inventory['site_audit']['explicit_site_connections']:
        raise ValueError('Linked target sulfur requires a separate chemical-state policy')
    authors = inventory['author_residue_by_entity_position']
    parsed = [re.fullmatch(r'(-?\d+)([A-Za-z]?)', name).groups() for name in authors]
    if any(not -999 <= int(number) <= 9999 for number, _ in parsed):
        raise ValueError('Author numbering cannot be written safely as PDB')
    out.mkdir(parents=True, exist_ok=False)
    def write(name, value):
        with (out / name).open('x') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.write('\n')
    start = time.monotonic()
    receipt = {'schema': 'receptor-heavy-atom-preparation-v1', 'status': 'running',
               'source_sha256': inventory['source_sha256'], 'runtime': inventory['runtime'],
               'seed': seed, 'seed_attempts': 1, 'environment_policy': environment_policy,
               'max_missing_residues': max_missing_residues, 'platform': 'CPU', 'threads': 1,
               'receptor_prepared': False, 'campaign_ready': False, 'artifacts': {}}
    write('inventory.json', inventory)
    try:
        from openmm import app, unit
        import numpy as np
        random.seed(seed)
        np.random.seed(seed)
        fixer.platform.setPropertyDefaultValue('Threads', '1')
        fixer.addMissingAtoms(seed=seed)
        residues = list(fixer.topology.residues())
        original = {(a['entity_position'], a['name']): a for a in inventory['retained_atoms']}
        expected_residues = {a['entity_position']: a['resname'] for a in inventory['retained_atoms']}
        expected_residues.update({a['entity_position']: a['resname'] for a in inventory['missing_construct_residues']})
        if [r.name for r in residues] != [expected_residues[i] for i in range(1, len(authors) + 1)]:
            raise ValueError('Repair changed deposited residue identities or order')
        for index, residue in enumerate(residues):
            residue.id, residue.insertionCode = parsed[index]
            residue.chain.id = inventory['author_chain']
        xyz = fixer.positions.value_in_unit(unit.angstrom)
        atoms, found, maximum_drift = [], set(), 0.0
        for atom in fixer.topology.atoms():
            position = atom.residue.index + 1
            key = (position, atom.name)
            if key in found or atom.element is None or atom.element.symbol in {'H', 'D'}:
                raise ValueError('Unexpected atom identity in heavy-atom repair')
            found.add(key)
            point = [float(v) for v in xyz[atom.index]]
            if not all(math.isfinite(v) for v in point):
                raise ValueError('Repair produced nonfinite coordinates')
            old = original.get(key)
            if old:
                if atom.element.symbol != old['element']:
                    raise ValueError('Repair changed observed atom element')
                maximum_drift = max(maximum_drift, math.dist(point, old['xyz']))
            atoms.append({'index': atom.index, 'entity_position': position,
                'chain': atom.residue.chain.id, 'author_residue': authors[position - 1],
                'resname': atom.residue.name, 'name': atom.name, 'element': atom.element.symbol,
                'xyz_angstrom': point, 'original_serial': old['serial'] if old else None,
                'coordinate_origin': 'retained_observed_coordinate' if old else 'modeled_heavy_atom',
                'occupancy': old['occupancy'] if old else 0.0})
        if not original.keys() <= found or maximum_drift > 1e-6:
            raise ValueError('Repair lost or moved observed heavy atoms')
        # The complete ordered sequence was checked above. Do not rematch the
        # internal SEQRES numbering after restoring original insertion codes.
        fixer.missingResidues = {}
        fixer.findMissingAtoms()
        if fixer.missingAtoms or fixer.missingTerminals:
            raise ValueError('Heavy-atom repair left missing atoms')
        stream = io.StringIO()
        app.PDBFile.writeFile(fixer.topology, fixer.positions, stream, keepIds=True)
        lines, index = [], 0
        for line in stream.getvalue().splitlines():
            if line.startswith(('ATOM  ', 'HETATM')):
                row = atoms[index]
                line = line[:54] + f'{row["occupancy"]:6.2f}{0:6.2f}' + line[66:]
                index += 1
            if not line.startswith(('REMARK', 'HEADER')):
                lines.append(line)
        if index != len(atoms):
            raise ValueError('PDB output atom count mismatch')
        header = next(line for line in Path(source).read_text().splitlines() if line.startswith('HEADER'))
        (out / 'heavy-atoms.pdb').write_text('\n'.join([header,
            'REMARK 900 COMPUTATIONAL HEAVY-ATOM PROPOSAL; NOT AN ACCEPTED RECEPTOR',
            'REMARK 900 MODELED ATOMS HAVE ZERO OCCUPANCY; ALL B-FACTORS ARE UNASSIGNED ZERO'] + lines) + '\n')
        write('atom-mapping.json', {'atoms': atoms, 'maximum_observed_drift_angstrom': maximum_drift})
        write('topology.json', {'atoms': [{k: a[k] for k in ('index', 'entity_position', 'chain',
            'author_residue', 'resname', 'name', 'element')} for a in atoms],
            'bonds': [[a.index, b.index] for a, b in fixer.topology.bonds()]})
        receipt.update(status='heavy_atoms_built_pending_independent_qc',
            atom_count=len(atoms), modeled_atoms=len(atoms) - len(original),
            maximum_observed_drift_angstrom=maximum_drift,
            remaining_checks=['stereochemistry', 'chemical graph and geometry', 'environment retention',
                              'protonation', 'physical relaxation', 'independent receptor admission'])
    except Exception as exc:
        receipt.update(status='heavy_atom_preparation_failed', error=str(exc))
        raise
    finally:
        receipt['elapsed_wall_seconds'] = time.monotonic() - start
        receipt['artifacts'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in out.iterdir() if p.is_file()}
        write('receipt.json', receipt)
    return receipt
