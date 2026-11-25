"""Real deposited 2ITY sources and graph generation; no vendor job is launched."""
import json

import pytest

from covscreen.ligand_design import generate, design_folder, write, seal
from covscreen.ligand_vs_input import freeze, load, build
from covscreen.ligand_workflow import DESIGN, navigate, directory, step, view, archive
from covscreen.mcp.server import dispatch
from covscreen.jobs import sha256_file
from covscreen.receptor_environment_review import CONCERNS
from test_refinement_target import design
from test_ligand_design import full_parent_design


@pytest.fixture
def study(tmp_path, design):
    result = generate(tmp_path, design)
    assert result['state'] == 'NEEDS_POSE_EVALUATION'
    started = navigate(tmp_path, 'start', request_id='vs-source-test', target='EGFR', site='C797', taxon=9606)
    path = directory(tmp_path, started['workflow']['workflow_id']) / 'state.json'
    state = json.loads(path.read_text())
    state.update(design_id=design, evidence_id='hyp-' + 'b'*24, stage='NEEDS_POSE_EVALUATION')
    write(path, state)
    return tmp_path, path


def call(study, action, **arguments):
    root, path = study
    state = json.loads(path.read_text())
    return dispatch(root, DESIGN, dict(action=action, workflow_id=state['workflow_id'],
        expected_revision=state['revision'], **arguments))


def test_real_study_binds_vs_without_any_old_pose_plan(study):
    root, path = study
    output = call(study, 'prepare_vs_input')
    state = json.loads(path.read_text())
    payload = load(root, state)
    assert len(payload['selected_candidate_ids']) == 3
    assert payload['source_site_mapping']['author_residue_id'] == '797'
    assert payload['attachment_residue'] == 'A:102'
    assert payload['review_requirements']['missing_construct_residue_count'] == 27
    assert payload['review_requirements']['excluded_atom_count'] == 50
    assert not payload['receptor_prepared'] and not payload['md_ready']
    assert not output['compute_submitted']
    assert not (design_folder(root, state['design_id']) / 'pose-plan').exists()
    assert not list(root.glob('state/jobs/*/spec.json'))
    assert freeze(root, state) == state['vs_input']
    assert call(study, 'read_vs_input')['result']['vs_input'] == state['vs_input']


def test_vs_reads_share_revision_without_mutating_study(study):
    root, path = study
    output = call(study, 'prepare_vs_input')
    before = path.read_bytes()
    state = json.loads(before)
    arguments = dict(action='read_vs_input', workflow_id=state['workflow_id'],
                     expected_revision=state['revision'])
    listing = dispatch(root, DESIGN, arguments)
    pdb = next(item for item in listing['result']['evidence']['documents']
               if item['path'].endswith('/source_structures/structure.pdb'))
    detail = dispatch(root, DESIGN, dict(arguments, evidence_path=pdb['path'],
                      evidence_locator='/header_records/REMARK 350'))
    assert detail['result']['evidence']['complete']
    assert path.read_bytes() == before
    assert detail['workflow']['revision'] == listing['workflow']['revision'] == output['workflow']['revision']
    assert not list(root.glob('state/jobs/*/spec.json'))


def test_construct_reader_is_source_bound_read_only_and_not_an_approval(study):
    root, path = study
    before = path.read_bytes()
    output = call(study, 'read_construct_evidence')
    evidence = output['result']['construct_evidence']
    assert evidence['schema'] == 'ligand-construct-evidence-v1'
    assert evidence['construct_audit']['source_species_verified']
    assert evidence['source_site_mapping']['uniprot_residue'] == 797
    assert not evidence['construct_approved'] and not evidence['target_drafts_created']
    assert not output['compute_submitted']
    assert path.read_bytes() == before
    assert not list(root.glob('state/jobs/*/spec.json'))
    state = json.loads(path.read_text())
    source = design_folder(root, state['design_id']) / 'case/evidence/entity_1.json'
    source.write_text('{}')
    with pytest.raises(ValueError, match='artifact changed'):
        call(study, 'read_construct_evidence')


