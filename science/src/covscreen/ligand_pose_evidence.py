"""Re-read registered CarsiDock ensembles using the existing geometry evaluator."""
import json
import numpy as np
from rdkit import Chem

from .carsidock_results import pose_name, resolve_pose_compound_id
from .jobs import sha256_file
from .ligand_design import design_folder
from .ligand_first_inputs import load_campaign, build_parent_pose_mol
from .ligand_first_geometry import parse_geometry_policy, evaluate_carsidock_geometry
from .pose_topology import compare_named_graphs
from .rdkit_covalentizer import WARHEAD_ATOM_MAPS
from .md_preparation_evidence import registered_output


def incomplete(candidate):
    if candidate.get("schema_version", "1.0") not in {"1.0", "2.0"}:
        raise ValueError("Unsupported ligand geometry result version")
    if candidate.get("schema_version") == "2.0":
        # Validate stored axes, policy and selection rather than trusting a flag.
        from covscreen_ligand_first.ligand_geometry_assessment import GeometryResultV2
        value = GeometryResultV2.model_validate({key: item for key, item in candidate.items()
            if key not in {"source_pose_record_indexes", "selected_pose_record_index"}})
        return not value.assessment.evidence_complete
    return bool(candidate['errors']) or candidate['pose_count'] == 0 or any(
        pose['strain_excess_over_parent_kcal_mol'] is None or not pose['protected_interactions']
        for pose in candidate['poses'])


def review_eligible(candidate):
    if candidate.get("schema_version") == "2.0":
        incomplete(candidate)  # Revalidate axes, policy and selected pose.
        return candidate["assessment"]["review_eligible"]
    if candidate.get("schema_version", "1.0") != "1.0":
        raise ValueError("Unsupported ligand geometry result version")
    return candidate["status"] == "PASS" and not candidate["errors"]


def strict_poses(path):
    values = list(Chem.SDMolSupplier(str(path), removeHs=False, sanitize=False))
    if not values or any(value is None for value in values):
        raise ValueError('Missing or unreadable pose records')
    for value in values:
        if value.GetNumConformers() != 1 or not np.isfinite(value.GetConformer().GetPositions()).all():
            raise ValueError('Pose coordinates must be finite and present')
    return values


def mapped_graph(molecule, *, dummy=False):
    molecule = Chem.Mol(molecule)
    try:
        Chem.SanitizeMol(molecule)
    except (ValueError, RuntimeError) as error:
        raise ValueError('Pose topology cannot be chemically sanitized') from error
    atoms, bonds = [], []
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() == 1:
            continue
        if dummy and atom.GetSymbol() == 'S' and atom.GetAtomMapNum() == 0:
            continue
        if atom.GetAtomMapNum() <= 0:
            raise ValueError('Pose has an unmapped heavy atom')
        atoms.append((str(atom.GetAtomMapNum()), atom.GetSymbol()))
    names = {name for name, _ in atoms}
    for bond in molecule.GetBonds():
        first, second = str(bond.GetBeginAtom().GetAtomMapNum()), str(bond.GetEndAtom().GetAtomMapNum())
        if first in names and second in names:
            bonds.append((first, second, bond.GetBondTypeAsDouble()))
    return atoms, bonds


