import pytest
from covscreen_de_novo.batch_glide_recipe import commands, docking_input, grid_input


def test_fixed_recipe_has_fresh_search_and_bounded_cpu():
    value = docking_input('prereaction_grid.zip', 'fresh_selected.sdf')
    assert 'CORE_RESTRAIN False\n' in value
    assert 'DOCKING_METHOD confgen\n' in value
    assert 'PRECISION SP\n' in value and 'POSES_PER_LIG 20\n' in value
    assert 'GRID_CENTER 1.0, 2.0, 3.0\n' in grid_input('/registered/receptor.mae', [1, 2, 3])
    recipe = commands('/registered/vendor', 'fresh_afatinib.sdf', cpu_count=4, input_count=7)
    assert 'localhost:4' in recipe['dock'] and 'localhost:1' in recipe['grid']
    assert recipe['dock'][recipe['dock'].index('-max_retries') + 1] == '0'
    assert '-epik' in recipe['control_ligprep']


def test_recipe_rejects_ambiguous_paths_coordinates_and_budgets():
    with pytest.raises(ValueError, match='single tokens'):
        docking_input('grid.zip\nCORE_RESTRAIN True', 'ligands.sdf')
    with pytest.raises(ValueError, match='finite'):
        grid_input('receptor.mae', [float('nan'), 0, 0])
    with pytest.raises(ValueError, match='bounded'):
        commands('/registered/vendor', 'control.sdf', cpu_count=55, input_count=8)


def test_native_glide_boundary_is_distinct_from_covdock_and_vina():
    from covscreen.jobs import JobSpec
    from covscreen_de_novo.batch_glide import ENGINE, validate_boundary
    spec = JobSpec(run_id='glide-software-fixture', request_id='glide-request-fixture',
        project_id='de-flow-'+'a'*24, route='de-novo', engine=ENGINE,
        command=['inert-not-executed'], working_directory='/fixture', output_directory='/fixture/out',
        parameters={'workflow_id': 'de-flow-'+'a'*24, 'execution_instance': 'asgbie3',
            'dispatch_policy': 'denovo-prereaction-glide-instance-v1', 'prepared_candidate_ids': ['PXM_fixture'],
            'control_parent': 'CONTROL_AFATINIB', 'max_control_states': 2},
        resource_budget={'cpu_count': 4, 'gpu_count': 0, 'memory_gb': 16, 'max_runtime_minutes': 120, 'max_candidates': 3},
        provenance={'worker_backend': 'external-instance'})
    validate_boundary(spec)
    for field, value in [('engine', 'de-novo-prereaction-vina'), ('route', 'ligand-first'), ('attempt', 2)]:
        with pytest.raises(ValueError, match='bounded independent'):
            validate_boundary(spec.model_copy(update={field: value}))
    spec.resource_budget.max_candidates = 7
    with pytest.raises(ValueError, match='bounded independent'):
        validate_boundary(spec)


def test_runner_rejects_changed_vendor_before_any_process(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from covscreen_de_novo.batch_glide_runner import run
    # Only isolate earlier source validation; no vendor call is simulated as successful.
    monkeypatch.setattr('covscreen_de_novo.batch_prereaction_input.load', lambda *args: ({}, tmp_path))
    binary = tmp_path / 'glide'
    binary.write_text('changed inert runtime')
    spec = SimpleNamespace(parameters={'prereaction_source_sha256': 'a'*64,
        'prereaction_manifest_sha256': 'b'*64, 'glide_runtime': {'files': {str(binary): '0'*64}}})
    with pytest.raises(ValueError, match='runtime changed'):
        run(tmp_path.resolve(), None, spec, None)
    assert not (tmp_path / 'artifacts').exists()


def test_glide_compiled_scope_rejects_changed_runtime(tmp_path, monkeypatch):
    import hashlib
    import json
    from types import SimpleNamespace
    from covscreen_de_novo.batch_glide import validate_compiled_spec
    from covscreen_de_novo.batch_lo_input import encoded
    monkeypatch.setattr('covscreen_de_novo.batch_glide.load', lambda *args: ({}, tmp_path))
    # Exercise immutable comparison independently from scientific-source replay.
    normalized = {'execution_mode': 'execute', 'human_approved': True, 'approval_note': 'test', 'runtime': 'original'}
    monkeypatch.setattr('covscreen.approval_scope.normalize_spec', lambda spec: dict(normalized))
    frozen = {**normalized, 'execution_mode': 'plan', 'human_approved': False, 'approval_note': None}
    (tmp_path / 'plan.json').write_text(json.dumps({'schema': 'denovo-batch-glide-plan-v1',
        'spec': frozen, 'spec_sha256': hashlib.sha256(encoded(frozen)).hexdigest()}))
    spec = SimpleNamespace(parameters={'prereaction_source_sha256': 'a'*64, 'prereaction_manifest_sha256': 'b'*64})
    validate_compiled_spec(tmp_path, spec)
    normalized['runtime'] = 'replacement'
    with pytest.raises(ValueError, match='runtime, inputs or budget changed'):
        validate_compiled_spec(tmp_path, spec)


def test_glide_evidence_rejects_unfinished_job_before_reading_any_output(tmp_path):
    from types import SimpleNamespace
    from covscreen_de_novo.batch_glide_evidence import read
    with pytest.raises(ValueError, match='completed registered task'):
        read(tmp_path, None, None, SimpleNamespace(state='RUNNING', return_code=None), {}, tmp_path)
