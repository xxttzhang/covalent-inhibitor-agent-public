"""Bounded local postprocessing only: reuse the scientific Pareto implementation.

Input and output are JSON over stdio. No engine, network, registry or scheduler.
Temporary scientific outputs are removed; timestamps/temporary paths are not
returned, so recovery produces exactly the same derived bytes.
"""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import pandas as pd
from covscreen.pareto import select_pareto_candidates


def rank(rows):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Expected 1..100 candidates')
    frame = pd.DataFrame(rows)
    if frame['compound_id'].duplicated().any():
        raise ValueError('Duplicate compound identity')
    if 'online_structural_status' in frame:
        frame.loc[frame['online_structural_status'] != 'BOTH_STRUCTURAL_QC_PASSED', 'physical_validity'] = 'FAIL'
    with TemporaryDirectory(prefix='covalent-ranking-') as directory:
        output = Path(directory)
        manifest = select_pareto_candidates(frame, output, final_n=len(rows), profile='library-first')
        all_rows = pd.read_csv(output / 'stage6_pareto_all_candidates.csv', keep_default_na=False, dtype=str)
        selected = pd.read_csv(output / 'stage6_final_candidates.csv', keep_default_na=False, dtype=str)
        order = {row['compound_id']: row['final_rank'] for row in selected.to_dict('records')}
        records = all_rows.to_dict('records')
        for row in records:
            row['final_rank'] = order.get(row['compound_id'], '')
        records.sort(key=lambda row: (int(row['final_rank']) if row['final_rank'] else 101, int(row['input_sdf_index'])))
        return {'ranking': {key: value for key, value in manifest.items() if key not in {'outputs', 'generated_at'}},
                'candidates': records}


if __name__ == '__main__':
    value = json.loads(sys.stdin.read(8 * 1024 * 1024 + 1))
    print(json.dumps(rank(value), allow_nan=False, sort_keys=True, separators=(',', ':')))
