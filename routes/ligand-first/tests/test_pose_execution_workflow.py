"""Signed software-only registry fixtures; no scientific worker is launched."""
import json
import time
from uuid import uuid4

import pytest
from test_ligand_design import acquisition, design, graph_design
from covscreen.ligand_design import generate, plan_pose, write
from covscreen.ligand_workflow import DESIGN, ENTRY, navigate, directory, step
from covscreen.mcp.server import dispatch
from covscreen.execution import JobManager
from covscreen.jobs import RunResult, utc_now
from covscreen.submission_identity import submit_once
from covscreen.approval_scope import approval_scope
from covscreen.native_approval import signed, needs_approval


@pytest.fixture
def pose(graph_design, tmp_path, monkeypatch):
    # Legacy isolated registry fixture, with inert model files and no scientific
    # worker. Production policy and new VS studies remain unchanged.
    from covscreen.docking_policy import POLICY
    monkeypatch.setitem(POLICY, 'disabled_engines', [])
    generate(tmp_path, graph_design)
    plan_pose(tmp_path, graph_design)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    checkpoint = runtime / 'fixture.pt'
    checkpoint.write_text('Software-only checkpoint placeholder')
    config = tmp_path / 'configs/local.inspire.yaml'
    config.parent.mkdir()
    write(config, {'runtime': {'enable_ligand_smoke_dispatch': True},
                   'carsidock': {'repo': str(runtime), 'checkpoint': str(checkpoint)}})
    secret = tmp_path / 'approval.key'
    secret.write_bytes(b'fixture-key-not-a-production-key!' * 2)
    secret.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(secret))
    initial = navigate(tmp_path, 'start', request_id='pose-execution', target='EGFR', site='C797', taxon=9606)
    identifier = initial['workflow']['workflow_id']
    path = directory(tmp_path, identifier) / 'state.json'
    state = json.loads(path.read_text())
    state.update(stage='POSE_PLAN_READY', design_id=graph_design, evidence_id='hyp-' + 'b' * 24)
    write(path, state)
    step(tmp_path, DESIGN, workflow_id=identifier, expected_revision=state['revision'], action='check_pose_runtime')
    return tmp_path, identifier, path


def args(pose, action):
    return {'action': action, 'workflow_id': pose[1], 'expected_revision': json.loads(pose[2].read_text())['revision']}


def approval(pose):
    values = {**args(pose, 'start_pose'), 'human_approved': True,
              'approval_note': 'Software-only approval; no scientific worker'}
    scope = approval_scope(pose[0], DESIGN, values)
    proof = signed({'tool': DESIGN, 'arguments_json': json.dumps(values), 'scope': scope,
                    'session': 'fixture', 'message': 'fixture', 'approval_request_id': 'permission_fixture',
                    'nonce': uuid4().hex, 'expires': time.time() + 300})
    return {**values, '_native_approval': proof}


def register(manager, request):
    spec = manager._prepare_submission(request)
    result = RunResult(run_id=spec.run_id, state='QUEUED', engine=spec.engine, route=spec.route,
        project_id=spec.project_id, parent_run_id=spec.parent_run_id, attempt=spec.attempt,
        submitted_at=utc_now(), stdout_path='unused', stderr_path='unused', message='No worker fixture')
    def save(_request):
        manager.registry.create(spec, result)
        return result
    return submit_once(manager, request, save, planned_run_id=spec.run_id)


def test_boolean_cannot_start_pose(pose):
    assert needs_approval(DESIGN, {'action': 'start_pose'})
    with pytest.raises(ValueError, match='approval'):
        dispatch(pose[0], DESIGN, {**args(pose, 'start_pose'), 'human_approved': True})
    assert json.loads(pose[2].read_text())['pending'] is None


