import hashlib
import json

import pytest

from covscreen.ligand_parent_index import search


def test_real_index_is_paginated_without_assigning_canonical_sites():
    first = search('P00533', limit=2)
    second = search('P00533', offset=2, limit=2)
    assert first['total_matches'] == 69
    assert first['next_offset'] == 2
    assert {x['source_row_id'] for x in first['candidates']}.isdisjoint(
        x['source_row_id'] for x in second['candidates'])
    assert all(x['canonical_site'] is None and not x['execution_ready']
               for x in first['candidates'])


def test_no_match_is_not_infeasibility():
    result = search('A00000')
    assert result['total_matches'] == 0 and result['next_offset'] is None
    assert 'No match does not establish absence' in result['limitations'][-1]


@pytest.mark.parametrize('kwargs', [{'offset': -1}, {'offset': True}, {'limit': 0}, {'limit': 51}])
def test_invalid_pagination(kwargs):
    with pytest.raises(ValueError):
        search('P00533', **kwargs)


def test_tampered_dataset_is_not_used(tmp_path):
    path = tmp_path / 'index.tsv'
    path.write_text('tampered')
    path.with_suffix('.manifest.json').write_text(json.dumps({
        'schema': 'covpdb-parent-index-v1', 'sha256': hashlib.sha256(b'original').hexdigest()}))
    with pytest.raises(ValueError, match='frozen manifest'):
        search('P00533', dataset=path)
