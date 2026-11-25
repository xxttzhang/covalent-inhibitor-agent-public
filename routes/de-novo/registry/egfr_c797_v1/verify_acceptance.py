"""Re-read and re-evaluate registered outputs; structural QC is not independent docking.

The caller must bind expected_request_sha256 to its frozen request. This module
never imports code from a task, promotes historical output to a new task, or
claims independent pre-reaction/adduct evaluation from imposed anchor geometry.
"""
import csv
import hashlib
import json
from pathlib import Path

import numpy as np
from rdkit import Chem

from evaluator import ROOT, digest, evaluate, protein
from accept import identity_failures


def inside(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Artifact escapes task: ' + str(name))
    return path


def verify(root, expected_request_sha256):
    root = Path(root).resolve()
    request_path = root / 'request.json'
    if digest(request_path) != expected_request_sha256:
        raise ValueError('Frozen request hash mismatch')
    request = json.loads(request_path.read_text())
    basis = request.get('scientific_basis')
    if basis is not None:
        if hashlib.sha256(basis['hypothesis_json'].encode()).hexdigest() != basis['hypothesis_sha256']:
            raise ValueError('Scientific hypothesis content hash differs')
        proposal = json.loads(basis['hypothesis_json'])
        if hashlib.sha256(basis['manifest_json'].encode()).hexdigest() != basis['evidence_manifest_sha256']:
            raise ValueError('Scientific evidence manifest hash differs')
        manifest = json.loads(basis['manifest_json'])
        if hashlib.sha256(basis['evidence_json'].encode()).hexdigest() != manifest['files'].get('evidence.json'):
            raise ValueError('Scientific evidence content hash differs')
        evidence = json.loads(basis['evidence_json'])
        if (evidence['target'] != 'EGFR' or evidence['site'] != 'C797' or evidence['taxon'] != 9606
            or evidence['identity']['uniprot_id'] != 'P00533' or evidence['identity'].get('canonical_cys_verified') is not True):
            raise ValueError('Scientific evidence target differs from registered package')
        if (proposal['evidence_id'] != basis['evidence_id']
            or proposal['evidence_manifest_sha256'] != basis['evidence_manifest_sha256']
            or proposal['hypothesis']['warhead_class'] != ['acrylamide']
            or proposal['hypothesis']['warhead_selection'].get('execution_branch_id', 'primary') != basis['execution_branch_id']):
            raise ValueError('Scientific hypothesis binding differs')
    if request['profile'] != 'egfr-c797-acrylamide-v1':
        raise ValueError('Unsupported generation profile')
    if type(request['slots']) is not int or not 1 <= request['slots'] <= 96:
        raise ValueError('Invalid generation denominator')
    # Only evaluate using the installed, known evaluator and its known inputs.
    dependencies = ['evaluator.py', 'control/rules.json', 'control/posebusters.yml',
        'assets/protein_A.pdb', 'assets/acrylamide_prereaction.sdf',
        'assets/acrylamide_adduct_fragment_named.sdf', 'assets/warhead_atom_map.json']
    for name in dependencies:
        if request['package_hashes'].get(name) != digest(ROOT / name):
            raise ValueError('Evaluator/input version differs: ' + name)
    download = root / 'download'
    manifest = json.loads((download / 'manifest.json').read_text())
    actual = {str(path.relative_to(download)) for path in download.rglob('*') if path.is_file()}
    if actual != set(manifest) | {'manifest.json'}:
        raise ValueError('Generation inventory differs')
    for name, expected in manifest.items():
        if digest(inside(download, name)) != expected:
            raise ValueError('Generation artifact changed: ' + name)
    receipt = json.loads((download / 'receipt.json').read_text())
    if (receipt['task_id'] != request['id'] or receipt['request_sha256'] != expected_request_sha256
        or receipt['returncode'] != 0 or receipt['recorded_slots'] != request['slots']
        or receipt['effective_seed'] != request['seed']):
        raise ValueError('Generation receipt does not match frozen request')
    infos = list(download.rglob('gen_info.csv'))
    if len(infos) != 1:
        raise ValueError('Expected one generation log')
    info = infos[0]
    records = {}
    for row in csv.DictReader(info.open()):
        name = row['filename']
        if Path(name).name != name:
            raise ValueError('Unsafe candidate filename')
        slot = int(name.split('-')[0].split('.')[0])
        if slot in records or slot not in range(request['slots']):
            raise ValueError('Duplicate or out-of-range slot')
        records[slot] = row
    acceptance = root / 'acceptance'
    prot = protein(ROOT / 'assets/protein_A.pdb')
    accepted = {'raw': {}, 'relaxed': {}}
    candidates = []
    for slot in range(request['slots']):
        log = records.get(slot, {})
        source = inside(download, str((info.parent / (info.parent.name + '_SDF') / log.get('filename', 'missing.sdf')).relative_to(download)))
        mol = Chem.MolFromMolFile(str(source), removeHs=True) if source.is_file() else None
        saved = json.loads((acceptance / f'slot_{slot:03d}.json').read_text())
        if saved['slot'] != slot or saved['upstream'] != log:
            raise ValueError('Slot identity/log differs')
        if saved['source_sha256'] != (digest(source) if source.is_file() else None):
            raise ValueError('Slot source hash differs')
        provenance = identity_failures(mol, log)
        for stage in ['raw', 'relaxed']:
            path = source if stage == 'raw' else acceptance / 'relaxed' / f'{slot}.sdf'
            candidate = mol if stage == 'raw' else (Chem.MolFromMolFile(str(path), removeHs=True) if path.is_file() else None)
            checked = evaluate(candidate, prot, 'pre')
            failures = checked['failures'] + provenance
            if stage == 'relaxed':
                if candidate is None or mol is None or Chem.MolToSmiles(candidate) != Chem.MolToSmiles(mol):
                    failures.append('relaxation_changed_chemical_identity')
                if saved['optimization'].get('status') != 'CONVERGED':
                    failures.append('relaxation_not_converged')
            passed = not failures
            if saved[stage]['passed'] is not passed:
                raise ValueError(f'Saved acceptance disagrees with re-evaluation: {slot}:{stage}')
            if passed:
                if saved[stage].get('smiles') != checked['smiles']:
                    raise ValueError('Saved candidate chemical identity differs')
                accepted[stage][slot] = candidate
            candidates.append({'candidate_id': f'{request["id"]}:{slot}:{stage}',
                'slot': slot, 'representation': stage, 'structural_qc_passed': passed,
                'smiles': checked.get('smiles'), 'failures': failures,
                'source_sha256': digest(path) if path.is_file() else None,
                'independent_prereaction_evaluation': 'NOT_PERFORMED',
                'independent_adduct_evaluation': 'NOT_PERFORMED',
                'scientific_status': 'PENDING_INDEPENDENT_EVALUATION' if passed else 'STRUCTURAL_QC_FAILED'})
    # Validate actual exported molecules, not only the summary's counts.
    for stage in ['raw', 'relaxed']:
        path = acceptance / f'{stage}_accepted.sdf'
        exported = list(Chem.SDMolSupplier(str(path), removeHs=True)) if path.stat().st_size else []
        seen = set()
        for mol in exported:
            if mol is None or not all(mol.HasProp(key) for key in ['task_id', 'slot', 'stage']):
                raise ValueError('Invalid exported candidate')
            slot = int(mol.GetProp('slot'))
            if slot in seen or slot not in accepted[stage] or mol.GetProp('task_id') != request['id'] or mol.GetProp('stage') != stage:
                raise ValueError('Export candidate identity differs')
            reference = accepted[stage][slot]
            if (Chem.MolToSmiles(mol) != Chem.MolToSmiles(reference)
                or mol.GetNumAtoms() != reference.GetNumAtoms()
                or not np.allclose(mol.GetConformer().GetPositions(), reference.GetConformer().GetPositions(), atol=1e-4, rtol=0)):
                raise ValueError('Export chemistry/coordinates differ')
            seen.add(slot)
        if seen != set(accepted[stage]):
            raise ValueError('Export omits accepted candidates')
    summary = json.loads((acceptance / 'summary.json').read_text())
    if (summary['task_id'] != request['id'] or summary['requested_slots'] != request['slots']
        or summary['seed'] != request['seed'] or summary['outputs'] != len(records)
        or summary['rules_sha256'] != digest(ROOT / 'control/rules.json')
        or summary['evaluator_sha256'] != digest(ROOT / 'evaluator.py')):
        raise ValueError('Summary provenance differs')
    for stage in ['raw', 'relaxed']:
        if (summary[stage]['pass_count'] != len(accepted[stage])
            or summary[stage]['unique_smiles'] != len({Chem.MolToSmiles(m) for m in accepted[stage].values()})):
            raise ValueError('Summary counts differ from re-evaluated molecules')
    return {'state': 'STRUCTURAL_QC_VERIFIED', 'task_id': request['id'],
        'request_sha256': expected_request_sha256, 'requested_slots': request['slots'],
        'target': 'EGFR WT', 'site': '4G5J:A:C797:SG', 'warhead': 'acrylamide',
        'seed': request['seed'], 'package_hashes': request['package_hashes'],
        'scientific_basis': basis,
        'hypothesis_binding_status': 'CONTENT_BOUND_NOT_INDEPENDENTLY_VERIFIED' if basis else 'LEGACY_UNBOUND',
        'raw_pass_count': len(accepted['raw']), 'relaxed_pass_count': len(accepted['relaxed']),
        'candidates': candidates, 'ready_for_scientific_handoff': False,
        'note': 'Recomputed structural QC only; anchored geometry is not independent reaction evidence.'}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('task', type=Path)
    parser.add_argument('--request-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.task, args.request_sha256)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
