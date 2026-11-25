"""Independent score-table denominator and pose checks for registered engines."""
import json
import math
from pathlib import Path

import pandas as pd
from rdkit import Chem


def verify_scores(root, spec, records):
    errors = []
    output = (Path(root) / spec.output_directory).resolve()
    carsi = spec.engine == 'carsidock-cov'
    stem = 'carsidock_scores' if carsi else 'rtmscore'
    table = output / (stem + '.parquet')
    manifest_path = output / (stem + ('_audit.json' if carsi else '.run_manifest.json'))
    if table not in records or manifest_path not in records:
        return ['missing_registered_scores_or_audit']
    frame = pd.read_parquet(table)
    source_name = spec.parameters.get('ligand_table' if carsi else 'carsidock_scores')
    if not source_name:
        return ['missing_score_input_identity']
    source_path = Path(root) / source_name
    source = pd.read_parquet(source_path) if source_path.suffix == '.parquet' else pd.read_csv(source_path)
    required = {'input_sdf_index', 'compound_id', 'engine_status', 'raw_score', 'score_direction'}
    if not required.issubset(frame.columns) or not {'input_sdf_index', 'compound_id'}.issubset(source.columns):
        return ['missing_score_identity_or_status_columns']
    keys = ['input_sdf_index', 'compound_id']
    if (frame.empty or frame['input_sdf_index'].duplicated().any() or source['input_sdf_index'].duplicated().any()
            or frame[keys].isna().any().any() or source[keys].isna().any().any()
            or set(map(tuple, frame[keys].to_numpy())) != set(map(tuple, source[keys].to_numpy()))):
        errors.append('score_identity_denominator_mismatch')
    success = frame['engine_status'].eq('SUCCESS')
    scores = pd.to_numeric(frame['raw_score'], errors='coerce')
    if not success.any() or any(not math.isfinite(value) for value in scores[success]):
        errors.append('successful_score_missing_or_nonfinite')
    direction = 'lower_is_better' if carsi else 'higher_is_better'
    if not frame['score_direction'].eq(direction).all():
        errors.append('score_direction_mismatch')
    if len(frame) > spec.resource_budget.max_candidates:
        errors.append('score_count_exceeds_budget')
    audit = json.loads(manifest_path.read_text())
    count_keys = ('input_rows', 'success_rows', 'failed_rows') if carsi else ('output_row_count', 'scored_row_count', 'failed_row_count')
    if [audit.get(k) for k in count_keys] != [len(frame), int(success.sum()), int((~success).sum())]:
        errors.append('score_audit_counts_mismatch')
    if carsi:
        if audit.get('identity_denominator_complete') is not True:
            errors.append('score_audit_denominator_failed')
        if not {'pose_file', 'selected_pose_record_index'}.issubset(frame.columns):
            errors.append('missing_selected_pose_identity')
        else:
            suppliers = {}
            for row in frame.loc[success].to_dict('records'):
                path = (Path(root) / str(row['pose_file'])).resolve()
                if path not in records:
                    errors.append('selected_pose_not_registered')
                    break
                if path not in suppliers:
                    suppliers[path] = list(Chem.SDMolSupplier(str(path), sanitize=False))
                index = row['selected_pose_record_index']
                if pd.isna(index) or int(index) != index or not 0 <= int(index) < len(suppliers[path]):
                    errors.append('selected_pose_index_invalid')
                    break
                molecule = suppliers[path][int(index)]
                if molecule is None or molecule.GetNumConformers() != 1:
                    errors.append('selected_pose_invalid')
                    break
                if not all(math.isfinite(v) for point in molecule.GetConformer().GetPositions() for v in point):
                    errors.append('selected_pose_coordinates_nonfinite')
                    break
    else:
        if audit.get('status') != 'SUCCESS' or audit.get('score_direction') != direction:
            errors.append('rtmscore_manifest_failed')
        if 'engine_status' not in source:
            errors.append('missing_upstream_scoring_status')
        else:
            upstream = source.set_index('input_sdf_index')['engine_status']
            for row in frame.loc[success].to_dict('records'):
                if upstream.get(row['input_sdf_index']) != 'SUCCESS':
                    errors.append('rtmscore_success_for_failed_upstream_pose')
                    break
    return errors
