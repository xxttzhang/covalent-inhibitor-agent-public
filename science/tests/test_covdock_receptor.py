"""Read real file formats and call actual guards; no vendor computation."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from rdkit import Chem

from covscreen.covdock_receptor import (attachment_site, inspect_receptor,
    verify_reused_inputs, read_effective_receptor)
from covscreen.jobs import sha256_file
from covscreen.covdock_gate import ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION
from receptor_fixture import receptor_molecule, write_receptor
from test_refinement_job_contract import configured

ROOT = Path(__file__).resolve().parents[1]


def runner():
    sys.path.insert(0, str(ROOT/'scripts'))
    try:
        spec = importlib.util.spec_from_file_location('receptor_covdock_runner', ROOT/'scripts/run_covdock_lo_registered.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)


@pytest.mark.parametrize('suffix', ['.pdb', '.mae', '.maegz'])
def test_identity_readback_never_claims_preparation_or_target_approval(tmp_path, suffix):
    path = write_receptor(tmp_path/('receptor.pdb' if suffix == '.pdb' else 'receptor.mae'))
    if suffix == '.maegz':
        compressed = tmp_path/'receptor.maegz'
        compressed.write_bytes(gzip.compress(path.read_bytes()))
        path = compressed
    check = inspect_receptor(path, 'A:797')
    assert check['receptor_sha256'] == sha256_file(path)
    assert check['target']['atom_name'] == 'SG'
    assert check['target_coordinate_identity_verified'] is True
    assert check['target_bond_graph_checked'] == (suffix != '.pdb')
    for field in ('receptor_preparation_quality_verified', 'study_target_identity_verified',
                  'canonical_mapping_verified', 'grants_resource_approval'):
        assert check[field] is False


@pytest.mark.parametrize('site', ['A:797.1', 'A:797A', 'A:0797', ':797', 'A: 797'])
def test_author_number_never_truncated_or_implicitly_disambiguated(site):
    with pytest.raises(ValueError, match='CHAIN:integer'):
        attachment_site(site)


@pytest.mark.parametrize('change,reason', [
    ('chain', 'unique'), ('insertion', 'insertion code'), ('duplicate', 'ambiguous atom'),
    ('modified', 'modified residue'), ('element', 'free Cys'), ('external_bond', 'outside'),
    ('missing_bond', 'heavy-atom bond graph')])
def test_receptor_site_requires_unambiguous_free_cys(tmp_path, change, reason):
    mol = receptor_molecule()
    if change == 'chain':
        for atom in mol.GetAtoms(): atom.GetPDBResidueInfo().SetChainId('B')
    if change == 'insertion':
        for atom in mol.GetAtoms(): atom.GetPDBResidueInfo().SetInsertionCode('A')
    if change == 'duplicate': mol.GetAtomWithIdx(0).GetPDBResidueInfo().SetName(' SG ')
    if change == 'modified':
        for atom in mol.GetAtoms(): atom.GetPDBResidueInfo().SetResidueName('CYX')
    if change == 'element': mol.GetAtomWithIdx(1).SetAtomicNum(8)
    if change == 'external_bond': mol.GetAtomWithIdx(2).GetPDBResidueInfo().SetResidueNumber(798)
    if change == 'missing_bond':
        editable = Chem.RWMol(mol)
        editable.RemoveBond(0, 1)
        mol = editable.GetMol()
    path = write_receptor(tmp_path/'receptor.mae', mol)
    with pytest.raises(ValueError, match=reason):
        inspect_receptor(path, 'A:797')


def test_multiple_models_and_alternatives_not_silently_selected(tmp_path):
    path = write_receptor(tmp_path/'receptor.pdb')
    original = path.read_text()
    path.write_text('MODEL        1\n'+original+'ENDMDL\nMODEL        2\n'+original+'ENDMDL\n')
    with pytest.raises(ValueError, match='one explicitly selected'):
        inspect_receptor(path, 'A:797')
    path.write_text('\n'.join(line[:16]+'A'+line[17:] if line.startswith('ATOM') else line
                              for line in original.splitlines())+'\n')
    with pytest.raises(ValueError, match='alternate'):
        inspect_receptor(path, 'A:797')


def test_plan_provenance_and_worker_guard_bind_receptor_bytes(tmp_path):
    manager, request, _ = configured(tmp_path, 'schrodinger-covdock-lo')
    spec = manager.build_spec(request)
    check = spec.provenance['covdock_receptor_input_check']
    assert check == spec.parameters['covdock_receptor_input_check']
    assert spec.command[spec.command.index('--expected-receptor-sha256')+1] == check['receptor_sha256']
    receptor = tmp_path/'inputs/receptor.pdb'
    receptor.write_text(receptor.read_text()+'\n')
    result = subprocess.run([sys.executable, *spec.command[1:]],
        env={**os.environ, 'PYTHONPATH': str(ROOT/'src')}, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'receptor differs from the frozen plan' in result.stderr
    assert not Path(spec.output_directory).exists()


def test_old_mutable_receptor_path_is_never_evidence_of_original_input(tmp_path):
    path = write_receptor(tmp_path/'receptor.pdb')
    old_sha = sha256_file(path)
    receipt = {'input_receptor': str(path), 'input_receptor_sha256': old_sha,
        'input_ligand_sha256': '1'*64, 'attachment_residue': 'A:797',
        'reaction_type': 'Michael Addition', 'mode': 'leadopt', 'prepare_receptor': False,
        'grid_sha256': None}
    verify_reused_inputs(receipt, old_sha, '1'*64, 'A:797', 'Michael Addition', 'leadopt')
    path.write_text(path.read_text()+'\n')
    with pytest.raises(ValueError, match='input_receptor_sha256'):
        verify_reused_inputs(receipt, sha256_file(path), '1'*64, 'A:797', 'Michael Addition', 'leadopt')
    del receipt['input_receptor_sha256']
    with pytest.raises(ValueError, match='input_receptor_sha256'):
        verify_reused_inputs(receipt, sha256_file(path), '1'*64, 'A:797', 'Michael Addition', 'leadopt')


@pytest.mark.parametrize('version', [ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION])
def test_actual_cli_accepts_gate_version_then_rejects_bad_receptor(tmp_path, version):
    manager, request, _ = configured(tmp_path, 'schrodinger-covdock-lo')
    request.parameters['gate_algorithm_version'] = version
    spec = manager.build_spec(request)
    path = tmp_path/'inputs/receptor.pdb'
    path.write_text('END\n')
    result = subprocess.run([sys.executable, *spec.command[1:]],
        env={**os.environ, 'PYTHONPATH': str(ROOT/'src')}, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'receptor differs from the frozen plan' in result.stderr
    assert 'invalid choice' not in result.stderr


def test_ligprep_resume_requires_matching_saved_input_and_output(tmp_path):
    script = runner()
    source, output, manifest = tmp_path/'in.sdf', tmp_path/'out.mae', tmp_path/'receipt.json'
    source.write_text('source software fixture')
    output.write_text('output software fixture')
    receipt = {'status': 'SUCCESS', 'input_sdf_sha256': sha256_file(source),
               'output_mae_sha256': sha256_file(output)}
    manifest.write_text(json.dumps(receipt))
    assert script._successful_ligprep(manifest, output, source)
    source.write_text('different source')
    with pytest.raises(ValueError, match='LigPrep retained input/output'):
        script._successful_ligprep(manifest, output, source)


@pytest.mark.parametrize('changed', [False, True])
def test_real_job_entry_freezes_input_before_license_or_vendor_execution(tmp_path, changed):
    receptor = write_receptor(tmp_path/'receptor.mae')
    ligand = tmp_path/'ligand.mae'
    ligand.write_text('not a molecule: must never reach vendor')
    expected = sha256_file(receptor)
    if changed: receptor.write_bytes(receptor.read_bytes()+b'\n')
    output = tmp_path/'output'
    result = subprocess.run([sys.executable, str(ROOT/'scripts/run_covdock_job.py'),
        '--schrodinger-root', str(tmp_path/'absent-vendor'), '--receptor', str(receptor),
        '--ligand', str(ligand), '--expected-receptor-sha256', expected,
        '--attachment-residue', 'A:797', '--jobname', 'software-test', '--output-dir', str(output)],
        env={**os.environ, 'PYTHONPATH': str(ROOT/'src'), 'SCHROD_LICENSE_FILE': ''},
        capture_output=True, text=True, timeout=30)
    if changed:
        assert result.returncode != 0 and 'receptor differs from the frozen plan' in result.stderr
        assert not output.exists()
    else:
        assert result.returncode == 3
        receipt = json.loads((output/'software-test.run_manifest.json').read_text())
        assert receipt['status'] == 'LICENSE_BLOCKED'
        assert receipt['input_receptor_sha256'] == expected
        assert receipt['input_ligand_sha256'] == sha256_file(ligand)
        assert receipt['receptor_input_check']['study_target_identity_verified'] is False
        assert 'command' not in receipt


def test_multiple_maestro_structures_rejected(tmp_path):
    path = tmp_path/'receptors.mae'
    with Chem.MaeWriter(str(path)) as writer:
        writer.write(receptor_molecule())
        writer.write(receptor_molecule())
    with pytest.raises(ValueError, match='exactly one'):
        inspect_receptor(path, 'A:797')


@pytest.mark.parametrize('prepared', [False, True])
def test_effective_receptor_readback_uses_hash_bound_local_artifact(tmp_path, prepared):
    source = write_receptor(tmp_path/'source.mae')
    effective = write_receptor(tmp_path/'case_receptor_prepared.mae') if prepared else source
    manifest = tmp_path/'case.run_manifest.json'
    manifest.write_text(json.dumps({'prepare_receptor': prepared,
        'effective_receptor': '/old/remote/path/is/not/authoritative.mae',
        'effective_receptor_sha256': sha256_file(effective)}))
    result = read_effective_receptor(manifest, source, 'A:797')
    assert result['receptor_sha256'] == sha256_file(effective)
    effective.write_bytes(effective.read_bytes()+b'\n')
    with pytest.raises(ValueError, match='frozen plan'):
        read_effective_receptor(manifest, source, 'A:797')


def test_license_blocked_restart_does_not_erase_prior_invocation(tmp_path):
    receptor = write_receptor(tmp_path/'source.mae')
    ligand = tmp_path/'ligand.mae'
    ligand.write_text('software fixture, no vendor run')
    output = tmp_path/'output'
    output.mkdir()
    receipt = {'status': 'STARTED', 'input_receptor_sha256': sha256_file(receptor),
        'input_ligand_sha256': sha256_file(ligand), 'attachment_residue': 'A:797',
        'reaction_type': 'Michael Addition', 'mode': 'enrichment', 'prepare_receptor': False,
        'grid_sha256': None, 'command': ['historical-software-fixture']}
    manifest = output/'case.run_manifest.json'
    manifest.write_text(json.dumps(receipt))
    before = manifest.read_bytes()
    command = [sys.executable, str(ROOT/'scripts/run_covdock_job.py'),
        '--schrodinger-root', str(tmp_path/'absent'), '--receptor', str(receptor),
        '--ligand', str(ligand), '--attachment-residue', 'A:797', '--jobname', 'case',
        '--output-dir', str(output)]
    env = {**os.environ, 'PYTHONPATH': str(ROOT/'src'), 'SCHROD_LICENSE_FILE': ''}
    duplicate = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30)
    assert duplicate.returncode != 0 and 'explicit verified restart' in duplicate.stderr
    retry = subprocess.run([*command, '--restart'], env=env, text=True, capture_output=True, timeout=30)
    assert retry.returncode == 3
    assert json.loads(retry.stdout)['status'] == 'LICENSE_BLOCKED'
    assert manifest.read_bytes() == before
