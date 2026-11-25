"""Display-only complexes from verified generated poses; never docking or inference."""
import hashlib
import json
from pathlib import Path
import sys
import shutil
import urllib.parse
import urllib.request
import numpy as np
from rdkit import Chem


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main(task):
    request = json.loads((task / 'request.json').read_text())
    protein = task / 'package/assets/protein_A.pdb'
    assert sha(protein) == request['package_hashes']['assets/protein_A.pdb']
    atoms = [l for l in protein.read_text().splitlines() if l.startswith('ATOM  ')]
    positions = np.array([[float(l[30:38]), float(l[38:46]), float(l[46:54])] for l in atoms])
    target = [l for l in atoms if l[21]=='A' and int(l[22:26])==797 and l[12:16].strip()=='SG']
    assert len(target)==1 and target[0][17:20]=='CYS'
    sg = np.array([float(target[0][30:38]), float(target[0][38:46]), float(target[0][46:54])])
    dest = Path(request['directory']) / 'denovo-results' / request['id'] / 'pocket_views'
    dest.mkdir(parents=True, exist_ok=True)
    offset = max(int(l[6:11]) for l in atoms) + 1
    records = []
    for file in sorted((task / 'acceptance').glob('slot_*.json')):
        row = json.loads(file.read_text())
        if not row['raw']['passed']:
            continue
        source = Path(row['source'])
        assert sha(source)==row['source_sha256']
        mol = Chem.MolFromMolFile(str(source), removeHs=True)
        assert mol is not None and Chem.MolToSmiles(mol)==row['raw']['smiles']
        for atom in mol.GetAtoms():
            info = Chem.AtomPDBResidueInfo()
            info.SetName(f'{atom.GetSymbol()}{atom.GetIdx()+1}'.rjust(4))
            info.SetResidueName('LIG'); info.SetResidueNumber(1); info.SetChainId('Z'); info.SetIsHeteroAtom(True)
            atom.SetMonomerInfo(info)
        block = Chem.MolToPDBBlock(mol)
        ligand = []
        for line in block.splitlines():
            if line.startswith('HETATM'):
                ligand.append(line[:6]+f'{int(line[6:11])+offset:5d}'+line[11:])
            if line.startswith('CONECT'):
                ligand.append('CONECT'+''.join(f'{int(line[i:i+5])+offset:5d}' for i in range(6,len(line),5) if line[i:i+5].strip()))
        xyz = np.array([[float(l[30:38]),float(l[38:46]),float(l[46:54])] for l in ligand if l.startswith('HETATM')])
        assert xyz.shape==mol.GetConformer().GetPositions().shape
        assert np.max(np.abs(xyz-mol.GetConformer().GetPositions())) <= 0.000501
        assert all(int(l[6:11])>offset for l in ligand if l.startswith('HETATM'))
        distance = np.linalg.norm(positions[:,None,:]-xyz[None,:,:],axis=-1)
        residues = sorted({(l[21],int(l[22:26]),l[17:20]) for l,near in zip(atoms,distance.min(axis=1)<=5) if near})
        core = row['raw']['core']['mapping']; beta = core[4]
        separation = float(np.linalg.norm(sg-mol.GetConformer().GetPositions()[beta]))
        assert abs(separation-row['raw']['covalent_geometry']['SG_beta_distance_A'])<1e-8
        text = '\n'.join([
            'HEADER    DISPLAY ONLY: EGFR + GENERATED PRE-REACTION POSE',
            f'TITLE     POCKETXMOL SLOT {row["slot"]:03d}; EGFR WT 4G5J A:C797',
            'REMARK 900 ORIGINAL GENERATED COORDINATES; NO DOCKING OR REALIGNMENT.',
            'REMARK 900 PROTEIN CHAIN A; GENERATED LIGAND LIG Z 1.',
            f'REMARK 900 A CYS 797 SG TO Z LIG 1 {mol.GetAtomWithIdx(beta).GetPDBResidueInfo().GetName().strip()} = {separation:.4f} ANGSTROM.',
            'REMARK 900 PRE-REACTION GEOMETRY; NO PROTEIN-LIGAND COVALENT BOND.',
            'REMARK 900 ORIGINAL SDF IS THE CHEMICAL IDENTITY/BOND-ORDER SOURCE.',
            *atoms, 'TER', *ligand, 'END', ''])
        output = dest / f'EGFR_C797_slot_{row["slot"]:03d}_prereaction.pdb'
        marker = 'REMARK 900 OPENSCIENCE_TARGET_V1 '
        binding = dict(task_id=request['id'], plan_digest=sha(task/'request.json'),
            structure_sha256=hashlib.sha256(text.encode()).hexdigest(), source_receptor_sha256=sha(protein),
            target='EGFR', chain='A', residue='CYS', residue_number=797, insertion_code='', atom='SG',
            reaction_type='Michael Addition', ligand=dict(chain='Z',residue='LIG',residue_number=1,
            atom=mol.GetAtomWithIdx(beta).GetPDBResidueInfo().GetName().strip()))
        bound = marker + json.dumps(binding,separators=(',',':')) + '\n' + text
        if output.exists():
            previous = output.read_text()
            assert '\n'.join(l for l in previous.split('\n') if not l.startswith(marker))==text, 'Refuse to change any structure bytes'
            if previous != bound:
                history=dest/'history'/sha(output);history.mkdir(parents=True,exist_ok=True)
                shutil.copy2(output,history/output.name)
        output.write_text(bound)
        check = Chem.MolFromPDBFile(str(output), sanitize=False, removeHs=False, proximityBonding=False)
        assert check is not None and check.GetNumAtoms()==len(atoms)+mol.GetNumAtoms()
        records.append(dict(slot=row['slot'],path=str(output),sha256=sha(output),source_sdf=str(source),source_sha256=sha(source),smiles=row['raw']['smiles'],protein_sha256=sha(protein),target='A:CYS:797:SG',ligand='Z:LIG:1',beta_atom=mol.GetAtomWithIdx(beta).GetPDBResidueInfo().GetName().strip(),sg_beta_distance_A=separation,pocket_5A_residues=residues))
    query='?'+urllib.parse.urlencode({'directory':request['directory']})
    def api(route,body=None):
        req=urllib.request.Request('http://127.0.0.1:4096'+route+query,data=None if body is None else json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req) as response:
            return json.load(response)
    existing=api('/file/artifact-store')
    for record in records:
        title=f'de novo 结合口袋 · 候选 {record["slot"]:03d} · EGFR C797 · 反应前'
        matches=[a for a in existing if a.get('current',{}).get('sourcePath')==record['path']]
        assert len(matches)<=1
        artifact=matches[0] if matches and matches[0]['current']['sha256']==record['sha256'] else api('/file/artifact',dict(path=record['path'],sessionID=request['session_id'],summary=title))
        assert artifact['current']['sha256']==record['sha256']
        record.update(artifact_id=artifact['id'],artifact_version=artifact['currentVersionID'],title=title)
    report=dict(task_id=request['id'],session_id=request['session_id'],display_only=True,new_generation=False,new_docking=False,coordinate_rounding_max_A=0.0005,candidates=records)
    previous_manifest=dest/'pocket_manifest.json'
    if previous_manifest.exists():
        history=dest/'history';history.mkdir(exist_ok=True)
        shutil.copy2(previous_manifest,history/(sha(previous_manifest)+'-pocket_manifest.json'))
    previous_manifest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(dict(directory=str(dest),candidates=len(records),first=records[0]),ensure_ascii=False))


if __name__=='__main__':
    main(Path(sys.argv[1]).resolve())
