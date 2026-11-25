"""Immutable CovDock measurement report; no activity or MD admission claim."""
import hashlib
import base64
import json
from uuid import uuid4

from .jobs import sha256_file
from .ligand_design import seal, verify
from .receptor_evidence import regular
from .metric_contracts import result_state_semantics


def export_report(root, state):
    """Deliver only the current workflow's sealed, independently reviewed LO report."""
    from .ligand_covdock_evidence import witness
    from .ligand_parent_delivery import LIMIT
    record = state.get('covdock_execution') or {}
    reference = record.get('report')
    if not reference or record.get('evaluation_verification') != 'verified' or record.get('evidence_error'):
        raise ValueError('Create and verify the current CovDock report before exporting it')
    witness(root, record['evaluation'], record['evaluation_artifact'])
    if freeze(root, state) != reference:
        raise ValueError('CovDock delivery differs from the current frozen report')
    directory = (root / reference['report_json']).parent
    manifest_path = directory / 'manifest.json'
    regular(root, manifest_path, reference['manifest_sha256'])
    manifest = json.loads(manifest_path.read_bytes())
    files = []
    for key, name, mime in [('report_json', 'report.json', 'application/json'),
                            ('report_markdown', 'report.md', 'text/markdown')]:
        path = root / reference[key]
        with path.open('rb') as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('CovDock report exceeds bounded delivery limit')
        if hashlib.sha256(raw).hexdigest() != manifest['files'].get(name):
            raise ValueError('CovDock report changed during delivery')
        files.append({'name': name, 'mime_type': mime, 'sha256': hashlib.sha256(raw).hexdigest(),
                      'bytes': len(raw), 'base64': base64.b64encode(raw).decode('ascii')})
    return {'schema': 'ligand-covdock-delivery-v1', 'workflow_id': state['workflow_id'],
            'evidence_id': state['evidence_id'], 'design_id': state['design_id'],
            'report_id': reference['artifact_id'], 'delivery_manifest_sha256': reference['manifest_sha256'],
            'files': files, 'hash_scope': 'sealed current-workflow CovDock report bytes', 'compute_submitted': False}


