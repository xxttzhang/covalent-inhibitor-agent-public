"""Study-to-shared-preparation integration; no receptor repair or GPU run.

Uses deposited 2ITY sources, synthetic positive pose review, actual loaders,
actual CPU dependency import probes and actual plan compilation.
"""
import json
import os
from pathlib import Path
import runpy
import sys

import pytest

from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.ligand_design import write
from covscreen.ligand_receptor_preparation import request_for
from covscreen.ligand_workflow import DESIGN, navigate
from covscreen.mcp.server import dispatch
from covscreen.receptor_input import load_receptor_preparation_input, load_receptor_preparation_directory
from test_af3_execution_workflow import design, graph_design, pose, inputs, jobs
from test_pose_execution_workflow import args


ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def bound(inputs):
    dispatch(inputs[0], DESIGN, args(inputs, 'prepare_receptor_input'))
    return inputs


def binding(pose):
    return json.loads(pose[2].read_text())['refinement_input']['receptor_input']


def configure(pose):
    path = pose[0] / 'configs/local.inspire.yaml'
    data = json.loads(path.read_text())
    runtime = Path(os.environ.get('COVSCREEN_OPENMM_TEST_ROOT', '/missing-external-openmm-test-runtime'))
    data['receptor_preparation'] = {'enabled': True, 'audit_python': sys.executable,
        'openmm_python': str(runtime / 'bin/python'),
        'openmm_pythonpath': str(runtime),
        'pdbfixer_source': 'bundled'}
    write(path, data)


def test_mcp_freezes_current_study_sources_for_the_shared_preparation_reader(bound):
    ref = binding(bound)
    loaded = load_receptor_preparation_input(bound[0], ref['artifact_id'], ref['manifest_sha256'])
    assert loaded['source_route'] == 'ligand-first'
    assert loaded['source_kind'] == 'sealed-ligand-study-v1'
    assert 'identity' not in loaded  # No fabricated unique target-search receipt.
    assert not (bound[0] / 'state/target_identity').exists()
    assert loaded['taxon'] == 9606
    assert loaded['acquisition']['acquisition']['mappings'][0]['author_residue_id'] == '797'
    assert loaded['acquisition']['acquisition']['mappings'][0]['entity_seq_id'] == 102
    assert ref['missing_construct_residue_count'] == 27 and ref['excluded_atom_count'] == 50
    assert not ref['receptor_prepared'] and not ref['compute_submitted']
    source = ROOT / 'routes/ligand-first/tests/fixtures/refinement-target-2ity/case/source_structures/structure.pdb'
    assert (loaded['acquisition_directory'] / 'structure.pdb').read_bytes() == source.read_bytes()
    assert load_receptor_preparation_directory(bound[0], loaded['acquisition_directory'], ref['manifest_sha256']) == loaded
    before = jobs(bound)
    response = dispatch(bound[0], DESIGN, args(bound, 'prepare_receptor_input'))
    assert response['refinement_input']['receptor_input'] == ref
    assert navigate(bound[0], 'status', workflow_id=bound[1])['refinement_input']['receptor_input'] == ref
    assert jobs(bound) == before


def test_runtime_wait_recovers_to_actual_shared_cpu_plan_without_registration(bound):
    before = jobs(bound)
    response = dispatch(bound[0], DESIGN, args(bound, 'check_receptor_runtime'))
    record = response['refinement_input']['receptor_preparation']
    assert record['runtime_check'] == 'WAITING_RUNTIME_CONFIGURATION'
    assert response['workflow']['stage'] == 'POSE_EXECUTION_REVIEW'
    configure(bound)
    response = dispatch(bound[0], DESIGN, args(bound, 'check_receptor_runtime'))
    record = response['refinement_input']['receptor_preparation']
    assert record['runtime_check'] == 'CPU_PREPARATION_PLAN_COMPILED'
    assert record['execution_connected'] is True
    spec = record['spec']
    assert spec['route'] == 'ligand-first'
    assert spec['engine'] == 'library-first-receptor-preparation'
    assert spec['parameters']['max_missing_residues'] == 27
    scope = record['preparation_scope']
    assert scope['max_missing_residues'] == spec['parameters']['max_missing_residues']
    assert scope['missing_residue_policy'] == 'model_deposited_construct_gaps_with_pdbfixer'
    assert scope['missing_heavy_atom_policy'] == 'model_missing_heavy_atoms'
    assert not scope['modeled_coordinates_are_experimental']
    assert scope['completion_is_not_quality_acceptance']
    assert spec['parameters']['runtime_versions']['openmm']['openmm']
    assert spec['resource_budget']['gpu_count'] == 0
    assert spec['resource_budget']['max_candidates'] == 1
    ref = binding(bound)
    command = spec['command']
    directory = Path(command[command.index('--receptor-input-dir') + 1])
    assert directory == bound[0] / ref['directory']
    assert str(directory / 'study-acquisition.json') in spec['input_paths']
    assert spec['provenance']['config_binding'] == 'immutable-runtime-config-v1'
    assert not Path(command[command.index('--output-dir') + 1]).exists()
    again = dispatch(bound[0], DESIGN, args(bound, 'check_receptor_runtime'))
    assert again['refinement_input']['receptor_preparation'] == record
    assert jobs(bound) == before  # Only the synthetic upstream pose task.


