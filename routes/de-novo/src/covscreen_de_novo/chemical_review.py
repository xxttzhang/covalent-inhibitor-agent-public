"""Full ligand reaction-graph and specified-stereo audit for the fixed N-growth recipe.

This verifies identity of a modeled terminal-acrylamide adduct. It does not prove
reactivity, potency, selectivity, receptor-wide chemistry, or MD readiness.
"""
import json
from pathlib import Path

from covscreen.jobs import sha256_file
from covscreen.pose_consistency import protein_residue_identity
from . import control_review

VERSION = 'denovo-terminal-acrylamide-chemical-review-v1'
MAX_MAPPINGS = 4096
POLICY = {'reaction': 'terminal_acrylamide_thiol_Michael_addition',
    'recipe': 'fixed_N_only_growth', 'receptor_attachment': 'A:C797:SG',
    'ligand_changes': 'alpha_beta_double_to_single_and_one_beta_SG_single_bond',
    'microstate': 'preserve_formal_charges_isotopes_and_all_nonreaction_bonds',
    'stereo': 'all_source_potential_tetrahedral_and_double_bond_stereo_must_be_specified_and_preserved',
    'hydrogens': 'explicit_vendor_hydrogens_match_product_valence',
    'max_mappings': MAX_MAPPINGS, 'min_normalized_stereo_volume': 1e-5}


def freeze():
    from rdkit import rdBase
    return {'version': VERSION, 'policy': POLICY.copy(), 'rdkit': rdBase.rdkitVersion,
        'implementation_sha256': sha256_file(Path(__file__)),
        'coordinate_reader': control_review.freeze()}


def validate(binding):
    if binding != freeze():
        raise ValueError('Candidate chemical evaluator changed after plan freeze')


def stereo_inventory(mol):
    from rdkit import Chem
    return [{'type':str(s.type), 'center':int(s.centeredOn), 'specified':str(s.specified),
             'descriptor':str(s.descriptor)} for s in Chem.FindPotentialStereo(mol)]


def reaction_expectation(precursor, core):
    from rdkit import Chem
    if (precursor is None or precursor.GetNumAtoms() != precursor.GetNumHeavyAtoms()
            or not 6 <= precursor.GetNumAtoms() <= 256 or len(Chem.GetMolFrags(precursor)) != 1
            or any(a.GetNumRadicalElectrons() for a in precursor.GetAtoms())
            or not isinstance(core, list) or len(core) != 5 or len(set(core)) != 5
            or any(type(i) is not int or not 0 <= i < precursor.GetNumAtoms() for i in core)):
        raise ValueError('Invalid frozen precursor or reaction-atom mapping')
    mol = Chem.Mol(precursor)
    for atom in mol.GetAtoms(): atom.SetAtomMapNum(0)
    n, carbonyl, oxygen, alpha, beta = core
    expected = {(0,1):1., (1,2):2., (1,3):1., (3,4):2.}
    actual = {(i,j):mol.GetBondBetweenAtoms(core[i],core[j]).GetBondTypeAsDouble()
        for i in range(5) for j in range(i+1,5) if mol.GetBondBetweenAtoms(core[i],core[j])}
    outside = [(i,other.GetIdx()) for i in core for other in mol.GetAtomWithIdx(i).GetNeighbors()
               if other.GetIdx() not in core]
    if ([mol.GetAtomWithIdx(i).GetAtomicNum() for i in core] != [7,6,8,6,6]
            or actual != expected or len(outside) != 1 or outside[0][0] != n
            or any(mol.GetAtomWithIdx(i).GetFormalCharge() != 0 for i in core)
            or mol.GetAtomWithIdx(alpha).GetTotalNumHs() != 1
            or mol.GetAtomWithIdx(beta).GetTotalNumHs() != 2):
        raise ValueError('Precursor is outside the frozen terminal-acrylamide N-growth recipe')
    stereo = stereo_inventory(mol)
    rw = Chem.RWMol(mol)
    bond = rw.GetBondBetweenAtoms(alpha, beta)
    bond.SetBondType(Chem.BondType.SINGLE); bond.SetStereo(Chem.BondStereo.STEREONONE)
    # The comparison molecule is H-capped after removing the actual S bond.
    # Do not mutate source bytes or drop unspecified remote stereochemistry.
    for index in (alpha,beta):
        atom = rw.GetAtomWithIdx(index)
        atom.SetNumExplicitHs(0); atom.SetNoImplicit(False)
    capped = rw.GetMol(); Chem.SanitizeMol(capped)
    Chem.AssignStereochemistry(capped, cleanIt=True, force=True)
    if any(a.GetNumRadicalElectrons() for a in capped.GetAtoms()):
        raise ValueError('Expected reaction product contains radicals')
    return capped, stereo