def test_reported_pose_plan_reopens_without_regenerating_candidates_or_starting_jobs(pose):
    root, identifier, path = pose
    before = json.loads(path.read_text())
    from covscreen.ligand_design import design_folder
    from covscreen.target_hypothesis import digest
    design = design_folder(root, before['design_id'])
    hashes = {name: digest(design / name) for name in
              ('manifest.json', 'generation/manifest.json', 'pose-plan/manifest.json')}
    delivered = step(root, DESIGN, **args(pose, 'deliver'))
    assert 'reopen_design' in delivered['workflow']['tools'][ENTRY]['actions']
    reopened = navigate(root, **args(pose, 'reopen_design'), reason='Runtime is now available for the same design')
    assert reopened['workflow']['stage'] == 'POSE_PLAN_READY'
    assert reopened['design_id'] == before['design_id']
    assert reopened['pose_execution'] is None
    assert 'start_pose' not in reopened['workflow']['tools'][DESIGN]['actions']
    assert (design / 'delivery/report.md').is_file()
    assert hashes == {name: digest(design / name) for name in hashes}
    step(root, DESIGN, **args(pose, 'check_pose_runtime'))
    after = json.loads(path.read_text())
    assert after['pose_execution']['request_id'] != before['pose_execution']['request_id']
    assert not list((root / 'state/jobs').glob('*/spec.json'))


def test_reopening_reported_design_rejects_changed_candidates(pose):
    from covscreen.ligand_design import design_folder
    root, identifier, path = pose
    step(root, DESIGN, **args(pose, 'deliver'))
    before = path.read_bytes()
    design = design_folder(root, json.loads(before)['design_id'])
    (design / 'generation/candidates.sdf').write_text('tampered')
    with pytest.raises(ValueError, match='changed'):
        navigate(root, **args(pose, 'reopen_design'), reason='Runtime is now available for the same design')
    assert path.read_bytes() == before


def test_signed_start_and_completion_are_not_scientific_acceptance(pose, monkeypatch):
    monkeypatch.setattr(JobManager, 'submit', register)
    result = dispatch(pose[0], DESIGN, approval(pose))
    assert result['execution']['state'] == 'submitted'
    assert result['assessment']['state'] == 'not_evaluated'
    assert result['workflow']['tools'][DESIGN]['actions'] == ['pose_status']
    run = result['execution']['task_ids'][0]
    path = pose[0] / 'state/jobs' / run / 'result.json'
    value = json.loads(path.read_text())
    value.update(state='COMPLETED', return_code=0)
    write(path, value)
    completed = dispatch(pose[0], DESIGN, args(pose, 'pose_status'))
    assert completed['execution']['state'] == 'completed'
    assert completed['assessment']['state'] == 'not_evaluated'
    assert completed['workflow']['stage'] == 'POSE_EXECUTION_REVIEW'
    assert not completed['result']['scientific_result_verified']


def test_lost_reply_reconciles_original_registration(pose, monkeypatch):
    def lost(manager, request):
        register(manager, request)
        raise ConnectionError('Fixture loses registration reply')
    monkeypatch.setattr(JobManager, 'submit', lost)
    with pytest.raises(ConnectionError):
        dispatch(pose[0], DESIGN, approval(pose))
    state = json.loads(pose[2].read_text())
    assert state['pending']['submission']['request_id'] == state['pose_execution']['request_id']
    status = navigate(pose[0], 'status', workflow_id=pose[1])
    assert status['execution']['state'] == 'outcome_unknown'
    assert 'reconcile' in status['workflow']['tools'][ENTRY]['actions']
    reconciled = dispatch(pose[0], ENTRY, args(pose, 'reconcile'))
    assert reconciled['execution']['state'] == 'submitted' and reconciled['pending'] is None
    assert len(list((pose[0] / 'state/jobs').glob('*/spec.json'))) == 1


def test_missing_receipt_remains_uncertain(pose, monkeypatch):
    def unavailable(manager, request):
        raise ConnectionError('Fixture fails before registry receipt')
    monkeypatch.setattr(JobManager, 'submit', unavailable)
    with pytest.raises(ConnectionError):
        dispatch(pose[0], DESIGN, approval(pose))
    result = dispatch(pose[0], ENTRY, args(pose, 'reconcile'))
    assert result['pending'] and result['execution']['state'] == 'outcome_unknown'
    assert not result['result']['automatic_resubmission_allowed']


