"""Registered worker for independent Vina poses; native JobManager owns execution."""
import importlib.metadata
import json
import os
from pathlib import Path
import sys

import numpy as np
from rdkit import Chem

from . import prereaction as science


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def runtime():
    """Hash the actual interpreter, science source and installed dependency files."""
    for name in ('vina', 'meeko', 'rdkit', 'numpy', 'scipy', 'posebusters', 'gemmi'):
        importlib.metadata.version(name)  # Fail the probe if a required engine is missing.
    distributions = {item.metadata['Name']: item for item in importlib.metadata.distributions()}
    names = sorted(distributions)
    files = {str(Path(sys.executable).resolve()): science.digest(Path(sys.executable).resolve()),
             str(Path(__file__).resolve()): science.digest(__file__)}
    for name in names:
        distribution = distributions[name]
        if not distribution.files:
            raise ValueError('Installed runtime dependency has no verifiable inventory: '+name)
        # Include transitive packages, data, metadata and versioned .so.1 ELF
        # files. Only bytecode caches are excluded: imports may regenerate them.
        for entry in distribution.files:
            item = Path(distribution.locate_file(entry)).resolve(strict=True)
            if item.is_file() and item.suffix != '.pyc':
                files[str(item)] = science.digest(item)
    return {'schema': 'denovo-prereaction-runtime-v1', 'python': str(Path(sys.executable).absolute()),
        'python_version': sys.version, 'versions': {name: importlib.metadata.version(name) for name in names},
        'files': files, 'science': science.freeze()}


def prepare_receptor(path):
    from meeko import MoleculePreparation, Polymer, ResidueChemTemplates, PDBQTWriterLegacy
    protein = science.receptor(path)
    polymer = Polymer.from_pdb_string(Path(path).read_text(), ResidueChemTemplates.create_from_defaults(),
        MoleculePreparation(), set_template={'A:797': 'CYS'}, wanted_altloc={'A:970': 'A'}, allow_bad_res=False)
    if (len(polymer.monomers) != len({key[:3] for key in protein['keys']})
            or len(polymer.get_valid_monomers()) != len(polymer.monomers)
            or polymer.monomers['A:797'].residue_template_key != 'CYS'):
        raise ValueError('Receptor preparation removed residues or changed target state')
    pdbqt, flexible = PDBQTWriterLegacy.write_from_polymer(polymer)
    if flexible:
        raise ValueError('Fixed protocol prohibits flexible receptor atoms')
    observed = {}
    for line in pdbqt.splitlines():
        if not line.startswith(('ATOM  ', 'HETATM')):
            continue
        if line[77:].strip() in ('H', 'HD', 'HS'):
            continue
        key = (line[21], int(line[22:26]), line[26:27].strip(), line[12:16].strip())
        if key in observed:
            raise ValueError('Prepared receptor has duplicate atoms')
        observed[key] = [float(line[30:38]), float(line[38:46]), float(line[46:54])]
    if set(observed) != set(protein['keys']) or not np.allclose(
            [observed[key] for key in protein['keys']], protein['xyz'], atol=0.001, rtol=0):
        raise ValueError('Preparation added, removed or moved receptor heavy atoms')
    return pdbqt, {'source_sha256': science.digest(path), 'heavy_atoms': len(observed),
        'residues': len(polymer.monomers), 'target_template': 'CYS', 'target_formal_charge': 0,
        'wanted_altloc': {'A:970': 'A'}, 'removed_residues': [], 'heavy_coordinates_preserved': True,
        'protonation_policy': 'Meeko default residue templates; explicit neutral A:C797; no pH ensemble',
        'scope': 'isolated crystal chain, not physiological environment validation'}


def prepare_ligand(source):
    from meeko import MoleculePreparation, PDBQTWriterLegacy
    mol = science.independent_conformer(source)
    setups = MoleculePreparation().prepare(mol)
    if len(setups) != 1:
        raise ValueError('Independent protocol requires exactly one molecular preparation')
    pdbqt, valid, message = PDBQTWriterLegacy.write_string(setups[0])
    if not valid:
        raise ValueError('Independent ligand preparation failed: ' + message)
    return pdbqt


