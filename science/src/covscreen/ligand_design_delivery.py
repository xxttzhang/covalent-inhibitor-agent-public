"""Exact, bounded delivery of current or archived workflow-bound design reports."""
import base64
import hashlib
import json
from pathlib import Path
import re

from .ligand_parent_delivery import LIMIT


def export_design_report(root: Path, state: dict, design_id: str | None = None) -> dict:
    selected = design_id or state.get('design_id')
    allowed = {state.get('design_id')} | {branch.get('design_id') for branch in state.get('branches', [])}
    if not re.fullmatch(r'design-[a-f0-9]{24}', selected or '') or selected not in allowed:
        raise ValueError('Design must belong to the current workflow or its archived branches')
    folder = root / 'results/ligand-designs' / selected

    def read(path):
        for part in (path, *path.parents):
            if part == root:
                break
            if part.is_symlink():
                raise ValueError('Design delivery refuses symlinks')
        with path.open('rb') as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('Design artifact exceeds bounded delivery limit')
        return raw

    def sealed(directory, name):
        manifest = read(directory / 'manifest.json')
        raw = read(directory / name)
        if json.loads(manifest).get('files', {}).get(name) != hashlib.sha256(raw).hexdigest():
            raise ValueError('Design delivery seal mismatch')
        return raw, hashlib.sha256(manifest).hexdigest()

    raw, design_seal = sealed(folder, 'design.json')
    design = json.loads(raw)
    if design.get('design_id') != selected or not re.fullmatch(r'hyp-[a-f0-9]{24}', design.get('evidence_id', '')):
        raise ValueError('Design provenance mismatch')
    # Verify the generation manifest named by the sealed disposition, and the
    # summary bytes; export never re-runs design or requires latest evidence.
    _, generation_seal = sealed(folder / 'generation', 'summary.json')
    if (selected == state.get('design_id')
            and (state.get('refinement_input') or {}).get('source_kind') == 'sealed-ligand-direct-lo-v1'
            and (state.get('covdock_execution') or {}).get('report')):
        return export_completed_design(root, state, design_seal, generation_seal)
    files = []
    for name, mime in [('disposition.json', 'application/json'), ('report.md', 'text/markdown')]:
        raw, delivery_seal = sealed(folder / 'delivery', name)
        if name == 'disposition.json':
            disposition = json.loads(raw)
            if (disposition.get('design_id') != selected or disposition.get('evidence_id') != design['evidence_id']
                    or disposition.get('binding') != {'design_manifest_sha256': design_seal,
                                                     'generation_manifest_sha256': generation_seal}):
                raise ValueError('Disposition provenance mismatch')
        files.append({'name': name, 'mime_type': mime, 'sha256': hashlib.sha256(raw).hexdigest(),
                      'bytes': len(raw), 'base64': base64.b64encode(raw).decode('ascii')})
    return {'schema': 'ligand-design-delivery-v1', 'workflow_id': state['workflow_id'],
            'evidence_id': design['evidence_id'], 'design_id': selected,
            'delivery_manifest_sha256': delivery_seal, 'files': files,
            'hash_scope': 'sealed design delivery bytes; workflow-bound current or archived design',
            'compute_submitted': False}


