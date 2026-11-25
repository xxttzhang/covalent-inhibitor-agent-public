import hashlib
import json
from pathlib import Path

import pytest

from covscreen.receptor_preparation import project_receptor, inspect_receptor, build_receptor_heavy_atoms


@pytest.fixture
def inputs():
    fixtures = Path(__file__).parent / 'fixtures'
    source = Path(__file__).parents[1] / 'projects/egfr-c797/ligand_first/source_structures/1M17.pdb'
    return [source, hashlib.sha256(source.read_bytes()).hexdigest(),
        json.loads((fixtures / 'target_acquisition/entity_1.json').read_text()),
        json.loads((fixtures / 'target_acquisition/chain_A.json').read_text()),
        'P00533', 797,
        ''.join((fixtures / 'target_identity/P00533.fasta').read_text().splitlines()[1:]), 9606]


def test_construct_inventory_excludes_unobserved_atoms_without_altering_source(inputs):
    projection, report = project_receptor(*inputs, altlocs={'751': 'A', '831': 'A'})
    assert len(report['missing_construct_residues']) == 22
    assert len(report['retained_atoms']) == 2460
    assert sum(a['reason'] == 'zero_occupancy_requires_rebuilding' for a in report['excluded_atoms']) == 37
    assert hashlib.sha256(inputs[0].read_bytes()).hexdigest() == inputs[1]
    assert report['receptor_prepared'] is False
    assert 'SEQRES' in projection
    assert report['mapping']['author_residue_id'] == '773'


@pytest.mark.parametrize('choices', [None, {}, {'751': 'A'}, {'751': 'C', '831': 'A'},
    {'751': 'A', '831': 'A', '773': 'A'}])
def test_alternate_conformers_are_never_implicitly_selected(inputs, choices):
    with pytest.raises(ValueError, match='alternate location'):
        project_receptor(*inputs, altlocs=choices)


def test_modified_source_is_rejected(inputs):
    inputs[1] = '0' * 64
    with pytest.raises(ValueError, match='checksum'):
        project_receptor(*inputs, altlocs={'751': 'A', '831': 'A'})


def test_ambiguous_author_numbering_is_rejected(inputs):
    ids = inputs[3]['rcsb_polymer_entity_instance_container_identifiers']['auth_to_entity_poly_seq_mapping']
    ids[0] = ids[1]
    with pytest.raises(ValueError, match='unique and complete'):
        project_receptor(*inputs, altlocs={'751': 'A', '831': 'A'})


def test_insertion_code_is_preserved_in_identity_and_projected_by_entity_index(inputs, tmp_path):
    # Change one author identifier consistently; its entity position remains 108.
    inputs[3]['rcsb_polymer_entity_instance_container_identifiers']['auth_to_entity_poly_seq_mapping'][107] = '773A'
    source = tmp_path / 'inserted.pdb'
    source.write_text('\n'.join(line[:26] + 'A' + line[27:]
        if line.startswith('ATOM') and line[21] == 'A' and line[22:26].strip() == '773'
        else line for line in inputs[0].read_text().splitlines()) + '\n')
    inputs[:2] = [source, hashlib.sha256(source.read_bytes()).hexdigest()]
    _, report = project_receptor(*inputs, altlocs={'751': 'A', '831': 'A'})
    site = [a for a in report['retained_atoms'] if a['residue'] == '773A']
    assert len(site) == 6
    assert {a['entity_position'] for a in site} == {108}
    assert report['mapping']['author_residue_id'] == '773A'


def test_actual_pdbfixer_inventory_agrees_with_construct_map(inputs):
    pytest.importorskip('pdbfixer')
    pytest.importorskip('openmm')
    report = inspect_receptor(*inputs, altlocs={'751': 'A', '831': 'A'})
    assert len(report['missing_construct_residues']) == 22
    assert sum(len(row['atoms']) for row in report['missing_heavy_atoms']) == 28
    assert report['coordinates_modified'] is False
    assert report['runtime']['pdbfixer']


@pytest.mark.parametrize('override', [{'seed': 0}, {'seed': True}, {'max_missing_residues': 101},
    {'max_missing_residues': -1}, {'environment_policy': 'auto'}])
def test_heavy_repair_requires_bounded_explicit_policy(inputs, tmp_path, override):
    output = tmp_path / 'repair'
    policy = dict(output=output, seed=42, max_missing_residues=30,
                  environment_policy='isolated_protein_proposal')
    with pytest.raises(ValueError):
        build_receptor_heavy_atoms(*inputs, **{**policy, **override})
    assert not output.exists()


def test_heavy_repair_does_not_overwrite_existing_results(inputs, tmp_path):
    with pytest.raises(ValueError, match='new directory'):
        build_receptor_heavy_atoms(*inputs, output=tmp_path, seed=42,
            max_missing_residues=30, environment_policy='isolated_protein_proposal')
