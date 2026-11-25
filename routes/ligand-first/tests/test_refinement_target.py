"""Real deposited sources; synthetic workflow and pose review, no engine run."""
import copy
import json
from pathlib import Path
import shutil

import pytest

from covscreen.af3_target import read_target_seed
from covscreen.covdock_receptor import inspect_receptor
from covscreen.jobs import sha256_file
from covscreen.ligand_design import POLICY, design_folder, protection, seal, write
from covscreen.ligand_first_inputs import load_campaign, build_parent_pose_mol
from covscreen.ligand_refinement_target import target_drafts, freeze, load
from covscreen.native_case import prepare_native_case
from covscreen.mcp.server import dispatch
from covscreen.ligand_workflow import DESIGN, navigate
from test_ligand_design import graph_design
from test_pose_execution_workflow import pose, args, completed_pose_files


FIXTURE = Path(__file__).parent / 'fixtures/refinement-target-2ity'


@pytest.fixture
def design(tmp_path):
    acquisition = tmp_path / 'acquisition'
    acquisition.mkdir()
    for path in (FIXTURE / 'case/evidence').glob('*.json'):
        shutil.copyfile(path, acquisition / path.name)
    for path in (FIXTURE / 'case/source_structures').iterdir():
        shutil.copyfile(path, acquisition / path.name)
    identifier = 'design-' + 'a' * 24
    out = design_folder(tmp_path, identifier)
    out.mkdir(parents=True)
    shutil.copyfile(FIXTURE / 'uniprot.json', out / 'uniprot.json')
    prepare_native_case(acquisition, out / 'case', campaign_id=identifier, gene='EGFR',
        taxon=9606, ligand_name='gefitinib', noncovalent_parent_assertion='Test fixture only',
        identity_record=out / 'uniprot.json')
    campaign = load_campaign(out / 'case/campaign.json')
    protected = protection(build_parent_pose_mol(campaign, out / 'case'), campaign, out / 'case')
    data = campaign.model_dump(mode='json')
    data.update(planning_mode='rule-derived-v1', allowed_warheads=POLICY['warheads'])
    data['protection'].update({key: protected[key] for key in (
        'scaffold_core_atom_names', 'protected_pharmacophore_atom_names', 'protected_interactions')})
    write(out / 'case/campaign.json', data)
    evidence_id = 'hyp-' + 'b' * 24
    evidence = tmp_path / 'results/target-hypotheses' / evidence_id
    evidence.mkdir(parents=True)
    identity = json.loads((out / 'uniprot.json').read_text())
    write(evidence / 'evidence.json', {'target': 'EGFR', 'site': 'C797', 'taxon': 9606,
        'identity': {'uniprot_id': 'P00533', 'sequence': identity['sequence']['value']},
        'literature': [], 'structures': [], 'existing_therapies': [],
        'fixture': 'Synthetic study with real deposited sources, not scientific acceptance'})
    seal(evidence)
    write(out / 'design.json', {'design_id': identifier, 'evidence_id': evidence_id,
        'state': 'DESIGN_INPUTS_READY', 'policy': POLICY,
        'evidence_manifest_sha256': sha256_file(evidence / 'manifest.json')})
    seal(out)
    return identifier


def positive_review(pose):
    state = json.loads(pose[2].read_text())
    plan = design_folder(pose[0], state['design_id']) / 'pose-plan'
    identities = json.loads((plan / 'input-binding.json').read_text())['selected_candidate_ids']
    return {'output_sha256': {'software-only': '0' * 64}, 'passing_candidate_count': 1,
        'candidate_count': len(identities), 'md_ready': False,
        'candidates': [{'candidate_id': identities[0], 'status': 'PASS', 'errors': [],
            'selected_pose_index': 0, 'selected_pose_record_index': 0,
            'selected_pose': {'carsidock_score': -1}}]}


@pytest.fixture
def handoff(pose):
    from covscreen.ligand_refinement_input import freeze_refinement_input
    state = json.loads(pose[2].read_text())
    state['pose_execution'].update(run_id='software-fixture', evaluation_verification='verified',
                                   evaluation=positive_review(pose))
    state['refinement_input'] = freeze_refinement_input(pose[0], state)
    return pose[0], state


def test_real_sources_produce_consistent_seed_and_numbered_projection(handoff):
    root, state = handoff
    binding = freeze(root, state)
    state['refinement_input']['target'] = binding
    assert freeze(root, state) == binding
    payload = load(root, state)
    directory = root / binding['directory']
    checked = read_target_seed(directory / 'af3-source.json', directory / 'af3-target.json')
    receptor = inspect_receptor(directory / 'receptor-projection.pdb', 'A:102')
    receipt = json.loads((directory / 'target-source.json').read_text())
    assert checked['cysteine_sequence_index'] == 102
    assert receipt['mapping']['author_residue_id'] == '797'
    assert receipt['mapping']['uniprot_residue'] == 797
    assert receipt['construct_length'] == 327
    assert receipt['missing_construct_residue_count'] == 27
    assert receipt['excluded_atom_count'] == 50
    assert receptor['target_coordinate_identity_verified']
    assert not payload['receptor_prepared'] and not payload['compute_submitted']
    assert not receipt['scientific_suitability_verified'] and not receipt['grants_resource_approval']
    assert not list((root / 'state/jobs').glob('*/spec.json'))


