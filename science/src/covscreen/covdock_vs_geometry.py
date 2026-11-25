"""Engine-neutral measurements for independently verified VS product poses.

Thresholds retain the existing provisional Ligand QC bounds. A passed pose is
eligible for further review, never proof of inhibition or native MD admission.
"""
import math
import re

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import Chem, rdBase

from .covdock_vs_output import residue
from .ligand_first_inputs import PdbAtom, read_pdb_atoms
from .ligand_first_geometry import (
    _atom_map_indices, _scaffold_rmsd, _interaction_measurements, _strain_energy,
    _clashes, _distance, _point, _angle,
)


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    version: str = 'covdock-vs-geometry-v1-provisional'
    max_scaffold_rmsd_angstrom: float = Field(default=2.0, gt=0)
    min_interaction_retention_fraction: float = Field(default=0.70, ge=0, le=1)
    covalent_cs_bond_window_angstrom: tuple[float,float] = (1.65,2.15)
    product_angle_window_degrees: tuple[float,float] = (80.0,140.0)
    max_strain_excess_over_parent_kcal_mol: float = Field(default=15.0, gt=0)
    clash_vdw_scale: float = Field(default=0.75, gt=0, lt=1)
    max_clash_fraction: float = Field(default=0.20, ge=0, le=1)
    max_severe_clash_pair_count: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def supported(self):
        if self.version != 'covdock-vs-geometry-v1-provisional':
            raise ValueError('Unsupported frozen VS geometry policy')
        for window in (self.covalent_cs_bond_window_angstrom, self.product_angle_window_degrees):
            if not 0 < window[0] < window[1]:
                raise ValueError('VS geometry windows must be positive and increasing')
        return self


def transformed(molecule, rotation, translation):
    points = molecule.GetConformer().GetPositions()
    if (np.shape(rotation) != (3,3) or np.shape(translation) != (3,)
            or not all(np.isfinite(value).all() for value in (points,rotation,translation))):
        raise ValueError('VS coordinate transform requires finite coordinates and a 3D frame')
    # Explicit three-coordinate contraction avoids platform BLAS matmul floating
    # status warnings observed for finite protein arrays. Do not suppress them:
    # verify both inputs and the resulting coordinates independently.
    with np.errstate(all='raise'):
        try:
            xyz = np.einsum('ij,jk->ik',points,rotation,optimize=False) + translation
        except FloatingPointError as error:
            raise ValueError('VS coordinate transform overflowed') from error
    if not np.isfinite(xyz).all():
        raise ValueError('VS coordinate transform produced non-finite coordinates')
    result = Chem.Mol(molecule)
    for index, point in enumerate(xyz):
        result.GetConformer().SetAtomPosition(index, point)
    return result


def reference_frame(receptor, projection):
    """Fit the exact deposited observed C-alpha set; never fit the ligand core."""
    actual = {residue(atom):atom.GetIdx() for atom in receptor.GetAtoms()}
    points, target = [], []
    for atom in read_pdb_atoms(projection):
        if atom.record != 'ATOM' or atom.atom_name != 'CA':
            continue
        key = (atom.chain_id,atom.residue_number,'',atom.residue_name,'CA',6)
        if key not in actual:
            raise ValueError('VS output lacks a deposited reference C-alpha atom')
        points.append(_point(receptor,actual[key]))
        target.append(atom.xyz)
    if len(points) < 3:
        raise ValueError('VS reference frame requires at least three C-alpha atoms')
    left, right = np.asarray(points), np.asarray(target)
    a, b = left-left.mean(axis=0), right-right.mean(axis=0)
    if not np.isfinite(left).all() or not np.isfinite(right).all() or min(np.linalg.matrix_rank(a),np.linalg.matrix_rank(b)) < 2:
        raise ValueError('VS reference frame is non-finite or geometrically degenerate')
    u, _, v = np.linalg.svd(a.T @ b)
    if np.linalg.det(u @ v) < 0:
        u[:,-1] *= -1
    rotation = u @ v
    translation = right.mean(axis=0)-left.mean(axis=0) @ rotation
    rmsd = float(np.sqrt(np.mean(np.sum((left @ rotation+translation-right)**2,axis=1))))
    return rotation, translation, {'method':'all_deposited_observed_CA_proper_rotation',
        'atom_count':len(points), 'rmsd_angstrom':rmsd,
        'rotation':rotation.tolist(), 'translation':translation.tolist(),
        'ligand_fitted':False, 'interpretation':'RMSD describes receptor change; it is not a biological suitability gate.'}


def protein_atoms(receptor, inventory):
    authors = inventory['author_residue_by_entity_position']
    atoms = []
    for atom in receptor.GetAtoms():
        chain, position, insertion, name, atom_name, atomic_number = residue(atom)
        if chain != 'A' or insertion or not 1 <= position <= len(authors):
            raise ValueError('VS receptor residue cannot be mapped to its deposited source')
        author = authors[position-1]
        if not re.fullmatch(r'-?\d+', str(author)):
            raise ValueError('VS geometry requires an explicit insertion-code adapter for this source')
        x,y,z = _point(receptor,atom.GetIdx())
        atoms.append(PdbAtom('ATOM', atom.GetIdx()+1, atom_name, '', name, inventory['author_chain'],
                             int(author), x,y,z, Chem.GetPeriodicTable().GetElementSymbol(atomic_number)))
    return atoms