def verify_product(original, raw, clean):
    """Only the declared acrylamide alpha/beta reduction is permitted here."""
    expected = Chem.Mol(original)
    maps = {atom.GetAtomMapNum(): atom.GetIdx() for atom in expected.GetAtoms()}
    alpha, beta = [WARHEAD_ATOM_MAPS[key] for key in ('electrophile_alpha_carbon', 'reaction_beta_carbon')]
    bond = expected.GetBondBetweenAtoms(maps[alpha], maps[beta])
    if bond is None or bond.GetBondTypeAsDouble() != 2:
        raise ValueError('Expected the declared acrylamide double bond')
    bond.SetBondType(Chem.BondType.SINGLE)
    atoms, bonds = mapped_graph(expected)
    for value, dummy in ((raw, True), (clean, False)):
        observed_atoms, observed_bonds = mapped_graph(value, dummy=dummy)
        compare_named_graphs(atoms, bonds, observed_atoms, observed_bonds, str(beta), str(beta))
    sulfur = [atom for atom in raw.GetAtoms() if atom.GetSymbol() == 'S' and atom.GetAtomMapNum() == 0]
    if (len(sulfur) != 1 or sulfur[0].GetDegree() != 1
            or sulfur[0].GetNeighbors()[0].GetAtomMapNum() != beta):
        raise ValueError('Pose dummy sulfur does not bind the declared electrophile')
    raw_points = {atom.GetAtomMapNum(): raw.GetConformer().GetPositions()[atom.GetIdx()]
                  for atom in raw.GetAtoms() if atom.GetAtomMapNum() > 0}
    for atom in clean.GetAtoms():
        if atom.GetAtomicNum() > 1 and not np.allclose(raw_points[atom.GetAtomMapNum()],
                clean.GetConformer().GetPositions()[atom.GetIdx()], rtol=0, atol=1e-4):
            raise ValueError('Raw and no-dummy pose coordinates differ')


def evaluate_outputs(root, state, manager, spec, result):
    if result.state != 'COMPLETED' or result.return_code != 0:
        raise ValueError('Successful registered pose execution is required')
    directory = design_folder(root, state['design_id'])
    plan = directory / 'pose-plan'
    originals = strict_poses(plan / 'ligands.sdf')
    identities = [molecule.GetProp('COMPOUND_ID') for molecule in originals]
    artifact_dir = manager.registry.job_dir(spec.run_id) / 'artifacts'
    paths = [registered_output(root, artifact_dir, spec, result, name) for name in (
        'carsidock_michael_addition.sdf', 'carsidock_michael_addition_nodumat.sdf')]
    hashes = {str(path.relative_to(root)): sha256_file(path) for path in paths}
    prior = state['pose_execution'].get('evaluation')
    if prior and prior['output_sha256'] != hashes:
        raise ValueError('Previously evaluated pose outputs changed')
    raw, clean = [strict_poses(path) for path in paths]
    if len(raw) != len(clean):
        raise ValueError('Raw/no-dummy pose counts differ')
    groups = {identity: ([], []) for identity in identities}
    indexes = {identity: [] for identity in identities}
    for index, (first, second) in enumerate(zip(raw, clean)):
        identity = resolve_pose_compound_id(pose_name(first), identities)
        if identity is None or pose_name(first) != pose_name(second):
            raise ValueError('Pose pair candidate/variant identity mismatch')
        verify_product(originals[identities.index(identity)], first, second)
        groups[identity][0].append(first)
        groups[identity][1].append(second)
        indexes[identity].append(index)
    campaign = load_campaign(directory / 'case/campaign.json')
    parent = build_parent_pose_mol(campaign, directory / 'case')
    policy = parse_geometry_policy(json.loads((plan / 'geometry-policy.json').read_text()))
    evaluations = [evaluate_carsidock_geometry(campaign=campaign, parent_pose=parent,
        candidate_id=identity, carsidock_run_id=spec.run_id, raw_poses=groups[identity][0],
        no_dummy_poses=groups[identity][1], receptor_pdb=root / spec.parameters['pdb'],
        policy=policy).model_dump(mode='json') for identity in identities]
    for value in evaluations:
        value['source_pose_record_indexes'] = indexes[value['candidate_id']]
        selected = value['selected_pose_index']
        value['selected_pose_record_index'] = indexes[value['candidate_id']][selected] if selected is not None else None
    return {'schema': ('ligand-pose-geometry-review-v2' if getattr(policy, 'version', None) else 'ligand-pose-geometry-review-v1'), 'run_id': spec.run_id,
        'output_sha256': hashes, 'candidate_count': len(identities), 'candidates': evaluations,
        'passing_candidate_count': sum(value['status'] == 'PASS' for value in evaluations),
        'incomplete_candidate_count': sum(incomplete(value) for value in evaluations),
        'assessment_scope': 'mapped_product_graph_and_pose_geometry', 'md_ready': False,
        'limitations': ['Geometry PASS does not establish potency, selectivity or reaction kinetics.',
                       'Protected N/O contacts are distance proxies; independent refinement remains required.']}