@pytest.mark.parametrize('change', ['sequence', 'site', 'taxon', 'sources', 'candidate'])
def test_recovery_refuses_cross_study_inputs(handoff, change):
    root, state = handoff
    state['refinement_input']['target'] = freeze(root, state)
    if change in {'site', 'taxon'}:
        state['target'][change] = 'C800' if change == 'site' else 10090
    elif change == 'sources':
        state['pose_execution']['source_manifests'] = {}
    elif change == 'candidate':
        state['refinement_input']['manifest_sha256'] = '0' * 64
    else:
        path = design_folder(root, state['design_id']) / 'uniprot.json'
        value = json.loads(path.read_text())
        value['sequence']['value'] = value['sequence']['value'].replace('L', 'A', 1)
        write(path, value)
    with pytest.raises(ValueError):
        load(root, state)


@pytest.mark.parametrize('name', ['af3-source.json', 'af3-target.json', 'receptor-projection.pdb', 'target-source.json'])
def test_frozen_target_bytes_cannot_change(handoff, name):
    root, state = handoff
    binding = freeze(root, state)
    path = root / binding['directory'] / name
    path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='artifact changed'):
        load(root, state, binding)


def test_pose_report_keeps_target_preparation_separate_from_computation(handoff):
    """Report adapter fixture; the positive selection is software-only."""
    from covscreen.ligand_pose_report import build, render
    root, state = handoff
    state['refinement_input']['target'] = freeze(root, state)
    record = state['pose_execution']
    record.update(job_state='COMPLETED', return_code=0)
    review = record['evaluation']
    review.update(schema='software-fixture-legacy-review', incomplete_candidate_count=0)
    policy = json.loads((design_folder(root, state['design_id']) / 'pose-plan/geometry-policy.json').read_text())
    review['candidates'][0].update(policy=policy, source_pose_record_indexes=[0], pose_count=1,
        passing_pose_count=1, poses=[])
    # Freeze again because the exact review is part of the candidate identity.
    from covscreen.ligand_refinement_input import freeze_refinement_input
    state['refinement_input'] = freeze_refinement_input(root, state)
    state['refinement_input']['target'] = freeze(root, state)
    report = build(root, state)
    target = report['refinement_target']
    assert target['projected_site'] == 102
    assert not target['receptor_prepared'] and not target['compute_submitted']
    assert not report['validated_inhibitor'] and not report['md_ready']
    text = render(report)
    assert 'canonical C797' in text and '未解析残基 27 个' in text
    assert '精修计算尚未执行' in text
    path = root / state['refinement_input']['target']['directory'] / 'af3-target.json'
    path.write_text('{}')
    with pytest.raises(ValueError, match='artifact changed'):
        build(root, state)


def test_current_acquisition_mapping_cannot_be_replaced_by_historical_offset(tmp_path):
    case = tmp_path / 'case'
    shutil.copytree(FIXTURE / 'case', case)
    path = case / 'evidence/structure_acquisition.json'
    data = json.loads(path.read_text())
    data['mappings'][0]['entity_seq_id'] = 105
    write(path, data)
    with pytest.raises(ValueError, match='mapping differs'):
        target_drafts(case, json.loads((FIXTURE / 'uniprot.json').read_text()))


def test_canonical_variant_mismatch_is_not_hidden_by_matching_target_cys():
    identity = json.loads((FIXTURE / 'uniprot.json').read_text())
    seq = list(identity['sequence']['value'])
    seq[789] = 'M'  # Different current target sequence, despite conserved C797.
    identity['sequence']['value'] = ''.join(seq)
    with pytest.raises(ValueError, match='sequence/variant review'):
        target_drafts(FIXTURE / 'case', identity)


def test_mcp_target_action_rechecks_pose_and_recovers_without_new_submission(pose, monkeypatch):
    """Registered software job and mocked positive review; actual MCP and target builder."""
    completed_pose_files(pose, monkeypatch)
    review = positive_review(pose)
    monkeypatch.setattr('covscreen.ligand_pose_evidence.evaluate_outputs', lambda *args: copy.deepcopy(review))
    first = dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement'))
    assert 'prepare_refinement_target' in first['workflow']['tools'][DESIGN]['actions']
    before = sorted((pose[0] / 'state/jobs').iterdir())
    result = dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement_target'))
    bound = result['refinement_input']['target']
    assert bound['status'] == 'TARGET_INPUTS_BOUND_PREPARATION_REQUIRED'
    again = dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement_target'))
    assert again['refinement_input']['target'] == bound
    assert sorted((pose[0] / 'state/jobs').iterdir()) == before
    review['passing_candidate_count'] = 0
    review['candidates'] = []
    with pytest.raises(ValueError, match='frozen refinement binding'):
        dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement_target'))
    assert json.loads(pose[2].read_text())['refinement_input'] is None


@pytest.mark.parametrize('action', ['status', 'pose_status'])
def test_status_rechecks_saved_target_and_keeps_failure_visible(pose, monkeypatch, action):
    completed_pose_files(pose, monkeypatch)
    review = positive_review(pose)
    monkeypatch.setattr('covscreen.ligand_pose_evidence.evaluate_outputs', lambda *args: copy.deepcopy(review))
    dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement'))
    response = dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement_target'))
    target = response['refinement_input']['target']
    assert any(b['code'] == 'preparation_required' for b in response['blockers'])
    path = pose[0] / target['directory'] / 'af3-source.json'
    path.write_text('{}')
    if action == 'pose_status':
        with pytest.raises(ValueError, match='failed revalidation'):
            dispatch(pose[0], DESIGN, args(pose, action))
    status = navigate(pose[0], 'status', workflow_id=pose[1])
    assert 'target' not in status['refinement_input']
    assert status['refinement_input']['target_error']['artifact_id'] == target['artifact_id']
    assert any(b['code'] == 'evidence_rejected' and b['scope'] == 'refinement_target_inputs'
               for b in status['blockers'])
    assert not any(a['kind'] == 'refinement_target_drafts' for a in status['artifacts'])
