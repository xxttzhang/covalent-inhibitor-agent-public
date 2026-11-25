import pytest

from covscreen.receptor_conformers import digest, validate_selection


def evidence():
    payload = {
        'schema': 'receptor-conformer-evidence-v1',
        'source_sha256': 'a' * 64,
        'source_site_mapping': {'author_residue_id': '773'},
        'model': 1,
        'target_altloc': None,
        'residues': [
            {'author_residue_id': '751', 'alternatives': {'A': [], 'B': []}},
            {'author_residue_id': '831', 'alternatives': {'A': [], 'B': []}},
        ],
        'observation': 'Deposited alternatives only.',
        'receptor_prepared': False,
        'compute_submitted': False,
    }
    return {**payload, 'evidence_sha256': digest(payload)}


def test_limitation_list_is_validated_and_normalized_to_v1_string():
    current = evidence()
    result = validate_selection(current, {
        'evidence_sha256': current['evidence_sha256'],
        'choices': {'751': 'A', '831': 'A'},
        'rationale': 'Deterministic deposited-order tie break.',
        'limitations': ['No scientific superiority is claimed.', 'B remains unevaluated.'],
    })
    assert result['selection']['limitations'] == (
        'No scientific superiority is claimed.\nB remains unevaluated.')


@pytest.mark.parametrize('limitations', [[], [''], ['valid', '  ']])
def test_limitation_list_rejects_empty_items(limitations):
    current = evidence()
    with pytest.raises(ValueError, match='nonempty limitations'):
        validate_selection(current, {
            'evidence_sha256': current['evidence_sha256'],
            'choices': {'751': 'A', '831': 'A'},
            'rationale': 'Explicit fixture choice.',
            'limitations': limitations,
        })
