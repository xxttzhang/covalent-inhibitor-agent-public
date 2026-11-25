"""Shared source replay must retain the batch before a VS top-N choice."""
import json

import pytest

from covscreen.ligand_candidate_source import read_design
from covscreen.ligand_design import design_folder, seal
from covscreen.ligand_docking_policy import decide
from test_vs_input_workflow import study, design


def test_complete_batch_is_available_without_a_vs_task(study):
    root, path = study
    state = json.loads(path.read_text())
    before = path.read_bytes()
    source = read_design(root, state)
    expected = json.loads((design_folder(root, state['design_id']) /
                           'generation/candidates.json').read_text())['candidates']
    assert source['rows'] == expected
    assert len(source['rows']) == 3  # Actual bounded 2ITY fixture, not a six-candidate proof.
    assert len(source['admitted_indices']) + len(source['deferrals']) == len(expected)
    assert len(source['admitted_indices']) == len(expected)
    assert decide(len(source['admitted_indices']))['selected_route'] == 'direct_lo'
    assert path.read_bytes() == before
    assert not list(root.glob('state/jobs/*/spec.json'))
    assert not state.get('vs_execution')


def test_even_resealed_candidate_identity_tampering_is_rejected(study):
    root, path = study
    state = json.loads(path.read_text())
    directory = design_folder(root, state['design_id']) / 'generation'
    candidates = directory / 'candidates.json'
    value = json.loads(candidates.read_text())
    value['candidates'][0]['canonical_smiles'] = 'C'
    candidates.write_text(json.dumps(value))
    seal(directory)
    with pytest.raises(ValueError):
        read_design(root, state)