@pytest.mark.parametrize('field,value', [('route', 'library-first'), ('project_id', 'different-study')])
def test_shared_compiler_rejects_route_or_study_substitution(bound, field, value):
    request = request_for(bound[0], json.loads(bound[2].read_text()))
    request = request.model_copy(update={field: value})
    with pytest.raises(ValueError, match='route/project'):
        JobManager(bound[0])._prepare_submission(request)
    assert len(jobs(bound)) == 1


@pytest.mark.parametrize('name', ['structure.pdb', 'canonical.fasta', 'study-uniprot.json', 'target_acquisition.json'])
def test_status_invalidates_changed_bundle_without_changing_scientific_verdict(bound, name):
    ref = binding(bound)
    path = bound[0] / ref['directory'] / name
    path.write_bytes(path.read_bytes() + b'\n')
    response = navigate(bound[0], 'status', workflow_id=bound[1])
    handoff = response['refinement_input']
    assert 'receptor_input' not in handoff and 'receptor_preparation' not in handoff
    assert handoff['receptor_input_error']['artifact_id'] == ref['artifact_id']
    assert any(row['scope'] == 'receptor_preparation_input' for row in response['blockers'])
    assert len(jobs(bound)) == 1


def test_original_study_parent_changes_invalidate_the_copy(bound):
    state = json.loads(bound[2].read_text())
    path = bound[0] / 'results/ligand-designs' / state['design_id'] / 'uniprot.json'
    path.write_bytes(path.read_bytes() + b'\n')
    response = navigate(bound[0], 'status', workflow_id=bound[1])
    assert 'receptor_input' not in response['refinement_input']
    assert 'receptor_input_error' in response['refinement_input']


def test_shared_worker_rechecks_corrupted_input_before_creating_output(bound, monkeypatch):
    ref = binding(bound)
    directory = bound[0] / ref['directory']
    (directory / 'structure.pdb').write_bytes(b'altered')
    output = bound[0] / 'must-not-run'
    script = ROOT / 'routes/library-first/scripts/run_receptor_preparation.py'
    main = runpy.run_path(str(script))['main']
    monkeypatch.setattr(sys, 'argv', [str(script), '--project-root', str(bound[0]),
        '--receptor-input-dir', str(directory), '--manifest-sha256', ref['manifest_sha256'],
        '--output-dir', str(output), '--openmm-python', sys.executable,
        '--openmm-pythonpath', str(bound[0]), '--pdbfixer-site-packages', str(bound[0]),
        '--heavy-seed', '20260725', '--max-missing-residues', '27', '--altlocs-json', '{}'])
    with pytest.raises(ValueError, match='changed|checksum'):
        main()
    assert not output.exists()
    assert len(jobs(bound)) == 1


def test_symlinked_acquisition_directory_is_not_accepted(bound):
    ref = binding(bound)
    directory = bound[0] / ref['directory']
    moved = directory.with_name('moved')
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError, match='unavailable'):
        load_receptor_preparation_input(bound[0], ref['artifact_id'], ref['manifest_sha256'])


