"""Library-specific provisional pose QC without an invented common scaffold.

Reference polar-contact sites are distance proxies, not assigned hydrogen
bonds or proven energetic hotspots. The strain estimate is a candidate's own
converged local relaxation energy, never a difference between unrelated ligands.
"""
import math
from pathlib import Path

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from rdkit import Chem, rdBase

from .covdock_vs import molecules
from .covdock_vs_output import residue
from .covdock_vs_geometry import reference_frame, transformed, axis
from .ligand_first_inputs import read_pdb_atoms
from .ligand_first_geometry import _atom_map_indices, _point, _distance, _angle, _strain_energy


class Policy(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    version: str = 'library-vs-geometry-v2-contacts-informational'
    reference_polar_contact_distance_angstrom: float = Field(default=3.5, gt=0)
    min_reference_contact_site_retention: float = Field(default=0.70, ge=0, le=1)
    max_local_relaxation_energy_kcal_mol: float = Field(default=20.0, gt=0)
    covalent_cs_bond_window_angstrom: tuple[float, float] = (1.65, 2.15)
    product_angle_window_degrees: tuple[float, float] = (80.0, 140.0)
    clash_vdw_scale: float = Field(default=0.75, gt=0, lt=1)
    max_clash_fraction: float = Field(default=0.20, ge=0, le=1)
    max_severe_clash_pair_count: int = Field(default=0, ge=0)

    @model_validator(mode='after')
    def supported(self):
        if self.version not in {'library-vs-geometry-v1-provisional', 'library-vs-geometry-v2-contacts-informational'}:
            raise ValueError('Unsupported Library VS geometry policy')
        for window in (self.covalent_cs_bond_window_angstrom, self.product_angle_window_degrees):
            if not 0 < window[0] < window[1]:
                raise ValueError('Library VS geometry windows must be positive and increasing')
        return self


def contact_sites(reference: Path, receptor: Path, policy):
    """Freeze sites measured in the same-coordinate, already bound reference."""
    references = molecules(reference, False)
    if len(references) != 1 or references[0].GetNumConformers() != 1:
        raise ValueError('Library VS contact reference requires exactly one coordinate molecule')
    ligand = references[0]
    if not np.isfinite(ligand.GetConformer().GetPositions()).all():
        raise ValueError('Library VS reference coordinates must be finite')
    polar = [a.GetIdx() for a in ligand.GetAtoms() if a.GetAtomicNum() in {7, 8}]
    sites = []
    seen = set()
    for atom in read_pdb_atoms(receptor):
        if atom.record != 'ATOM' or atom.element not in {'N', 'O'}:
            continue
        key = (atom.chain_id, atom.residue_number, atom.residue_name, atom.atom_name)
        if key in seen or not all(math.isfinite(v) for v in atom.xyz):
            raise ValueError('Library VS contact source contains ambiguous or nonfinite protein atoms')
        seen.add(key)
        nearby = [(index, _distance(_point(ligand, index), atom.xyz)) for index in polar]
        nearby = [(index, distance) for index, distance in nearby
                  if distance <= policy.reference_polar_contact_distance_angstrom]
        if nearby:
            sites.append({'protein_atom': list(key), 'element': atom.element,
                'reference_ligand_atoms': [{'index': index, 'distance_angstrom': distance} for index, distance in nearby]})
    return {'schema': 'library-vs-reference-contact-sites-v1', 'sites': sorted(sites, key=lambda row: row['protein_atom']),
        'distance_cutoff_angstrom': policy.reference_polar_contact_distance_angstrom,
        'definition': 'Unique protein N/O sites within cutoff of reference ligand N/O atoms in their bound coordinate frame',
        'hydrogen_bonds_assigned': False, 'energetic_importance_validated': False}


def contacts(ligand, receptor, reference, policy):
    indexed = {residue(atom): atom.GetIdx() for atom in receptor.GetAtoms()}
    polar = [atom.GetIdx() for atom in ligand.GetAtoms() if atom.GetAtomicNum() in {7, 8}]
    values = []
    for site in reference['sites']:
        chain, number, name, atom_name = site['protein_atom']
        key = (chain, number, '', name, atom_name, Chem.GetPeriodicTable().GetAtomicNumber(site['element']))
        if key not in indexed:
            raise ValueError('VS output lacks a frozen reference contact site')
        distances = [(i, _distance(_point(ligand, i), _point(receptor, indexed[key]))) for i in polar]
        nearest = min(distances, key=lambda pair: (pair[1], pair[0])) if distances else None
        values.append({'protein_atom': site['protein_atom'], 'candidate_atom_index': nearest[0] if nearest else None,
            'distance_angstrom': nearest[1] if nearest else None,
            'retained': bool(nearest and nearest[1] <= policy.reference_polar_contact_distance_angstrom)})
    return values, sum(item['retained'] for item in values) / len(values) if values else None


def near_bonds(molecule, start):
    depths, pending = {start: 0}, [start]
    while pending:
        index = pending.pop(0)
        if depths[index] >= 2:
            continue
        for atom in molecule.GetAtomWithIdx(index).GetNeighbors():
            if atom.GetIdx() not in depths:
                depths[atom.GetIdx()] = depths[index] + 1
                pending.append(atom.GetIdx())
    return depths


def clashes(ligand, receptor, attachment, policy):
    chain, number = attachment.split(':')
    targets = [atom.GetIdx() for atom in receptor.GetAtoms()
               if residue(atom) == (chain, int(number), '', 'CYS', 'SG', 16)]
    if len(targets) != 1:
        raise ValueError('Library VS clash check requires the unique actual target sulfur')
    beta = _atom_map_indices(ligand)[906]
    left, right = near_bonds(ligand, beta), near_bonds(receptor, targets[0])
    atoms = [a for a in receptor.GetAtoms() if a.GetAtomicNum() > 1]
    xyz = np.asarray([_point(receptor, a.GetIdx()) for a in atoms])
    table = Chem.GetPeriodicTable()
    radii = np.asarray([table.GetRvdw(a.GetAtomicNum()) for a in atoms])
    severe, affected, heavy = 0, 0, 0
    for atom in ligand.GetAtoms():
        if atom.GetAtomicNum() <= 1:
            continue
        heavy += 1
        excluded = np.asarray([left.get(atom.GetIdx(), 100) + right.get(a.GetIdx(), 100) + 1 <= 3 for a in atoms])
        with np.errstate(all='raise'):
            delta = xyz - np.asarray(_point(ligand, atom.GetIdx()))
            squared = np.einsum('ij,ij->i', delta, delta, optimize=False)
            limits = policy.clash_vdw_scale * (radii + table.GetRvdw(atom.GetAtomicNum()))
            count = int(np.count_nonzero((squared < limits ** 2) & ~excluded))
        severe += count
        affected += bool(count)
    if not heavy:
        raise ValueError('Library VS clash check requires ligand heavy atoms')
    return severe, affected, heavy, affected / heavy


def assess(metrics, policy):
    def maximum(value, threshold):
        return None if value is None else value <= threshold
    def window(value, bounds):
        return None if value is None else bounds[0] <= value <= bounds[1]
    flags = {'covalent_cs_distance': window(metrics['covalent_cs_distance_angstrom'], policy.covalent_cs_bond_window_angstrom),
        'product_angle': window(metrics['product_s_cbeta_calpha_angle_degrees'], policy.product_angle_window_degrees),
        'protein_clash': maximum(metrics['clash_fraction'], policy.max_clash_fraction),
        'severe_protein_clash_absent': maximum(metrics['severe_clash_pair_count'], policy.max_severe_clash_pair_count),
        'ligand_local_strain': maximum(metrics['ligand_local_relaxation_energy_kcal_mol'], policy.max_local_relaxation_energy_kcal_mol),
        'reference_contact_site_retention': None if metrics['reference_contact_site_retention'] is None else
            metrics['reference_contact_site_retention'] >= policy.min_reference_contact_site_retention}
    # Preserve frozen v1 replay; new studies use contacts only as descriptive evidence.
    if policy.version == 'library-vs-geometry-v2-contacts-informational':
        flags.pop('reference_contact_site_retention')
    complete = all(value is not None for value in flags.values())
    passed = complete and all(flags.values())
    return {'status': 'PASS' if passed else 'FAIL' if complete else 'UNKNOWN',
        'physical_validity': axis(flags, ('covalent_cs_distance', 'product_angle', 'protein_clash', 'severe_protein_clash_absent')),
        'evidence_complete': complete, 'review_eligible': passed, 'gate_flags': flags,
        'failure_reasons': [name for name, value in flags.items() if value is False],
        'missing_reasons': [name for name, value in flags.items() if value is None]}


def contact_observation(ligand, receptor, reference, policy):
    if policy.version == 'library-vs-geometry-v1-provisional':
        observations, retention = contacts(ligand, receptor, reference, policy)
        return observations, retention, None
    if reference is None or reference.get('status') == 'UNAVAILABLE':
        return [], None, 'Reference contacts unavailable; informational only'
    try:
        observations, retention = contacts(ligand, receptor, reference, policy)
        return observations, retention, None
    except (ValueError, KeyError, TypeError, RuntimeError, FloatingPointError) as error:
        return [], None, 'Reference contact calculation unavailable: ' + str(error)


def measure(row, reference, receptor_path, attachment, policy):
    rotation, translation, frame = reference_frame(row['receptor'], receptor_path)
    ligand = transformed(row['ligand'], rotation, translation)
    product = transformed(row['product'], rotation, translation)
    receptor = transformed(row['receptor'], rotation, translation)
    maps = _atom_map_indices(ligand)
    observations, retention, contact_error = contact_observation(ligand, receptor, reference, policy)
    energy, method = _strain_energy(ligand)
    severe, affected, heavy, fraction = clashes(ligand, receptor, attachment, policy)
    sg = product.GetNumAtoms() - 1
    metrics = {'covalent_cs_distance_angstrom': _distance(_point(product, sg), _point(product, maps[906])),
        'product_s_cbeta_calpha_angle_degrees': _angle(_point(product, sg), _point(product, maps[906]), _point(product, maps[905])),
        'ligand_local_relaxation_energy_kcal_mol': energy, 'candidate_forcefield': method,
        'strain_estimator': 'local_relaxation_convergence_checked_v1', 'rdkit_version': rdBase.rdkitVersion,
        'strain_molecule': 'hydrogen_capped_postreaction_ligand_fragment_after_removing_protein_sulfur',
        'reference_contact_site_retention': retention, 'reference_polar_contacts': observations,
        'reference_contact_limitation': contact_error,
        'severe_clash_pair_count': severe, 'clashing_ligand_atom_count': affected,
        'ligand_heavy_atom_count': heavy, 'clash_fraction': fraction,
        'clash_method': 'exact_product_cross_bond_graph_exclusions_through_1_4',
        'reference_frame': frame, 'receptor_coordinates': 'this_exact_VS_output_pose',
        'scaffold_rmsd': {'status': 'NOT_APPLICABLE', 'reason': 'Library candidates have no declared common scaffold with the reference'},
        'parent_strain_subtraction': False}
    if any(isinstance(value, float) and not math.isfinite(value) for value in metrics.values()):
        raise ValueError('Library VS measured geometry contains a nonfinite value')
    return {'pose_index': row['pose_index'], 'candidate_id': row['candidate_id'], 'input_index': row['input_index'],
        'covdock_vs_score': row['score'], 'metrics': metrics, 'assessment': assess(metrics, policy), 'md_ready': False}