def render(payload):
    review = payload['evaluation']
    policy = review['gate_policy']
    optional_consensus = policy.get('cross_method_consensus_required') is False
    lines = ['# Ligand-first CovDock 结果核验报告', '',
        f"工作流：{payload['workflow_id']}；任务：{review['run_id']}。",
        f"批次任务状态：{payload['execution']['state']}；批次退出码：{payload['execution']['return_code']}（不是单个候选的退出码）。",
        f"研究位点：{review['target']['target']} canonical {review['target']['site']}；受体作者编号：{review['attachment_residue']}。",
        f"计划候选 {review['candidate_count']} 个；有结构测量 {review['measured_candidate_count']} 个；引擎/产物失败 {review['failed_candidate_count']} 个。", '',
        ('本报告重新读取成功候选的登记结构并重算冻结规则。' if review['measured_candidate_count']
         else '本批没有成功结构产物；本报告核验候选分母、失败记录和冻结规则。'),
        '**本报告不证明完整化学有效性、亲和力、共价反应速率、实验活性或 MD 准入。**', '',
        '候选 SUCCESS 表示生产者记录了可解析的成功产物，本身不证明数值收敛。physical_validity=PASS 只覆盖列出的暂定检查。',
        'validated_inhibitor=false 表示尚未验证，不是已证明没有抑制活性；补齐一个门槛也不等于获得药效或 MD 准入。',
        f"规则版本：{review['gate_algorithm_version']}（暂定、未经独立校准）。",
        (f"原冻结规则：{review.get('original_gate_algorithm_version')}；原始产物与原分组保留，本报告以新策略独立再评估，未重跑对接。"
         if review.get('policy_reassessment') else '本报告按明确登记的规则核验。'),
        f"共价键长范围：{policy['covalent_bond_length_range']} Å；局部应变提示/拒绝值："
        f"{policy['max_local_strain']} / {policy['hard_max_strain']} kcal/mol。",
        '局部应变使用厂商 r_strain_Local_Strain_Energy；不是相对母体的应变增量，也不是结合自由能。',
        '原始对接分数仅为姿势证据，不进行跨引擎混排。没有本次获准的 AF3/CovDock 姿势比较时，一致性为 NA，不补成高一致性。', '',
        '| 候选 | 引擎状态 | 当前分组/细分 | 已测结构检查 | 共价键长 / Å | 局部应变 / kcal/mol | 姿势一致性 | 满足本次入选条件 |',
        '|---|---|---|---|---|---|---|---|']
    for row in review['candidates']:
        metrics, gate = row['measurements'] or {}, row['gate']
        values = [row['compound_id'], row['engine_status'], str(gate['covdock_gate']) + '/' + str(gate['covdock_gate_detail']), gate['physical_validity'],
                  metrics.get('covalent_bond_length_angstrom'), metrics.get('strain_kcal_mol'),
                  gate['cross_method_consensus'], gate['review_eligible']]
        lines.append('| ' + ' | '.join('未测得' if value is None else str(value).replace('|', '\\|').replace('\n', ' ')
                                       for value in values) + ' |')
    lines += ['', '引擎失败、结构测量缺失、已测结构不符合暂定规则分别保留。原始 C 分组也可能来自引擎失败，应结合独立状态轴解释，不能一律解释为结构实测失败。',
        'review_eligible=false 表示未满足本次冻结策略的候选入选条件，仍可审阅现有证据；不等于禁止复核或分子无效。',
        '本报告未执行 MD 准入评估；md_ready=false 表示未授予就绪状态，不是已经检查并拒绝，不能据此补写拒绝原因。',
        ('本次入选要求执行成功和完整物理证据，允许 physical_validity 为 PASS 或 WARN；AF3 比较为可选补充证据，NA 不单独阻断入选，不要求一律达到 A 组。'
         if optional_consensus else '本次入选条件允许 physical_validity 为 PASS 或 WARN、已评估的姿势一致性为 HIGH 或 LOW，并要求执行成功；不要求一律达到 A 组。'),
        '候选入选、MD 准入和实验活性是不同判断；不能将 A 组、MD、活性验证写成通用的必经阶梯。缺失评分保留为未测得，不改写为数值零。', '']
    identity = review.get('prepared_residue_identity') or {}
    changes = identity.get('histidine_microstate_changes', [])
    if changes:
        lines += ['受体准备保留了观察到的残基身份；以下组氨酸质子化状态名称发生变化，'
                  '这些变化不视为氨基酸替换，也不代表其生物学适宜性已获验证：']
        for change in changes:
            lines.append(f"- {change['chain']}:{change['residue_number']}{change['insertion_code']}："
                         f"{change['input_name']} → {change['prepared_name']}")
        lines.append('')
    for row in review['candidates']:
        reason = row['failure_reason'] or row['gate']['covdock_gate_reason']
        lines.append('- ' + row['compound_id'] + '：' + str(reason).replace('\n', ' '))
        if review.get('policy_reassessment'):
            original = row['original_gate']
            lines.append('  原冻结分组：' + str(original['covdock_gate_detail']) + '；原入选条件：' + str(original['review_eligible']) + '（保留，不覆盖）。')
    lines += ['', '完整分母、测量、冻结规则、来源哈希和环境评估引用保存在 report.json。',
        ('厂商准备后受体已重新读取，但这不替代完整体系的生物学适用性验证。'
         if review['effective_receptor_check'] else '本批没有成功结构，未进行厂商准备后受体的独立读回核验。'), '']
    return '\n'.join(lines)


def freeze(root, state):
    record = state['covdock_execution']
    if record.get('evaluation_verification') != 'verified':
        raise ValueError('CovDock report requires freshly revalidated measurements')
    payload = {'schema': 'ligand-covdock-report-v1', 'workflow_id': state['workflow_id'],
        'design_id': state['design_id'], 'evidence_id': state['evidence_id'],
        'execution': {'scope': 'batch', 'state': record['job_state'], 'return_code': record['return_code']},
        'result_state_semantics': result_state_semantics(),
        'evaluation': record['evaluation'], 'compute_submitted_by_report': False}
    from .metric_contracts import md_not_evaluated
    payload["md_admission"] = md_not_evaluated()
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
    identifier = 'ligand-covdock-report-' + hashlib.sha256(encoded.encode()).hexdigest()[:24]
    parent = root / 'results/ligand-covdock-reports'
    destination = parent / identifier
    for path in (root / 'results', parent, destination):
        if path.is_symlink():
            raise ValueError('CovDock report cannot traverse symbolic links')
    parent.mkdir(parents=True, exist_ok=True)
    previous = record.get('report')
    if previous and previous['artifact_id'] == identifier:
        regular(root, destination / 'manifest.json', previous['manifest_sha256'])
    markdown = render(payload)
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
    if json.loads((destination / 'report.json').read_text()) != payload or (destination / 'report.md').read_text() != markdown:
        raise ValueError('Frozen CovDock report differs from current evidence')
    return {'artifact_id': identifier, 'manifest_sha256': sha256_file(destination / 'manifest.json'),
        'report_json': str((destination / 'report.json').relative_to(root)),
        'report_markdown': str((destination / 'report.md').relative_to(root)),
        'candidate_count': record['evaluation']['candidate_count'],
        'physical_acceptance': 'not_established', 'validated_inhibitor': False, 'md_ready': False,
        'compute_submitted_by_report': False}