def test_ambiguous_full_parent_products_cannot_freeze_into_vs(full_parent_design, tmp_path):
    """Real 2ITY symmetry remains a negative case, not a forced positive."""
    result = generate(tmp_path, full_parent_design)
    assert result['state'] == 'NO_GRAPH_CANDIDATES'
    assert result['candidate_count'] == 0
    generated = design_folder(tmp_path, full_parent_design) / 'generation'
    audit = json.loads((generated / 'upstream-review.json').read_text())
    rejected = [row for row in audit['inventory'] if row['disposition'] == 'mapping_or_chemistry_rejected']
    assert len(rejected) == 23
    assert all('unambiguous' in row['reason'] for row in rejected)
    started = navigate(tmp_path, 'start', request_id='upstream-vs-negative-test',
                       target='EGFR', site='C797', taxon=9606)
    path = directory(tmp_path, started['workflow']['workflow_id']) / 'state.json'
    state = json.loads(path.read_text())
    state.update(design_id=full_parent_design, evidence_id='hyp-' + 'b'*24,
                 stage='NO_GRAPH_CANDIDATES')
    write(path, state)
    with pytest.raises(ValueError, match='VS planning requires'):
        call((tmp_path, path), 'prepare_vs_input')
    with pytest.raises(ValueError, match='verified nonempty graph'):
        build(tmp_path, state)
    assert not list(tmp_path.glob('state/jobs/*/spec.json'))


def test_historical_rejection_is_preserved_and_does_not_block_new_inputs(study):
    root, path = study
    state = json.loads(path.read_text())
    old = {'run_id': 'historical-pose', 'job_state': 'COMPLETED', 'return_code': 0,
           'evaluation': {'status': 'REJECTED', 'passing_candidate_count': 0}}
    state.update(stage='POSE_EXECUTION_REVIEW', pose_execution=old)
    write(path, state)
    output = call(study, 'prepare_vs_input')
    assert json.loads(path.read_text())['pose_execution'] == old
    assert output['assessment']['scope'] == 'covdock_vs_environment'
    assert output['assessment']['state'] == 'review_required'
    assert output['execution']['state'] == 'not_submitted'
    assert output['execution']['task_ids'] == []


def test_vs_rejects_resealed_candidate_with_missing_upstream_source(study):
    root, path = study
    state = json.loads(path.read_text())
    generated = design_folder(root, state['design_id']) / 'generation'
    record_path = generated / 'candidates.json'
    payload = json.loads(record_path.read_text())
    payload['candidates'][0]['provenance'] = 'upstream-covalentizer-full-parent-v1'
    write(record_path, payload)
    seal(generated)
    with pytest.raises(ValueError, match='source evidence'):
        call(study, 'prepare_vs_input')
    assert not list(root.glob('state/jobs/*/spec.json'))


