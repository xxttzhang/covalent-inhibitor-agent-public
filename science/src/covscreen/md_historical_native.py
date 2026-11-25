"""Native structure checks for the explicit historical single-adduct profile.

The caller supplies the registered vendor reader. This module never launches
processes, grants approvals, repairs structures, or trusts a historical pass flag.
"""
import json
import math

from .md_historical_pose import sequence, GRAPH_POLICY

AMINO = dict(zip(
    'ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split(),
    'ARNDCQEGHILKMFPSTWYV'))
AMINO.update(CYX='C', CYM='C', HID='H', HIE='H', HIP='H')


def require(condition, message):
    if not condition:
        raise ValueError('Historical native graph: ' + message)


def atom_value(atom):
    return (atom.atomic_number, atom.formal_charge, atom.chain.strip(), int(atom.resnum),
            atom.inscode.strip(), atom.pdbres.strip(), atom.pdbname.strip(),
            tuple(round(float(v), 6) for v in atom.xyz))


def graph(structure):
    return ([atom_value(a) for a in structure.atom], sorted(
        (min(b.atom1.index, b.atom2.index), max(b.atom1.index, b.atom2.index), float(b.order))
        for b in structure.bond))


def single(reader, path):
    values = list(reader(str(path)))
    require(len(values) == 1, 'expected one structure in ' + path.name)
    return values[0]


def heavy(structure):
    result = {}
    for atom in structure.atom:
        if atom.atomic_number <= 1:
            continue
        key = (atom.chain.strip(), int(atom.resnum), atom.inscode.strip(), atom.pdbres.strip(), atom.pdbname.strip())
        require(key not in result, 'ambiguous heavy-atom identity')
        result[key] = atom
    return result


def original_atoms(before, after, chain, rebuilt, policy):
    """Keep legacy plans strict; v2 permits only gap-junction C/O and N movement.

    Movement is recorded, never treated as preserved coordinates. Chemistry and
    all other original coordinates remain exact; final geometry is checked below.
    """
    require(policy in (None, GRAPH_POLICY), 'unsupported historical graph policy')
    require(set(before).issubset(after), 'loop repair removed or renamed original heavy atoms')
    moved = []
    for key, atom in before.items():
        old, new = atom_value(atom), atom_value(after[key])
        require(all(math.isfinite(v) for v in (*atom.xyz, *after[key].xyz)), 'nonfinite atom coordinates')
        require(old[:-1] == new[:-1], 'loop repair changed original atom identity or charge')
        if old[-1] == new[-1]:
            continue
        junction = (policy == GRAPH_POLICY and key[0] == chain and key[2] == ''
                    and key[1] not in rebuilt and (
                        key[4] in {'C', 'O'} and key[1] + 1 in rebuilt
                        or key[4] == 'N' and key[1] - 1 in rebuilt))
        require(junction, 'loop repair changed an original heavy atom outside a permitted peptide junction')
        moved.append({'identity': list(key), 'displacement_A': math.dist(atom.xyz, after[key].xyz)})
    return moved


