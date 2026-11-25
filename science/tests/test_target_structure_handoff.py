"""Use captured public identity bytes to verify artifact handoff and failure gates."""
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from covscreen.mcp.server import dispatch
from covscreen.structure_discovery import build_query, build_target_query
from covscreen.target_identity import load_target_identity


@pytest.fixture
def artifact(tmp_path):
    source = Path(__file__).parent / 'fixtures/target_identity'
    folder = tmp_path / 'state/target_identity' / ('a' * 32)
    folder.mkdir(parents=True)
    for a, b in [('egfr-manifest.json', 'target_identity.json'),
                 ('egfr.json', 'uniprot.json'), ('P00533.fasta', 'P00533.fasta')]:
        shutil.copyfile(source / a, folder / b)
    digest = hashlib.sha256((folder / 'target_identity.json').read_bytes()).hexdigest()
    return folder, digest


def arguments(digest):
    return {'target_artifact_id': 'target-' + 'a' * 32, 'target_manifest_sha256': digest}


def test_handoff_consumes_verified_identity_without_user_paths_or_ligand(tmp_path, artifact):
    folder, digest = artifact
    result = dispatch(tmp_path, 'covalent_library_first_discover_structures', arguments(digest))
    assert result['plan']['uniprot'] == 'P00533'
    assert result['network_request_sent'] is False
    assert result['calculation_submitted'] is False
    assert len(result['plan']['query']['query']['nodes']) == 2
    assert not (tmp_path / 'state/target_structures').exists()


@pytest.mark.parametrize('name', ['target_identity.json', 'uniprot.json', 'P00533.fasta'])
def test_modified_artifact_bytes_cannot_reach_discovery(tmp_path, artifact, name):
    folder, digest = artifact
    with (folder / name).open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError):
        dispatch(tmp_path, 'covalent_library_first_discover_structures', {**arguments(digest), 'action': 'search'})
    assert not (tmp_path / 'state/target_structures').exists()


def test_recomputed_manifest_hash_cannot_hide_identity_corruption(artifact):
    folder, _ = artifact
    path = folder / 'target_identity.json'
    data = json.loads(path.read_text())
    data['identity']['candidates'][0]['canonical_cysteines'] = [1]
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='raw UniProt evidence'):
        load_target_identity(folder, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.parametrize('update', [
    {'total_results_header': None}, {'total_results_header': '2'},
    {'link_header': '<https://rest.uniprot.org/next>; rel="next"'},
    {'path': '../outside'}, {'url': 'https://example.org/'},
])
def test_ambiguous_or_redirected_source_handoff_is_rejected(artifact, update):
    folder, _ = artifact
    path = folder / 'target_identity.json'
    data = json.loads(path.read_text())
    data['sources'][0].update(update)
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load_target_identity(folder, hashlib.sha256(path.read_bytes()).hexdigest())


def test_id_traversal_and_symlink_are_rejected(tmp_path, artifact):
    folder, digest = artifact
    with pytest.raises(ValueError, match='artifact ID'):
        dispatch(tmp_path, 'covalent_library_first_discover_structures',
                 {**arguments(digest), 'target_artifact_id': '../outside'})
    alias = folder.parent / ('b' * 32)
    alias.symlink_to(folder)
    with pytest.raises(ValueError, match='symbolic link'):
        dispatch(tmp_path, 'covalent_library_first_discover_structures',
                 {**arguments(digest), 'target_artifact_id': 'target-' + 'b' * 32})


def test_ligand_route_still_requires_explicit_parent_component():
    assert len(build_target_query('P00533', 3)['query']['nodes']) == 2
    assert len(build_query('P00533', 'AQ4', 3)['query']['nodes']) == 3
    with pytest.raises(ValueError, match='CCD'):
        build_query('P00533', None, 3)
