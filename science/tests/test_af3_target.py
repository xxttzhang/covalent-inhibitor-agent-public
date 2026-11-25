"""Chain and variant-sequence guards; no AF3 model or molecular GPU job."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from covscreen.af3_target import read_target_seed, replace_design_ligand, validate_target
from test_refinement_job_contract import configured


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    sys.path.insert(0, str(ROOT/'scripts'))
    try:
        spec = importlib.util.spec_from_file_location('test_target_'+name, ROOT/'scripts'/f'{name}.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


def seed():
    return {'sequences': [
        {'protein': {'id': 'B', 'sequence': 'AAA', 'templates': []}},
        {'protein': {'id': ['A', 'D'], 'sequence': 'ACG', 'unpairedMsa': 'query'}},
        {'ligand': {'id': 'G', 'ccdCodes': ['MG']}},
        {'ligand': {'id': 'L', 'ccdCodes': ['OLD']}}],
        'bondedAtomPairs': [[['A', 2, 'SG'], ['L', 1, 'C8']]]}


def test_explicit_chain_not_first_protein_and_all_polymers_preserved():
    original = seed()
    frozen = deepcopy(original)
    result, evidence = replace_design_ligand(original, 'A', 2, 'L', 'NEW', 'C1')
    assert original == frozen
    assert result['sequences'][:3] == original['sequences'][:3]
    assert result['sequences'][3] == {'ligand': {'id': 'L', 'ccdCodes': ['NEW']}}
    assert result['bondedAtomPairs'] == [[['A', 2, 'SG'], ['L', 1, 'C1']]]
    assert evidence['protein_sequence_sha256'] == hashlib.sha256(b'ACG').hexdigest()
    assert evidence['study_target_identity_verified'] is False
    assert evidence['canonical_mapping_verified'] is False
    assert evidence['grants_resource_approval'] is False


@pytest.mark.parametrize('chain,index,atom,reason', [
    ('Z', 2, 'SG', 'absent'), ('G', 2, 'SG', 'absent'),
    ('B', 2, 'SG', 'Cys SG'), ('A', 1, 'SG', 'Cys SG'),
    ('A', 2, 'CA', 'Cys SG'), ('A', 0, 'SG', '1-based'),
    ('A', 4, 'SG', '1-based'), ('A', 2.0, 'SG', '1-based'),
    ('A', True, 'SG', '1-based')])
def test_invalid_chain_or_site_rejected(chain, index, atom, reason):
    with pytest.raises(ValueError, match=reason):
        validate_target(seed(), chain, index, nucleophilic_atom=atom)


def test_chain_collisions_modifications_and_sequence_fingerprint():
    data = seed()
    with pytest.raises(ValueError, match='collides'):
        validate_target(data, 'A', 2, ligand_chain='D')
    data['sequences'].append({'ligand': {'id': 'A', 'ccdCodes': ['MG']}})
    with pytest.raises(ValueError, match='duplicate'):
        validate_target(data, 'A', 2)
    data = seed()
    data['sequences'][1]['protein']['modifications'] = [{'ptmPosition': 2, 'ptmType': 'CSO'}]
    with pytest.raises(ValueError, match='modified'):
        validate_target(data, 'A', 2)
    data = seed()
    fingerprint = hashlib.sha256(b'ACG').hexdigest()
    assert validate_target(data, 'A', 2, expected_sequence_sha256=fingerprint)[
        'configured_sequence_fingerprint_verified']
    # The target Cys still matches; a mutation elsewhere must not pass silently.
    data['sequences'][1]['protein']['sequence'] = 'ACA'
    with pytest.raises(ValueError, match='sequence fingerprint'):
        validate_target(data, 'A', 2, expected_sequence_sha256=fingerprint)


def test_other_bonds_preserved_and_occupied_sg_rejected():
    data = seed()
    bond = [['B', 1, 'N'], ['G', 1, 'MG']]
    data['bondedAtomPairs'].append(bond)
    result, _ = replace_design_ligand(data, 'A', 2, 'L', 'NEW', 'C1')
    assert result['bondedAtomPairs'][0] == bond
    data['bondedAtomPairs'].append([['A', 2, 'SG'], ['G', 1, 'MG']])
    with pytest.raises(ValueError, match='already bonded'):
        replace_design_ligand(data, 'A', 2, 'L', 'NEW', 'C1')


def test_only_requested_copy_of_old_ligand_is_replaced():
    data = seed()
    data['sequences'][-1]['ligand']['id'] = ['L', 'M']
    result, _ = replace_design_ligand(data, 'A', 2, 'L', 'NEW', 'C1')
    assert result['sequences'][-2] == {'ligand': {'id': ['M'], 'ccdCodes': ['OLD']}}
    assert result['sequences'][-1] == {'ligand': {'id': 'L', 'ccdCodes': ['NEW']}}


def test_new_plan_binds_seed_config_and_custom_ligand_chain(tmp_path):
    manager, request, _ = configured(tmp_path, 'alphafold3-mpae')
    config = tmp_path/'inputs/target.yaml'
    config.write_text(config.read_text()+'ligand_chain: Z\n')
    spec = manager.build_spec(request)
    check = spec.provenance['af3_target_seed_check']
    assert check == spec.parameters['af3_target_seed_check']
    for flag, value in [('--expected-source-data-sha256', check['source_data_sha256']),
                        ('--expected-target-config-sha256', check['target_config_sha256']),
                        ('--ligand-chain', 'Z')]:
        assert spec.command[spec.command.index(flag)+1] == value
    assert check['study_target_identity_verified'] is False
    assert check['configured_sequence_fingerprint_verified'] is False


@pytest.mark.parametrize('change', ['seed', 'config', 'command'])
def test_real_worker_rejects_changed_target_before_creating_output(tmp_path, change):
    manager, request, _ = configured(tmp_path, 'alphafold3-mpae')
    spec = manager.build_spec(request)
    command = [sys.executable, *spec.command[1:]]
    if change == 'command':
        command[command.index('--cysteine-sequence-index')+1] = '104'
    else:
        path = tmp_path/'inputs'/('source.json' if change == 'seed' else 'target.yaml')
        path.write_text(path.read_text()+'\n')
    result = subprocess.run(command, capture_output=True, text=True, timeout=30,
        env={**os.environ, 'PYTHONPATH': str(ROOT/'src')})
    assert result.returncode != 0
    assert ('differs from target configuration' if change == 'command' else
            'differs from the frozen plan') in result.stderr
    assert not Path(spec.output_directory).exists()


def test_read_guard_requires_complete_hash_pair_and_exact_integer(tmp_path):
    _, _, _ = configured(tmp_path, 'alphafold3-mpae')
    source, config = tmp_path/'inputs/source.json', tmp_path/'inputs/target.yaml'
    with pytest.raises(ValueError, match='both source and target'):
        read_target_seed(source, config, expected_source_sha256='0'*64)
    target = yaml.safe_load(config.read_text())
    target['cysteine_sequence_index'] = 105.9
    config.write_text(yaml.safe_dump(target))
    with pytest.raises(ValueError, match='1-based'):
        read_target_seed(source, config)


def test_single_preparation_rejects_wrong_chain_without_af3_runtime(tmp_path):
    source = tmp_path/'seed.json'
    source.write_text(json.dumps(seed()))
    result = subprocess.run([sys.executable, str(ROOT/'scripts/prepare_af3_covalent_input.py'),
        '--source-data-json', str(source), '--protein-chain', 'B',
        '--cysteine-sequence-index', '2', '--ligand-smiles', 'C=CC(=O)N',
        '--compound-id', 'case', '--protomer-id', '0', '--output-dir', str(tmp_path/'out')],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, 'PYTHONPATH': str(ROOT/'src')})
    assert result.returncode != 0
    assert 'Cys SG' in result.stderr
    assert 'ModuleNotFoundError' not in result.stderr
    assert not (tmp_path/'out').exists()


@pytest.mark.parametrize('changed', [None, 'source_data_json', 'target_config', 'candidate_table'])
def test_reuse_is_bound_to_seed_configuration_and_candidates(tmp_path, changed):
    runner = load_script('run_af3_registered_batch')
    _, _, table = configured(tmp_path, 'alphafold3-mpae')
    check = read_target_seed(tmp_path/'inputs/source.json', tmp_path/'inputs/target.yaml')
    inputs = {key: {'sha256': value} for key, value in {
        'source_data_json': check['source_data_sha256'], 'target_config': check['target_config_sha256'],
        'candidate_table': hashlib.sha256(table.read_bytes()).hexdigest()}.items()}
    if changed:
        inputs[changed]['sha256'] = '0'*64
    (tmp_path/'stage4_selection_manifest.json').write_text(json.dumps({'inputs': inputs}))
    if changed:
        with pytest.raises(ValueError, match=changed):
            runner._verify_reuse_source(tmp_path, check, table)
    else:
        runner._verify_reuse_source(tmp_path, check, table)


def test_successful_prediction_cannot_be_resumed_with_changed_prepared_input(tmp_path):
    runner = load_script('run_af3_registered_batch')
    prepared = tmp_path/'input.json'
    prepared.write_text(json.dumps(seed()))
    manifest = tmp_path/'run.json'
    manifest.write_text(json.dumps({'status': 'SUCCESS',
        'input_json_sha256': hashlib.sha256(prepared.read_bytes()).hexdigest()}))
    output = tmp_path/'predictions'
    output.mkdir()
    # File names are enough for this historical completion probe; no scientific
    # content/quality is asserted by this test or by _successful_run.
    (output/'case_model.cif').write_text('software fixture')
    (output/'case_summary_confidences.json').write_text('{}')
    assert runner._successful_run(manifest, output, prepared)
    prepared.write_text(prepared.read_text()+'\n')
    with pytest.raises(ValueError, match='different or unverifiable'):
        runner._successful_run(manifest, output, prepared)


def cif_fixture(rows):
    return ('data_fixture\nloop_\n_pdbx_poly_seq_scheme.pdb_strand_id\n'
        '_pdbx_poly_seq_scheme.seq_id\n_pdbx_poly_seq_scheme.mon_id\n'
        '_pdbx_poly_seq_scheme.auth_seq_num\n_pdbx_poly_seq_scheme.asym_id\n'
        '_pdbx_poly_seq_scheme.pdb_ins_code\n'+'\n'.join(rows)+'\n#\n')


@pytest.mark.parametrize('rows,reason', [
    (['A 1 ALA 796 A .', 'A 2 CYS 797 A .', 'A 2 CYS 797 B .'], 'Ambiguous'),
    (['A 1 ALA 796 A .', 'A 3 CYS 797 A .'], 'Non-contiguous'),
    (['A 1 ALA 796 A .', 'A 2 CYS 797 B .'], 'multiple label'),
])
def test_cif_mapping_rejects_ambiguity_instead_of_overwriting_rows(tmp_path, rows, reason):
    source = tmp_path/'source.cif'
    source.write_text(cif_fixture(rows))
    with pytest.raises(ValueError, match=reason):
        load_script('prepare_af3_protein_seed').chain_sequence(source, 'A')


@pytest.mark.parametrize('author,code,expected_success', [
    ('797', '.', True), ('797', 'A', False), ('797.1', '.', False)])
def test_real_seed_cli_preserves_mapping_without_float_truncation(tmp_path, author, code, expected_success):
    source = tmp_path/'source.cif'
    source.write_text(cif_fixture(['A 1 ALA 796 A .', f'A 2 CYS {author} A {code}']))
    config = tmp_path/'target.yaml'
    config.write_text(yaml.safe_dump({'target_id': 'fixture', 'site_id': 'fixture-site',
        'protein_chain': 'A', 'residue_number': 797, 'cysteine_sequence_index': 2,
        'nucleophilic_atom': 'SG'}))
    output = tmp_path/'seed.json'
    result = subprocess.run([sys.executable, str(ROOT/'scripts/prepare_af3_protein_seed.py'),
        '--cif', str(source), '--target-config', str(config), '--output', str(output)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, 'PYTHONPATH': str(ROOT/'src')})
    assert (result.returncode == 0) == expected_success, result.stderr
    if expected_success:
        receipt = json.loads(output.with_suffix('.manifest.json').read_text())
        assert receipt['source_residue_mapping']['auth_residue_number'] == '797'
        assert receipt['canonical_mapping_verified'] is False
        assert receipt['af3_target_seed_check']['protein_sequence_length'] == 2
    else:
        assert not output.exists()
