from pathlib import Path
import pytest
from covscreen.covdock_receptor import inspect_receptor
from covscreen_de_novo.covdock_input import REGISTRY, freeze_receptor


def test_registered_receptor_keeps_all_atoms_and_coordinates_and_passes_strict_reader(tmp_path):
    source = REGISTRY/'assets/protein_A.pdb'
    with pytest.raises(ValueError, match='alternate coordinates'):
        inspect_receptor(source, 'A:797')
    output = tmp_path/'derived'
    receipt = freeze_receptor(source, output)
    original = source.read_text().splitlines()
    changed = (output/'receptor.pdb').read_text().splitlines()
    assert len(original) == len(changed)
    assert all(a[:16]+a[17:] == b[:16]+b[17:] for a,b in zip(original, changed))
    checked = inspect_receptor(output/'receptor.pdb', 'A:797', expected_sha256=receipt['output_sha256'])
    assert checked['receptor_sha256'] == receipt['output_sha256']
    assert receipt['atom_count'] == 2471
    assert len(receipt['normalized_atoms']) == 9
    assert receipt['selection'] == {'A:970': 'A'}
    assert receipt['atoms_removed'] == 0 and not receipt['coordinates_changed']
    assert freeze_receptor(source, output) == receipt


def test_changed_source_and_tampered_frozen_derivation_are_rejected(tmp_path):
    source = REGISTRY/'assets/protein_A.pdb'
    changed = tmp_path/'changed.pdb'
    changed.write_bytes(source.read_bytes().replace(b'ALYS A 970', b'BLYS A 970'))
    assert changed.read_bytes() != source.read_bytes()
    with pytest.raises(ValueError, match='differs from registered'):
        freeze_receptor(changed, tmp_path/'not-created')
    output = tmp_path/'derived'
    freeze_receptor(source, output)
    (output/'receptor.pdb').write_text('changed')
    with pytest.raises(ValueError, match='derivation changed'):
        freeze_receptor(source, output)
