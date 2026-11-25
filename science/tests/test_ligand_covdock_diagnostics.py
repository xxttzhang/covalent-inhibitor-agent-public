"""Failed engines remain diagnosable without admitting their poses or retrying."""
from types import SimpleNamespace
import json

from covscreen.jobs import sha256_file
from covscreen.ligand_covdock_execution import failure_diagnostics


def setup_case(tmp_path):
    root = tmp_path.resolve()
    folder = root / 'state/jobs/artificial-failed-lo'
    folder.mkdir(parents=True)
    manager = SimpleNamespace(registry=SimpleNamespace(job_dir=lambda _: folder))
    spec = SimpleNamespace(run_id='artificial-failed-lo')
    result = SimpleNamespace(failure_reason='engine_return_code_1', artifacts=[])
    return root, folder, manager, spec, result


def register(root, path, spec, result):
    result.artifacts.append(SimpleNamespace(path=str(path.relative_to(root)),
        run_id=spec.run_id, sha256=sha256_file(path), size_bytes=path.stat().st_size))


def test_failed_engine_log_is_bounded_and_bound_to_original_run(tmp_path):
    root, folder, manager, spec, result = setup_case(tmp_path)
    path = folder / 'artifacts/covdock_lo/library_first_core_subset_covdock_lo.log'
    path.parent.mkdir(parents=True)
    path.write_text('progress\n' * 2000 + 'Error: index 2 is out of bounds for axis 0 with size 2\n')
    register(root, path, spec, result)
    value = failure_diagnostics(root, manager, spec, result)
    source = value['sources'][0]
    assert source['sha256'] == sha256_file(path)
    assert len(source['tail'].encode()) <= 4096
    assert source['truncated'] is True
    assert 'index 2 is out of bounds' in source['tail']
    assert value['scientific_result_verified'] is False
    assert 'do not automatically repeat' in value['recovery']


def test_unregistered_and_changed_logs_are_not_exposed(tmp_path):
    root, folder, manager, spec, result = setup_case(tmp_path)
    path = folder / 'stderr.log'
    path.write_text('unregistered error')
    assert failure_diagnostics(root, manager, spec, result)['sources'] == []
    register(root, path, spec, result)
    path.write_text('changed error')
    value = failure_diagnostics(root, manager, spec, result)
    assert 'tail' not in value['sources'][0]
    assert 'integrity' in value['sources'][0]['unavailable_reason']


def test_other_run_log_is_not_exposed(tmp_path):
    root, folder, manager, spec, result = setup_case(tmp_path)
    path = folder / 'stderr.log'
    path.write_text('another run')
    register(root, path, spec, result)
    result.artifacts[0].run_id = 'other-run'
    assert 'tail' not in failure_diagnostics(root, manager, spec, result)['sources'][0]


def test_quality_exit_guides_original_output_review_without_claiming_acceptance(tmp_path):
    root, folder, manager, spec, result = setup_case(tmp_path)
    result.return_code = 4
    path = folder / 'artifacts/covdock_lo_registered_manifest.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'gate_quality': {'input_identity_count': 2,
        'covdock_success_count': 2, 'final_candidate_count': 0}}))
    register(root, path, spec, result)
    value = failure_diagnostics(root, manager, spec, result)
    assert value['producer_summary']['counts']['final_candidate_count'] == 0
    assert not value['producer_summary']['independently_evaluated']
    assert 'evaluate_covdock' in value['recovery'] and 'Do not' in value['recovery']
    path.write_text('{}')
    changed = failure_diagnostics(root, manager, spec, result)
    assert 'producer_summary' not in changed
    assert changed['quality_summary_unavailable']
