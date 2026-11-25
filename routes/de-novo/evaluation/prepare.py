"""Prepare coordinate-independent inputs from verified, hypothesis-bound outputs.

This does not run docking or confer scientific acceptance. Repeated chemical
identities retain every source candidate ID; no generation seed/pose is reused
as an independent result. The output directory must be new.
"""
import argparse
import json
from pathlib import Path
import sys

from rdkit import Chem, rdBase
from rdkit.Chem import rdDepictor

REGISTRY = Path(__file__).resolve().parents[1] / 'registry/egfr_c797_v1'
sys.path.insert(0, str(REGISTRY))
from verify_acceptance import verify
from evaluator import evaluate, protein, digest


def independent_input(molecule):
    if molecule is None:
        raise ValueError('Invalid source molecule')
    result = Chem.Mol(molecule)
    identity = Chem.MolToSmiles(result, isomericSmiles=True)
    result.RemoveAllConformers()
    rdDepictor.Compute2DCoords(result)
    if Chem.MolToSmiles(result, isomericSmiles=True) != identity or result.GetConformer().Is3D():
        raise ValueError('Coordinate removal changed identity or retained 3D')
    return result


def prepare(task, expected, output, candidate_id=None):
    task = task.resolve()
    checked = verify(task, expected)
    if not checked['scientific_basis']:
        raise ValueError('Independent study requires a bound scientific hypothesis')
    request = json.loads((task / 'request.json').read_text())
    structures = {}
    for stage in ['raw', 'relaxed']:
        source = task / 'acceptance' / (stage + '_accepted.sdf')
        if source.stat().st_size:
            for molecule in Chem.SDMolSupplier(str(source), removeHs=True):
                if molecule is None:
                    raise ValueError('Unreadable accepted molecule')
                structures[(int(molecule.GetProp('slot')), stage)] = molecule
    receptor = REGISTRY / 'assets/protein_A.pdb'
    prot = protein(receptor)
    groups = {}
    for candidate in checked['candidates']:
        if not candidate['structural_qc_passed']:
            continue
        if candidate_id is not None and candidate['candidate_id'] != candidate_id:
            continue
        molecule = structures[(candidate['slot'], candidate['representation'])]
        identity = Chem.MolToSmiles(molecule, isomericSmiles=True)
        if identity not in groups:
            # Recompute atom mapping; never trust the saved acceptance mapping.
            mapping = evaluate(molecule, prot, 'pre')['core']['mapping']
            groups[identity] = {'molecule': independent_input(molecule), 'mapping': mapping, 'origins': []}
        groups[identity]['origins'].append(candidate)
    if not groups:
        raise ValueError('No structurally accepted candidates to evaluate')
    output.mkdir(parents=True, exist_ok=False)
    records = []
    for index, (identity, group) in enumerate(groups.items()):
        name = f'candidate_{index:03d}'
        molecule = group['molecule']
        molecule.SetProp('_Name', name)
        molecule.SetProp('source_task', request['id'])
        molecule.SetProp('source_request_sha256', expected)
        molecule.SetProp('source_candidate_ids', json.dumps([c['candidate_id'] for c in group['origins']]))
        target = output / (name + '.sdf')
        with Chem.SDWriter(str(target)) as writer:
            writer.write(molecule)
        readback = Chem.SDMolSupplier(str(target), removeHs=True)[0]
        if readback is None or Chem.MolToSmiles(readback, isomericSmiles=True) != identity or readback.GetConformer().Is3D():
            raise ValueError('Exported independent input differs')
        records.append({'id': name, 'input_file': target.name, 'smiles': identity,
            'core_indices_0based': group['mapping'], 'alpha_index_1based': group['mapping'][3] + 1,
            'beta_index_1based': group['mapping'][4] + 1, 'origins': group['origins'],
            'independent_prereaction_evaluation': 'NOT_PERFORMED', 'independent_adduct_evaluation': 'NOT_PERFORMED'})
    (output / 'protein_A.pdb').write_bytes(receptor.read_bytes())
    manifest = {'schema': 'denovo-independent-inputs-v1', 'state': 'INPUTS_PREPARED_NOT_EVALUATED',
        'source_task': request['id'], 'request_sha256': expected, 'seed': request['seed'],
        'target': checked['target'], 'site': checked['site'], 'warhead': checked['warhead'],
        'scientific_basis': checked['scientific_basis'], 'rdkit_version': rdBase.rdkitVersion,
        'generated_coordinate_restraints': False, 'records': records,
        'files': {p.name: digest(p) for p in sorted(output.iterdir()) if p.is_file()},
        'ready_for_scientific_handoff': False,
        'pending': ['Independent receptor/ligand preparation', 'Validated docking control',
            'Independent prereaction pose evaluation', 'Independent covalent adduct evaluation']}
    (output / 'input_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('task', type=Path)
    parser.add_argument('--request-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--candidate-id')
    args = parser.parse_args()
    result = prepare(args.task, args.request_sha256, args.output, args.candidate_id)
    print(json.dumps({'state': result['state'], 'chemical_identities': len(result['records'])}))
