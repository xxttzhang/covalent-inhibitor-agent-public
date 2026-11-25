"""Freeze a named, verifiable UI baseline; restore only into an empty staging folder."""
import argparse, hashlib, json, shutil, tarfile, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def create(version, source_record=None, deployment_record=None, screenshots_directory=None):
    source_record = Path(source_record) if source_record else ROOT/'reports/workbench-standardization-20261008/baseline/source-release.json'
    deployment_record = Path(deployment_record) if deployment_record else ROOT/'reports/three-route-workbench-20261008/deployment.json'
    source = json.loads(source_record.read_text())
    deployment = json.loads(deployment_record.read_text())
    live = ROOT/'openscience-custom/backend/cli/integrations/live-web-assets'
    assets = json.loads((live/(deployment['version']+'.json')).read_text())
    for item in assets.values():
        if sha(Path(item['file'])) != item['sha256']: raise ValueError('Runtime asset hash mismatch')
    folder = ROOT/'releases/workbench'/version
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source['archive'], folder/'source.tar.gz')
    with tarfile.open(folder/'web.tar.gz','w:gz') as archive:
        for name,item in sorted(assets.items()): archive.add(item['file'], arcname=name.lstrip('/'), recursive=False)
    screenshots=Path(screenshots_directory) if screenshots_directory else ROOT/'reports/workbench-standardization-20261008/baseline'
    for file in screenshots.glob('*.png'): shutil.copy2(file, folder/file.name)
    shutil.copy2(deployment_record,folder/'deployment.json')
    if deployment.get('runtimeBundle'):
        runtime=Path(deployment['runtimeBundle'])
        if sha(runtime)!=deployment['runtimeSha256']: raise ValueError('Runtime bundle changed')
        shutil.copy2(runtime,folder/'discovery-runtime.mjs')
        for name in ['project','runtime']:
            shutil.copy2(ROOT/'openscience-custom/.discovery'/(name+'.json'),folder/(name+'-config.json'))
        shutil.copy2(ROOT/'ops/workbench-release/md-analysis-requirements.lock',folder/'md-analysis-requirements.lock')
    metadata={'schema':1,'version':version,'source_id':source['release_id'],'runtime_id':deployment['version'],
              'entry':deployment['entry'],'viewport':{'width':1600,'height':1100},
              'dynamic_regions':['.discovery-sync small','canvas','.discovery-footer'],
              'external_state':'Scientific evidence, project configuration, artifact store and credentials are not rolled back.',
              'files':{p.name:sha(p) for p in folder.iterdir()}}
    (folder/'version.json').write_text(json.dumps(metadata,indent=2))
    return folder

def verify(folder):
    meta=json.loads((folder/'version.json').read_text())
    for name,expected in meta['files'].items():
        if Path(name).name != name or sha(folder/name)!=expected: raise ValueError('Baseline changed: '+name)
    with tempfile.TemporaryDirectory(prefix='workbench-restore-') as directory:
        for name in ['source.tar.gz','web.tar.gz']:
            with tarfile.open(folder/name) as archive:
                for member in archive:
                    parts=Path(member.name).parts
                    if not member.isfile() or Path(member.name).is_absolute() or '..' in parts: raise ValueError('Unsafe archive')
                archive.extractall(Path(directory)/name[:-7],filter='data')
        manifest=json.loads((Path(directory)/'source/manifest.json').read_text())
        for name,expected in manifest['files'].items():
            if sha(Path(directory)/'source'/name)!=expected: raise ValueError('Restored source differs')
        entry=Path(directory)/'web'/meta['entry'].lstrip('/')
        if not entry.is_file(): raise ValueError('Restored frontend entry missing')
    return {'verified':True,'version':meta['version'],'source_files':len(manifest['files']),'staging_restore_verified':True}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['create','verify']);parser.add_argument('version');parser.add_argument('--source-record');parser.add_argument('--deployment-record');parser.add_argument('--screenshots');args=parser.parse_args()
    folder=create(args.version,args.source_record,args.deployment_record,args.screenshots) if args.command=='create' else ROOT/'releases/workbench'/args.version
    print(json.dumps(verify(folder)))