def test_changed_checkpoint_rejects_prepared_approval_before_registration(pose):
    approved = approval(pose)
    (pose[0] / 'runtime/fixture.pt').write_text('Different checkpoint after user approved')
    with pytest.raises(ValueError, match='Frozen pose runtime plan changed'):
        dispatch(pose[0], DESIGN, approved)
    assert not list((pose[0] / 'state/jobs').glob('*/spec.json'))
    assert json.loads(pose[2].read_text())['pending'] is None


def test_registered_result_identity_cannot_be_substituted(pose, monkeypatch):
    monkeypatch.setattr(JobManager, 'submit', register)
    result = dispatch(pose[0], DESIGN, approval(pose))
    run = result['execution']['task_ids'][0]
    path = pose[0] / 'state/jobs' / run / 'result.json'
    value = json.loads(path.read_text())
    value['project_id'] = 'other-design'
    write(path, value)
    with pytest.raises(ValueError, match='approved workflow request'):
        dispatch(pose[0], DESIGN, args(pose, 'pose_status'))
    assert json.loads(pose[2].read_text())['stage'] == 'POSE_EXECUTION_RUNNING'


def completed_pose_files(pose, monkeypatch):
    """Artificial failed geometry from the exported graphs, never docking evidence."""
    from rdkit import Chem
    from covscreen.ligand_design import design_folder
    monkeypatch.setattr(JobManager, 'submit', register)
    response = dispatch(pose[0], DESIGN, approval(pose))
    manager = JobManager(pose[0])
    run = response['execution']['task_ids'][0]
    spec, result = manager.registry.load_spec(run), manager.registry.load_result(run)
    plan = design_folder(pose[0], spec.project_id) / 'pose-plan'
    raw, clean = [], []
    for original in Chem.SDMolSupplier(str(plan / 'ligands.sdf'), removeHs=False):
        editable = Chem.RWMol(original)
        mapping = {atom.GetAtomMapNum(): atom.GetIdx() for atom in original.GetAtoms()}
        editable.GetBondBetweenAtoms(mapping[905], mapping[906]).SetBondType(Chem.BondType.SINGLE)
        clean_mol = editable.GetMol()
        sulfur = editable.AddAtom(Chem.Atom('S'))
        editable.AddBond(mapping[906], sulfur, Chem.BondType.SINGLE)
        molecule = editable.GetMol()
        molecule.GetConformer().SetAtomPosition(sulfur, (-100, -100, -100))
        for value in (molecule, clean_mol):
            value.SetProp('_Name', original.GetProp('COMPOUND_ID') + '-0')
            value.SetProp('CarsiScore', '-1')
        raw.append(molecule)
        clean.append(clean_mol)
    paths = [manager.registry.job_dir(run) / 'artifacts' / name for name in (
        'carsidock_michael_addition.sdf', 'carsidock_michael_addition_nodumat.sdf')]
    for path, molecules in zip(paths, (raw, clean)):
        path.parent.mkdir(exist_ok=True)
        with Chem.SDWriter(str(path)) as writer:
            for molecule in molecules:
                writer.write(molecule)
        result.artifacts.append(manager.registry.register_file(run, path, 'pose'))
    result.state, result.return_code = 'COMPLETED', 0
    write(manager.registry.job_dir(run) / 'result.json', result.model_dump(mode='json'))
    return manager, spec, result, paths, plan


