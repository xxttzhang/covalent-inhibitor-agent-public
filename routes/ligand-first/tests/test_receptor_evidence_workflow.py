"""Historical 1M17 coordinates through real Ligand loaders and shared audits.

The study, positive parent-pose/construct review and completed task are fixtures.
Historical repair/relaxation files are copied into an isolated test registry;
no scientific worker is launched, and this is not a new preparation experiment.
The historical construct has five unmapped terminal positions: production
refinement rejects it. Only downstream lifecycle tests isolate that upstream
decision; a separate test proves the real rejection remains in force.
"""
import json
import os
from pathlib import Path
import shutil

import pytest

from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.ligand_design import POLICY, design_folder, protection, seal, write
from covscreen.ligand_first_inputs import load_campaign, build_parent_pose_mol
from covscreen.ligand_workflow import DESIGN, navigate
from covscreen.mcp.server import dispatch
from covscreen.native_case import prepare_native_case
from covscreen.receptor_admission import admit_isolated_receptor
from covscreen.receptor_input import load_receptor_preparation_input
from covscreen.structure_site_audit import audit_structure_site
from covscreen import ligand_refinement_target
from test_receptor_execution_workflow import (graph_design, pose, inputs, bound, planned_receptor,
                                             isolated, approval, args, record, jobs)


ROOT = Path(__file__).resolve().parents[3]
HISTORY = Path(os.environ.get('COVSCREEN_RECEPTOR_FIXTURE_ROOT', '/missing-external-receptor-fixture'))
ACQUISITION = HISTORY / 'state/target_acquisition/3fe7754be1b14546a7141aa26cebde6b'
PIPELINE = HISTORY / 'state/jobs/20261014T072946-library-first-receptor-preparation-cd6429155a/artifacts/receptor-preparation'
CONSTRUCT_SCOPE = ligand_refinement_target.require_construct_scope


