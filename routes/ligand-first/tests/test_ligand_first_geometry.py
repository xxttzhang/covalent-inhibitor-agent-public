from pathlib import Path

from covscreen.ligand_first_geometry import (
    evaluate_carsidock_geometry,
    load_sdf_poses,
)
from covscreen.ligand_first_inputs import build_parent_pose_mol, load_campaign


ROOT = (Path(__file__).resolve().parents[3] / "science")
CAMPAIGN_PATH = ROOT / "projects/egfr-c797/ligand_first/campaign.json"
GEOMETRY_DIR = ROOT / "projects/egfr-c797/ligand_first/geometry/carsidock_20260817T135020"


def test_real_carsidock_ensemble_has_independent_geometry_metrics():
    campaign = load_campaign(CAMPAIGN_PATH)
    result = evaluate_carsidock_geometry(
        campaign=campaign,
        parent_pose=build_parent_pose_mol(campaign, CAMPAIGN_PATH.parent),
        candidate_id="egfr-f9f62be250f14d9a",
        carsidock_run_id="20260817T135020-carsidock-cov-be42799293",
        raw_poses=load_sdf_poses(GEOMETRY_DIR / "carsidock.sdf"),
        no_dummy_poses=load_sdf_poses(GEOMETRY_DIR / "carsidock_nodumat.sdf"),
        receptor_pdb=CAMPAIGN_PATH.parent / campaign.parent_pose.structure_path,
    )
    assert result.pose_count == 9
    assert len(result.poses) == 9
    assert result.errors == []
    assert result.selected_pose is not None
    selected = result.selected_pose
    assert selected.scaffold_rmsd_angstrom < 1.0
    assert selected.protected_interactions[0].protein_residue == "MET793"
    assert selected.interaction_retention_fraction == 1.0
    assert 1.7 < selected.covalent_cs_bond_distance_angstrom < 2.0
    assert 100 < selected.attack_angle_s_cbeta_calpha_degrees < 125
    assert selected.dummy_to_target_sg_distance_angstrom < 0.01
    assert 1.7 < selected.actual_sg_cbeta_distance_angstrom < 2.0
    assert 100 < selected.actual_sg_cbeta_calpha_angle_degrees < 125
    assert selected.ligand_strain_kcal_mol is not None
    assert selected.parent_ligand_strain_kcal_mol is not None
    assert selected.strain_excess_over_parent_kcal_mol is not None
    assert selected.strain_excess_over_parent_kcal_mol < 0
    assert selected.strain_estimator == "local_relaxation_convergence_checked_v1"
    assert "protein_clash" in selected.gate_flags


def test_badly_displaced_pose_cluster_is_rejected_by_scaffold_and_interaction_gates():
    campaign = load_campaign(CAMPAIGN_PATH)
    raw = load_sdf_poses(GEOMETRY_DIR / "carsidock.sdf")
    no_dummy = load_sdf_poses(GEOMETRY_DIR / "carsidock_nodumat.sdf")
    result = evaluate_carsidock_geometry(
        campaign=campaign,
        parent_pose=build_parent_pose_mol(campaign, CAMPAIGN_PATH.parent),
        candidate_id="egfr-f9f62be250f14d9a",
        carsidock_run_id="20260817T135020-carsidock-cov-be42799293-bad-cluster",
        raw_poses=raw[6:],
        no_dummy_poses=no_dummy[6:],
        receptor_pdb=CAMPAIGN_PATH.parent / campaign.parent_pose.structure_path,
    )
    assert all(not pose.gate_flags["scaffold_rmsd"] for pose in result.poses)
    assert all(not pose.gate_flags["interaction_retention"] for pose in result.poses)
    assert result.status == "FAIL"


def test_fixed_mapped_product_graph_accepts_recorded_kekule_pose_files():
    from covscreen.ligand_pose_evidence import strict_poses, verify_product
    original = strict_poses(ROOT / 'projects/egfr-c797/ligand_first/generated/candidates.sdf')[0]
    raw = strict_poses(GEOMETRY_DIR / 'carsidock.sdf')
    clean = strict_poses(GEOMETRY_DIR / 'carsidock_nodumat.sdf')
    assert len(raw) == len(clean) == 9
    for first, second in zip(raw, clean):
        verify_product(original, first, second)


def test_real_forcefield_nonconvergence_does_not_become_zero_strain():
    from rdkit import Chem
    from rdkit.Chem import AllChem
    from covscreen.ligand_first_geometry import _local_relaxation_energy
    molecule = Chem.AddHs(Chem.MolFromSmiles("CCCCCCO"))
    assert AllChem.EmbedMolecule(molecule, randomSeed=1020) == 0
    properties = AllChem.MMFFGetMoleculeProperties(molecule, mmffVariant="MMFF94s")
    reference = AllChem.MMFFGetMoleculeForceField(molecule, properties)
    minimum = Chem.Mol(molecule)
    field = AllChem.MMFFGetMoleculeForceField(minimum, properties)
    assert _local_relaxation_energy(reference, field, max_iterations=0) is None
    release = _local_relaxation_energy(reference, field)
    assert release is not None and release > 0


