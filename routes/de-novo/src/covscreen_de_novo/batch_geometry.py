"""Benchmark De novo VS geometry, including intraligand checks, before LO."""
import math
from rdkit import Chem

POLICY = {'version': 'denovo-pose-v1-provisional', 'bond_window_A': [1.65, 2.15],
    'product_angle_window_degrees': [80, 140], 'nonbonded_vdw_scale': 0.75,
    'max_clashing_ligand_heavy_atom_fraction': 0.2, 'severe_nonbonded_distance_A': 1.2,
    'max_severe_pairs': 0, 'bonded_exclusions': 'graph distance 1 and 2',
    'intraligand_max_clashing_fraction': 0.2}


def measure(row):
    molecule = row['complex']
    xyz = molecule.GetConformer().GetPositions()
    if not all(math.isfinite(float(v)) for point in xyz for v in point):
        raise ValueError('Nonfinite VS coordinates')
    ligand = set(row['raw_ligand_indices'])
    protein = [a.GetIdx() for a in molecule.GetAtoms() if a.GetAtomicNum() > 1 and a.GetIdx() not in ligand]
    sg, beta = row['target_index'], row['partner_index']
    alpha = [a.GetIdx() for a in molecule.GetAtomWithIdx(beta).GetNeighbors() if a.GetIdx() in ligand and a.GetAtomicNum() > 1]
    cb = [a.GetIdx() for a in molecule.GetAtomWithIdx(sg).GetNeighbors() if a.GetAtomicNum() > 1 and a.GetIdx() != beta]
    if not ligand or len(alpha) != 1 or len(cb) != 1:
        raise ValueError('Ambiguous VS reaction geometry')
    def angle(i, j, k):
        a, b = xyz[i] - xyz[j], xyz[k] - xyz[j]
        denominator = math.sqrt(float(a @ a) * float(b @ b))
        if denominator == 0:
            raise ValueError('Degenerate VS reaction angle')
        return math.degrees(math.acos(max(-1., min(1., float(a @ b) / denominator))))
    table = Chem.GetPeriodicTable()
    def clashes(internal):
        affected, severe = set(), []
        for i in sorted(ligand):
            neighbors = {a.GetIdx() for a in molecule.GetAtomWithIdx(i).GetNeighbors()}
            excluded = neighbors | {i}
            for j in neighbors:
                excluded.update(a.GetIdx() for a in molecule.GetAtomWithIdx(j).GetNeighbors())
            targets = sorted(j for j in ligand if j > i) if internal else protein
            for j in targets:
                if j in excluded:
                    continue
                distance = math.dist(xyz[i], xyz[j])
                if distance < POLICY['severe_nonbonded_distance_A']:
                    severe.append([i, j, distance])
                limit = POLICY['nonbonded_vdw_scale'] * sum(table.GetRvdw(molecule.GetAtomWithIdx(k).GetAtomicNum()) for k in (i, j))
                if distance < limit:
                    affected.add(i)
                    if internal:
                        affected.add(j)
        return {'clashing_fraction': len(affected) / len(ligand), 'severe_pairs': severe}
    bond = math.dist(xyz[sg], xyz[beta])
    angles = [angle(sg, beta, alpha[0]), angle(cb[0], sg, beta)]
    external, internal = clashes(False), clashes(True)
    flags = {'covalent_bond': POLICY['bond_window_A'][0] <= bond <= POLICY['bond_window_A'][1],
        'product_angles': all(POLICY['product_angle_window_degrees'][0] <= a <= POLICY['product_angle_window_degrees'][1] for a in angles),
        'protein_clashes': external['clashing_fraction'] <= POLICY['max_clashing_ligand_heavy_atom_fraction'] and not external['severe_pairs'],
        'intraligand_clashes': internal['clashing_fraction'] <= POLICY['intraligand_max_clashing_fraction'] and not internal['severe_pairs']}
    return {'bond_A': bond, 'angles_degrees': angles, 'protein': external, 'intraligand': internal,
        'gate_flags': flags, 'eligible_for_lo': all(flags.values()),
        'failure_reasons': [name for name, passed in flags.items() if not passed]}
