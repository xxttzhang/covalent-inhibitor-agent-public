"""Evaluate fresh PocketXMol output and produce traceable, separate raw/relaxed exports."""
import csv
import json
from collections import Counter
from pathlib import Path
import sys
from rdkit import Chem
from rdkit.Chem import Draw
from evaluator import ROOT, SMOKE, digest, evaluate, protein, relax, save_json


def identity_failures(mol, upstream):
    failures = []
    if upstream.get('tag') == 'bad':
        failures.append('upstream_reconstruction_failed')
    logged = Chem.MolFromSmiles(upstream.get('smiles', ''))
    if mol is not None and (logged is None or Chem.MolToSmiles(logged) != Chem.MolToSmiles(mol)):
        failures.append('sdf_log_identity_mismatch')
    return failures


def audit(root):
    request = json.loads((root / 'request.json').read_text())
    source = root / 'download'
    receipt = json.loads((source / 'receipt.json').read_text())
    if receipt['task_id'] != request['id'] or receipt['request_sha256'] != digest(root / 'request.json'):
        raise RuntimeError('Output belongs to a different request')
    if receipt['returncode'] or receipt['recorded_slots'] != request['slots']:
        raise RuntimeError('Incomplete upstream output')
    infos = list(source.rglob('gen_info.csv'))
    if len(infos) != 1:
        raise RuntimeError('Expected one sampling log')
    info = infos[0]
    records = {}
    for record in csv.DictReader(info.open()):
        filename = record['filename']
        if Path(filename).name != filename:
            raise RuntimeError('Unexpected candidate path')
        slot = int(filename.split('-')[0].split('.')[0])
        if slot in records or slot not in range(request['slots']):
            raise RuntimeError('Duplicate or out of budget slot')
        records[slot] = record
    output = root / 'acceptance'
    output.mkdir(exist_ok=False)
    (output / 'relaxed').mkdir()
    prot = protein(SMOKE / 'protein_A.pdb')
    rows = []
    exports = {stage: Chem.SDWriter(str(output / f'{stage}_accepted.sdf')) for stage in ['raw', 'relaxed']}
    pictures = []
    legends = []
    for slot in range(request['slots']):
        upstream = records.get(slot, {})
        sdf = info.parent / (info.parent.name + '_SDF') / upstream.get('filename', 'missing.sdf')
        mol = Chem.MolFromMolFile(str(sdf), sanitize=True, removeHs=True) if sdf.is_file() else None
        raw = evaluate(mol, prot, 'pre')
        provenance = identity_failures(mol, upstream)
        raw['failures'] += provenance
        raw['passed'] = not raw['failures']
        relaxed, optimization = relax(mol, raw.get('core', {}), prot, 'pre')
        checked = {'passed': False, 'failures': ['relaxation_not_available']}
        relaxed_path = output / 'relaxed' / f'{slot}.sdf'
        if relaxed is not None:
            Chem.MolToMolFile(relaxed, str(relaxed_path))
            relaxed = Chem.MolFromMolFile(str(relaxed_path), sanitize=True, removeHs=True)
            checked = evaluate(relaxed, prot, 'pre')
            if optimization['status'] != 'CONVERGED':
                checked['failures'].append('relaxation_not_converged')
            if relaxed is None or Chem.MolToSmiles(relaxed) != Chem.MolToSmiles(mol):
                checked['failures'].append('relaxation_changed_chemical_identity')
        checked['failures'] += provenance
        checked['passed'] = not checked['failures']
        row = dict(slot=slot, upstream=upstream, raw=raw, relaxed=checked, optimization=optimization, source=str(sdf), source_sha256=digest(sdf) if sdf.is_file() else None)
        save_json(output / f'slot_{slot:03d}.json', row)
        rows.append(row)
        for stage, candidate, check in [('raw', mol, raw), ('relaxed', relaxed, checked)]:
            if check['passed']:
                candidate.SetProp('_Name', f'{request["id"]}:{slot}:{stage}')
                candidate.SetProp('task_id', request['id'])
                candidate.SetProp('slot', str(slot))
                candidate.SetProp('stage', stage)
                candidate.SetProp('canonical_smiles', check['smiles'])
                exports[stage].write(candidate)
        if raw['passed'] and len(pictures) < 24:
            pictures.append(Chem.MolFromSmiles(raw['smiles']))
            legends.append(f'Slot {slot} | raw pass')
        print(json.dumps(dict(slot=slot, raw=raw['passed'], relaxed=checked['passed'])), flush=True)
    for writer in exports.values():
        writer.close()
    summary = dict(task_id=request['id'], engine='PocketXMol', requested_slots=request['slots'], outputs=len(records), seed=request['seed'], no_refills=True, target='EGFR WT C797 (4G5J A)', warhead='fixed acrylamide; N-only growth', inference='Reference-assisted pre-reaction poses; structural checks only, no activity or covalent binding confirmation.', rules_sha256=digest(ROOT / 'control/rules.json'), evaluator_sha256=digest(ROOT / 'evaluator.py'))
    for stage in ['raw', 'relaxed']:
        passing = [r[stage] for r in rows if r[stage]['passed']]
        summary[stage] = dict(pass_count=len(passing), unique_smiles=len({r['smiles'] for r in passing}), failure_counts=dict(Counter(f for r in rows for f in r[stage]['failures'])))
    save_json(output / 'summary.json', summary)
    with (output / 'candidates.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['slot', 'raw_pass', 'relaxed_pass', 'smiles', 'raw_failures', 'relaxed_failures'])
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(slot=r['slot'], raw_pass=r['raw']['passed'], relaxed_pass=r['relaxed']['passed'], smiles=r['raw'].get('smiles', ''), raw_failures=';'.join(r['raw']['failures']), relaxed_failures=';'.join(r['relaxed']['failures'])))
    if pictures:
        Draw.MolsToGridImage(pictures, molsPerRow=4, subImgSize=(320, 240), legends=legends).save(output / 'candidates.png')
    report = f'# EGFR C797 从头生成结果\n\n任务：{request["id"]}\n\n本次真实请求 {request["slots"]} 个生成槽位，原始结构通过 {summary["raw"]["pass_count"]} 个，约束优化后通过 {summary["relaxed"]["pass_count"]} 个。两组分别统计，不能相加。\n\n靶点：EGFR WT，4G5J A:C797；固定丙烯酰胺五个重原子，只从 N 生长。PocketXMol，随机种子 {request["seed"]}，无自动补样。\n\n验收包括分子合法性、warhead 保留、生长连接点、固定坐标、蛋白碰撞、反应前几何、PoseBusters 和来源一致性。输出是结构候选，尚未证明 EGFR 活性或形成共价键。\n\n原始与优化后 SDF、逐分子 CSV、结构图和完整验收记录已保留。\n'
    (output / 'report.md').write_text(report)
    return summary


if __name__ == '__main__':
    audit(Path(sys.argv[1]).resolve())
