import hashlib
import json
from pathlib import Path

import pytest

from covscreen.receptor_admission import admit_isolated_receptor
from covscreen.target_acquisition_artifact import load_target_acquisition


ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT / 'reports/library-first-site-acquisition-20261014'
ACQUISITION = ROOT / 'reports/library-first-target-identity-20261014/state/target_acquisition/3fe7754be1b14546a7141aa26cebde6b'
HEAVY = REPORT / 'heavy-repair-regression'
RELAXATION = REPORT / 'receptor-relaxation-regression-v4'
QUALITY = REPORT / 'receptor-relaxation-regression-v4-quality.json'
GEOMETRY = REPORT / 'receptor-relaxation-regression-v4-geometry.json'
SEQUENCE = ''.join((ROOT / 'science/tests/fixtures/target_identity/P00533.fasta').read_text().splitlines()[1:])


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_legacy_acquisition_recomputes_missing_construct_audit_from_raw_evidence():
    stored = json.loads((ACQUISITION / 'target_acquisition.json').read_text())
    assert 'construct_audit' not in stored
    loaded = load_target_acquisition(ACQUISITION, sha(ACQUISITION / 'target_acquisition.json'),
                                     SEQUENCE, 9606)
    assert loaded['construct_audit_recomputed_from_raw'] is True
    assert loaded['construct_audit']['source_species_verified'] is True
    assert loaded['construct_audit']['sequence_differences'] == []


def test_real_regression_admits_only_isolated_protein_scope(tmp_path):
    output = tmp_path / 'prepared'
    result = admit_isolated_receptor(ACQUISITION, sha(ACQUISITION / 'target_acquisition.json'),
        SEQUENCE, 9606, HEAVY, RELAXATION, sha(RELAXATION / 'receipt.json'),
        QUALITY, sha(QUALITY), GEOMETRY, sha(GEOMETRY), output)
    assert result['status'] == 'prepared_isolated_protein_scope'
    assert result['receptor_prepared'] is True
    assert result['prepared_scope'] == 'isolated_selected_chain_protein'
    assert result['campaign_ready'] is False
    environment = json.loads((output / 'environment-decision.json').read_text())
    assert [(row['resname'], row['atom_count']) for row in environment['nonwater_components']] == [('AQ4', 29)]
    assert len(environment['crystal_waters']) == 20
    assert sum(row['atom_count'] for row in environment['crystal_waters']) == 20
    manifest = json.loads((output / 'prepared-receptor.json').read_text())
    assert all(sha(output / name) == digest for name, digest in manifest['artifacts'].items())


def test_rehashed_failed_geometry_cannot_be_admitted(tmp_path):
    geometry = json.loads(GEOMETRY.read_text())
    geometry['status'] = 'geometry_quality_failed'
    path = tmp_path / 'geometry.json'
    path.write_text(json.dumps(geometry))
    with pytest.raises(ValueError, match='did not pass'):
        admit_isolated_receptor(ACQUISITION, sha(ACQUISITION / 'target_acquisition.json'),
            SEQUENCE, 9606, HEAVY, RELAXATION, sha(RELAXATION / 'receipt.json'),
            QUALITY, sha(QUALITY), path, sha(path), tmp_path / 'prepared')


def test_admission_never_overwrites_an_existing_directory(tmp_path):
    with pytest.raises(ValueError, match='new directory'):
        admit_isolated_receptor(ACQUISITION, sha(ACQUISITION / 'target_acquisition.json'),
            SEQUENCE, 9606, HEAVY, RELAXATION, sha(RELAXATION / 'receipt.json'),
            QUALITY, sha(QUALITY), GEOMETRY, sha(GEOMETRY), tmp_path)
