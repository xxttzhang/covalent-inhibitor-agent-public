"""Freeze the VS preparation recipe before native resource approval.

Preparation and docking share one total HPC budget. Preparation does not expand
the candidate set or invent a source CarsiDock run. No subprocess is launched by
this module; the registered worker must verify the signed plan first.
"""
from pathlib import Path
import hashlib
import json
import re
from typing import Literal

from rdkit import Chem

from .covdock_vs import Record, Source, Budget, bound_file, molecules, identity, inspect_inputs, unresolved_stereo
from .covdock_receptor import inspect_receptor, runtime_paths
from .jobs import sha256_file
from .schrodinger import validate_license_config


class Preparation(Record):
    schema_version: Literal['covdock-vs-preparation-v1'] = 'covdock-vs-preparation-v1'
    source: Source
    original_ligands: str
    receptor: str
    attachment_residue: str
    budget: Budget
    njobs: int
    reaction_type: Literal['Michael Addition'] = 'Michael Addition'
    execution_instance: Literal['asgbie2', 'asgbie3', 'asgbie4'] | None = None


def freeze(root: Path, value: Preparation | dict, configuration: dict) -> dict:
    plan = Preparation.model_validate(value)
    if plan.source.route == 'de-novo':
        from .layout import enable_de_novo
        enable_de_novo()
        from covscreen_de_novo.batch_vs import verify_source
        verify_source(root, plan.source, plan.original_ligands, plan.receptor)
    if plan.execution_instance is not None:
        from .ligand_instance import selected_instance
        expected = {'library-first': 'asgbie2', 'ligand-first': selected_instance(), 'de-novo': 'asgbie3'}
        if expected.get(plan.source.route) != plan.execution_instance:
            raise ValueError('Instance execution requires the route-bound CPU instance')
    prefix = plan.source.route.split('-')[0]
    if not re.fullmatch(prefix + r'-flow-[a-f0-9]{24}', plan.source.workflow_id):
        raise ValueError('VS preparation must identify the matching native research')
    if not 1 <= plan.njobs <= min(plan.budget.cpu_count, plan.budget.max_candidates):
        raise ValueError('VS preparation concurrency exceeds the resource budget')
    files = [plan.original_ligands, plan.receptor, *plan.source.manifests]
    paths = {name: bound_file(root, name) for name in files}
    hashes = {name: sha256_file(path) for name, path in paths.items()}
    if any(not re.fullmatch(r'[a-f0-9]{64}', checksum) or hashes[name] != checksum
           for name, checksum in plan.source.manifests.items()):
        raise ValueError('VS preparation source manifest changed')
    candidates = molecules(paths[plan.original_ligands], False)
    if not plan.njobs <= len(candidates) <= plan.budget.max_candidates:
        raise ValueError('VS preparation input denominator exceeds its budget')
    names, records = set(), []
    for index, mol in enumerate(candidates):
        name = mol.GetProp('_Name') if mol.HasProp('_Name') else ''
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', name) or name in names:
            raise ValueError('VS preparation candidate names must be unique and explicit')
        names.add(name)
        ambiguous = unresolved_stereo(mol)
        if ambiguous:
            raise ValueError('VS input requires explicit stereochemistry before approval: '
                             + name + ' ' + json.dumps(ambiguous, sort_keys=True))
        maps = [atom.GetAtomMapNum() for atom in mol.GetAtoms() if atom.GetAtomMapNum()]
        if len(maps) != len(set(maps)) or not {905, 906}.issubset(maps):
            raise ValueError('VS preparation requires unique Michael alpha/beta maps')
        alpha, beta = (next(atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomMapNum() == number)
                       for number in (905,906))
        bond = mol.GetBondBetweenAtoms(alpha,beta)
        if (bond is None or bond.GetBondType() != Chem.BondType.DOUBLE
                or any(mol.GetAtomWithIdx(i).GetAtomicNum() != 6 for i in (alpha,beta))):
            raise ValueError('VS preparation Michael input lacks its mapped carbon double bond')
        records.append({'input_index':index,'candidate_id':name,'canonical_isomeric_smiles':identity(mol)})
    if paths[plan.receptor].suffix.lower() != '.pdb':
        raise ValueError('VS preparation requires an explicitly selected PDB receptor')
    receptor = inspect_receptor(paths[plan.receptor],plan.attachment_residue,include_residue_inventory=True)
    # Selection/removal is a prior scientific decision, not an implicit worker
    # cleanup operation. Refuse extra chains, ligands, ions, waters and modified
    # residues until a separate preparation contract supports them.
    amino = set('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split())
    if any(row['chain'] != receptor['target']['chain'] or row['residue_name'] not in amino
           for row in receptor['observed_residues']):
        raise ValueError('VS receptor must be a reviewed single protein chain without unhandled components')
    runtime = runtime_paths(configuration)
    if not validate_license_config(str(runtime['license_file']))[0]:
        raise ValueError('VS preparation requires a legitimate configured vendor license')
    required = ['run','covalent_docking','utilities/prepwizard','ligprep']
    assets = [runtime['root']/name for name in required] + [runtime['license_file']]
    if any(not path.is_file() for path in assets):
        raise ValueError('VS preparation vendor installation is incomplete')
    sources = [Path(__file__),Path(__file__).with_name('covdock_vs.py'),
               Path(__file__).with_name('covdock_receptor.py'),Path(__file__).with_name('schrodinger.py'),
               Path(__file__).parent/'adapters/covdock.py',Path(__file__).with_name('covdock_vs_runner.py'),
               Path(__file__).with_name('covdock_vs_output.py'),
               Path(__file__).with_name('covdock_vs_geometry.py'),
               Path(__file__).parents[2]/'scripts/run_covdock_vs.py']
    from . import ligand_first_geometry, ligand_first_inputs
    sources += [Path(ligand_first_geometry.__file__),Path(ligand_first_inputs.__file__),
                Path(__file__).with_name('ligand_vs_evidence.py')]
    if plan.source.route == 'library-first':
        sources += [Path(__file__).with_name(name) for name in
                    ('library_vs_evidence.py', 'library_vs_geometry.py', 'covdock_vs_evidence.py')]
    if plan.source.route == 'de-novo':
        from covscreen_de_novo import batch_vs, batch_input, batch_selection, batch_chemical_qc, batch_diversity, batch_geometry, batch_lo_selection
        sources += [Path(module.__file__) for module in
                    (batch_vs, batch_input, batch_selection, batch_chemical_qc, batch_diversity, batch_geometry, batch_lo_selection)]
    if any(sha256_file(path) != hashes[name] for name,path in paths.items()):
        raise ValueError('VS preparation input changed during inspection')
    intent = plan.model_dump(mode='json')
    if plan.execution_instance is None:
        intent.pop('execution_instance')
    else:
        sources.extend(Path(__file__).with_name(name) for name in
                       ('instance_process.py', 'instance_vs_claim.py'))
    return {'schema':'covdock-vs-preparation-plan-v1','plan':intent,
        'intent_sha256':hashlib.sha256(json.dumps(intent,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        'engine':'schrodinger-covdock-vs','mode':'enrichment','candidate_count':len(records),
        'candidates':records,'input_sha256':hashes,'receptor_input':receptor,
        'runtime':{name:str(path.resolve()) if path else None for name,path in runtime.items()},
        'runtime_assets':{str(path.resolve()):sha256_file(path) for path in assets},
        'sources':{str(path.resolve()):sha256_file(path) for path in sources},
        'preparation_recipe':{'receptor':'prepwizard-heavy-identity-bound-noepik-ph7-v2',
            'ligands':'ligprep-preserve-ionization-no-tautomers-single-stereo-v1'},
        'worker_backend':'external-instance' if plan.execution_instance else 'external-hpc',
        'timeout_seconds':plan.budget.max_runtime_minutes*60,
        'native_approval_required':True,'scientific_acceptance':False,'md_ready':False}


def revalidate(root: Path, packet: dict) -> dict:
    runtime = packet['runtime']
    actual = freeze(root,packet['plan'],{'enabled':True,**runtime})
    if actual != packet:
        raise ValueError('VS preparation inputs, recipe, sources or configured runtime changed')
    return actual


def commands(root: Path, packet: dict) -> list[list[str]]:
    """Run both commands from a fresh prepared directory on the reserved host."""
    actual = revalidate(root,packet)
    plan = actual['plan']
    vendor = Path(actual['runtime']['root'])
    return [
        [str(vendor/'utilities/prepwizard'),str(bound_file(root,plan['receptor'])),
         'receptor.mae','-fix','-noepik','-propka_pH','7.0','-NJOBS','1','-WAIT','-LOCAL'],
        [str(vendor/'ligprep'),'-isd',str(bound_file(root,plan['original_ligands'])),
         '-omae','ligands.mae','-i','0','-nt','-s','1','-NJOBS','1','-LOCAL','-WAIT'],
    ]


def inspect_prepared(root: Path, packet: dict, prepared: Path) -> dict:
    """Only exact identity-preserving outputs may become a VS docking input."""
    actual = revalidate(root,packet)
    plan = actual['plan']
    directory = prepared.resolve().relative_to(root.resolve())
    inputs = {key:plan[key] for key in ('source','original_ligands','attachment_residue',
                                      'budget','njobs','reaction_type')}
    inputs.update(prepared_receptor=(directory/'receptor.mae').as_posix(),
                  prepared_ligands=(directory/'ligands.mae').as_posix())
    witness = inspect_inputs(root,inputs)
    # A valid target SG alone cannot establish that PrepWizard retained the
    # approved protein. Compare every heavy-atom residue/name/element identity;
    # hydrogens and protonation may change in the declared preparation recipe.
    # This is not a claim that coordinates stayed fixed or the receptor is MD-ready.
    from .covdock_vs_output import protein_graph
    original = Chem.MolFromPDBBlock(bound_file(root, plan['receptor']).read_text(),
        sanitize=False, removeHs=False, proximityBonding=False)
    prepared_receptors = molecules(bound_file(root, inputs['prepared_receptor']), True)
    if original is None or len(prepared_receptors) != 1:
        raise ValueError('VS preparation requires one original and prepared receptor')
    prepared_receptor = prepared_receptors[0]
    if protein_graph(original, range(original.GetNumAtoms()))[0] != protein_graph(
            prepared_receptor, range(prepared_receptor.GetNumAtoms()))[0]:
        raise ValueError('VS preparation changed the approved receptor heavy atom identity')
    return witness
