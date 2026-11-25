"""Real synthetic 20k-prefix producer outputs through signed native readback.

No remote/vendor worker runs and this is not research or web acceptance.
"""
import json

import pandas as pd
import pytest
from rdkit import Chem

from covscreen.execution import JobManager
from covscreen.docking_policy import POLICY
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.library_vs_prescreen_input import load as load_input
from covscreen.library_vs_prescreen_evidence import evaluate
from covscreen.mcp.server import dispatch
from covscreen.workflow_store import load
from test_library_workflow import call, state_file
from test_receptor_handoff import bound
from test_workflow_screening_inputs import registration
from test_workflow_screening_plan import REVIEW
from test_library_vs_prescreen import grant


def registered_prescreen(bound, monkeypatch, positive=True, additional_smiles=()):
    from covscreen.covindb import run_prescreen
    root, initial = bound
    source, _ = registration(root)
    first = 'c1cc(OCC)cc(NC(=O)C=C)c1' if positive else 'C'
    selected_rows = [first, *additional_smiles]
    source.write_text('SMILES,InChI_Key,Source,CompoundType\n'
        + ''.join(smiles + ',software,fixture,synthetic\n' for smiles in selected_rows)
        + 'C,software,fixture,synthetic\n' * (20000 - len(selected_rows)))
    reviewed = call(root, initial, RESEARCH, 'review_receptor')
    frozen = call(root, reviewed, RESEARCH, 'prepare_screening_inputs', profile='egfr-c797-acrylamide-research-pilot-v1')
    planned = call(root, frozen, RESEARCH, 'plan_vs_prescreen', scientific_review=REVIEW)
    monkeypatch.setitem(POLICY, 'native_vs_workflow_ready', True)
    queued = dispatch(root, RESEARCH, grant(root, planned))
    state = load(state_file(root, queued))
    manager = JobManager(root)
    run_id = state['vs_prescreen']['run_id']
    spec = manager.registry.load_spec(run_id)
    result = manager.registry.load_result(run_id)
    record, _ = load_input(root, state['vs_prescreen']['input'])
    output = manager.registry.job_dir(run_id) / 'artifacts'
    manifest = run_prescreen(root / record['roles']['library'], root / record['roles']['science_config'], output,
        top_n=5, max_rows=20000, chunk_size=5000, seed=20260725)
    assert manifest['selection']['top_n_selected'] == (len(selected_rows) if positive else 0)
    # Register actual software fixture products without invoking the remote worker.
    result.state, result.return_code = ('COMPLETED', 0) if positive else ('FAILED', 2)
    result.failure_reason = None if positive else 'engine_return_code_2'
    result.artifacts = [manager.registry.register_file(run_id, path, 'other') for path in sorted(output.iterdir()) if path.is_file()]
    manager.registry.write_result(result)
    observed = call(root, queued, RESEARCH, 'vs_prescreen_status')
    checked = call(root, observed, RESEARCH, 'evaluate_vs_prescreen')
    return root, checked, manager, spec, result, manifest, output


@pytest.mark.parametrize('positive', [True, False])
def test_registered_prescreen_readback_maps_actual_sdf_and_preserves_zero_candidates(bound, monkeypatch, positive):
    root, checked, manager, spec, result, manifest, output = registered_prescreen(bound, monkeypatch, positive)
    state = load(state_file(root, checked))
    run_id = spec.run_id
    summary = checked['vs_prescreen']['evaluation_summary']
    assert summary['candidate_count'] == (1 if positive else 0)
    assert summary['counts']['input_rows'] == 20000
    assert summary['vs_input_eligible'] is positive
    assert checked['assessment']['state'] != 'passed'
    reference = checked['vs_prescreen']['evaluation_artifact']
    if positive:
        mapped = list(Chem.SDMolSupplier(str(root / reference['directory'] / 'vs-candidates.sdf')))[0]
        maps = {atom.GetAtomMapNum(): atom.GetIdx() for atom in mapped.GetAtoms() if atom.GetAtomMapNum()}
        assert set(maps) == {905, 906}
        assert mapped.GetBondBetweenAtoms(maps[905], maps[906]).GetBondType() == Chem.BondType.DOUBLE
        row = pd.read_parquet(output / 'stage2_candidates.parquet').to_dict('records')[0]
        assert row['electrophile_atom_index'] != maps[906]  # canonical SDF changes atom order
        assert summary['candidates'][0]['source_record_index'] == 0
    else:
        assert 'prepare_vs_input' not in checked['workflow']['tools'].get(RESEARCH, {}).get('actions', [])
        from covscreen.library_vs_input import freeze
        with pytest.raises(ValueError, match='nonempty eligible'):
            freeze(root, state)
        assert summary['selection_outcome'] == 'NO_CANDIDATES_SELECTED'
        assert not summary['quality_gates']['selected_warhead_mapping_rate_ge_0_98']
        assert 'vs-candidates.sdf' not in reference['files']
    before = load(state_file(root, checked))
    current = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
    assert current['workflow']['revision'] == checked['workflow']['revision']
    # Signed task completion is insufficient: changed output bytes revoke review.
    manifest_path = output / 'stage2_selection_manifest.json'
    original = manifest_path.read_bytes()
    manifest_path.write_bytes(original + b' ')
    with pytest.raises(ValueError, match='Navigation unavailable'):
        call(root, checked, ENTRY, 'reconsider', return_to='structure', reason='Do not archive changed evidence using a cached terminal label')
    rejected = dispatch(root, ENTRY, {'action': 'status', 'workflow_id': state['workflow_id']})
    assert rejected['vs_prescreen']['output_validation'] == 'EVIDENCE_REJECTED'
    assert 'evaluation_summary' not in rejected['vs_prescreen']
    assert rejected['execution']['state'] == 'outcome_unknown'
    manifest_path.write_bytes(original)
    restored = dispatch(root, ENTRY, {'action': 'resume', 'workflow_id': state['workflow_id']})
    assert restored['vs_prescreen']['evaluation_summary'] == summary
    assert restored['vs_prescreen']['run_id'] == run_id
    # Even re-registered contradictory records cannot pass the independent reader.
    wrong = {**manifest, 'source': {**manifest['source'], 'records_read': 19999}}
    manifest_path.write_text(json.dumps(wrong))
    changed = result.model_copy(deep=True)
    changed.artifacts = [manager.registry.register_file(run_id, path, 'other') for path in sorted(output.iterdir()) if path.is_file()]
    with pytest.raises(ValueError, match='frozen source'):
        evaluate(root, before, manager, spec, changed)
    manifest_path.write_bytes(original)
    reconsidered = call(root, restored, ENTRY, 'reconsider', return_to='structure',
        reason='Archive this terminal software branch and retain all native task evidence')
    archived = load(state_file(root, reconsidered))
    assert archived['vs_prescreen'] is None
    assert archived['branches'][-1]['vs_prescreen']['run_id'] == run_id
    assert len(list((root / 'state/jobs').glob('*/spec.json'))) == 2
