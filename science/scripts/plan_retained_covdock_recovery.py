"""Compile a read-only recovery preview; this command never submits a job."""
import argparse
import json
from pathlib import Path

from covscreen.covdock_retained import compile_retained_request, validate_retained_recovery
from covscreen.execution import JobManager
from covscreen.jobs import sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--parent-run-id', required=True)
    parser.add_argument('--retained-candidate', nargs=2, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.project_root.resolve()
    records = [{'path': name, 'sha256': digest} for name, digest in args.retained_candidate]
    request = compile_retained_request(root, args.parent_run_id, records)
    manager = JobManager(root)
    spec = manager.build_spec(request)
    retained = validate_retained_recovery(root, spec.parameters, records, args.parent_run_id)
    import pandas as pd
    candidates = pd.read_parquet(root / spec.parameters['candidate_table'])
    backend_error = None
    try:
        backend = manager.worker_backend(request)
    except ValueError as error:
        backend, backend_error = None, str(error)
    result = {'kind': 'retained-covdock-recovery-preview', 'compute_submitted': False,
        'native_approval_granted': False, 'registry_entry_created': False,
        'request': request.model_dump(mode='json'), 'compiled_spec': spec.model_dump(mode='json'),
        'worker_backend': backend, 'worker_backend_error': backend_error,
        'original_candidates': candidates[['compound_id', 'input_sdf_index']].to_dict('records'),
        'retained_candidates': candidates.loc[candidates['input_sdf_index'].isin(retained),
            ['compound_id', 'input_sdf_index']].to_dict('records'),
        'new_docking_candidates': candidates.loc[~candidates['input_sdf_index'].isin(retained),
            ['compound_id', 'input_sdf_index']].to_dict('records'),
        'planner_sha256': sha256_file(Path(__file__)),
        'scope_note': 'Preview only. Deployment source hashes and native approval must be prepared again before execution.'}
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2, ensure_ascii=False)
    print(json.dumps({key: value for key, value in result.items() if key not in {'request', 'compiled_spec'}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
