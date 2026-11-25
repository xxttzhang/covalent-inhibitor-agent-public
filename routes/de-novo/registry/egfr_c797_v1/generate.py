"""Run once on one GPU. All inputs are pinned and every requested slot is retained."""
import csv
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preflight(root):
    request = json.loads((root / 'request.json').read_text())
    profile = json.loads((root / 'profile.json').read_text())
    for name, expected in request['package_hashes'].items():
        if sha(root / name) != expected:
            raise RuntimeError('Package changed: ' + name)
    for name, expected in profile['external_hashes'].items():
        if sha(name) != expected:
            raise RuntimeError('Model/input changed: ' + name)
    repo = Path(profile['repo'])
    commit = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    if commit != profile['repo_commit']:
        raise RuntimeError('Model commit differs')
    subprocess.run(['git', '-C', str(repo), 'diff', '--exit-code', 'HEAD', '--'], check=True)
    if not 1 <= request['slots'] <= profile['max_slots']:
        raise RuntimeError('Slot budget exceeded')
    required = [repo / 'scripts/sample_use.py',
                repo / 'data/trained_models/pxm/train_config/train.yml',
                repo / 'data/trained_models/pxm/checkpoints/pocketxmol.ckpt']
    if any(str(name) not in profile['external_hashes'] for name in required):
        raise RuntimeError('Required model assets are not pinned')
    return dict(state='ASSETS_VERIFIED', request_sha256=sha(root / 'request.json'),
                repo_commit=commit, external_hashes=profile['external_hashes'],
                compute_submitted=False)


def run(root):
    preflight(root)
    import yaml
    request = json.loads((root / 'request.json').read_text())
    profile = json.loads((root / 'profile.json').read_text())
    repo = Path(profile['repo'])
    output = root / 'output'
    output.mkdir(exist_ok=False)
    config = yaml.safe_load((root / 'assets/pocketxmol.yml').read_text())
    cfg = output / 'task.yml'
    raw = output / 'raw'
    offset = sum(map(ord, str(raw) + str(cfg)))
    config['sample'].update(seed=request['seed'] - offset, num_mols=request['slots'], num_repeats=1)
    config['data'].update(protein_path=str(root / 'assets/protein_A.pdb'), input_ligand=str(root / 'assets/acrylamide_prereaction.sdf'))
    cfg.write_text(yaml.safe_dump(config))
    model = output / 'model.yml'
    model.write_text(yaml.safe_dump({'model': {'checkpoint': str(repo / 'data/trained_models/pxm/checkpoints/pocketxmol.ckpt')}}))
    env = {k: v for k, v in os.environ.items() if k not in ['PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_AUDIT']}
    env.update(OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4', WANDB_MODE='disabled', PYTHONDONTWRITEBYTECODE='1')
    env['PATH'] = str(Path(profile['python']).parent) + ':' + env.get('PATH', '')
    hardware = subprocess.check_output([profile['python'], '-c', 'import torch,json; assert torch.cuda.is_available(); print(json.dumps(dict(torch=torch.__version__,gpu=torch.cuda.get_device_name(0))))'], env=env, text=True)
    started = time.time()
    cmd = [profile['python'], str(repo / 'scripts/sample_use.py'), '--config_task', str(cfg), '--config_model', str(model), '--outdir', str(raw), '--device', 'cuda:0', '--batch_size', '2', '--num_workers', '0']
    with (output / 'generation.log').open('w') as log:
        result = subprocess.run(cmd, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=720)
    infos = list(raw.rglob('gen_info.csv'))
    rows = list(csv.DictReader(infos[0].open())) if len(infos) == 1 else []
    receipt = dict(task_id=request['id'], requested_slots=request['slots'], recorded_slots=len(rows), effective_seed=request['seed'], config_seed=config['sample']['seed'], path_seed_offset=offset, returncode=result.returncode, started=started, finished=time.time(), hardware=json.loads(hardware), request_sha256=sha(root / 'request.json'), no_refills=True, command=cmd)
    (output / 'receipt.json').write_text(json.dumps(receipt, indent=2))
    files = {str(p.relative_to(output)): sha(p) for p in output.rglob('*') if p.is_file()}
    (output / 'manifest.json').write_text(json.dumps(files, indent=2))
    with zipfile.ZipFile(root / 'output.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for file in output.rglob('*'):
            if file.is_file():
                archive.write(file, file.relative_to(output))
    print(json.dumps(receipt), flush=True)
    if result.returncode != 0 or len(rows) != request['slots']:
        raise RuntimeError('Generation incomplete; retain all output, never refill automatically')


if __name__ == '__main__':
    root = Path(sys.argv[1]).resolve()
    if len(sys.argv) == 3 and sys.argv[2] == '--preflight':
        print(json.dumps(preflight(root)), flush=True)
    elif len(sys.argv) == 2:
        run(root)
    else:
        raise SystemExit('Usage: generate.py TASK_ROOT [--preflight]')
