"""Compile the real batch bridge with software-only inputs; no vendor execution."""
import hashlib
import json

import pytest

from covscreen.layout import enable_de_novo
from covscreen.execution import JobManager
from test_covdock_vs_preparation import configured

enable_de_novo()
from covscreen_de_novo.batch_input import export
from covscreen_de_novo.batch_vs import compile_request, status, evaluate


def test_lo_selection_uses_passing_poses_and_preserves_diversity_and_failures():
    from covscreen_de_novo.batch_lo_selection import select
    originals, candidates = [], []
    for index, smiles in enumerate(['C=CC(=O)Nc1ccccc1', 'C=CC(=O)Nc1ccc(C)cc1',
                                   'C=CC(=O)Nc1ccc(F)cc1', 'C=CC(=O)NCC', 'C=CC(=O)NC']):
        name = str(index)
        originals.append({'candidate_id': name, 'parent_id': name, 'mapped_smiles': smiles, 'input_index': index})
        poses = [{'pose_index': index, 'score': -10 + index,
            'geometry': {'eligible_for_lo': index != 4}, 'product_identity_verified': True,
            'prepared_receptor_identity_verified': True}]
        poses.append({**poses[0], 'pose_index': 100 + index, 'score': -99,
                      'geometry': {'eligible_for_lo': False}})
        candidates.append({'candidate_id': name, 'parent_id': name, 'poses': poses})
    result = select(candidates, originals)
    assert [row['candidate_id'] for row in result['selected']] == ['0', '3', '1']
    assert all(row['vs_score'] != -99 for row in result['selected'])
    assert result['deferred'] == [{'candidate_id': '2', 'reason': 'DIVERSITY_OR_LO_BUDGET'},
                                  {'candidate_id': '4', 'reason': 'NO_PASSING_VS_POSE'}]
    assert select(list(reversed(candidates)), originals) == result
    with pytest.raises(ValueError, match='complete VS candidate denominator'):
        select(candidates[:-1], originals)
    assert select([], [])['selected_count'] == 0


def test_lo_table_passes_shared_exact_identity_contract(tmp_path):
    from covscreen_de_novo.batch_lo_input import table
    from covscreen.refinement_input import verify_refinement_input
    original = {'candidate_id': 'PXM_fixture', 'parent_id': 'generation:0:raw',
        'input_index': 7, 'mapped_smiles': 'CNC(=O)[CH:905]=[CH2:906]'}
    review = {'run_id': 'software-vs-fixture', 'lo_selection': {'selected': [
        {**original, 'vs_pose_index': 3, 'vs_score': -5.2}]}}
    raw, identity = table(review, {'candidates': [original]})
    path = tmp_path / 'candidates.csv'
    path.write_bytes(raw)
    receipt = verify_refinement_input(path, 1, hashlib.sha256(raw).hexdigest(), identity, expected_route='de-novo')
    assert receipt['verified'] and not receipt['grants_resource_approval']
    assert b'generation:0:raw' in raw
    assert b'[CH:905]=[CH2:906]' in raw
    with pytest.raises(ValueError, match='one to twelve'):
        table({'lo_selection': {'selected': []}}, {'candidates': [original]})


def test_frozen_lo_request_rejects_budget_and_source_changes(tmp_path):
    from covscreen_de_novo.batch_lo_input import table, encoded, load
    from covscreen_de_novo.batch_lo import request_for, validate_request, observe
    root = tmp_path.resolve()
    batch_digest, source = registered_batch(root)
    manifest = json.loads((source / 'input.json').read_text())
    original = manifest['candidates'][0]
    workflow = 'de-flow-' + hashlib.sha256(manifest['task_id'].encode()).hexdigest()[:24]
    review = {'run_id': 'software-fixture-not-executed', 'workflow_id': workflow,
        'lo_selection': {'selected': [{**original, 'vs_pose_index': 0, 'vs_score': -5.2}]}}
    raw, identity = table(review, manifest)
    payload = {'schema': 'denovo-batch-lo-input-v1', 'task_id': manifest['task_id'],
        'workflow_id': workflow, 'vs_run_id': review['run_id'], 'batch_manifest_sha256': batch_digest,
        'review_sha256': hashlib.sha256(encoded(review)).hexdigest(), 'candidate_count': 1,
        'candidate_table_sha256': hashlib.sha256(raw).hexdigest(), 'identity_sha256': identity,
        'receptor': str((source / 'receptor.pdb').relative_to(root)),
        'lineage_sha256': {str((source / 'receptor.pdb').relative_to(root)):
            hashlib.sha256((source / 'receptor.pdb').read_bytes()).hexdigest()}}
    digest = hashlib.sha256(encoded(payload)).hexdigest()
    folder = root / 'state/de-novo-lo-inputs' / digest
    folder.mkdir(parents=True)
    (folder / 'input.json').write_bytes(encoded(payload))
    (folder / 'review.json').write_bytes(encoded(review))
    (folder / 'candidates.csv').write_bytes(raw)
    request = request_for(root, digest)
    validate_request(root, request)
    assert request.parameters['execution_instance'] == 'asgbie3'
    assert request.resource_budget.max_candidates == 1
    observed = observe(root, digest)
    assert observed['execution']['submission_state'] == 'NO_RECEIPT'
    assert not observed['compute_submitted']
    with pytest.raises(ValueError, match='No registered batch LO'):
        observe(root, digest, evaluate=True)
    changed = request.model_copy(deep=True)
    changed.resource_budget.cpu_count = 2
    with pytest.raises(ValueError, match='budget'):
        validate_request(root, changed)
    (folder / 'candidates.csv').write_bytes(raw + b'\n')
    with pytest.raises(ValueError, match='VS selection'):
        load(root, digest)


