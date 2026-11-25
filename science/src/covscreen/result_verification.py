"""Fail-closed verification of persisted results; never starts scientific work."""
from pathlib import Path
import csv
import json

from .jobs import ArtifactRegistry, sha256_file


def verify_job(root: Path, run_id: str) -> dict:
    root = Path(root).resolve()
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run_id)
    result = registry.load_result(run_id)
    errors = []
    if spec.run_id != run_id or result.run_id != run_id:
        errors.append('identity_mismatch:requested_run_id')
    for field in ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'):
        if getattr(spec, field) != getattr(result, field):
            errors.append('identity_mismatch:' + field)
    if errors:
        return {'status': 'FAIL', 'verified': False, 'run_id': run_id,
                'job_state': result.state, 'errors': errors,
                'next_action': 'stop_dependent_stages', 'calculation_submitted': False}
    from .job_origin import verify_origin
    try:
        origin = verify_origin(root, run_id)
    except (ValueError, KeyError, TypeError, OSError) as error:
        return {'status': 'FAIL', 'verified': False, 'run_id': run_id,
                'errors': ['historical_origin_invalid:' + str(error)],
                'next_action': 'stop_dependent_stages', 'calculation_submitted': False}
    if origin is not None:
        return origin
    details = {}
    if result.state != 'COMPLETED' or result.return_code != 0:
        return {'status': 'BLOCKED', 'verified': False, 'run_id': run_id,
                'job_state': result.state, 'errors': errors + ['job_not_successful'],
                'next_action': 'wait_same_job' if result.state in {'QUEUED', 'RUNNING'} else 'report_state'}
    if not spec.input_sha256:
        errors.append('missing_frozen_input_hashes')
    for name, digest in spec.input_sha256.items():
        path = root / name
        if not path.is_file() or sha256_file(path) != digest:
            errors.append('input_changed_or_missing:' + name)
    records = {}
    for artifact in result.artifacts:
        path = (root / artifact.path).resolve()
        if not path.is_relative_to(root) or artifact.run_id != run_id:
            errors.append('invalid_artifact_identity:' + artifact.path)
            continue
        if path in records:
            errors.append('duplicate_artifact:' + artifact.path)
        records[path] = artifact
        if not path.is_file() or path.stat().st_size != artifact.size_bytes or sha256_file(path) != artifact.sha256:
            errors.append('artifact_changed_or_missing:' + artifact.path)
    if not spec.expected_output_paths:
        errors.append('missing_expected_outputs')
    for name in spec.expected_output_paths:
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.exists():
            errors.append('expected_output_missing:' + name)
            continue
        files = list(path.rglob('*')) if path.is_dir() else [path]
        files = [p for p in files if p.is_file()]
        if not files or any(p.resolve() not in records for p in files):
            errors.append('expected_output_unregistered:' + name)
    # Engine-specific semantics are required: file existence alone cannot certify a stage.
    if spec.engine in {'carsidock-cov', 'rtmscore'}:
        if not errors:
            from .score_verification import verify_scores
            errors.extend(verify_scores(root, spec, records))
    elif spec.engine == 'shared-deepcosi-site-prediction':
        if not errors:
            from .deepcosi_verification import verify_deepcosi
            prediction_verification = verify_deepcosi(root, spec, records)
            errors.extend(prediction_verification['errors'])
            if not errors:
                details['structure_coverage'] = prediction_verification['structure_coverage']
    elif spec.engine != 'covindb-prescreen':
        errors.append('engine_verifier_not_implemented:' + spec.engine)
    elif not errors:
        output = Path(spec.output_directory)
        if not output.is_absolute():
            output = root / output
        manifest_path = (output / 'stage2_selection_manifest.json').resolve()
        if manifest_path not in records:
            errors.append('missing_registered_selection_manifest')
        else:
            manifest = json.loads(manifest_path.read_text())
            required = {'rdkit_parse_rate_ge_0_99', 'selected_warhead_mapping_rate_ge_0_98',
                        'identity_loss_zero', 'failure_table_present'}
            gates = manifest.get('quality_gates', {})
            if any(gates.get(k) is not True for k in required):
                errors.append('quality_gate_failed_or_missing')
            if manifest.get('status') != 'completed' or manifest.get('selection_outcome') != 'CANDIDATES_SELECTED':
                errors.append('selection_not_successful')
            source = manifest.get('source', {})
            source_path = Path(source.get('path', ''))
            expected_source = spec.parameters.get('source_sha256')
            if (not expected_source or source.get('sha256') != expected_source
                    or not source_path.is_file() or sha256_file(source_path) != expected_source):
                errors.append('source_identity_mismatch')
            for key, manifest_key in (('max_rows', 'max_rows'), ('seed', 'sampling_seed'), ('record_selection', 'record_selection')):
                if source.get(manifest_key) != spec.parameters.get(key):
                    errors.append('sampling_contract_mismatch:' + key)
            count = manifest.get('selection', {}).get('top_n_selected')
            if type(count) is not int or not 0 < count <= spec.resource_budget.max_candidates:
                errors.append('candidate_count_outside_budget')
            if manifest.get('counts', {}).get('selected') != count:
                errors.append('selection_count_mismatch')
            table = (output / 'stage2_candidates.csv').resolve()
            if table not in records:
                errors.append('missing_registered_candidate_csv')
            else:
                with table.open(newline='') as handle:
                    rows = list(csv.DictReader(handle))
                ids = [row.get('compound_id') for row in rows]
                if len(rows) != count or not all(ids) or len(set(ids)) != len(ids):
                    errors.append('candidate_identity_or_count_mismatch')
            for name in ('stage2_candidates.sdf', 'stage2_candidates.parquet', 'covindb_rejected.parquet'):
                if (output / name).resolve() not in records:
                    errors.append('missing_registered_output:' + name)
            if not errors:
                from rdkit import Chem
                import pyarrow.parquet as pq
                try:
                    parquet = pq.read_table(output / 'stage2_candidates.parquet').to_pylist()
                    molecules = list(Chem.SDMolSupplier(str(output / 'stage2_candidates.sdf')))
                    if [row.get('compound_id') for row in parquet] != ids or len(molecules) != count:
                        errors.append('candidate_format_identity_mismatch')
                    else:
                        for index, (row, molecule) in enumerate(zip(rows, molecules)):
                            parent = Chem.MolFromSmiles(row.get('standardized_smiles', ''))
                            if (molecule is None or parent is None or not molecule.HasProp('COMPOUND_ID')
                                    or molecule.GetProp('COMPOUND_ID') != ids[index]
                                    or not molecule.HasProp('INPUT_SDF_INDEX')
                                    or molecule.GetIntProp('INPUT_SDF_INDEX') != index
                                    or Chem.MolToInchiKey(molecule) != Chem.MolToInchiKey(parent)
                                    or parquet[index].get('standardized_smiles') != row.get('standardized_smiles')):
                                errors.append('candidate_structure_or_order_mismatch')
                                break
                    pq.read_metadata(output / 'covindb_rejected.parquet')
                except (OSError, ValueError, KeyError, RuntimeError) as exc:
                    errors.append('candidate_format_invalid:' + type(exc).__name__)
    return {**details, 'status': 'PASS' if not errors else 'FAIL', 'verified': not errors,
            'run_id': run_id, 'job_state': result.state, 'errors': errors,
            'verification_version': 'job-result-v1',
            'spec_sha256': sha256_file(registry.job_dir(run_id) / 'spec.json'),
            'result_sha256': sha256_file(registry.job_dir(run_id) / 'result.json'),
            'next_action': 'plan_next_stage_requires_separate_approval' if not errors else 'stop_dependent_stages',
            'limitations': ['Computational integrity verification does not establish binding or biological activity.'],
            'calculation_submitted': False}