def pocket(reference):
    mol = science.molecule(reference)
    xyz = mol.GetConformer().GetPositions()
    # Covalent crystal coordinates define only the search BOX, never an input
    # ligand pose, pharmacophore, RMSD score or a prereaction reference.
    center = (xyz.max(axis=0) + xyz.min(axis=0)) / 2
    size = np.maximum(xyz.max(axis=0) - xyz.min(axis=0) + 2*science.DOCKING['padding_A'],
                      science.DOCKING['minimum_box_A'])
    if np.any(size > 40):
        raise ValueError('Registered pocket exceeds bounded docking box')
    return {'center': center.tolist(), 'box_size': size.tolist(),
        'source_sha256': science.digest(reference), 'use': 'search_box_only'}


def unpack(pdbqt):
    from meeko import PDBQTMolecule, RDKitMolCreate
    molecules = RDKitMolCreate.from_pdbqt_mol(PDBQTMolecule(pdbqt, skip_typing=True))
    if len(molecules) != 1 or molecules[0] is None:
        raise ValueError('Docking output failed lossless Meeko molecule reconstruction')
    original = molecules[0]
    poses = []
    for conf in original.GetConformers():
        mol = Chem.Mol(original)
        mol.RemoveAllConformers()
        mol.AddConformer(Chem.Conformer(conf), assignId=True)
        poses.append(mol)
    scores = []
    for line in pdbqt.splitlines():
        if line.startswith('REMARK VINA RESULT:'):
            scores.append(float(line.split()[3]))
    if len(scores) != len(poses):
        raise ValueError('Pose count differs between Vina scores and reconstructed molecules')
    return poses, scores


def run(source, expected, output, runtime_file, runtime_sha256):
    from .independent_review import source_api
    from vina import Vina
    if science.digest(runtime_file) != runtime_sha256:
        raise ValueError('Runtime binding checksum changed')
    binding = json.loads(Path(runtime_file).read_text())
    if runtime() != binding:
        raise ValueError('Independent docking runtime changed after approval')
    source, output = Path(source), Path(output)
    original = source_api().validate(source, expected)
    output.mkdir(parents=True, exist_ok=False)
    pdbqt, preparation = prepare_receptor(source/'independent/protein_A.pdb')
    (output/'receptor.pdbqt').write_text(pdbqt)
    (output/'receptor-preparation.json').write_bytes(encoded(preparation))
    box = pocket(source/'control/control_crystal_adduct.sdf')
    records = [json.loads((source/'independent/input_manifest.json').read_text())['records'][0],
               json.loads((source/'control/control_manifest.json').read_text())['record']]
    results = {}
    for index, record in enumerate(records):
        name = ('candidate_000', 'control_afatinib')[index]
        if record['id'] != name:
            raise ValueError('Independent ligand role changed')
        folder = output/name
        folder.mkdir()
        path = source/('independent' if index == 0 else 'control')/record['input_file']
        ligand = science.molecule(path, two_dimensional=True)
        prepared = prepare_ligand(ligand)
        (folder/'input.pdbqt').write_text(prepared)
        docking = Vina(sf_name='vina', cpu=science.DOCKING['cpu'], seed=science.DOCKING['seed'], verbosity=1)
        docking.set_receptor(str(output/'receptor.pdbqt'))
        docking.set_ligand_from_string(prepared)
        docking.compute_vina_maps(center=box['center'], box_size=box['box_size'])
        docking.dock(exhaustiveness=science.DOCKING['exhaustiveness'], n_poses=science.DOCKING['n_poses'])
        docking.write_poses(str(folder/'poses.pdbqt'), n_poses=science.DOCKING['n_poses'],
                            energy_range=science.DOCKING['energy_range'], overwrite=False)
        poses, scores = unpack((folder/'poses.pdbqt').read_text())
        with Chem.SDWriter(str(folder/'poses.sdf')) as writer:
            for pose_index, pose in enumerate(poses):
                pose.SetProp('_Name', name)
                pose.SetIntProp('pose_index', pose_index)
                pose.SetDoubleProp('vina_score_kcal_mol', scores[pose_index])
                writer.write(pose)
        result = science.assess(ligand, poses, scores, science.receptor(source/'independent/protein_A.pdb'),
            record['alpha_index_1based']-1, record['beta_index_1based']-1, control=index == 1)
        (folder/'assessment.json').write_bytes(encoded(result))
        results[name] = result
    source_api().validate(source, expected)
    if runtime() != binding:
        raise ValueError('Independent docking runtime changed during execution')
    report = {'schema': 'denovo-independent-prereaction-result-v1',
        'study_id': original['study_id'], 'source_manifest_sha256': expected,
        'candidate_id': original['candidate_id'], 'runtime_sha256': runtime_sha256,
        'box': box, 'receptor_preparation': preparation, 'results': results,
        'independent_prereaction_evaluation': results['candidate_000']['state'],
        'control_prereaction_evaluation': results['control_afatinib']['state'],
        'covalent_redocking_control_completed_by_this_job': False,
        'native_md_admission_granted': False, 'generated_coordinates_used': False}
    (output/'report.json').write_bytes(encoded(report))
    files = {p.relative_to(output).as_posix(): science.digest(p) for p in sorted(output.rglob('*')) if p.is_file()}
    (output/'manifest.json').write_bytes(encoded({'schema': 'denovo-prereaction-artifacts-v1',
        'source_manifest_sha256': expected, 'runtime_sha256': runtime_sha256, 'files': files}))
    return report