def test_geometry_recomputed_from_registered_files_and_tampering_invalidates(pose, monkeypatch):
    manager, spec, result, paths, plan = completed_pose_files(pose, monkeypatch)
    dispatch(pose[0], DESIGN, args(pose, 'pose_status'))
    response = dispatch(pose[0], DESIGN, args(pose, 'evaluate_pose'))
    # The ring-only generation fixture removes a protected contact atom.
    # Missing mapping is insufficient evidence, not a negative geometry verdict.
    assert response['assessment']['state'] == 'evidence_insufficient'
    assert response['assessment']['scope'] == 'mapped_product_graph_and_pose_geometry'
    evaluation = response['result']['evaluation']
    assert evaluation['candidate_count'] == spec.resource_budget.max_candidates
    assert not evaluation['md_ready'] and evaluation['passing_candidate_count'] == 0
    assert evaluation['incomplete_candidate_count'] == evaluation['candidate_count']
    assert evaluation['schema'] == 'ligand-pose-geometry-review-v2'
    assert all(row['status'] == 'NOT_EVALUATED' for row in evaluation['candidates'])
    assert all(row['assessment']['physical_validity'] == 'NOT_EVALUATED'
               and not row['assessment']['evidence_complete']
               and not row['assessment']['review_eligible'] for row in evaluation['candidates'])
    paths[0].write_text(paths[0].read_text() + '\n')
    with pytest.raises(ValueError, match='registered evidence'):
        dispatch(pose[0], DESIGN, args(pose, 'pose_status'))
    status = navigate(pose[0], 'status', workflow_id=pose[1])
    assert status['assessment']['state'] == 'evidence_insufficient'


def test_exported_identity_survives_real_carsidock_parser(pose, monkeypatch):
    import importlib.util
    from pathlib import Path
    import pandas as pd
    manager, spec, result, paths, plan = completed_pose_files(pose, monkeypatch)
    source = Path(__file__).resolve().parents[3] / 'science/scripts/parse_carsidock_scores.py'
    module_spec = importlib.util.spec_from_file_location('pose_parser_fixture', source)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    table = pd.read_csv(plan / 'ligands.csv')
    parquet = pose[0] / 'parser-fixture.parquet'
    table.to_parquet(parquet, index=False)
    prefix = paths[0].with_suffix('')
    prefix.with_name(prefix.name + '_withpckt.pdb').write_text('Fixture only; geometry not inferred')
    parsed, audit = module.parse_chunk(parquet, plan / 'ligands.sdf', prefix)
    assert audit['identity_mapping_available']
    assert parsed['compound_id'].tolist() == table['compound_id'].tolist()
    assert parsed['sdf_compound_id'].tolist() == table['compound_id'].tolist()
    assert parsed['engine_status'].eq('SUCCESS').all()  # Scores only, not geometry acceptance.


@pytest.mark.parametrize('change', ['identity', 'graph', 'paired_coordinates'])
def test_registered_but_inconsistent_pose_pairs_are_rejected(pose, monkeypatch, change):
    from rdkit import Chem
    manager, spec, result, paths, plan = completed_pose_files(pose, monkeypatch)
    path = paths[1]
    molecules = list(Chem.SDMolSupplier(str(path), removeHs=False, sanitize=False))
    if change == 'identity':
        molecules[0].SetProp('_Name', 'unrelated-compound-0')
    elif change == 'graph':
        molecules[0].GetAtomWithIdx(0).SetAtomicNum(8)
    else:
        point = molecules[0].GetConformer().GetAtomPosition(0)
        molecules[0].GetConformer().SetAtomPosition(0, (point.x + 2, point.y, point.z))
    with Chem.SDWriter(str(path)) as writer:
        for molecule in molecules:
            writer.write(molecule)
    item = manager.registry.register_file(spec.run_id, path, 'pose')
    result.artifacts = [item if old.path == item.path else old for old in result.artifacts]
    write(manager.registry.job_dir(spec.run_id) / 'result.json', result.model_dump(mode='json'))
    dispatch(pose[0], DESIGN, args(pose, 'pose_status'))
    with pytest.raises(ValueError, match='identity|topology|coordinates'):
        dispatch(pose[0], DESIGN, args(pose, 'evaluate_pose'))
    status = navigate(pose[0], 'status', workflow_id=pose[1])
    assert status['assessment']['state'] == 'evidence_insufficient'