def test_explicit_v2_policy_is_bound_to_compiled_plan_without_submission(bound):
    import hashlib
    from covscreen.receptor_relaxation import RELAXATION_POLICY_V2
    configure(bound)
    before = jobs(bound)
    request = request_for(bound[0], json.loads(bound[2].read_text()))
    digest = hashlib.sha256(json.dumps(RELAXATION_POLICY_V2,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    request = request.model_copy(update={'parameters': {**request.parameters,
        'relaxation_policy_version': 'v2', 'relaxation_policy': RELAXATION_POLICY_V2,
        'relaxation_policy_sha256': digest}})
    spec = JobManager(bound[0])._prepare_submission(request)
    assert spec.command[-2:] == ['--relaxation-policy', 'v2']
    assert spec.parameters['relaxation_policy_sha256'] == digest
    assert jobs(bound) == before
    assert not Path(spec.command[spec.command.index('--output-dir') + 1]).exists()


def test_v2_orchestrator_keeps_motion_review_pending(bound, monkeypatch):
    """Software control-flow test: phase outputs and audits are synthetic."""
    from covscreen.receptor_relaxation import RELAXATION_POLICY_V2
    script = ROOT / 'routes/library-first/scripts/run_receptor_preparation.py'
    main = runpy.run_path(str(script))['main']
    scope = main.__globals__
    ref = binding(bound)
    output = bound[0] / 'synthetic-v2-pipeline'
    calls = []
    def phase(command, **kwargs):
        request = json.loads(Path(command[command.index('--request') + 1]).read_text())
        mode = command[command.index('--mode') + 1]
        directory = Path(command[command.index('--output') + 1])
        directory.mkdir()
        (directory / 'receipt.json').write_text('{}')
        if mode == 'relax':
            assert request['relaxation_policy'] == RELAXATION_POLICY_V2
            assert len(request['relaxation_policy_sha256']) == 64
        calls.append(mode)
    def quality(*args):
        calls.append('v2_identity')
        return {'status': 'identity_and_stereochemistry_passed_geometry_pending'}
    def geometry(*args):
        calls.append('v2_geometry')
        assert args[-1] == output / 'heavy'
        return {'status': 'geometry_passed_environment_review_pending'}
    def forbidden(*args, **kwargs):
        raise AssertionError('Old audit or automatic admission must not run for v2')
    monkeypatch.setattr(scope['subprocess'], 'run', phase)
    monkeypatch.setitem(scope, 'build_chirality_plan', lambda *args: {})
    monkeypatch.setitem(scope, 'audit_restrained_receptor', quality)
    monkeypatch.setitem(scope, 'audit_restrained_geometry', geometry)
    for name in ('audit_relaxed_receptor', 'audit_receptor_geometry', 'admit_isolated_receptor'):
        monkeypatch.setitem(scope, name, forbidden)
    monkeypatch.setattr(sys, 'argv', [str(script), '--project-root', str(bound[0]),
        '--receptor-input-dir', str(bound[0] / ref['directory']),
        '--manifest-sha256', ref['manifest_sha256'], '--output-dir', str(output),
        '--openmm-python', sys.executable, '--openmm-pythonpath', str(bound[0]),
        '--pdbfixer-site-packages', str(bound[0]), '--heavy-seed', '20260725',
        '--max-missing-residues', '27', '--altlocs-json', '{}', '--relaxation-policy', 'v2'])
    main()
    result = json.loads((output / 'pipeline.json').read_text())
    assert calls == ['heavy', 'relax', 'v2_identity', 'v2_geometry']
    assert result['status'] == 'observed_motion_review_pending'
    assert result['receptor_prepared'] is False
    assert result['campaign_ready'] is False
    assert result['observed_motion_acceptance'] == 'not_evaluated'
    assert 'geometry-quality.json' in result['artifacts']
    assert not (output / 'prepared').exists()


def test_selected_v2_policy_has_distinct_request_identity(bound):
    import hashlib
    from covscreen.ligand_receptor_source import context
    from covscreen.receptor_relaxation import RELAXATION_POLICY_V2
    state = json.loads(bound[2].read_text())
    old = request_for(bound[0], state)
    digest = hashlib.sha256(json.dumps(RELAXATION_POLICY_V2,
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    selection = {'version': 'v2', 'policy_sha256': digest,
        'input_manifest_sha256': binding(bound)['manifest_sha256']}
    context(state)['receptor_relaxation_selection'] = selection
    new = request_for(bound[0], state)
    assert old.request_id != new.request_id
    assert new.parameters['relaxation_policy_sha256'] == digest
    assert new.execution_mode == 'plan'
    selection['input_manifest_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='current input or policy'):
        request_for(bound[0], state)