def review(source, expected, output, runtime_file, runtime_sha256):
    """Recompute from raw PDBQT poses in the approved runtime, without docking."""
    from .independent_review import source_api
    source, output = Path(source), Path(output)
    original = source_api().validate(source, expected)
    if science.digest(runtime_file) != runtime_sha256 or runtime() != json.loads(Path(runtime_file).read_text()):
        raise ValueError('Prereaction readback runtime changed')
    inventory = json.loads((output/'manifest.json').read_text())
    files = {p.relative_to(output).as_posix(): science.digest(p) for p in output.rglob('*')
             if p.is_file() and p != output/'manifest.json'}
    if any(p.is_symlink() for p in output.rglob('*')) or files != inventory['files']:
        raise ValueError('Prereaction output inventory changed')
    report = json.loads((output/'report.json').read_text())
    if (report['study_id'] != original['study_id'] or report['candidate_id'] != original['candidate_id']
            or report['source_manifest_sha256'] != expected or report['runtime_sha256'] != runtime_sha256
            or report['generated_coordinates_used'] is not False
            or report['native_md_admission_granted'] is not False
            or report['covalent_redocking_control_completed_by_this_job'] is not False):
        raise ValueError('Prereaction report identity or scope changed')
    pdbqt, preparation = prepare_receptor(source/'independent/protein_A.pdb')
    if ((output/'receptor.pdbqt').read_text() != pdbqt or report['receptor_preparation'] != preparation
            or report['box'] != pocket(source/'control/control_crystal_adduct.sdf')):
        raise ValueError('Prepared receptor or search box differs from frozen source')
    records = [json.loads((source/'independent/input_manifest.json').read_text())['records'][0],
               json.loads((source/'control/control_manifest.json').read_text())['record']]
    for index, record in enumerate(records):
        name = ('candidate_000', 'control_afatinib')[index]
        ligand = science.molecule(source/('independent' if index == 0 else 'control')/record['input_file'],
                                   two_dimensional=True)
        poses, scores = unpack((output/name/'poses.pdbqt').read_text())
        fresh = science.assess(ligand, poses, scores, science.receptor(source/'independent/protein_A.pdb'),
            record['alpha_index_1based']-1, record['beta_index_1based']-1, control=index == 1)
        if encoded(fresh) != encoded(report['results'][name]):
            raise ValueError('Saved prereaction assessment differs from fresh raw-pose measurements')
    if (report['independent_prereaction_evaluation'] != report['results']['candidate_000']['state']
            or report['control_prereaction_evaluation'] != report['results']['control_afatinib']['state']):
        raise ValueError('Prereaction summary contradicts measured results')
    source_api().validate(source, expected)
    return report


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--probe', action='store_true')
    parser.add_argument('--review-only', action='store_true')
    parser.add_argument('--source', type=Path)
    parser.add_argument('--source-sha256')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--runtime-file', type=Path)
    parser.add_argument('--runtime-sha256')
    args = parser.parse_args()
    if args.probe:
        print(json.dumps(runtime(), sort_keys=True, allow_nan=False))
        return
    if any(value is None for value in (args.source, args.source_sha256, args.output,
                                       args.runtime_file, args.runtime_sha256)):
        parser.error('Frozen source, runtime binding and output required')
    if hasattr(os, 'sched_getaffinity'):
        os.sched_setaffinity(0, {min(os.sched_getaffinity(0))})
    if args.review_only:
        print(json.dumps(review(args.source, args.source_sha256, args.output,
                                args.runtime_file, args.runtime_sha256), allow_nan=False))
        return
    result = run(args.source, args.source_sha256, args.output, args.runtime_file, args.runtime_sha256)
    print(json.dumps({'state': result['independent_prereaction_evaluation'],
                      'md_admission_granted': False}))


if __name__ == '__main__':
    main()
