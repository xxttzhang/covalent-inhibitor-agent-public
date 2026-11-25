"""Official chemistry to native docking routing; no remote computation."""
import json
from pathlib import Path

from covscreen.ligand_design import write, seal, design_folder
from covscreen.jobs import sha256_file
from test_ligand_design import acquisition, design, full_parent_design

def test_official_enumeration_routes_to_direct_lo_and_resumes(full_parent_design, tmp_path):
    from covscreen.ligand_workflow import DESIGN, navigate, step, directory
    from covscreen.ligand_refinement_input import load_refinement_input

    # Complete the synthetic study lineage used by the 1M17 chemistry fixture.
    folder = design_folder(tmp_path, full_parent_design)
    identity = json.loads((Path(__file__).parent / 'fixtures/refinement-target-2ity/uniprot.json').read_text())
    write(folder / 'uniprot.json', identity)
    evidence = tmp_path / 'results/target-hypotheses' / ('hyp-' + 'b' * 24)
    data = json.loads((evidence / 'evidence.json').read_text())
    data.update(target='EGFR', site='C797', taxon=9606,
                identity={'uniprot_id': 'P00533', 'sequence': identity['sequence']['value']})
    write(evidence / 'evidence.json', data)
    seal(evidence)
    record = json.loads((folder / 'design.json').read_text())
    record['evidence_manifest_sha256'] = sha256_file(evidence / 'manifest.json')
    write(folder / 'design.json', record)
    seal(folder)
    initial = navigate(tmp_path, 'start', request_id='official-enumeration-direct-lo',
                       target='EGFR', site='C797', taxon=9606)
    identity = initial['workflow']['workflow_id']
    path = directory(tmp_path, identity) / 'state.json'
    state = json.loads(path.read_text())
    # Only the reviewed parent input is a fixture; enumeration and routing run normally.
    state.update(stage='DESIGN_INPUTS_READY', design_id=full_parent_design,
                 evidence_id='hyp-' + 'b' * 24)
    write(path, state)
    generated = step(tmp_path, DESIGN, workflow_id=identity,
                     expected_revision=state['revision'], action='generate')
    routed = step(tmp_path, DESIGN, workflow_id=identity,
                  expected_revision=generated['workflow']['revision'],
                  action='prepare_docking_input', docking_route='auto')
    assert routed['workflow']['stage'] == 'DIRECT_LO_READY'
    saved = json.loads(path.read_text())
    payload = load_refinement_input(tmp_path, saved)
    assert 0 < payload['input_count'] <= generated['result']['candidate_count'] == 4
    assert payload['routing_decision']['selected_route'] == 'direct_lo'
    assert not saved.get('vs_execution') and not saved.get('pose_execution')
    resumed = navigate(tmp_path, 'resume', workflow_id=identity)
    assert resumed['refinement_input']['artifact_id'] == saved['refinement_input']['artifact_id']
    assert not list(tmp_path.glob('state/jobs/*/spec.json'))
