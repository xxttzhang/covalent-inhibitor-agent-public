import math
import pytest
from rdkit import Chem
from covscreen_de_novo.batch_prereaction_input import fresh
from covscreen_de_novo.batch_prereaction_geometry import measure, summarize


def test_independent_pose_distance_angle_and_graph_are_measured():
    original, expected = fresh({'candidate_id': 'PXM_test', 'parent_id': 'parent', 'mapped_smiles': 'C=CC(=O)NC'})
    pose = Chem.Mol(original)
    pose.SetDoubleProp('r_i_glide_gscore', -5)
    xyz = pose.GetConformer().GetPositions()
    beta, alpha = expected['beta_index_0based'], expected['alpha_index_0based']
    direction = xyz[alpha] - xyz[beta]
    perpendicular = [-direction[1], direction[0], 0]
    norm = math.sqrt(sum(x*x for x in perpendicular))
    sulfur = [float(xyz[beta][i] + 3.2 * perpendicular[i] / norm) for i in range(3)]
    result = measure(pose, original, expected, sulfur)
    assert result['sg_beta_A'] == pytest.approx(3.2)
    assert result['sg_beta_alpha_angle_degrees'] == pytest.approx(90)
    assert result['geometry_passed']
    assert not measure(pose, original, expected, [x + 100 for x in sulfur])['geometry_passed']
    pose.SetProp('_Name', 'foreign')
    with pytest.raises(ValueError, match='identity'):
        measure(pose, original, expected, sulfur)


def test_best_score_is_reported_separately_from_best_geometry():
    rows = [{'identity_verified': True, 'score': -9, 'structurally_eligible': False, 'raw_file': 'one', 'pose_index': 0},
            {'identity_verified': True, 'score': -5, 'structurally_eligible': True, 'raw_file': 'one', 'pose_index': 1},
            {'identity_verified': False, 'raw_file': 'one', 'pose_index': 2}]
    result = summarize(rows)
    assert result['best_score_pose']['pose_index'] == 0
    assert result['best_geometry_pose']['pose_index'] == 1
    assert result['eligible_pose_count'] == 1
    assert summarize([])['best_score_pose'] is None