def test_web_review_can_read_real_bound_fields_without_historical_output_overflow(study):
    root, path = study
    state = json.loads(path.read_text())
    old = {'run_id': 'historical-pose', 'job_state': 'COMPLETED', 'return_code': 0,
           'evaluation': {'status': 'REJECTED', 'passing_candidate_count': 0,
                          'raw_poses': ['historical coordinates' * 10000]}}
    state.update(stage='POSE_EXECUTION_REVIEW', pose_execution=old)
    write(path, state)
    call(study, 'prepare_vs_input')
    output = call(study, 'read_vs_input')
    assert len(json.dumps(output)) < 30000
    assert json.loads(path.read_text())['pose_execution'] == old
    assert output['pose_execution']['assessment_summary']['status'] == 'REJECTED'
    assert 'evidence_path' in output['workflow']['tools'][DESIGN]['fields']
    pdb = next(item for item in output['result']['evidence']['documents']
               if item['path'].endswith('/source_structures/structure.pdb'))
    assembly = call(study, 'read_vs_input', evidence_path=pdb['path'],
                    evidence_locator='/header_records/REMARK 350')['result']['evidence']
    declared = next(row for row in assembly['value'] if 'AUTHOR DETERMINED' in row['text'])
    assert 'MONOMERIC' in declared['text']
    assert (root / pdb['path']).read_text().splitlines()[declared['line_number'] - 1] == declared['text']
    document = next(item for item in output['result']['evidence']['documents']
                    if item['path'].endswith('/target-source.json'))
    page = call(study, 'read_vs_input', evidence_path=document['path'],
                evidence_locator='/missing_construct_residue_count')['result']['evidence']
    assert page['complete'] and page['value'] == 27 and page['sha256'] == document['sha256']
    # A citation can be created from the actual returned node, with no direct
    # access to the remote filesystem and no guessed source pointer.
    review = assessment(output)
    for item in review.values():
        item['evidence'] = [{key: page[key] for key in ('path', 'sha256', 'locator')} |
                            {'observation': 'The bound source records 27 unresolved construct residues.'}]
    reviewed = call(study, 'review_vs_environment', environment_review=review)
    assert reviewed['result']['environment_review']['cited_values']['assembly'][0]['value'] == 27
    assert not reviewed['compute_submitted']
    review['assembly']['evidence'] = [{key: assembly[key] for key in ('path', 'sha256', 'locator')} |
        {'observation': 'The deposited PDB declares a monomeric biological unit; this is a source declaration.'}]
    reviewed = call(study, 'review_vs_environment', environment_review=review)
    assert reviewed['result']['environment_review']['cited_values']['assembly'][0]['value'] == assembly['value']
    assert reviewed['vs_environment_review']['decision'] == 'evidence_insufficient'
    assert not list(root.glob('state/jobs/*/spec.json'))
    with pytest.raises(ValueError, match='bound inventory'):
        call(study, 'read_vs_input', evidence_path='configs/local.inspire.yaml')
    with pytest.raises(ValueError):
        call(study, 'check_vs_runtime', evidence_path=document['path'])


@pytest.mark.parametrize('change', ['target', 'source', 'candidate', 'unsealed', 'new_evidence', 'projection', 'cross_study'])
def test_recovery_rejects_changed_sources_or_inputs(study, change):
    root, path = study
    call(study, 'prepare_vs_input')
    state = json.loads(path.read_text())
    design = design_folder(root, state['design_id'])
    if change == 'target':
        state['target']['taxon'] = 10090
    elif change == 'source':
        (design/'case/source_structures/structure.pdb').write_text('changed')
    elif change == 'candidate':
        (design/'generation/candidates.sdf').write_text('changed')
    elif change == 'unsealed':
        manifest = design/'manifest.json'
        data = json.loads(manifest.read_text())
        del data['files']['uniprot.json']
        write(manifest, data)
    elif change == 'new_evidence':
        write(root/'results/target-hypotheses'/state['evidence_id']/'successor.json',
              {'evidence_id':'hyp-'+'c'*24})
    elif change == 'projection':
        (root/state['vs_input']['directory']/'receptor-projection.pdb').write_text('changed')
    else:
        state['workflow_id'] = 'ligand-flow-'+'f'*24
    with pytest.raises((ValueError, KeyError)):
        load(root, state)


@pytest.mark.parametrize('pending,job_state', [(True,None), (False,'RUNNING'), (False,'QUEUED'), (False,None)])
def test_no_fork_from_pending_or_uncertain_computation(study, pending, job_state):
    root, path = study
    state = json.loads(path.read_text())
    if pending:
        state['pending'] = {'action':'start_pose'}
    else:
        state['pose_execution'] = {'run_id':'original', 'job_state':job_state}
    write(path, state)
    assert 'prepare_vs_input' not in view(state)['tools'].get(DESIGN, {}).get('actions', [])
    with pytest.raises(ValueError, match='idle current Ligand'):
        step(root, DESIGN, action='prepare_vs_input', workflow_id=state['workflow_id'],
             expected_revision=state['revision'])