def test_geometry_measures_real_graph_and_rejects_nonfinite_coordinates():
    from test_covdock_vs_output import complex_fixture
    from covscreen.covdock_vs_output import inspect_complex
    from covscreen_de_novo.batch_geometry import measure
    original, prepared, receptor, molecule = complex_fixture()
    row = inspect_complex(molecule, original, prepared, receptor, 'A:797', 0)
    result = measure(row)
    assert len(result['angles_degrees']) == 2
    assert result['eligible_for_lo'] == all(result['gate_flags'].values())
    molecule.GetConformer().SetAtomPosition(row['partner_index'], (float('nan'), 0, 0))
    with pytest.raises(ValueError, match='Nonfinite'):
        measure(row)


def registered_batch(root):
    folder = root / 'input'
    export({'task_id': 'task-batch-test', 'provenance': {}, 'diversity': {'selected': [
        {'attempt_id': 'task-batch-test:0:raw', 'source_sha256': 'a' * 64,
         'canonical_smiles': 'C=CC(=O)NC'}]}}, folder)
    from covscreen_de_novo.covdock_input import REGISTRY
    (folder / 'receptor.pdb').write_bytes((REGISTRY / 'assets/protein_A.pdb').read_bytes())
    digest = hashlib.sha256((folder / 'input.json').read_bytes()).hexdigest()
    (folder / 'binding.json').write_text(json.dumps({
        'manifest_sha256': digest, 'attachment_residue': 'A:797',
        'receptor_sha256': hashlib.sha256((folder / 'receptor.pdb').read_bytes()).hexdigest()}))
    registered = root / 'state/de-novo-batches' / ('batch-' + digest)
    registered.parent.mkdir(parents=True)
    folder.rename(registered)
    return digest, registered


def test_batch_compiles_shared_spec_and_rejects_source_substitution(tmp_path):
    root = tmp_path.resolve()
    _, configuration, _ = configured(root)
    digest, folder = registered_batch(root)
    request = compile_request(root, digest, configuration, cpu_count=4)
    manager = JobManager(root)
    manager.config = {'schrodinger': configuration}
    spec = manager._prepare_submission(request)
    assert spec.route == 'de-novo'
    assert spec.project_id.startswith('de-flow-')
    assert spec.resource_budget.max_candidates == 1
    assert spec.resource_budget.gpu_count == 0
    assert spec.provenance['worker_backend'] == 'external-instance'
    assert request.parameters['vs_preparation']['plan']['execution_instance'] == 'asgbie3'
    plan = request.parameters['vs_preparation']['plan']
    assert plan['receptor'].endswith('/covdock-receptor/receptor.pdb')
    receipt = json.loads((folder / 'covdock-receptor/receipt.json').read_text())
    assert receipt['selection'] == {'A:970': 'A'}
    assert receipt['atoms_removed'] == 0 and not receipt['coordinates_changed']
    assert len(receipt['normalized_atoms']) == 9
    assert spec.input_sha256[str(folder.relative_to(root) / 'covdock-receptor/receipt.json')]
    execute = request.model_copy(update={'execution_mode': 'execute'})
    with pytest.raises(ValueError, match='instance VS executor is not enabled'):
        manager.worker_backend(execute)
    manager.config['runtime'] = {'enable_denovo_vs_instance': True}
    assert manager.worker_backend(execute) == 'external-instance'
    # Exercise execute-mode compilation used by native approval, without dispatch.
    approved = execute.model_copy(update={'human_approved': True, 'approval_note': 'test native scope'})
    compiled = manager.build_spec(approved)
    assert compiled.request_id == approved.request_id
    changed = approved.model_copy(update={'request_id': approved.request_id + '-changed'})
    with pytest.raises(ValueError, match='frozen batch request'):
        manager.build_spec(changed)
    with pytest.raises(ValueError, match='instance VS executor is not enabled'):
        manager.worker_backend(execute.model_copy(update={'route': 'library-first'}))
    assert spec.input_sha256[str(folder.relative_to(root) / 'ligands.sdf')]
    assert not list(root.glob('state/jobs/*/spec.json'))
    (folder / 'receptor.pdb').write_text('substituted receptor')
    with pytest.raises(ValueError, match='receptor'):
        compile_request(root, digest, configuration)


def test_batch_status_never_creates_or_retries_a_job(tmp_path):
    root = tmp_path.resolve()
    digest, _ = registered_batch(root)
    observed = status(root, digest)
    assert observed['execution']['submission_state'] == 'NO_RECEIPT'
    assert observed['execution']['automatic_resubmission_allowed'] is False
    assert observed['compute_submitted'] is False
    assert not (root / 'state/submissions').exists()
    assert not (root / 'state/jobs').exists()
    with pytest.raises(ValueError, match='completed registered request'):
        evaluate(root, digest)
    identity = [observed['workflow_id'], observed['execution']['identity']['request_id'], 'execute']
    receipts = root / 'state/submissions'
    receipts.mkdir()
    receipt = receipts / (hashlib.sha256(json.dumps(identity).encode()).hexdigest() + '.json')
    receipt.write_text(json.dumps({'identity': identity, 'state': 'RESERVED'}))
    before = receipt.read_bytes()
    uncertain = status(root, digest)
    assert uncertain['execution']['submission_state'] == 'RESERVED'
    assert uncertain['execution']['automatic_resubmission_allowed'] is False
    assert receipt.read_bytes() == before
    with pytest.raises(ValueError, match='completed registered request'):
        evaluate(root, digest)
    assert not (root / 'state/jobs').exists()
    with pytest.raises(ValueError, match='digest'):
        status(root, '../outside')
