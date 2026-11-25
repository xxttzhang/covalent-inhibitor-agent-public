"""Real plan compilers and worker input guards; no scientific engine is run."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from covscreen.execution import JobManager
from covscreen.jobs import JobSubmissionRequest, sha256_file
from covscreen.covdock_gate import STRICT_ALGORITHM_VERSION
from receptor_fixture import write_receptor


def configured(root, engine, route='ligand-first'):
    inputs = root/'inputs'; inputs.mkdir(exist_ok=True)
    table = inputs/'candidates.csv'
    pd.DataFrame([{'input_sdf_index': index, 'compound_id': name,
        'standardized_smiles': 'C=CC(=O)NC', 'raw_score': -1.0,
        'route': route, 'covdock_reaction_type': 'Michael Addition'}
        for index, name in [(7, '007'), (9, 'candidate-b')]]).to_csv(table, index=False)
    identity = hashlib.sha256(json.dumps([[7, '007'], [9, 'candidate-b']],
        separators=(',', ':')).encode()).hexdigest()
    runtime = root/'runtime'; runtime.mkdir(exist_ok=True)
    for name in ['run_alphafold.py', 'python', 'run', 'covalent_docking', 'vendor.lic']:
        (runtime/name).write_text('software-only fixture; never execute\n')
    for name in ['models', 'databases']:
        (runtime/name).mkdir(exist_ok=True)
    (inputs/'source.json').write_text(json.dumps({'sequences': [
        {'protein': {'id': 'A', 'sequence': 'A'*104+'C'}}]}))
    write_receptor(inputs/'receptor.pdb')
    (inputs/'target.yaml').write_text('target_id: EGFR\nprotein_chain: A\n'
        'cysteine_sequence_index: 105\nresidue_number: 797\nnucleophilic_atom: SG\n')
    manager = JobManager(root)
    manager.config = {'alphafold3': {'runner': str(runtime/'run_alphafold.py'),
        'python': str(runtime/'python'), 'model_dir': str(runtime/'models'),
        'database_dir': str(runtime/'databases')}, 'schrodinger': {'enabled': True,
        'root': str(runtime), 'license_file': str(runtime/'vendor.lic')}}
    parameters = {'candidate_table': 'inputs/candidates.csv',
        'expected_input_count': 2, 'expected_input_sha256': sha256_file(table),
        'expected_identity_sha256': identity}
    if engine == 'alphafold3-mpae':
        parameters.update(source_data_json='inputs/source.json', target_config='inputs/target.yaml', top_n=2)
    else:
        parameters.update(receptor='inputs/receptor.pdb', attachment_residue='A:797',
            reaction_type='Michael Addition', final_n=2, input_score_source='core',
            gate_algorithm_version=STRICT_ALGORITHM_VERSION)
    request = JobSubmissionRequest(request_id='refinement-contract-test', project_id='fixture',
        route=route, engine=engine, parameters=parameters, execution_mode='plan',
        resource_budget={'max_runtime_minutes': 30, 'max_candidates': 2,
                         'gpu_count': int(engine == 'alphafold3-mpae'), 'cpu_count': 2, 'memory_gb': 8})
    return manager, request, table


def test_denovo_covdock_command_freezes_control_reader_without_changing_ligand_binding(tmp_path):
    from covscreen.layout import enable_de_novo
    enable_de_novo()
    from covscreen_de_novo.control_review import validate
    from covscreen_de_novo.chemical_review import validate as validate_chemical
    manager, request, _ = configured(tmp_path, 'schrodinger-covdock-lo')
    # Exercise only the command compiler on artificial inputs. This neither
    # approves nor bypasses build_spec's separately tested native source guards.
    ligand = {**request.parameters, 'workflow_id':'artificial-study'}
    manager._build_covdock_lo('artificial-ligand-plan', ligand)
    assert 'denovo_control_readback' not in ligand
    assert 'denovo_chemical_readback' not in ligand
    denovo = {**request.parameters, 'workflow_id':'artificial-study',
              'denovo_source_id':'denovo-review-'+'a'*32}
    manager._build_covdock_lo('artificial-control-plan', denovo)
    validate(denovo['denovo_control_readback'])
    validate_chemical(denovo['denovo_chemical_readback'])
    assert denovo['covdock_readback'] == ligand['covdock_readback']


@pytest.mark.parametrize('field', ['runner', 'python', 'model_dir', 'database_dir'])
@pytest.mark.parametrize('invalid', ['missing', 'empty', 'wrong_kind'])
def test_af3_runtime_requires_explicit_assets_of_the_right_kind(tmp_path, field, invalid):
    from covscreen.af3_target import AF3RuntimeUnavailable
    manager, request, _ = configured(tmp_path, 'alphafold3-mpae')
    if invalid == 'missing':
        del manager.config['alphafold3'][field]
    elif invalid == 'empty':
        manager.config['alphafold3'][field] = ''
    else:
        manager.config['alphafold3'][field] = str(tmp_path / 'runtime' /
            ('models' if field in {'runner', 'python'} else 'python'))
    with pytest.raises(AF3RuntimeUnavailable, match=field):
        manager.build_spec(request)


@pytest.mark.parametrize('engine', ['alphafold3-mpae', 'schrodinger-covdock-lo'])
def test_lineage_directory_is_not_silently_dropped_from_frozen_inputs(tmp_path, engine):
    manager, request, _ = configured(tmp_path, engine)
    request.parameters['refinement_lineage_files'] = ['inputs']
    with pytest.raises(ValueError, match='entries must be files'):
        manager.build_spec(request)


@pytest.mark.parametrize('engine', ['alphafold3-mpae', 'schrodinger-covdock-lo'])
def test_complete_lineage_is_bound_by_the_shared_compiler(tmp_path, engine):
    manager, request, _ = configured(tmp_path, engine)
    lineage = tmp_path / 'inputs/study-source.json'
    lineage.write_text('{"study": "bounded-fixture"}\n')
    request.parameters['refinement_lineage_files'] = ['inputs/study-source.json']
    spec = manager.build_spec(request)
    assert spec.input_sha256['inputs/study-source.json'] == sha256_file(lineage)
    assert str(lineage) in spec.input_paths


@pytest.mark.parametrize('field', ['root', 'license_file', 'runtime_lib_dir'])
def test_covdock_empty_asset_paths_do_not_fall_back_to_working_directory(tmp_path, field):
    from covscreen.covdock_receptor import CovDockRuntimeUnavailable
    manager, request, _ = configured(tmp_path, 'schrodinger-covdock-lo')
    manager.config['schrodinger'][field] = ''
    with pytest.raises(CovDockRuntimeUnavailable):
        manager.build_spec(request)


@pytest.mark.parametrize('engine', ['alphafold3-mpae', 'schrodinger-covdock-lo'])
@pytest.mark.parametrize('route', ['library-first', 'ligand-first', 'de-novo'])
def test_exact_contract_reaches_worker_and_plan_provenance(tmp_path, engine, route):
    manager, request, table = configured(tmp_path, engine, route)
    spec = manager.build_spec(request)
    for key in ('expected_input_count', 'expected_input_sha256', 'expected_identity_sha256'):
        flag = '--' + key.replace('_', '-')
        assert spec.command[spec.command.index(flag)+1] == str(request.parameters[key])
    assert spec.route == route
    assert spec.provenance['refinement_input_contract']['verified'] is True
    assert spec.provenance['refinement_input_contract']['grants_resource_approval'] is False
    if engine == 'schrodinger-covdock-lo':
        assert spec.command[spec.command.index('--input-score-source')+1] == 'core'
        assert spec.command[spec.command.index('--gate-algorithm-version')+1] == STRICT_ALGORITHM_VERSION
        assert spec.provenance['gate_algorithm_version'] == STRICT_ALGORITHM_VERSION
        assert spec.provenance['primary_order_within_gate'] == 'input core order'
    assert not list((tmp_path/'state/jobs').glob('*/spec.json'))


@pytest.mark.parametrize('engine', ['alphafold3-mpae', 'schrodinger-covdock-lo'])
@pytest.mark.parametrize('changed', ['count', 'hash', 'identity', 'partial', 'null'])
def test_plan_rejects_broken_or_partial_exact_contract(tmp_path, engine, changed):
    manager, request, _ = configured(tmp_path, engine)
    params = request.parameters.copy()
    if changed == 'count': params['expected_input_count'] = 1
    elif changed == 'hash': params['expected_input_sha256'] = '0'*64
    elif changed == 'identity': params['expected_identity_sha256'] = '0'*64
    elif changed == 'partial': del params['expected_identity_sha256']
    else: params.update({key: None for key in params if key.startswith('expected_')})
    with pytest.raises(ValueError, match='Refinement'):
        manager.build_spec(request.model_copy(update={'parameters': params}))
    assert not list((tmp_path/'state/jobs').glob('*/spec.json'))


@pytest.mark.parametrize('change', ['missing_source', 'unknown_source', 'unknown_policy', 'grouped'])
def test_covdock_never_silently_drops_source_policy_or_contract(tmp_path, change):
    manager, request, _ = configured(tmp_path, 'schrodinger-covdock-lo')
    params = request.parameters.copy()
    if change == 'missing_source': del params['input_score_source']
    elif change == 'unknown_source': params['input_score_source'] = 'invented'
    elif change == 'unknown_policy': params['gate_algorithm_version'] = 'invented'
    else: params['reaction_type_column'] = 'covdock_reaction_type'
    reason = ('Grouped CovDock' if change == 'grouped' else
              'gate_algorithm_version' if change == 'unknown_policy' else 'input_score_source')
    with pytest.raises(ValueError, match=reason):
        manager.build_spec(request.model_copy(update={'parameters': params}))


@pytest.mark.parametrize('change', ['untagged', 'foreign', 'missing_contract'])
def test_de_novo_covdock_cannot_relabel_legacy_or_foreign_candidates(tmp_path, change):
    manager, request, table = configured(tmp_path, 'schrodinger-covdock-lo', 'de-novo')
    params = request.parameters.copy()
    if change == 'missing_contract':
        params = {k: v for k, v in params.items() if not k.startswith('expected_')}
    else:
        frame = pd.read_csv(table, dtype={'compound_id': str})
        if change == 'untagged': frame = frame.drop(columns=['route'])
        else: frame.loc[0, 'route'] = 'ligand-first'
        frame.to_csv(table, index=False)
        params['expected_input_sha256'] = sha256_file(table)
    with pytest.raises(ValueError, match='route|contract'):
        manager.build_spec(request.model_copy(update={'parameters': params}))


@pytest.mark.parametrize('engine', ['alphafold3-mpae', 'schrodinger-covdock-lo'])
def test_actual_worker_guard_refuses_changed_bytes_before_engine_launch(tmp_path, engine):
    manager, request, table = configured(tmp_path, engine)
    spec = manager.build_spec(request)
    table.write_text(table.read_text()+'\n')
    # Execute the actual registered script; its input guard must stop before the
    # intentionally unusable scientific runtime can be invoked.
    result = subprocess.run([sys.executable, *spec.command[1:]], capture_output=True, text=True,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1]/'src')}, timeout=30)
    assert result.returncode != 0
    assert 'Refinement candidate file differs from the frozen subset' in result.stderr
    assert not Path(spec.output_directory).exists()