def test_refinement_preparation_does_not_trust_a_stored_pass_label(pose, monkeypatch):
    completed_pose_files(pose, monkeypatch)
    dispatch(pose[0], DESIGN, args(pose, 'pose_status'))
    dispatch(pose[0], DESIGN, args(pose, 'evaluate_pose'))
    state = json.loads(pose[2].read_text())
    state['pose_execution']['evaluation']['passing_candidate_count'] = 1
    state['pose_execution']['evaluation']['candidates'][0]['status'] = 'PASS'
    write(pose[2], state)
    with pytest.raises(ValueError, match='geometry-passing candidates'):
        dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement'))
    assert json.loads(pose[2].read_text())['refinement_input'] is None
    assert not (pose[0] / 'results/ligand-refinement-inputs').exists()


def test_pose_report_delivers_incomplete_branch_and_recovers_without_new_job(pose, monkeypatch):
    manager, spec, result, paths, plan = completed_pose_files(pose, monkeypatch)
    dispatch(pose[0], DESIGN, args(pose, 'pose_status'))
    before = sorted((pose[0] / 'state/jobs').iterdir())
    reported = dispatch(pose[0], DESIGN, args(pose, 'report_pose'))
    binding = reported['pose_report']
    assert binding and not binding['validated_inhibitor'] and not binding['md_ready']
    assert reported['assessment']['state'] == 'evidence_insufficient'
    serialized = (pose[0] / binding['report_json']).read_text()
    assert len(serialized.splitlines()) > 2
    assert max(map(len, serialized.splitlines())) < 2000
    report = json.loads(serialized)
    assert report['schema'] == 'ligand-exploratory-pose-report-v3'
    assert 'covscreen_ligand_first.ligand_geometry_assessment' in report['implementation_sha256']
    assert all(row['independent_axes']['physical_validity'] == 'NOT_EVALUATED'
               for row in report['candidates'])
    assert report['run_id'] == spec.run_id
    assert report['candidate_count'] > 0
    assert report['exploratory_passing_candidate_count'] == 0
    assert report['incomplete_candidate_count'] > 0
    assert any(candidate['errors'] for candidate in report['candidates'])
    assert all(candidate['free_ligand']['canonical_smiles'] for candidate in report['candidates'])
    assert report['physical_acceptance'] == 'not_established'
    assert report['policy'] == json.loads((plan / 'geometry-policy.json').read_text())
    assert '旧字段名含 attack_angle' in (pose[0] / binding['report_markdown']).read_text()
    resumed = navigate(pose[0], 'status', workflow_id=pose[1])
    assert resumed['pose_report'] == binding
    repeated = dispatch(pose[0], DESIGN, args(pose, 'report_pose'))
    assert repeated['pose_report'] == binding
    assert sorted((pose[0] / 'state/jobs').iterdir()) == before
    assert not needs_approval(DESIGN, args(pose, 'report_pose'))
    # A changed registered output invalidates the report rather than serving it.
    paths[0].write_text(paths[0].read_text() + '\n')
    with pytest.raises(ValueError):
        dispatch(pose[0], DESIGN, args(pose, 'pose_status'))
    assert json.loads(pose[2].read_text())['pose_report'] is None


def test_resealed_pose_report_is_rejected_and_branch_history_retains_binding(pose, monkeypatch):
    from covscreen.ligand_design import seal
    completed_pose_files(pose, monkeypatch)
    dispatch(pose[0], DESIGN, args(pose, 'pose_status'))
    reported = dispatch(pose[0], DESIGN, args(pose, 'report_pose'))
    binding = reported['pose_report']
    path = pose[0] / binding['report_markdown']
    path.write_text('Forged claim of validated inhibition')
    seal(path.parent)
    with pytest.raises(ValueError, match='checksum mismatch'):
        dispatch(pose[0], DESIGN, args(pose, 'report_pose'))
    assert json.loads(pose[2].read_text())['pose_report'] is None
    with pytest.raises(ValueError, match='report text differs'):
        dispatch(pose[0], DESIGN, args(pose, 'report_pose'))
    # Existing branch archiving owns report invalidation as well.
    from covscreen.ligand_workflow import archive
    state = json.loads(pose[2].read_text())
    state['pose_report'] = binding
    archive(state, 'Change parent evidence')
    assert state['pose_report'] is None
    assert state['branches'][-1]['pose_report'] == binding