def test_missing_local_energy_is_incomplete_evidence_not_a_negative_result():
    from covscreen.ligand_pose_evidence import incomplete
    candidate = {"errors": [], "pose_count": 1, "poses": [{
        "strain_excess_over_parent_kcal_mol": None, "protected_interactions": [{"retained": True}]}]}
    assert incomplete(candidate)
    candidate["poses"][0]["strain_excess_over_parent_kcal_mol"] = 5
    assert not incomplete(candidate)
    candidate["poses"][0]["protected_interactions"] = []
    assert incomplete(candidate)


def test_dummy_alignment_does_not_substitute_for_actual_sg_bond_geometry(tmp_path):
    import numpy as np
    from covscreen.rdkit_covalentizer import WARHEAD_ATOM_MAPS
    campaign = load_campaign(CAMPAIGN_PATH)
    raw = load_sdf_poses(GEOMETRY_DIR / "carsidock.sdf")[:1]
    clean = load_sdf_poses(GEOMETRY_DIR / "carsidock_nodumat.sdf")[:1]
    molecule = raw[0]
    beta = next(atom.GetIdx() for atom in molecule.GetAtoms()
                if atom.GetAtomMapNum() == WARHEAD_ATOM_MAPS["reaction_beta_carbon"])
    sulfur = next(atom.GetIdx() for atom in molecule.GetAtoms()
                  if atom.GetSymbol() == "S" and atom.GetAtomMapNum() == 0)
    positions = molecule.GetConformer().GetPositions()
    direction = positions[sulfur] - positions[beta]
    # Move only the actual receptor SG; dummy geometry remains unchanged.
    shifted = positions[sulfur] + 0.49 * direction / np.linalg.norm(direction)
    lines = (CAMPAIGN_PATH.parent / campaign.parent_pose.structure_path).read_text().splitlines()
    count = 0
    for index, line in enumerate(lines):
        if (line.startswith("ATOM") and line[12:16].strip() == "SG"
                and line[21] == campaign.parent_pose.protein_chain_id
                and int(line[22:26]) == campaign.parent_pose.target_author_residue_number):
            lines[index] = line[:30] + "".join(f"{value:8.3f}" for value in shifted) + line[54:]
            count += 1
    assert count == 1
    receptor = tmp_path / "shifted-sg.pdb"
    receptor.write_text("\n".join(lines) + "\n")
    result = evaluate_carsidock_geometry(
        campaign=campaign, parent_pose=build_parent_pose_mol(campaign, CAMPAIGN_PATH.parent),
        candidate_id="sg-displacement-fixture", carsidock_run_id="software-only",
        raw_poses=raw, no_dummy_poses=clean, receptor_pdb=receptor)
    assert result.errors == []
    pose = result.poses[0]
    assert pose.gate_flags["covalent_cs_distance"]
    assert pose.gate_flags["dummy_target_alignment"]
    assert not pose.gate_flags["actual_target_cs_distance"]
    assert "actual_target_cs_distance" in pose.failure_reasons
    assert result.status == "FAIL"


def test_target_cysteine_backbone_is_not_exempt_from_clash_check():
    from dataclasses import replace
    from covscreen.ligand_first_geometry import _clashes, LigandFirstGeometryPolicy
    from covscreen.ligand_first_inputs import read_pdb_atoms
    campaign = load_campaign(CAMPAIGN_PATH)
    pose = load_sdf_poses(GEOMETRY_DIR / "carsidock_nodumat.sdf")[0]
    atoms = read_pdb_atoms(CAMPAIGN_PATH.parent / campaign.parent_pose.structure_path)
    baseline = _clashes(pose, atoms, campaign, LigandFirstGeometryPolicy())
    # Place the target Cys backbone N on a distant ligand atom. The old routine
    # ignored the whole target residue and therefore could never detect this.
    point = pose.GetConformer().GetAtomPosition(0)
    changed = []
    count = 0
    for atom in atoms:
        if (atom.chain_id == campaign.parent_pose.protein_chain_id
                and atom.residue_number == campaign.parent_pose.target_author_residue_number
                and atom.atom_name == "N"):
            atom = replace(atom, x=point.x, y=point.y, z=point.z)
            count += 1
        changed.append(atom)
    assert count == 1
    observed = _clashes(pose, changed, campaign, LigandFirstGeometryPolicy())
    assert observed[0] > baseline[0]
    assert observed[1] > 0


def test_covalent_local_neighbors_are_not_counted_as_nonbonded_clashes():
    from covscreen.ligand_first_geometry import _clashes, LigandFirstGeometryPolicy
    from covscreen.ligand_first_inputs import read_pdb_atoms
    campaign = load_campaign(CAMPAIGN_PATH)
    pose = load_sdf_poses(GEOMETRY_DIR / "carsidock_nodumat.sdf")[0]
    atoms = read_pdb_atoms(CAMPAIGN_PATH.parent / campaign.parent_pose.structure_path)
    observed = _clashes(pose, atoms, campaign, LigandFirstGeometryPolicy())
    assert observed[0] == 0
