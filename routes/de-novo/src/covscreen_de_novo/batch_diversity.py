"""Deterministic max-min fingerprint selection within identity-eligible rows."""
import argparse,json,math
from pathlib import Path
from collections import Counter
from rdkit import Chem,DataStructs
from rdkit.Chem import rdFingerprintGenerator

def select(qc: dict, limit: int = 150) -> dict:
    if not 1 <= limit <= 200:
        raise ValueError('Expected a limit between 1 and 200')
    rows=sorted((r for r in qc['rows'] if r['identity_eligible']),key=lambda r:(r['canonical_smiles'],r['attempt_id']))
    keys=[r['canonical_smiles'] for r in rows]
    if len(keys)!=len(set(keys)):
        raise ValueError('Deduplicate before diversity selection')
    generator=rdFingerprintGenerator.GetMorganGenerator(radius=2,fpSize=2048)
    fps=[generator.GetFingerprint(Chem.MolFromSmiles(k)) for k in keys]
    # Acyclic candidates are not one scaffold family; distinguish their fingerprints.
    families=[r['scaffold'] or 'acyclic:'+r['canonical_smiles'] for r in rows]
    cap=max(5,math.ceil(limit*.2)); counts=Counter(); chosen=[]; distances=[1.0]*len(rows)
    remaining=set(range(len(rows)))
    while remaining and len(chosen)<limit:
        eligible=[i for i in sorted(remaining) if counts[families[i]]<cap]
        if not eligible:break
        i=max(eligible,key=lambda j:distances[j])
        chosen.append(i);remaining.remove(i);counts[families[i]]+=1
        for j in remaining:
            distances[j]=min(distances[j],1-DataStructs.TanimotoSimilarity(fps[i],fps[j]))
    selected=set(chosen)
    return {'method':'deterministic Morgan radius2/2048 max-min; lexical first and ties',
            'scope':'diversity only, no affinity inference','limit':limit,'scaffold_cap':cap,
            'eligible_count':len(rows),'selected_count':len(chosen),
            'selected':[rows[i] for i in chosen],
            'deferred':[dict(rows[i],selection_reason='SCAFFOLD_CAP' if counts[families[i]]>=cap else 'BATCH_LIMIT') for i in range(len(rows)) if i not in selected]}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('qc',type=Path);p.add_argument('output',type=Path);p.add_argument('--limit',type=int,default=150);a=p.parse_args()
    with a.output.open('x') as f:json.dump(select(json.loads(a.qc.read_text()),a.limit),f,indent=2)