def test_current_metric_semantics_do_not_upgrade_legacy_gate_labels():
    from covscreen.ligand_pose_report import measurement_rows
    # Minimal legacy-format record; no historical research output is required.
    legacy = {'poses': [{'attack_angle_s_cbeta_calpha_degrees': 110.0,
                         'interaction_retention_fraction': 1.0,
                         'protected_interactions': ['legacy-contact'],
                         'gate_flags': {'attack_angle': True, 'interaction_retention': True}}],
              'policy': {}}
    rows = measurement_rows(legacy['poses'][0], legacy['policy'])
    angle = next(row for row in rows if row['legacy_field'] == 'attack_angle_s_cbeta_calpha_degrees')
    assert angle['metric_id'] == 'product_bond_geometry'
    assert angle['value'] == legacy['poses'][0]['attack_angle_s_cbeta_calpha_degrees']
    assert all(row['decision'] == 'undetermined' for row in rows)
    assert all(row['calibration_status'] == 'exploratory_default' for row in rows)
    pose = {**legacy['poses'][0], 'protected_interactions': [], 'interaction_retention_fraction': 0}
    contact = next(row for row in measurement_rows(pose, legacy['policy'])
                   if row['metric_id'] == 'protected_contact_retention')
    assert contact['measurement_status'] == 'unavailable' and contact['value'] is None


def test_older_frozen_policy_cannot_silently_inherit_new_clash_gate(pose):
    from covscreen.ligand_design import design_folder, seal
    from covscreen.ligand_pose_execution import bound_request
    state = json.loads(pose[2].read_text())
    plan = design_folder(pose[0], state['design_id']) / 'pose-plan'
    policy = json.loads((plan / 'geometry-policy.json').read_text())
    del policy['max_severe_clash_pair_count']
    write(plan / 'geometry-policy.json', policy)
    seal(plan)
    with pytest.raises(ValueError, match='omits current criteria'):
        bound_request(pose[0], state['design_id'])


def test_refinement_table_writer_preserves_original_indexes_and_shared_contract(pose):
    """Writer-only positive fixture; synthetic review is never sent to the workflow."""
    import pandas as pd
    from covscreen.ligand_design import design_folder
    from covscreen.ligand_refinement_input import freeze_refinement_input, load_refinement_input
    from covscreen.refinement_input import verify_refinement_input
    state = json.loads(pose[2].read_text())
    plan = design_folder(pose[0], state['design_id']) / 'pose-plan'
    identities = json.loads((plan / 'input-binding.json').read_text())['selected_candidate_ids']
    assert len(identities) > 1
    record = state['pose_execution']
    record.update(run_id='fixture-registered-parent', evaluation_verification='verified', evaluation={
        'output_sha256': {'fixture-only': '0' * 64}, 'candidates': [{
            'candidate_id': identities[1], 'status': 'PASS', 'errors': [],
            'selected_pose_index': 0, 'selected_pose_record_index': 7,
            'selected_pose': {'carsidock_score': -1}}]})
    result = freeze_refinement_input(pose[0], state)
    state['refinement_input'] = result
    assert load_refinement_input(pose[0], state)['source_run_id'] == record['run_id']
    import copy
    for field in ('workflow_id', 'design_id', 'evidence_id'):
        changed = copy.deepcopy(state)
        changed[field] += '-foreign'
        with pytest.raises(ValueError, match='current workflow evidence'):
            load_refinement_input(pose[0], changed)
    changed = copy.deepcopy(state)
    changed['pose_execution']['evaluation']['candidates'][0]['selected_pose_record_index'] = 8
    with pytest.raises(ValueError, match='current workflow evidence'):
        load_refinement_input(pose[0], changed)
    changed = copy.deepcopy(state)
    changed['refinement_input']['candidate_table'] = '../foreign.csv'
    with pytest.raises(ValueError, match='table path changed'):
        load_refinement_input(pose[0], changed)
    assert result == freeze_refinement_input(pose[0], state)
    assert result['input_count'] == 1 and not result['refinement_submitted'] and not result['md_ready']
    path = pose[0] / result['candidate_table']
    checked = verify_refinement_input(path, 1, result['input_sha256'], result['identity_sha256'])
    assert checked['verified'] and not checked['grants_resource_approval']
    table = pd.read_csv(path)
    assert table['input_sdf_index'].tolist() == [1]
    assert table['compound_id'].tolist() == [identities[1]]
    assert table['selected_pose_record_index'].tolist() == [7]
    assert table['covdock_reaction_type'].tolist() == ['Michael Addition']
    assert table['route'].tolist() == ['ligand-first']
    path.write_text(path.read_text() + '\n')
    with pytest.raises(ValueError, match='artifact changed'):
        freeze_refinement_input(pose[0], state)
    with pytest.raises(ValueError, match='artifact changed'):
        load_refinement_input(pose[0], state)