def test_missing_runtime_is_a_configuration_wait_not_compute(study):
    call(study, 'prepare_vs_input')
    output = call(study, 'check_vs_runtime')
    assert output['vs_runtime']['state'] == 'WAITING_RUNTIME_CONFIGURATION'
    assert not output['result']['compute_submitted']
    assert not json.loads(study[1].read_text()).get('vs_plan')


def configure(root):
    vendor = root/'vendor'
    (vendor/'utilities').mkdir(parents=True)
    for name in ('run','covalent_docking','ligprep','utilities/prepwizard','test.lic'):
        (vendor/name).write_text('software-only compiler fixture, never executed')
    config = root/'configs/local.inspire.yaml'
    config.parent.mkdir(exist_ok=True)
    write(config, {'schrodinger': {'enabled':True,'root':str(vendor),'license_file':str(vendor/'test.lic')}})
    return vendor


@pytest.mark.parametrize('instance', [None, 'asgbie3'])
def test_private_plan_compiles_with_frozen_source_and_reopens_idempotently(study, instance):
    root, path = study
    vendor = configure(root)
    if instance:
        config = root/'configs/local.inspire.yaml'
        settings = json.loads(config.read_text())
        settings['schrodinger']['execution_instance'] = instance
        write(config, settings)
    call(study, 'prepare_vs_input')
    output = call(study, 'check_vs_runtime')
    assert output['vs_runtime']['state'] == 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED'
    assert output['vs_runtime']['resource_budget']['gpu_count'] == 0
    assert output['vs_runtime'].get('execution_instance') == instance
    assert output['vs_runtime'].get('execution_blocker') == ('INSTANCE_EXECUTOR_NOT_ENABLED' if instance else None)
    message = output['vs_runtime'].get('execution_blocker_message')
    if instance:
        assert instance in message and 'no per-task memory cap' in message
        assert 'No compute was submitted' in message
    else:
        assert message is None
    assert output['vs_runtime']['worker_backend'] == ('external-instance' if instance else 'external-hpc')
    assert not output['vs_runtime']['native_execution_available']
    assert str(vendor) not in json.dumps(output)
    state = json.loads(path.read_text())
    frozen = state['vs_plan']
    private = root/frozen['path']
    assert private.stat().st_mode & 0o777 == 0o600
    spec = json.loads(private.read_text())
    assert spec['parameters']['vs_preparation']['plan'].get('execution_instance') == instance
    assert spec['parameters']['vs_preparation']['plan']['source']['workflow_id'] == state['workflow_id']
    assert spec['parameters']['vs_preparation']['plan']['attachment_residue'] == 'A:102'
    assert sha256_file(private) == frozen['sha256']
    call(study, 'check_vs_runtime')
    assert json.loads(path.read_text())['vs_plan'] == frozen
    if instance:
        call(study, 'review_vs_environment', environment_review=assessment(output, 'suitable_for_exploratory_vs'))
        output = call(study, 'check_vs_runtime')
        assert output['vs_runtime']['state'] == 'VS_PLAN_COMPILED'
        actions = view(json.loads(path.read_text()))['tools'][DESIGN]['actions']
        assert 'read_vs_input' in actions
        assert 'start_vs' not in actions
    assert not list(root.glob('state/jobs/*/spec.json'))


def test_reconsider_archives_vs_with_its_design(study):
    call(study, 'prepare_vs_input')
    state = json.loads(study[1].read_text())
    before = state['vs_input']
    archive(state, 'Change evidence scope')
    assert state['vs_input'] is None and state['design_id'] is None
    assert len(state['branches']) == 1
    assert state['branches'][0]['vs_input'] == before
    assert state['branches'][0]['design_id']


def assessment(output, decision='evidence_insufficient'):
    inventory = output['result']['evidence_inventory']
    path = next(name for name in inventory if name.endswith('/target-source.json'))
    return {name: {'decision': decision,
        'rationale': 'Controlled software test of a bound assessment; this is not scientific endorsement.',
        'evidence': [{'path':path, 'sha256':inventory[path], 'locator':'/missing_construct_residue_count',
            'observation':'The recorded missing residue count is explicitly available for independent review.'}],
        'limitations':['Software fixture only; context suitability and experimental activity remain unverified.']}
        for name in CONCERNS}