def linkage(pose, beta_atom_index):
    def is_target(atom, name, atomic_number):
        return (atom['chain'] == 'A' and atom['resnum'] == 797 and atom['icode'] == ''
            and protein_residue_identity(atom['resname']) == 'CYS' and atom['atom_name'] == name
            and atom['atomic_number'] == atomic_number)
    edges = pose['external_heavy_bonds']
    if len(edges) != 1:
        return 'Expected exactly one ligand-to-receptor heavy-atom bond'
    edge = edges[0]
    sulfur = edge['partner']
    if (edge['ligand_atom_index'] != beta_atom_index or edge['order'] != 1.
            or not is_target(sulfur, 'SG', 16) or sulfur['formal_charge'] != 0
            or sulfur['isotope'] != 0 or sulfur['hydrogen_count'] != 0):
        return 'Covalent edge differs from neutral A:C797:SG to terminal beta carbon'
    target = pose['nucleophile']
    if target['atom'] != sulfur or len(target['bonds']) != 2:
        return 'C797 sulfur must have exactly the ligand beta and C797 CB bonds'
    neighbors = target['bonds']
    if (len({b['atom']['atom_index'] for b in neighbors}) != 2
            or sulfur['atom_index'] in {b['atom']['atom_index'] for b in neighbors}
            or any(b['order'] != 1. for b in neighbors)
            or sum(b['atom']['atom_index'] == beta_atom_index for b in neighbors) != 1
            or sum(is_target(b['atom'], 'CB', 6) for b in neighbors) != 1):
        return 'C797 sulfur connectivity or bond orders changed'
    return None


