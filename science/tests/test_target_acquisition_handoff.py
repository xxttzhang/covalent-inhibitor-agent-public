import hashlib
import json
from pathlib import Path
import shutil
import io

import pytest

from covscreen.mcp.server import dispatch


@pytest.fixture
def handoff(tmp_path):
    fixtures = Path(__file__).parent / 'fixtures'
    discovery = json.loads((fixtures / 'target_discovery/egfr-manifest.json').read_text())
    target = tmp_path / 'state/target_identity' / discovery['plan']['target_artifact_id'][7:]
    target.mkdir(parents=True)
    for a, b in [('egfr-manifest.json', 'target_identity.json'),
                 ('egfr.json', 'uniprot.json'), ('P00533.fasta', 'P00533.fasta')]:
        shutil.copyfile(fixtures / 'target_identity' / a, target / b)
    folder = tmp_path / 'state/target_structures' / ('c' * 32)
    folder.mkdir(parents=True)
    for a, b in [('egfr-manifest.json', 'target_structure_discovery.json'), ('egfr-rcsb.json', 'rcsb_response.json')]:
        shutil.copyfile(fixtures / 'target_discovery' / a, folder / b)
    args = {'discovery_artifact_id': 'structures-' + 'c' * 32,
            'discovery_manifest_sha256': hashlib.sha256((folder / 'target_structure_discovery.json').read_bytes()).hexdigest(),
            'entry': '1M17', 'cys': 797}
    return folder, args


def test_actual_parent_chain_can_plan_without_network_or_new_artifacts(tmp_path, handoff):
    _, args = handoff
    result = dispatch(tmp_path, 'covalent_library_first_acquire_structure', args)
    assert result['plan']['uniprot'] == 'P00533'
    assert result['plan']['cys'] == 797
    assert result['network_request_sent'] is False
    assert not (tmp_path / 'state/target_acquisition').exists()


@pytest.mark.parametrize('updates', [{'cys': 1}, {'cys': True}, {'entry': '5P9J'},
    {'model': 0}, {'altloc': 'AB'}, {'label_chain': '../outside'}])
def test_bad_selection_stops_before_acquisition(tmp_path, handoff, updates):
    _, args = handoff
    with pytest.raises(ValueError):
        dispatch(tmp_path, 'covalent_library_first_acquire_structure', {**args, **updates, 'action': 'acquire'})
    assert not (tmp_path / 'state/target_acquisition').exists()


@pytest.mark.parametrize('field', ['entry_ids', 'gene'])
def test_recomputed_checksum_cannot_hide_forged_handoff(tmp_path, handoff, field):
    folder, args = handoff
    path = folder / 'target_structure_discovery.json'
    data = json.loads(path.read_text())
    if field == 'entry_ids':
        data['discovery']['entry_ids'].append('5P9J')
    else:
        data['plan']['gene'] = 'BTK'
    path.write_text(json.dumps(data))
    args['discovery_manifest_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='differs'):
        dispatch(tmp_path, 'covalent_library_first_acquire_structure', {**args, 'action': 'acquire'})
    assert not (tmp_path / 'state/target_acquisition').exists()


def test_raw_discovery_corruption_is_detected(tmp_path, handoff):
    folder, args = handoff
    (folder / 'rcsb_response.json').write_text('{}')
    with pytest.raises(ValueError, match='source checksum'):
        dispatch(tmp_path, 'covalent_library_first_acquire_structure', {**args, 'action': 'acquire'})


def test_acquisition_replays_real_sources_and_audits_construct(tmp_path, handoff, monkeypatch):
    """Only HTTP transport is replaced; mapping, auditing and MCP dispatch run normally."""
    from covscreen import structure_acquisition
    fixtures = Path(__file__).parent / 'fixtures/target_acquisition'
    sources = json.loads((fixtures / 'source.json').read_text())
    pdb = Path(__file__).parents[1] / 'projects/egfr-c797/ligand_first/source_structures/1M17.pdb'
    responses = {s['url']: (pdb if s['path'] == 'structure.pdb' else fixtures / s['path']).read_bytes()
                 for s in sources}
    for source in sources:
        assert hashlib.sha256(responses[source['url']]).hexdigest() == source['sha256']
    calls = []
    def fetch(url, timeout):
        calls.append(url)
        return io.BytesIO(responses[url])
    monkeypatch.setattr(structure_acquisition.urllib.request, 'urlopen', fetch)
    _, args = handoff
    result = dispatch(tmp_path, 'covalent_library_first_acquire_structure', {**args, 'action': 'acquire'})
    assert result['status'] == 'PASS'
    assert len(calls) == 4
    audit = result['result']['construct_audit']
    assert audit['aligned_positions'] == 328
    assert audit['source_species_verified'] is True
    assert audit['sequence_differences'] == []
    assert result['result']['receptor_prepared'] is False
    manifest = tmp_path / result['artifact_directory'] / 'target_acquisition.json'
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == result['manifest_sha256']
    assert json.loads(manifest.read_text())['construct_audit'] == audit