def export_completed_design(root, state, design_seal, generation_seal):
    """Compose a read-only delivery from verified inputs and a frozen LO report.

    Do not create the old pre-docking delivery or overwrite historical reports.
    Both source loaders verify their sealed evidence before any counts are used.
    """
    from .ligand_direct_lo import load
    from .ligand_covdock_report import export_report
    source = load(root, state)
    delivery = export_report(root, state)
    report = json.loads(base64.b64decode(next(
        item['base64'] for item in delivery['files'] if item['name'] == 'report.json')))
    disposition, markdown = completed_design(source, report)
    disposition['binding'] = {'design_manifest_sha256': design_seal,
        'generation_manifest_sha256': generation_seal,
        'input_manifest_sha256': state['refinement_input']['manifest_sha256'],
        'covdock_report_manifest_sha256': delivery['delivery_manifest_sha256']}
    files = []
    for name, mime, raw in [
        ('disposition.json', 'application/json',
         (json.dumps(disposition, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()),
        ('report.md', 'text/markdown', markdown.encode()),
    ]:
        if len(raw) > LIMIT:
            raise ValueError('Design artifact exceeds bounded delivery limit')
        files.append({'name': name, 'mime_type': mime, 'sha256': hashlib.sha256(raw).hexdigest(),
            'bytes': len(raw), 'base64': base64.b64encode(raw).decode('ascii')})
    manifest = json.dumps({'files': {item['name']: item['sha256'] for item in files}}, sort_keys=True).encode()
    return {'schema': 'ligand-design-delivery-v1', 'workflow_id': state['workflow_id'],
        'evidence_id': state['evidence_id'], 'design_id': state['design_id'],
        'delivery_manifest_sha256': hashlib.sha256(manifest).hexdigest(), 'files': files,
        'hash_scope': 'deterministic delivery bytes composed from verified sealed design, direct LO input and LO report',
        'compute_submitted': False}


def completed_design(source, report):
    """Join candidate identities, never infer a denominator from successful poses."""
    for key in ('workflow_id', 'design_id', 'evidence_id'):
        if source[key] != report[key]:
            raise ValueError('Design and LO report provenance mismatch')
    selected = source['selected_candidate_ids']
    deferred = source['candidate_deferrals']
    rows = report['evaluation']['candidates']
    ids = [row['compound_id'] for row in rows]
    deferred_ids = [row['candidate_id'] for row in deferred]
    if (len(set(ids)) != len(ids) or set(ids) != set(selected)
            or len(set(selected + deferred_ids)) != source['generated_candidate_count']
            or len(selected) + len(deferred_ids) != source['generated_candidate_count']
            or report['evaluation']['candidate_count'] != len(ids)):
        raise ValueError('Design and LO candidate denominators differ')
    counts = {'generated': source['generated_candidate_count'], 'deferred': len(deferred),
        'docked': len(rows), 'passed': sum(row['gate']['review_eligible'] is True for row in rows)}
    disposition = {'schema': 'ligand-design-completed-report-v1',
        **{key: report[key] for key in ('workflow_id', 'design_id', 'evidence_id')},
        'counts': counts, 'candidate_deferrals': deferred, 'candidates': rows,
        'execution': report['execution'], 'run_id': report['evaluation']['run_id'],
        'gate_algorithm_version': report['evaluation']['gate_algorithm_version'],
        'count_semantics': {'docked': 'Candidates in the verified LO batch, including engine failures',
            'passed': 'review_eligible under the recorded frozen policy; not experimental validation or MD admission'},
        'compute_submitted_by_report': False, 'validated_inhibitor': False, 'md_ready': False}
    lines = ['# Ligand-first 完整候选交付报告', '',
        f"研究：{report['workflow_id']}；LO 任务：{disposition['run_id']}。", '',
        f"生成 {counts['generated']} 个；暂缓 {counts['deferred']} 个；进入 LO 批次 {counts['docked']} 个；满足本次入选条件 {counts['passed']} 个。", '',
        '对接数量包含批次内失败候选；通过指本次冻结规则的 review_eligible，不代表活性验证或 MD 准入。', '',
        '## 暂缓候选', '']
    lines += [f"- {row['candidate_id']}：{row['reason']}；详情：{json.dumps(row, ensure_ascii=False, sort_keys=True)}" for row in deferred]
    lines += ['', '## LO 候选及判定原因', '']
    lines += [f"- {row['compound_id']}：引擎 {row['engine_status']}；满足本次入选条件 {row['gate']['review_eligible']}；"
              + str(row.get('failure_reason') or row['gate']['covdock_gate_reason']) for row in rows]
    lines += ['', '完整测量与判定保存在配套 disposition.json；原正式 LO 报告与历史记录保留。',
              '本报告未提交计算，未运行 MD；不证明亲和力、反应动力学、选择性或实验活性。', '']
    return disposition, '\n'.join(lines)
