"""Run the signed VS recipe only inside its reserved HPC worker.

The scheduler allocation is the hard wall-clock/resource boundary for vendor
JobControl descendants. This runner never resubmits, resumes or changes engines.
"""
import getpass
import json
import os
from pathlib import Path
import subprocess
import time

from . import covdock_vs_preparation as preparation
from . import covdock_vs
from .jobs import ArtifactRegistry, sha256_file
from .native_approval import verify_spec
from .source_release import verify_input, verify_source


def environment(packet: dict, scratch: Path) -> dict:
    runtime = packet['runtime']
    result = {key:os.environ[key] for key in ('HOME','PATH','LANG','LC_ALL','SLURM_JOB_ID') if key in os.environ}
    result.update(SCHRODINGER=runtime['root'],SCHROD_LICENSE_FILE=runtime['license_file'],
        SCHRODINGER_TMPDIR=str(scratch),SCHRODINGER_SAVE_JOBDIR='1',
        USER=getpass.getuser(),LOGNAME=getpass.getuser(),OMP_NUM_THREADS='1',
        OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
    if runtime['runtime_lib_dir']:
        result['LD_LIBRARY_PATH'] = runtime['runtime_lib_dir'] + ':/usr/lib/x86_64-linux-gnu:/lib/x86_64-linux-gnu'
    return result


def validate_approved_recipe(root: Path, spec):
    verify_spec(spec)
    verify_source(spec)
    from .docking_policy import POLICY
    if not POLICY['native_vs_workflow_ready']:
        raise ValueError('CovDock-VS native workflow migration is not yet complete')
    if (spec.engine != 'schrodinger-covdock-vs' or spec.execution_mode != 'execute'
            or not spec.human_approved
            or spec.attempt != 1 or spec.parent_run_id is not None):
        raise ValueError('VS requires its original native-approved job')
    packet = preparation.revalidate(root,spec.parameters['vs_preparation'])
    if spec.provenance.get('worker_backend') != packet['worker_backend']:
        raise ValueError('VS job backend differs from its frozen recipe')
    plan = packet['plan']
    if (spec.route != plan['source']['route'] or spec.project_id != plan['source']['workflow_id']
            or spec.resource_budget.model_dump(mode='json') != plan['budget']):
        raise ValueError('VS job source or budget differs from its frozen recipe')
    for name,checksum in spec.input_sha256.items():
        verify_input(root,name,checksum,spec)
    if any(spec.input_sha256.get(name) != checksum for name,checksum in packet['input_sha256'].items()):
        raise ValueError('VS preparation inputs are not bound to the approved job')
    return packet


def validate_job(root: Path, spec):
    if spec.provenance.get('worker_backend') != 'external-hpc':
        raise ValueError('VS requires its original native-approved HPC job')
    packet = validate_approved_recipe(root, spec)
    registry = ArtifactRegistry(root)
    if registry.load_result(spec.run_id).state != 'RUNNING':
        raise ValueError('VS wrapper requires the active registered worker state')
    job = registry.job_dir(spec.run_id)
    receipt = json.loads((job/'external_hpc.json').read_text())
    if (receipt.get('spec_sha256') != sha256_file(job/'spec.json')
            or not (job/'external_worker_claim').is_dir()):
        raise ValueError('VS worker lacks the exact HPC reservation and worker claim')
    return packet


def run(root: Path, run_id: str):
    root = root.resolve(strict=True)
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(run_id)
    packet = validate_job(root,spec)
    return _run_approved_recipe(root, registry, spec, packet)


def _run_approved_recipe(root, registry, spec, packet, *, instance_tree=None):
    run_id = spec.run_id
    job = registry.job_dir(run_id)
    # A separate claim protects against manual wrapper replay even when the
    # scheduler's existing worker claim is present. No stale-claim deletion.
    (job/'vs_worker_claim').mkdir()
    output = job/'artifacts'
    prepared, docking, scratch = (output/name for name in ('prepared','docking','scratch'))
    for directory in (prepared,docking,scratch): directory.mkdir(parents=True,exist_ok=False)
    deadline = time.monotonic() + packet['timeout_seconds']
    env = environment(packet,scratch)
    steps = []

    def execute(argv, directory, label):
        remaining = deadline-time.monotonic()
        if instance_tree is None and remaining <= 0:
            raise TimeoutError('VS total preparation/docking budget exhausted')
        with (directory/(label+'.stdout.log')).open('xb') as stdout, (directory/(label+'.stderr.log')).open('xb') as stderr:
            options = dict(cwd=directory, env=env, stdin=subprocess.DEVNULL,
                           stdout=stdout, stderr=stderr)
            if instance_tree is None:
                return_code = subprocess.run(argv, timeout=remaining, check=False, **options).returncode
            else:
                from .job_cancellation import requested
                return_code = instance_tree.run(
                    argv, deadline=deadline, cancelled=lambda: requested(registry, run_id),
                    cpu_count=spec.resource_budget.cpu_count, **options,
                )
        steps.append({'stage':label,'return_code':return_code})
        if return_code: raise ValueError('VS vendor stage returned nonzero: '+label)

    result = {'schema':'covdock-vs-execution-result-v1','run_id':run_id,'engine':spec.engine,
        'mode':'enrichment','state':'FAILED','steps':steps,'scientific_acceptance':False,'md_ready':False}
    try:
        for label,argv in zip(('prepwizard','ligprep'),preparation.commands(root,packet)):
            execute(argv,prepared,label)
        witness = preparation.inspect_prepared(root,packet,prepared)
        (output/'prepared-input-witness.json').write_text(json.dumps(witness,sort_keys=True,indent=2)+'\n')
        configuration = {'enabled':True,**packet['runtime']}
        # Run IDs need not be vendor-safe; a digest yields a stable bounded name.
        name = 'vs_' + sha256_file(job/'spec.json')[:20]
        argv = covdock_vs.command(root,witness,configuration,name)
        preparation.revalidate(root,packet)
        execute(argv,docking,'covdock_vs')
        raw = docking/(name+'-out.maegz')
        if not raw.is_file() or raw.is_symlink() or not raw.stat().st_size:
            raise ValueError('VS vendor exit zero lacks its exact nonempty output file')
        from .covdock_vs_output import read
        from rdkit import rdBase
        raw_sha = sha256_file(raw)
        rejected = [] if spec.route == 'de-novo' else None
        rows, counts = read(raw, covdock_vs.molecules(root/packet['plan']['original_ligands'], False),
            covdock_vs.molecules(prepared/'ligands.mae', True),
            covdock_vs.molecules(prepared/'receptor.mae', True)[0], packet['plan']['attachment_residue'],
            expected_sha256=raw_sha, reaction_site_rejections=rejected)
        identity = {'schema':'covdock-vs-output-identity-v1', 'rdkit_version':rdBase.rdkitVersion,
            'raw_sha256':raw_sha, 'candidate_pose_counts':counts,
            'poses':[{key:row[key] for key in ('pose_index','candidate_id','input_index','score',
                'product_identity_verified','prepared_receptor_identity_verified')} for row in rows],
            'scientific_acceptance':False, 'md_ready':False}
        if rejected:
            identity['reaction_site_rejections'] = rejected
        (output/'vs-output-identity.json').write_text(json.dumps(identity,sort_keys=True,indent=2)+'\n')
        result.update(state='COMPLETED',raw_output={'path':str(raw.relative_to(root)),
            'sha256':raw_sha},output_validation='IDENTITY_VERIFIED_SCIENTIFIC_GATES_PENDING')
    except Exception as error:
        result['error_type'] = type(error).__name__
        result['error'] = str(error)
        raise
    finally:
        (output/'vs-execution.json').write_text(json.dumps(result,sort_keys=True,indent=2)+'\n')
    return result