def axis(flags, names):
    values = [flags[key] for key in names]
    if any(value is False for value in values):
        return 'FAIL'
    return 'UNKNOWN' if any(value is None for value in values) else 'PASS'


PHYSICAL = ('covalent_cs_distance','product_angle','protein_clash','severe_protein_clash_absent')
DESIGN = ('scaffold_rmsd','interaction_retention','ligand_strain')


def assess(metrics, policy):
    def limit(value, maximum):
        return None if value is None else value <= maximum
    def window(value, bounds):
        return None if value is None else bounds[0] <= value <= bounds[1]
    flags = {'scaffold_rmsd':limit(metrics['scaffold_rmsd_angstrom'],policy.max_scaffold_rmsd_angstrom),
        'interaction_retention':(None if metrics['interaction_retention_fraction'] is None else
            metrics['interaction_retention_fraction'] >= policy.min_interaction_retention_fraction),
        'ligand_strain':limit(metrics['strain_excess_over_parent_kcal_mol'],policy.max_strain_excess_over_parent_kcal_mol),
        'covalent_cs_distance':window(metrics['covalent_cs_distance_angstrom'],policy.covalent_cs_bond_window_angstrom),
        'product_angle':window(metrics['product_s_cbeta_calpha_angle_degrees'],policy.product_angle_window_degrees),
        'protein_clash':limit(metrics['clash_fraction'],policy.max_clash_fraction),
        'severe_protein_clash_absent':limit(metrics['severe_clash_pair_count'],policy.max_severe_clash_pair_count)}
    complete = all(value is not None for value in flags.values())
    passed = complete and all(flags.values())
    return {'status':'PASS' if passed else 'FAIL' if complete else 'UNKNOWN',
        'physical_validity':axis(flags,PHYSICAL), 'design_constraints':axis(flags,DESIGN),
        'evidence_complete':complete, 'review_eligible':passed, 'gate_flags':flags,
        'failure_reasons':[name for name,value in flags.items() if value is False],
        'missing_reasons':[name for name,value in flags.items() if value is None]}


def measure(row, campaign, parent, inventory, projection, policy, parent_strain=None):
    rotation, translation, frame = reference_frame(row['receptor'], projection)
    product = transformed(row['product'],rotation,translation)
    ligand = transformed(row['ligand'],rotation,translation)
    receptor = transformed(row['receptor'],rotation,translation)
    atoms = protein_atoms(receptor,inventory)
    maps = _atom_map_indices(ligand)
    interactions = _interaction_measurements(ligand,maps,parent,atoms,campaign)
    retention = sum(item.retained for item in interactions)/len(interactions) if interactions else None
    rmsd = _scaffold_rmsd(parent,ligand,campaign,maps)
    strain, method = _strain_energy(ligand)
    parent_energy, parent_method = parent_strain if parent_strain is not None else _strain_energy(parent)
    excess = strain-parent_energy if strain is not None and parent_energy is not None and method == parent_method else None
    severe, clashing, heavy, fraction = _clashes(ligand,atoms,campaign,policy)
    sg = product.GetNumAtoms()-1  # Verified product stores the actual target SG last.
    metrics = {'scaffold_rmsd_angstrom':rmsd, 'interaction_retention_fraction':retention,
        'protected_interactions':[item.model_dump(mode='json') for item in interactions],
        'covalent_cs_distance_angstrom':_distance(_point(product,sg),_point(product,maps[906])),
        'product_s_cbeta_calpha_angle_degrees':_angle(_point(product,sg),_point(product,maps[906]),_point(product,maps[905])),
        'ligand_strain_kcal_mol':strain, 'parent_ligand_strain_kcal_mol':parent_energy,
        'strain_excess_over_parent_kcal_mol':excess, 'candidate_forcefield':method, 'parent_forcefield':parent_method,
        'strain_estimator':'local_relaxation_convergence_checked_v1', 'rdkit_version':rdBase.rdkitVersion,
        'severe_clash_pair_count':severe, 'clashing_ligand_atom_count':clashing,
        'ligand_heavy_atom_count':heavy, 'clash_fraction':fraction,
        'clash_method':'cys_product_cross_bond_exclusions_through_1_4_v2',
        'reference_frame':frame, 'receptor_coordinates':'this_exact_VS_output_pose'}
    if any(isinstance(value,float) and not math.isfinite(value) for value in metrics.values()):
        raise ValueError('VS measured geometry contains a non-finite metric')
    return {'pose_index':row['pose_index'], 'candidate_id':row['candidate_id'],
        'input_index':row['input_index'], 'covdock_vs_score':row['score'],
        'metrics':metrics, 'assessment':assess(metrics,policy), 'md_ready':False}
