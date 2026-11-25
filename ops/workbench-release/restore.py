"""Verify a named workbench release and restore it to a new staging directory."""
import argparse, json, shutil, tarfile
from pathlib import Path
from baseline import ROOT, verify
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('version');parser.add_argument('destination',type=Path)
args=parser.parse_args()
source=ROOT/'releases/workbench'/args.version
receipt=verify(source)
destination=args.destination.resolve()
destination.mkdir(parents=True,exist_ok=False)
for name in ['source','web']:
    with tarfile.open(source/(name+'.tar.gz')) as archive:
        archive.extractall(destination/name,filter='data')
for file in source.iterdir():
    if file.suffix in ['.json','.mjs','.lock']: shutil.copy2(file,destination/file.name)
print(json.dumps({**receipt,'restored_to':str(destination),'production_changed':False}))
