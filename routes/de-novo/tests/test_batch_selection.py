import pytest

from covscreen_de_novo.batch_chemical_qc import assess
from covscreen_de_novo.batch_diversity import select
from covscreen_de_novo.batch_selection import prepare, preparation_records
from covscreen_de_novo.batch_input import export, validate


@pytest.mark.parametrize('cpu', [0, 56, True, 2.5])
def test_vs_rejects_cpu_outside_authorized_instance_budget_before_preparation(tmp_path, cpu):
    from covscreen_de_novo.batch_vs import compile_request
    with pytest.raises(ValueError, match='1 to 55 CPUs'):
        compile_request(tmp_path, 'a' * 64, None, cpu_count=cpu)
    assert list(tmp_path.iterdir()) == []


def test_batch_input_revalidates_transport_and_rejects_tampering(tmp_path, monkeypatch):
    import hashlib
    import json
    selection = {'task_id': 'task', 'provenance': {}, 'diversity': {'selected': [
        {'attempt_id': 'task:0:raw', 'source_sha256': 'a' * 64,
         'canonical_smiles': 'C=CC(=O)NC'}]}}
    folder = tmp_path.resolve() / 'batch'
    export(selection, folder)
    receptor = b'engineering receptor fixture'
    (folder / 'receptor.pdb').write_bytes(receptor)
    digest = hashlib.sha256((folder / 'input.json').read_bytes()).hexdigest()
    (folder / 'binding.json').write_text(json.dumps({'manifest_sha256': digest,
        'attachment_residue': 'A:797', 'receptor_sha256': hashlib.sha256(receptor).hexdigest()}))
    assert validate(folder, digest)[0]['candidate_count'] == 1
    from rdkit import Chem
    molecule = next(iter(Chem.SDMolSupplier(str(folder / 'ligands.sdf'), removeHs=False)))
    assert molecule.GetIntProp('INPUT_SDF_INDEX') == 0
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'runtime/batch_transport.py'
    spec = importlib.util.spec_from_file_location('batch_transport_test', script)
    transport = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transport)
    monkeypatch.delenv('COVALENT_RUNTIME_ROOT', raising=False)
    with pytest.raises(ValueError, match='Explicit acceptance runtime'):
        transport.transport(folder, digest, tmp_path)
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'binding.json').write_text(json.dumps({'source': '/different-source'}))
    monkeypatch.setenv('COVALENT_RUNTIME_ROOT', str(runtime))
    with pytest.raises(ValueError, match='different source'):
        transport.transport(folder, digest, tmp_path)
    with (folder / 'ligands.sdf').open('a') as stream:
        stream.write('\n')
    with pytest.raises(ValueError, match='ligand file changed'):
        validate(folder, digest)


def test_native_batch_uses_raw_once_and_keeps_geometry_separate():
    results = {'state': 'RESULTS_READ', 'requested_slots': 1, 'task_id': 'task',
        'provenance': {}, 'candidates': [
            {'representation': 'raw', 'slot': 0, 'candidate_id': 'task:0:raw',
             'smiles': 'C=CC(=O)NC', 'source_sha256': 'abc',
             'structural_qc_passed': False, 'failures': ['geometry']},
            {'representation': 'relaxed', 'slot': 0}]}
    result = prepare(results)
    assert result['chemical_qc']['attempted'] == 1
    assert result['diversity']['selected_count'] == 1
    assert result['diversity']['selected'][0]['generation_geometry_failures'] == ['geometry']
    from rdkit import Chem
    record = preparation_records(result)[0]
    molecule = Chem.MolFromSmiles(record['mapped_smiles'])
    beta = next(a for a in molecule.GetAtoms()
                if a.GetAtomMapNum() == record['reaction_atom_maps']['beta'])
    assert beta.GetTotalNumHs() == 2
    assert record['reaction_atom_maps']['alpha'] == 905
    assert record['reaction_atom_maps']['beta'] == 906
    assert molecule.GetNumConformers() == 0
    assert record['parent_id'] == 'task:0:raw'
    results['candidates'].append(results['candidates'][0])
    with pytest.raises(ValueError, match='every requested slot'):
        prepare(results)


def test_qc_preserves_failures_and_deduplicates_before_selection():
    smiles = ['C=CC(=O)NC', 'CNC(=O)C=C', '', 'CC',
              'C=CC(=O)NC.Cl', 'C=CC(=O)NC(C)CC', 'C=CC(=O)NCC']
    qc = assess([{'attempt_id': str(i), 'smiles': s} for i, s in enumerate(smiles)])
    assert qc['attempted'] == 7
    assert qc['identity_eligible'] == 2
    assert qc['rows'][1]['duplicate_of'] == '0'
    assert 'INVALID_OR_EMPTY_SMILES' in qc['rows'][2]['reasons']
    assert 'WARHEAD_MISSING_OR_AMBIGUOUS' in qc['rows'][3]['reasons']
    assert 'DISCONNECTED_COMPONENTS' in qc['rows'][4]['reasons']
    assert 'UNRESOLVED_STEREOCHEMISTRY' in qc['rows'][5]['reasons']
    result = select(qc, limit=1)
    assert result['selected_count'] == 1
    assert len(result['deferred']) == 1
    assert select(qc, limit=150)['selected_count'] == 2
    assert select(qc, limit=1) == result


def test_duplicate_attempt_ids_and_invalid_limits_rejected():
    with pytest.raises(ValueError, match='Duplicate attempt IDs'):
        assess([{'attempt_id': 'same'}, {'attempt_id': 'same'}])
    with pytest.raises(ValueError, match='limit'):
        select({'rows': []}, limit=201)
    assert select(assess([]))['selected_count'] == 0


def test_qc_rejects_nonring_aromatic_bond_types_before_ligprep():
    # RDKit parses this generated graph despite its non-ring aromatic bond types.
    bad = 'C=CC(=O)Nc1cccc(N(:C:N:C(:NCCCN)c2cccnc2):N:C)c1'
    good = 'C=CC(=O)Nc1ccccc1'
    qc = assess([{'attempt_id': 'bad', 'smiles': bad},
                 {'attempt_id': 'good', 'smiles': good}])
    assert qc['identity_eligible'] == 1
    assert 'INCONSISTENT_AROMATIC_BONDS' in qc['rows'][0]['reasons']
    assert len(qc['rows'][0]['inconsistent_aromatic_bonds']) == 6
    assert qc['rows'][1]['identity_eligible'] is True
    assert qc['rows'][1]['inconsistent_aromatic_bonds'] == []
