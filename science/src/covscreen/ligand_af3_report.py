"""Immutable, measurement-only AF3 report. No scientific acceptance is inferred."""
import hashlib
import json
from uuid import uuid4

from .jobs import sha256_file
from .ligand_design import seal, verify
from .receptor_evidence import regular


def render(payload):
    review = payload['evaluation']
    mapping = review['target_mapping']
    source = mapping['source']
    lines = ['# Ligand-first AF3 结果核验报告', '',
        f"工作流：{payload['workflow_id']}；任务：{review['run_id']}。",
        f"任务状态：{payload['execution']['state']}；退出码：{payload['execution']['return_code']}。",
        f"位点映射：canonical C{source['uniprot_residue']} → {source['entry_id']} 作者链 "
        f"{source['auth_asym_id']} / {source['author_residue_id']} → 投影构建体 "
        f"{mapping['projected_chain']}:{mapping['projected_site']} → AF3 "
        f"{mapping['af3_protein_chain']}:{mapping['af3_sequence_site']}。",
        f"计划候选 {review['candidate_count']} 个；已测量 {review['measured_candidate_count']} 个；"
        f"引擎或产物解析失败 {review['failed_candidate_count']} 个。", '',
        '**本报告核验产物来源并重算置信度及有限几何，不授予完整物理验收、药效验证或 MD 准入。**', '',
        'geometry.source_residue 使用投影构建体编号，不是 canonical 编号。', '',
        'mPAE（Å）是当前定义的配体→蛋白链对预测对齐误差指标；ipTM 与 pLDDT 是模型置信度。'
        '它们不代表结合自由能、亲和力或共价反应速率，本报告不设置新的通过阈值。', '',
        '共价连接本来就是预测输入的约束。模型中存在该连接，不证明反应能够发生。'
        '重原子最短距离只覆盖所指定的蛋白和配体，并仅排除声明的反应原子对，不能直接视为完整碰撞检验。', '',
        '| 候选 | 产物状态 | mPAE / Å | ipTM | 配体 pLDDT | SG–反应碳 / Å | 声明连接存在 |',
        '|---|---|---|---|---|---|---|']
    for row in review['candidates']:
        confidence, geometry = row['confidence'] or {}, row['geometry'] or {}
        values = [row['compound_id'], row['engine_status'], confidence.get('mpae'), confidence.get('iptm'),
            confidence.get('ligand_plddt_mean'), geometry.get('reactive_distance_angstrom'),
            geometry.get('model_has_exact_covalent_link')]
        lines.append('| ' + ' | '.join('未测得' if value is None else str(value).replace('|', '\\|').replace('\n', ' ')
                                       for value in values) + ' |')
    lines += ['', '候选失败或未测得不等于分子无效。全部候选、失败原因、来源校验值和详细测量保存在同目录 report.json。', '']
    for row in review['candidates']:
        if row['failure_reason']:
            lines.append('- ' + row['compound_id'] + '：' + row['failure_reason'].replace('\n', ' '))
    lines += ['', '完整蛋白身份、完整成键拓扑和立体化学、反应动力学、选择性、合成可行性和实验活性尚未由此核验。'
              'CovDock 受体准备与共享 MD 交接仍需后续完成。', '']
    return '\n'.join(lines)


def freeze(root, state):
    """Caller must freshly revalidate signed inputs and registered AF3 outputs."""
    record = state['af3_execution']
    if record.get('evaluation_verification') != 'verified':
        raise ValueError('AF3 report requires freshly revalidated measurements')
    payload = {'schema': 'ligand-af3-report-v1', 'workflow_id': state['workflow_id'],
        'design_id': state['design_id'], 'evidence_id': state['evidence_id'], 'target': state['target'],
        'execution': {'state': record['job_state'], 'return_code': record['return_code']},
        'evaluation': record['evaluation'], 'compute_submitted_by_report': False}
    from .metric_contracts import md_not_evaluated
    payload["md_admission"] = md_not_evaluated()
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
    markdown = render(payload)
    identifier = 'ligand-af3-report-' + hashlib.sha256(encoded.encode()).hexdigest()[:24]
    parent = root / 'results/ligand-af3-reports'
    destination = parent / identifier
    for path in (root / 'results', parent, destination):
        if path.is_symlink():
            raise ValueError('AF3 report cannot traverse symbolic links')
    parent.mkdir(parents=True, exist_ok=True)
    previous = record.get('report')
    if previous and previous['artifact_id'] == identifier:
        regular(root, destination / 'manifest.json', previous['manifest_sha256'])
    if not destination.exists():
        staging = parent / ('.' + identifier + '-' + uuid4().hex[:12])
        staging.mkdir()
        (staging / 'report.json').write_text(encoded + '\n')
        (staging / 'report.md').write_text(markdown)
        seal(staging)
        staging.rename(destination)
    for name in ('manifest.json', 'report.json', 'report.md'):
        regular(root, destination / name)
    verify(destination)
    if (json.loads((destination / 'report.json').read_text()) != payload
            or (destination / 'report.md').read_text() != markdown):
        raise ValueError('Frozen AF3 report differs from current evidence')
    return {'artifact_id': identifier, 'manifest_sha256': sha256_file(destination / 'manifest.json'),
        'report_json': str((destination / 'report.json').relative_to(root)),
        'report_markdown': str((destination / 'report.md').relative_to(root)),
        'candidate_count': record['evaluation']['candidate_count'],
        'physical_acceptance': 'not_established', 'validated_inhibitor': False,
        'md_ready': False, 'compute_submitted_by_report': False}