@pytest.fixture
def design(tmp_path, monkeypatch, request):
    origin = json.loads((HISTORY / 'fixture-origin.json').read_text())
    for item in origin['files']:
        path = HISTORY / item['path']
        if not path.resolve().is_relative_to(HISTORY.resolve()) or sha256_file(path) != item['sha256']:
            raise ValueError('External receptor fixture changed')
    def controlled_upstream_review(construct):
        assert construct['unaligned_entity_positions'] == [1, 2, 3, 4, 5]
        # Software-only prerequisite, like the positive parent-pose fixture.
        # Actual source sequence, coordinate hashes and all downstream quality
        # calculations are preserved; this is not a new production tag policy.
        # No scientific metadata are edited: the actual unmapped positions
        # remain in every inventory and are rechecked by the shared loader.
    monkeypatch.setattr(ligand_refinement_target, 'require_construct_scope', controlled_upstream_review)
    source = tmp_path / 'historical-input'
    source.mkdir()
    acquired = json.loads((ACQUISITION / 'target_acquisition.json').read_text())['acquisition']
    for item in acquired['sources']:
        shutil.copyfile(ACQUISITION / item['path'], source / item['path'])
    shutil.copyfile(ROOT / 'science/tests/fixtures/AQ4.cif', source / 'component.cif')
    acquired['sources'].append({'path': 'component.cif', 'sha256': sha256_file(source / 'component.cif'),
                               'url': 'test:existing-CCD-fixture'})
    acquired['plan'].update(component='AQ4', expected_inchikey='AAKJLRGGTJKAMG-UHFFFAOYSA-N')
    acquired['coordinate_audit'] = audit_structure_site(source / 'structure.pdb', acquired['mappings'][0], 'AQ4')
    # Retain the historical explicit choices, never infer conformers or change
    # deposited coordinates to make the fixture pass.
    acquired['receptor_altlocs'] = json.loads((PIPELINE / 'heavy/inventory.json').read_text())['altlocs']
    write(source / 'structure_acquisition.json', acquired)
    identifier = 'design-' + 'a' * 24
    out = design_folder(tmp_path, identifier)
    out.mkdir(parents=True)
    identity = json.loads((HISTORY / 'state/target_identity/b2da731ddc46453da2b996bebdfae971/uniprot.json').read_text())['results'][0]
    write(out / 'uniprot.json', identity)
    prepare_native_case(source, out / 'case', campaign_id=identifier, gene='EGFR', taxon=9606,
        ligand_name='erlotinib', noncovalent_parent_assertion='Historical-source software fixture only',
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
    write(evidence / 'evidence.json', {'target': getattr(request, 'param', 'EGFR'), 'site': 'C797', 'taxon': 9606,
        'identity': {'uniprot_id': 'P00533', 'sequence': identity['sequence']['value']},
        'literature': [], 'structures': [], 'existing_therapies': [],
        'fixture': 'Historical 1M17 sources, synthetic study; no current scientific computation'})
    seal(evidence)
    write(out / 'design.json', {'design_id': identifier, 'evidence_id': evidence_id,
        'state': 'DESIGN_INPUTS_READY', 'policy': POLICY,
        'evidence_manifest_sha256': sha256_file(evidence / 'manifest.json')})
    seal(out)
    return identifier


def test_ambiguous_receptor_requires_complete_explicit_sealed_choices(design, tmp_path):
    from covscreen.ligand_refinement_target import target_drafts
    case = design_folder(tmp_path, design) / 'case'
    identity = json.loads((case.parent / 'uniprot.json').read_text())
    path = case / 'evidence/structure_acquisition.json'
    original = json.loads(path.read_text())
    assert original['receptor_altlocs'] == {'751': 'A', '831': 'A'}
    for choices in (None, {'751': 'A'}, {'751': 'Z', '831': 'A'}):
        write(path, {**original, 'receptor_altlocs': choices})
        with pytest.raises(ValueError, match='alternate location'):
            target_drafts(case, identity)


def test_real_unmapped_terminal_sequence_still_requires_review(design, tmp_path, monkeypatch):
    monkeypatch.setattr(ligand_refinement_target, 'require_construct_scope', CONSTRUCT_SCOPE)
    case = design_folder(tmp_path, design) / 'case'
    identity = json.loads((case.parent / 'uniprot.json').read_text())
    with pytest.raises(ValueError, match='explicit sequence/variant review'):
        ligand_refinement_target.target_drafts(case, identity)


def test_real_unmapped_construct_can_be_read_without_bypassing_its_gate(design, tmp_path, monkeypatch):
    monkeypatch.setattr(ligand_refinement_target, 'require_construct_scope', CONSTRUCT_SCOPE)
    case = design_folder(tmp_path, design) / 'case'
    identity = json.loads((case.parent / 'uniprot.json').read_text())
    evidence = ligand_refinement_target.target_drafts(case, identity, construct_evidence_only=True)
    assert evidence['construct_audit']['unaligned_entity_positions'] == [1, 2, 3, 4, 5]
    segment, = evidence['construct_audit']['unaligned_segments']
    assert segment['location_relative_to_alignment'] == 'N_terminal'
    assert segment['origin'] == 'not_assigned'
    assert evidence['blockers'] == ['Refinement construct needs an explicit sequence/variant review']
    assert not evidence['construct_approved'] and not evidence['target_drafts_created']
    assert not evidence['compute_submitted']
    with pytest.raises(ValueError, match='explicit sequence/variant review'):
        ligand_refinement_target.target_drafts(case, identity)


def test_source_bound_terminal_review_retains_sequence_and_rejects_stale_evidence(design, tmp_path, monkeypatch):
    from covscreen.construct_audit import validate_construct_review
    monkeypatch.setattr(ligand_refinement_target, 'require_construct_scope', CONSTRUCT_SCOPE)
    case = design_folder(tmp_path, design) / 'case'
    identity = json.loads((case.parent / 'uniprot.json').read_text())
    evidence = ligand_refinement_target.target_drafts(case, identity, construct_evidence_only=True)
    submission = {'evidence_sha256': evidence['evidence_sha256'], 'decision': 'retain_deposited_construct',
        'rationale': 'Software fixture judgment: retain the entire deposited sequence without claiming canonical identity of the unmapped segment.',
        'limitations': ['Test judgment only; no scientific or receptor acceptance is demonstrated.'],
        'segments': [{'locator': '/construct_audit/unaligned_segments/0',
            'observation': 'The source audit records five N-terminal unaligned residues.',
            'rationale': 'Retain their deposited identities in this software fixture; do not infer a harmless tag.'}]}
    review = validate_construct_review(evidence, submission)
    from covscreen.receptor_conformers import validate_selection
    selected = validate_selection(evidence['receptor_conformers'], {
        'evidence_sha256': evidence['receptor_conformers']['evidence_sha256'],
        'choices': {'751': 'B', '831': 'A'}, 'rationale': 'Software fixture alternate selection',
        'limitations': 'Not scientific acceptance'})
    selected_drafts = ligand_refinement_target.target_drafts(
        case, identity, construct_review=review, conformer_selection=selected)
    assert json.loads(selected_drafts['receptor-inventory.json'])['altlocs'] == {'751': 'B', '831': 'A'}
    assert json.loads(selected_drafts['target-source.json'])['conformer_selection'] == selected
    stale = json.loads(json.dumps(selected))
    stale['selection']['evidence_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='Conformer evidence changed'):
        ligand_refinement_target.target_drafts(case, identity, construct_review=review, conformer_selection=stale)
    drafts = ligand_refinement_target.target_drafts(case, identity, construct_review=review)
    receipt = json.loads(drafts['target-source.json'])
    assert receipt['construct_review'] == review
    assert receipt['construct_length'] == evidence['construct_audit']['construct_length']
    assert receipt['construct_sequence_sha256'] == evidence['construct_audit']['construct_sequence_sha256']
    assert receipt['projected_site'] == evidence['source_site_mapping']['entity_seq_id']
    assert not receipt['receptor_prepared'] and not receipt['grants_resource_approval']
    assert not receipt['scientific_suitability_verified']
    bad = {**submission, 'evidence_sha256': '0' * 64}
    with pytest.raises(ValueError, match='evidence changed'):
        validate_construct_review(evidence, bad)
    with pytest.raises(ValueError, match='exactly once'):
        validate_construct_review(evidence, {**submission, 'segments': []})
    insufficient = validate_construct_review(evidence, {**submission, 'decision': 'evidence_insufficient'})
    with pytest.raises(ValueError, match='current retain'):
        ligand_refinement_target.target_drafts(case, identity, construct_review=insufficient)
    from copy import deepcopy
    from covscreen.construct_audit import construct_evidence_digest
    for change in ('internal', 'unknown', 'species', 'variant'):
        changed = deepcopy(evidence)
        audit = changed['construct_audit']
        if change == 'internal':
            audit['unaligned_segments'][0]['location_relative_to_alignment'] = 'internal'
        elif change == 'unknown':
            audit['unaligned_segments'][0]['nonstandard_entity_positions'] = [1]
        elif change == 'species':
            audit['source_species_verified'] = False
        else:
            audit['sequence_differences'] = [{'entity_position': 108, 'construct_residue': 'S'}]
        current = {**submission, 'evidence_sha256': construct_evidence_digest(changed)}
        with pytest.raises(ValueError, match='cannot admit'):
            validate_construct_review(changed, current)


def test_full_parent_design_and_explicit_construct_review_reach_vs_input(design, tmp_path, monkeypatch):
    """Source/generator/dispatcher integration; reviewer judgment is a fixture."""
    from covscreen.ligand_design import FULL_PARENT_POLICY, upstream_assets, generate
    from covscreen.ligand_workflow import directory
    from covscreen.ligand_vs_input import load as load_vs
    repository = os.environ.get('COVALENTIZER_TEST_REPO')
    if not repository:
        pytest.skip('Explicit upstream checkout required')
    monkeypatch.setenv('COVALENTIZER', repository)
    monkeypatch.setattr(ligand_refinement_target, 'require_construct_scope', CONSTRUCT_SCOPE)
    out = design_folder(tmp_path, design)
    campaign = load_campaign(out / 'case/campaign.json').model_copy(update={'planning_mode': 'upstream-native'})
    (out / 'case/campaign.json').write_text(campaign.model_dump_json())
    data = json.loads((out / 'design.json').read_text())
    acquisition_path = out / 'case/evidence/structure_acquisition.json'
    acquisition = json.loads(acquisition_path.read_text())
    acquisition.pop('receptor_altlocs')
    write(acquisition_path, acquisition)
    data.update(policy=FULL_PARENT_POLICY, upstream_assets=upstream_assets())
    write(out / 'design.json', data)
    seal(out)
    generated = generate(tmp_path, design)
    assert generated['candidate_count'] == 4
    started = navigate(tmp_path, 'start', request_id='source-bound-construct-test',
                       target='EGFR', site='C797', taxon=9606)
    path = directory(tmp_path, started['workflow']['workflow_id']) / 'state.json'
    state = json.loads(path.read_text())
    state.update(stage='NEEDS_POSE_EVALUATION', design_id=design, evidence_id='hyp-' + 'b'*24)
    write(path, state)
    def call(action, **arguments):
        current = json.loads(path.read_text())
        return dispatch(tmp_path, DESIGN, dict(action=action, workflow_id=current['workflow_id'],
            expected_revision=current['revision'], **arguments))
    with pytest.raises(ValueError, match='alternate location'):
        call('prepare_vs_input')
    evidence = call('read_construct_evidence')['result']['construct_evidence']
    submission = {'evidence_sha256': evidence['evidence_sha256'], 'decision': 'retain_deposited_construct',
        'rationale': 'Software-only review judgment retains the entire deposited construct, without claiming terminal residues are harmless.',
        'limitations': ['This test does not establish scientific suitability; receptor preparation and independent approval remain required.'],
        'segments': [{'locator': '/construct_audit/unaligned_segments/0',
            'observation': 'Five unaligned residues occupy the deposited N-terminal segment.',
            'rationale': 'Retain their original sequence and numbering in this software fixture; no clipping is authorized.'}]}
    conformers = evidence['receptor_conformers']
    selection = {'evidence_sha256': conformers['evidence_sha256'],
        'choices': {'751': 'A', '831': 'A'}, 'rationale': 'Software fixture explicit selection',
        'limitations': 'Not scientific acceptance'}
    selected = call('select_receptor_conformers', conformer_selection=selection)
    assert not selected['compute_submitted']
    unchanged = path.read_bytes()
    call('select_receptor_conformers', conformer_selection=selection)
    assert path.read_bytes() == unchanged
    reviewed = call('review_construct', construct_review=submission)
    assert not reviewed['compute_submitted']
    before = path.read_bytes()
    call('review_construct', construct_review=submission)
    assert path.read_bytes() == before
    call('prepare_vs_input')
    state = json.loads(path.read_text())
    payload = load_vs(tmp_path, state)
    assert len(payload['selected_candidate_ids']) == 3
    assert len(payload['deferred_candidate_ids']) == 1
    assert not payload['receptor_prepared'] and not payload['md_ready']
    with pytest.raises(ValueError, match='scope is frozen'):
        call('review_construct', construct_review=submission)
    with pytest.raises(ValueError, match='Conformers are frozen'):
        call('select_receptor_conformers', conformer_selection=selection)
    assert not list(tmp_path.glob('state/jobs/*/spec.json'))


def register_outputs(manager, run, pipeline):
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'COMPLETED', 0
    result.message = 'SOFTWARE FIXTURE: copied historical output; no new repair or relaxation'
    result.artifacts = [manager.registry.register_file(run, path, 'other')
                        for path in sorted(pipeline.rglob('*')) if path.is_file()]
    manager.registry.write_result(result)


@pytest.fixture
def completed(isolated):
    case, launches = isolated
    response = dispatch(case[0], DESIGN, approval(case))
    run = record(response)['run_id']
    manager = JobManager(case[0])
    folder = manager.registry.job_dir(run) / 'artifacts/receptor-preparation'
    folder.mkdir(parents=True)
    # Reuse the actual historical heavy/relaxation coordinates unchanged.
    for name in ('heavy', 'relaxation'):
        shutil.copytree(PIPELINE / name, folder / name)
    for name in ('identity-quality.json', 'geometry-quality.json'):
        shutil.copyfile(PIPELINE / name, folder / name)
    binding = response['refinement_input']['receptor_input']
    loaded = load_receptor_preparation_input(case[0], binding['artifact_id'], binding['manifest_sha256'])
    assert sha256_file(loaded['acquisition_directory'] / 'structure.pdb') == json.loads(
        (folder / 'relaxation/receipt.json').read_text())['source_sha256']
    admit_isolated_receptor(loaded['acquisition_directory'], binding['manifest_sha256'],
        loaded['canonical_sequence'], loaded['taxon'], folder / 'heavy', folder / 'relaxation',
        sha256_file(folder / 'relaxation/receipt.json'), folder / 'identity-quality.json',
        sha256_file(folder / 'identity-quality.json'), folder / 'geometry-quality.json',
        sha256_file(folder / 'geometry-quality.json'), folder / 'prepared')
    spec = manager.registry.load_spec(run)
    pipeline = {'schema': 'receptor-preparation-pipeline-v1', 'status': 'prepared_isolated_protein_scope',
        'target_acquisition_sha256': binding['manifest_sha256'], 'target_artifact_id': loaded['target_artifact_id'],
        'input_source_kind': loaded['source_kind'], 'input_source_route': loaded['source_route'],
        'prepared_manifest_sha256': sha256_file(folder / 'prepared/prepared-receptor.json'),
        **{key: spec.parameters[key] for key in ('altlocs', 'heavy_seed', 'max_missing_residues')},
        'artifacts': {str(path.relative_to(folder)): sha256_file(path) for path in folder.rglob('*') if path.is_file()},
        'test_fixture': 'Historical coordinates in a synthetic completed task; no new scientific run'}
    write(folder / 'pipeline.json', pipeline)
    register_outputs(manager, run, folder)
    dispatch(case[0], DESIGN, args(case, 'receptor_preparation_status'))
    return case, launches, folder, manager, run


def test_real_shared_quality_replay_and_report_preserve_scientific_scope(completed, monkeypatch):
    case, launches, folder, manager, run = completed
    before = jobs(case)
    response = dispatch(case[0], DESIGN, args(case, 'report_receptor'))
    evaluation = record(response)['evaluation']
    assert record(response)['evaluation_verification'] == 'verified'
    assert evaluation['result']['identity']['canonical_cys'] == 797
    assert evaluation['receptor_prepared'] is True
    assert evaluation['prepared_scope'] == 'isolated_selected_chain_protein'
    assert not evaluation['md_ready'] and not evaluation['covdock_ready']
    assert evaluation['environment_suitability'] == 'not_established'
    assert response['assessment']['state'] == 'review_required'
    report = record(response)['report']
    serialized = (case[0] / report['report_json']).read_text()
    assert len(serialized.splitlines()) > 2
    assert max(map(len, serialized.splitlines())) < 2000
    assert json.loads(serialized)['evaluation'] == evaluation
    assert '组装体' in (case[0] / report['report_markdown']).read_text()
    assert 'Å²' in (case[0] / report['report_markdown']).read_text()
    def unexpected_replay(*args):
        raise ValueError('Explicit replay boundary reached')
    monkeypatch.setattr('covscreen.receptor_evidence.replay', unexpected_replay)
    def snapshot():
        return {str(p): p.read_bytes() for base in ('state', 'results')
                for p in (case[0] / base).rglob('*') if p.is_file() and not p.name.endswith('.lock')}
    saved = snapshot()
    for _ in range(2):
        readback = dispatch(case[0], DESIGN, args(case, 'receptor_preparation_status'))
        assert record(readback)['evaluation'] == evaluation
        assert record(readback)['report'] == report
        assert record(readback)['receptor_prepared'] is True
        assert snapshot() == saved
    restored = navigate(case[0], 'resume', workflow_id=case[1])
    assert snapshot() == saved
    assert record(restored)['report'] == report
    assert record(restored)['evaluation'] == evaluation
    assert record(restored)['run_id'] == run
    assert jobs(case) == before and len(launches) == 1
    # Invalidating a published report removes it from current guidance but
    # retains the scientific witness and task identity.
    (case[0] / report['report_markdown']).write_text('changed report')
    rejected = navigate(case[0], 'status', workflow_id=case[1])
    assert rejected['assessment']['state'] == 'evidence_insufficient'
    assert 'report' not in record(rejected)
    assert record(rejected)['evaluation'] == evaluation
    assert record(rejected)['run_id'] == run
    # An explicit evaluation still requests the actual auditor, not the cache.
    with pytest.raises(ValueError, match='Explicit replay boundary'):
        dispatch(case[0], DESIGN, args(case, 'evaluate_receptor'))
    witness = record(rejected)['evaluation_artifact']
    (case[0] / witness['path']).write_text('{}')
    rejected = navigate(case[0], 'status', workflow_id=case[1])
    assert record(rejected)['evaluation_verification'] == 'evidence_rejected'
    assert record(rejected)['evaluation'] == evaluation


@pytest.mark.parametrize('change', ['exit_code', 'unregistered_output', 'source_route', 'forged_quality'])
def test_invalid_receipts_or_false_quality_do_not_become_acceptance(completed, change):
    case, launches, folder, manager, run = completed
    if change == 'exit_code':
        result = manager.registry.load_result(run)
        result.return_code = 1
        manager.registry.write_result(result)
    elif change == 'unregistered_output':
        result = manager.registry.load_result(run)
        result.artifacts = [item for item in result.artifacts if not item.path.endswith('/prepared-receptor.pdb')]
        manager.registry.write_result(result)
    else:
        path = folder / 'pipeline.json'
        value = json.loads(path.read_text())
        if change == 'source_route':
            value['input_source_route'] = 'library-first'
        else:
            quality = folder / 'identity-quality.json'
            audit = json.loads(quality.read_text())
            audit['stereocenters_checked'] += 1
            write(quality, audit)
            value['artifacts']['identity-quality.json'] = sha256_file(quality)
        write(path, value)
        register_outputs(manager, run, folder)
    with pytest.raises(ValueError):
        dispatch(case[0], DESIGN, args(case, 'evaluate_receptor'))
    response = navigate(case[0], 'status', workflow_id=case[1])
    assert record(response)['run_id'] == run
    assert record(response)['receptor_prepared'] is False
    assert response['assessment']['state'] == 'evidence_insufficient'
    assert 'report' not in record(response)
    assert len(launches) == 1


def test_rehashed_outputs_cannot_replace_an_existing_verified_witness(completed):
    case, launches, folder, manager, run = completed
    response = dispatch(case[0], DESIGN, args(case, 'evaluate_receptor'))
    witness = record(response)['evaluation']
    path = folder / 'pipeline.json'
    value = json.loads(path.read_text())
    value['test_fixture'] += '; rewritten after first review'
    write(path, value)
    register_outputs(manager, run, folder)
    response = navigate(case[0], 'status', workflow_id=case[1])
    assert record(response)['evaluation'] == witness
    assert record(response)['evaluation_verification'] == 'evidence_rejected'
    assert record(response)['run_id'] == run
    assert len(launches) == 1


def test_construct_reader_without_whole_receptor_choices_preserves_projection_gate(design, tmp_path, monkeypatch):
    monkeypatch.setattr(ligand_refinement_target, 'require_construct_scope', CONSTRUCT_SCOPE)
    case = design_folder(tmp_path, design) / 'case'
    identity = json.loads((case.parent / 'uniprot.json').read_text())
    before = ligand_refinement_target.target_drafts(case, identity, construct_evidence_only=True)
    path = case / 'evidence/structure_acquisition.json'
    acquisition = json.loads(path.read_text())
    acquisition.pop('receptor_altlocs')
    write(path, acquisition)
    unchanged = path.read_bytes()
    evidence = ligand_refinement_target.target_drafts(case, identity, construct_evidence_only=True)
    assert evidence == before
    assert path.read_bytes() == unchanged
    with pytest.raises(ValueError, match='alternate location'):
        ligand_refinement_target.target_drafts(case, identity)
    source = case / 'source_structures/structure.pdb'
    source.write_bytes(source.read_bytes() + b'REMARK changed source\n')
    with pytest.raises(ValueError, match='acquisition source changed'):
        ligand_refinement_target.target_drafts(case, identity, construct_evidence_only=True)


def test_source_bound_conformer_inventory_and_explicit_selection(design, tmp_path):
    from covscreen.receptor_conformers import validate_selection
    case = design_folder(tmp_path, design) / 'case'
    identity = json.loads((case.parent / 'uniprot.json').read_text())
    evidence = ligand_refinement_target.target_drafts(case, identity, construct_evidence_only=True)['receptor_conformers']
    assert {row['author_residue_id']: set(row['alternatives']) for row in evidence['residues']} == {
        '751': {'A', 'B'}, '831': {'A', 'B'}}
    selection = {'evidence_sha256': evidence['evidence_sha256'], 'choices': {'751': 'A', '831': 'B'},
                 'rationale': 'Software fixture only', 'limitations': 'Not a scientific acceptance'}
    receipt = validate_selection(evidence, selection)
    assert not receipt['grants_resource_approval'] and not receipt['scientific_suitability_verified']
    for choices in ({}, {'751': 'A'}, {'751': 'A', '831': 'Z'}, {'751': 'A', '831': 'B', '773': 'A'}):
        with pytest.raises(ValueError, match='alternate location'):
            validate_selection(evidence, {**selection, 'choices': choices})
    with pytest.raises(ValueError, match='evidence changed'):
        validate_selection({**evidence, 'model': 2}, selection)
    with pytest.raises(ValueError, match='nonempty rationale'):
        validate_selection(evidence, {**selection, 'rationale': ' '})