def measure(precursor, core, pose):
    import numpy as np
    from rdkit import Chem
    expected, source_stereo = reaction_expectation(precursor, core)
    base = {'version': VERSION, 'policy': POLICY.copy(), 'source_stereochemistry':source_stereo,
            'md_ready': False, 'scientific_result_verified': False,
            'scope': 'full_ligand_reaction_graph_and_source_stereochemistry_with_C797_link'}
    def result(state, reason, **details): return {**base, 'state':state, 'reason':reason, **details}
    if any(s['type'] not in {'Atom_Tetrahedral','Bond_Double'} or s['specified'] != 'Specified'
           for s in source_stereo):
        return result('EVIDENCE_INSUFFICIENT', 'Source has unspecified or unsupported potential stereochemistry')
    observed, names = control_review.ligand_graph(pose)
    beta_position = names[pose['covalent_partner_atom_name']]
    beta_atom_index = pose['ligand_heavy_atoms'][beta_position]['atom_index']
    problem = linkage(pose, beta_atom_index)
    if problem: return result('FAIL', problem)
    if (Chem.MolToSmiles(expected,isomericSmiles=False) != Chem.MolToSmiles(observed,isomericSmiles=False)
            or any(a.GetNumRadicalElectrons() for a in observed.GetAtoms())):
        return result('FAIL', 'Full ligand atom/bond graph or formal-charge state differs from the expected reaction')
    if Chem.MolToSmiles(expected,isomericSmiles=True) != Chem.MolToSmiles(observed,isomericSmiles=True):
        return result('FAIL', 'Ligand isotope or specified stereochemistry differs from the expected reaction')
    matches = observed.GetSubstructMatches(expected, uniquify=False, useChirality=True, maxMatches=MAX_MAPPINGS+1)
    if len(matches) > MAX_MAPPINGS:
        return result('EVIDENCE_INSUFFICIENT', 'Complete chemical mapping enumeration exceeds frozen bound')
    matches = [m for m in matches if m[core[4]] == beta_position]
    if not matches: return result('FAIL', 'C797 sulfur attaches to the wrong ligand atom')
    hydrogen_counts = [row['hydrogen_count'] for row in pose['ligand_heavy_atoms']]
    if any(type(n) is not int or not 0 <= n <= 4 for n in hydrogen_counts):
        raise ValueError('Missing or invalid explicit vendor hydrogen counts')
    for row in pose['ligand_heavy_atoms']:
        isotopes = row['hydrogen_isotopes']
        if (not isinstance(isotopes,list) or len(isotopes) != row['hydrogen_count']
                or any(type(i) is not int or i < 0 for i in isotopes)):
            raise ValueError('Missing or inconsistent vendor hydrogen isotope census')
        if any(i not in (0,1) for i in isotopes):
            return result('FAIL', 'Unexpected isotope-labeled ligand hydrogen')
    expected_h = [a.GetTotalNumHs() - int(a.GetIdx() == core[4]) for a in expected.GetAtoms()]
    matches = [m for m in matches if all(hydrogen_counts[m[i]] == n for i,n in enumerate(expected_h))]
    if not matches:
        return result('FAIL', 'Explicit ligand hydrogens do not match the addition product')
    match = min(matches)
    xyz = observed.GetConformer().GetPositions()
    volumes = []
    for stereo in source_stereo:
        if stereo['type'] != 'Atom_Tetrahedral': continue
        center = stereo['center']
        neighbors = [a.GetIdx() for a in expected.GetAtomWithIdx(center).GetNeighbors()]
        if len(neighbors) not in (3,4):
            return result('EVIDENCE_INSUFFICIENT', 'Unsupported tetrahedral neighborhood')
        # Three heavy neighbors plus implicit H: use the center as origin.
        # Four heavy neighbors: use the fourth neighbor as tetrahedron origin.
        origin = xyz[match[neighbors[3]]] if len(neighbors) == 4 else xyz[match[center]]
        vectors = np.asarray([xyz[match[n]]-origin for n in neighbors[:3]])
        denominator = float(np.prod(np.linalg.norm(vectors,axis=1)))
        volume = abs(float(np.linalg.det(vectors)))/denominator if denominator > 0 else 0.
        volumes.append({'precursor_atom_index_0based':center, 'normalized_volume':volume})
        if volume <= POLICY['min_normalized_stereo_volume']:
            return result('EVIDENCE_INSUFFICIENT', 'A specified stereocenter has degenerate coordinate evidence')
    return result('PASS', 'Expected terminal-acrylamide reaction and complete ligand identity verified',
        ligand_heavy_atom_count=expected.GetNumAtoms(), ligand_heavy_bond_count=expected.GetNumBonds(),
        graph_mappings_verified=len(matches),
        precursor_to_vendor_atom_indices=[pose['ligand_heavy_atoms'][i]['atom_index'] for i in match],
        alpha_vendor_atom_index=pose['ligand_heavy_atoms'][match[core[3]]]['atom_index'],
        beta_vendor_atom_index=beta_atom_index, source_stereo_coordinate_checks=volumes,
        precursor_ligand_hydrogens=sum(a.GetTotalNumHs() for a in precursor.GetAtoms()),
        product_ligand_hydrogens=sum(hydrogen_counts), covalent_external_bond_count=1)


def evaluate(root, source_folder, manager, spec, job, review):
    from rdkit import Chem
    validate(spec.parameters.get('denovo_chemical_readback'))
    candidates = [row for row in review['candidates'] if row['input_sdf_index'] == 0]
    if len(candidates) != 1 or candidates[0]['compound_id'] != 'candidate_000':
        raise ValueError('Chemical review lost its frozen candidate identity')
    if candidates[0]['engine_status'] != 'SUCCESS':
        return {'version':VERSION, 'state':'ENGINE_FAILED', 'md_ready':False}
    pose, provenance = control_review.read_pose(root, manager, spec, job, review,
        input_index=0, compound_id='candidate_000', version='covdock-candidate-chemistry-v1')
    folder = source_folder/'independent'
    manifest = json.loads((folder/'input_manifest.json').read_text())
    records = manifest['records']
    if len(records) != 1 or records[0]['id'] != 'candidate_000':
        raise ValueError('Chemical precursor manifest lost its single selected candidate')
    precursor = Chem.MolFromMolFile(str(folder/'candidate_000.sdf'), removeHs=True)
    if precursor is None or Chem.MolToSmiles(precursor,isomericSmiles=True) != records[0]['smiles']:
        raise ValueError('Chemical precursor identity differs from frozen input')
    measured = measure(precursor, records[0]['core_indices_0based'], pose)
    control_review.revalidate_outputs(root, manager, spec, job, review)
    validate(spec.parameters['denovo_chemical_readback'])
    return {**measured, **provenance, 'precursor_sha256':sha256_file(folder/'candidate_000.sdf')}