def test_vs_review_retires_plan_and_binds_new_decision_without_launch(study):
    root, path = study
    configure(root)
    input_output = call(study, 'prepare_vs_input')
    call(study, 'check_vs_runtime')
    previous = json.loads(path.read_text())['vs_plan']
    output = call(study, 'review_vs_environment', environment_review=assessment(input_output))
    state = json.loads(path.read_text())
    assert not state.get('vs_plan')
    assert state['vs_plan_history'][-1]['vs_plan'] == previous
    assert output['vs_environment_review']['decision'] == 'evidence_insufficient'
    call(study, 'check_vs_runtime')
    negative = json.loads(path.read_text())['vs_plan']
    assert negative != previous
    ref = output['vs_environment_review']
    spec = json.loads((root/negative['path']).read_text())
    assert spec['input_sha256'][ref['path']] == ref['manifest_sha256']
    call(study, 'review_vs_environment', environment_review=assessment(input_output))
    assert json.loads(path.read_text())['vs_plan'] == negative
    positive = assessment(input_output, 'suitable_for_exploratory_vs')
    reviewed = call(study, 'review_vs_environment', environment_review=positive)
    assert reviewed['result']['environment_review']['review_scope'] == 'covdock-vs'
    output = call(study, 'check_vs_runtime')
    assert output['vs_runtime']['state'] == 'VS_PLAN_COMPILED'
    assert output['vs_runtime']['environment_eligible_for_exploratory_vs']
    assert not output['vs_runtime']['native_execution_available']
    assert not list(root.glob('state/jobs/*/spec.json'))
    ref = output['vs_environment_review']
    (root/ref['path']).write_text('{}')
    resumed = navigate(root, 'status', workflow_id=json.loads(path.read_text())['workflow_id'])
    assert resumed['vs_environment_review'] is None and resumed['vs_runtime'] is None
    assert resumed['vs_review_error']


def test_review_rejects_wrong_scope_wrong_citation_and_arbitrary_runtime_arguments(study):
    output = call(study, 'prepare_vs_input')
    review = assessment(output, 'suitable_for_exploratory_refinement')
    with pytest.raises(ValueError, match='Invalid environment judgment'):
        call(study, 'review_vs_environment', environment_review=review)
    review = assessment(output)
    review['assembly']['evidence'][0]['locator'] = '/fabricated'
    with pytest.raises(ValueError, match='locator'):
        call(study, 'review_vs_environment', environment_review=review)
    with pytest.raises(ValueError):
        call(study, 'check_vs_runtime', environment_review=assessment(output))


def test_reopen_missing_reason_reports_exact_field_before_state_access(tmp_path):
    import pytest
    from covscreen.ligand_workflow import navigate
    with pytest.raises(ValueError) as error:
        navigate(tmp_path, action="reopen_design", workflow_id="unused", expected_revision=44)
    assert "missing=['reason']" in str(error.value)
    assert "unexpected=[]" in str(error.value)


def test_retire_plan_preserves_history_after_branch_invalidation():
    from covscreen.ligand_vs_input import retire_plan
    state = {'vs_plan_history': None, 'vs_plan': {'id': 'first'}}
    retire_plan(state, 'replacement')
    assert state['vs_plan_history'][0]['vs_plan'] == {'id': 'first'}
    state['vs_plan'] = {'id': 'second'}
    retire_plan(state, 'another replacement')
    assert [row['vs_plan']['id'] for row in state['vs_plan_history']] == ['first', 'second']
    malformed = {'vs_plan_history': {}, 'vs_plan': {'id': 'keep'}}
    with pytest.raises(ValueError, match='history must be a list or null'):
        retire_plan(malformed, 'reject')
    assert malformed['vs_plan'] == {'id': 'keep'}