def inspect(loaded, reader):
    manifest = loaded['manifest']
    folder = loaded['directory'] / 'history'
    record = loaded['history']['manifest']['identity']
    poses = list(reader(str(folder / 'original-pose.maegz')))
    require(manifest['pose_index'] < len(poses), 'original pose index is absent')
    original = poses[manifest['pose_index']]
    extracted = single(reader, folder / 'extracted-pose.mae')
    require(graph(original) == graph(extracted), 'extracted pose differs from original CovDock structure')
    source = json.loads((folder / 'source-manifest.json').read_text())
    rows = [r for r in source['records'] if r['system_id'] == record['system_id']]
    require(len(rows) == 1 and str(original.title) == rows[0]['title'], 'original pose title identity differs')
    for key in ('s_sd_COMPOUND_ID', 's_sd_COMPOUND\\_ID'):
        if key in original.property:
            require(str(original.property[key]) == manifest['compound_id'], 'native compound identity differs')
    for key in ('i_sd_INPUT_SDF_INDEX', 'i_sd_INPUT\\_SDF\\_INDEX'):
        if key in original.property:
            require(int(original.property[key]) == manifest['input_sdf_index'], 'native candidate index differs')
    loop = single(reader, folder / 'original-loop-input.mae')
    final = single(reader, folder / 'adduct-input.mae')
    before, after = heavy(original), heavy(loop)
    target = manifest['target']
    rebuilt = set(record['recorded_construct']['missing_internal_residues'])
    moved = original_atoms(before, after, target['author_chain'], rebuilt,
                           manifest.get('historical_graph_policy'))
    old_keys = set(before)
    def bonds_in(structure, atoms):
        indices = {a.index: k for k, a in atoms.items() if k in old_keys}
        return sorted((min(indices[b.atom1.index], indices[b.atom2.index]),
                       max(indices[b.atom1.index], indices[b.atom2.index]), float(b.order))
            for b in structure.bond if b.atom1.index in indices and b.atom2.index in indices)
    require(bonds_in(original, before) == bonds_in(loop, after), 'loop repair changed original heavy-atom bonds')
    added = set(after) - set(before)
    require(all(k[0] == target['author_chain'] and k[1] in rebuilt for k in added),
            'new heavy atoms occur outside declared missing protein residues')
    cleanup = json.loads((folder / 'cleanup.json').read_text())
    removed = cleanup['removed_atoms']
    require(isinstance(removed, list) and len(set(removed)) == len(removed)
            and all(type(i) is int and 1 <= i <= len(list(loop.atom)) for i in removed),
            'invalid cleanup atom list')
    removed = set(removed)
    require(not any((b.atom1.index in removed) != (b.atom2.index in removed) for b in loop.bond),
            'cleanup removed atoms bonded to retained structure')
    labels = {'%s:%s:%s' % (a.chain.strip(), a.resnum, a.pdbres.strip())
              for a in loop.atom if a.index in removed}
    require(labels == set(cleanup['removed_residues']), 'cleanup removed residue identity differs')
    keep = [a.index for a in loop.atom if a.index not in removed]
    require(graph(loop.extract(keep)) == graph(final), 'cleanup changed retained graph, charge or coordinates')
    require(not any(a.index in removed and a.chain.strip() == target['author_chain']
                    and a.resnum == target['author_residue'] for a in loop.atom), 'cleanup removed target residue')
    definition = record['recorded_construct']
    start, end = definition['construct_start'], definition['construct_end']
    construct = sequence(folder / 'construct.fasta')
    cas = [a for a in final.atom if a.chain.strip() == target['author_chain'] and a.pdbname.strip() == 'CA']
    require(len(cas) == len(construct) and {int(a.resnum) for a in cas} == set(range(start, end + 1)),
            'native protein construct is incomplete or contains duplicate residue numbers')
    for atom in cas:
        require(not atom.inscode.strip() and AMINO.get(atom.pdbres.strip()) == construct[int(atom.resnum) - start],
                'native residue identity differs from canonical construct')
    # Verify peptide connectivity, not just matching C-alpha labels.
    heavy_final = heavy(final)
    backbone = {(a.resnum, a.pdbname.strip()): a for a in final.atom
                if a.chain.strip() == target['author_chain'] and start <= a.resnum <= end
                and a.pdbname.strip() in {'N', 'CA', 'C', 'O'}}
    require(len(backbone) == 4 * len(construct), 'native peptide backbone is incomplete or ambiguous')
    bond_pairs = {frozenset((b.atom1.index, b.atom2.index)) for b in final.bond}
    for residue in range(start, end):
        c, n = backbone[(residue, 'C')], backbone[(residue + 1, 'N')]
        require(frozenset((c.index, n.index)) in bond_pairs and 1.1 <= math.dist(c.xyz, n.xyz) <= 1.6,
                'protein chain has a broken or distorted peptide bond')
    moved_keys = {tuple(item['identity']) for item in moved}
    for key in moved_keys:
        atom = heavy_final.get(key)
        require(atom is not None, 'cleanup removed a moved peptide junction')
        # Coordinate exceptions do not excuse distorted covalent geometry.
        neighbors = [b.atom2 if b.atom1.index == atom.index else b.atom1 for b in final.bond
                     if atom.index in (b.atom1.index, b.atom2.index)]
        require(neighbors and all(1.0 <= math.dist(atom.xyz, other.xyz) <= 1.7
                                 for other in neighbors if other.atomic_number > 1),
                'moved peptide junction has distorted heavy-atom bond geometry')
    changed_atoms = [a for k, a in heavy_final.items() if k in added or k in moved_keys]
    for atom in changed_atoms:
        for other in heavy_final.values():
            if atom.index == other.index or frozenset((atom.index, other.index)) in bond_pairs:
                continue
            require(math.dist(atom.xyz, other.xyz) >= 1.2, 'new loop or moved junction contains a severe heavy-atom clash')
    audit = {'schema': 'historical-adduct-native-audit-v1', 'compound_id': manifest['compound_id'],
        'candidate_id': manifest['candidate_id'], 'source_manifest_sha256': manifest['parents']['historical_source_sha256'],
        'target_manifest_sha256': manifest['parents']['target_identity_sha256'],
        'canonical_site': target['canonical_cys'], 'author_chain': target['author_chain'],
        'author_residue': target['author_residue'], 'protein_residue_count': len(cas),
        'original_pose_identity_verified': True, 'original_heavy_graph_preserved': True,
        'cleanup_retained_graph_verified': True, 'native_coordinate_mapping_verified': True,
        'peptide_connectivity_verified': True, 'new_loop_clash_check_passed': True,
        'removed_residues': sorted(labels), 'new_heavy_atom_count': len(added),
        'historical_graph_policy': manifest.get('historical_graph_policy', 'strict-original-coordinates-v1'),
        'moved_junction_atoms': moved, 'unchanged_original_heavy_atom_count': len(before) - len(moved),
        'junction_geometry_verified': True,
        'parameterization_completed': False, 'md_system_ready': False}
    return final, len(poses), audit