def test_complete_legacy_policy_reopens_without_upgrading_or_changing_bytes(pose):
    from covscreen.ligand_design import design_folder, seal, plan_pose
    from covscreen.ligand_pose_execution import bound_request
    from covscreen.jobs import sha256_file
    state = json.loads(pose[2].read_text())
    plan = design_folder(pose[0], state['design_id']) / 'pose-plan'
    policy = json.loads((plan / 'geometry-policy.json').read_text())
    del policy['version']
    del policy['calibration_status']
    del policy['ensemble_eligibility']
    write(plan / 'geometry-policy.json', policy)
    seal(plan)
    before = {str(path): sha256_file(path) for path in plan.iterdir() if path.is_file()}
    bound_request(pose[0], state['design_id'])
    plan_pose(pose[0], state['design_id'])
    assert before == {str(path): sha256_file(path) for path in plan.iterdir() if path.is_file()}
    assert json.loads((plan / 'geometry-policy.json').read_text()) == policy


@pytest.mark.parametrize('engine', ['alphafold3-mpae', 'schrodinger-covdock-lo'])
def test_frozen_ligand_candidates_compile_into_exact_engine_inputs(pose, engine):
    """Actual producer/consumer connection with a synthetic positive review only."""
    from covscreen.ligand_design import design_folder
    from covscreen.ligand_refinement_input import freeze_refinement_input
    from covscreen.jobs import sha256_file
    from test_refinement_job_contract import configured
    root = pose[0]
    state = json.loads(pose[2].read_text())
    plan = design_folder(root, state['design_id'])/'pose-plan'
    identities = json.loads((plan/'input-binding.json').read_text())['selected_candidate_ids']
    state['pose_execution'].update(run_id='fixture-registered-parent',
        evaluation_verification='verified', evaluation={
            'output_sha256': {'fixture-only': '0'*64}, 'candidates': [{
                'candidate_id': identities[1], 'status': 'PASS', 'errors': [],
                'selected_pose_index': 0, 'selected_pose_record_index': 7,
                'selected_pose': {'carsidock_score': -1}}]})
    binding = freeze_refinement_input(root, state)
    manager, template, _ = configured(root, engine)
    parameters = {**template.parameters, 'candidate_table': binding['candidate_table'],
        'expected_input_count': binding['input_count'], 'expected_input_sha256': binding['input_sha256'],
        'expected_identity_sha256': binding['identity_sha256']}
    parameters['top_n' if engine == 'alphafold3-mpae' else 'final_n'] = binding['input_count']
    spec = manager.build_spec(template.model_copy(update={'parameters': parameters,
        'resource_budget': template.resource_budget.model_copy(update={'max_candidates': binding['input_count']})}))
    assert spec.parameters['input_row_count'] == 1
    assert spec.parameters['refinement_input_contract']['identity_sha256'] == binding['identity_sha256']
    assert sha256_file(root/binding['candidate_table']) == binding['input_sha256']
    assert spec.command[spec.command.index('--expected-input-sha256')+1] == binding['input_sha256']
    assert not list((root/'state/jobs').glob('*/spec.json'))
