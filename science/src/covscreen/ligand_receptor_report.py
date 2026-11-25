"""Immutable receptor review report; structural quality is scoped explicitly."""
import hashlib
import json
from uuid import uuid4

from .ligand_receptor_source import context
from .jobs import sha256_file
from .ligand_design import seal, verify
from .receptor_evidence import regular


def freeze(root, state):
    record = context(state)['receptor_preparation']
    if record.get('evaluation_verification') != 'verified':
        raise ValueError('Receptor report requires freshly replayed evidence')
    review = record['evaluation']
    payload = {'schema': 'ligand-receptor-report-v1', 'workflow_id': state['workflow_id'],
        'design_id': state['design_id'], 'target': state['target'], 'evaluation': review,
        'compute_submitted_by_report': False}
    restrained = review.get('schema') == 'ligand-receptor-review-v2'
    if restrained:
        payload['schema'] = 'ligand-receptor-report-v2'
    from .metric_contracts import md_not_evaluated
    payload["md_admission"] = md_not_evaluated()
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
    identifier = 'ligand-receptor-report-' + hashlib.sha256(encoded.encode()).hexdigest()[:24]
    parent = root / 'results/ligand-receptor-reports'
    destination = parent / identifier
    for path in (root / 'results', parent, destination):
        if path.is_symlink():
            raise ValueError('Receptor report cannot traverse symbolic links')
    result = review['result']
    if restrained:
        admitted = review.get('receptor_prepared') is True
        if admitted and not review.get('motion_review'):
            raise ValueError('Restrained admission report requires its bound motion review')
        motion = result['quality']['observed_coordinate_displacements']
        markdown = '\n'.join(['# Ligand-first 位置约束受体构象审阅报告', '',
            f"工作流：{state['workflow_id']}；准备任务：{review['run_id']}。", '',
            ('**已依据绑定审阅生成探索性受体产物；不代表生物学适用或 MD 准入。**' if admitted
             else '**位移与结合位点保留情况待审阅；受体尚未准入。**'), '',
            f"身份/立体化学：{result['quality']['status']}。",
            f"几何：{result['geometry']['status']}。", '',
            '| 观测重原子位移测量 | 数值 | 单位 |', '|---|---:|---|',
            f"| 原子数 | {motion['atom_count']} | 个 |",
            f"| RMS 位移 | {motion['rms_displacement_angstrom']:.6f} | Å |",
            f"| 最大位移 | {motion['maximum_displacement_angstrom']:.6f} | Å |", '',
            '这些位移使用原始原子身份逐一对应计算；不代表位移可接受或结合位点已保留。',
            '这是含位置约束的计算构象，不是新的实验观测结构。',
            '环境适用性、CovDock 交接与 MD 准入尚未通过；后续计算仍需独立计划与审批。', '',
            '完整逐原子位移、身份和几何审计及来源校验值保存在 report.json。', ''])
    else:
        measured = result['context']['prepared_isolated_protein']
        policy = result['context']['policy']
        markdown = '\n'.join(['# Ligand-first 受体准备核验报告', '',
            f"工作流：{state['workflow_id']}；准备任务：{review['run_id']}。", '',
            '登记产物、身份/立体化学、几何及局部环境测量已重读核验。',
            '**通过范围仅为隔离的指定蛋白链，不等于生物学环境适用或允许后续计算。**', '',
            f"身份/立体化学：{result['quality']['status']}。",
            f"几何：{result['geometry']['status']}。",
            f"位点身份：{json.dumps(result['identity'], ensure_ascii=False, sort_keys=True)}。", '',
            '| 描述性测量 | 数值 | 单位 |', '|---|---:|---|',
            f"| 目标 SG 溶剂可及表面积 | {measured['sg_sasa_angstrom_squared']:.3f} | Å² |",
            f"| 目标 Cys 残基溶剂可及表面积 | {measured['cysteine_residue_sasa_angstrom_squared']:.3f} | Å² |", '',
            f"SASA 使用 {policy['sasa_algorithm']}，探针半径 {policy['probe_radius_angstrom']} Å，"
            f"每原子 {policy['surface_points_per_atom']} 个采样点；这里不设反应性或准入阈值。", '',
            '组装体、移除组分/水/辅因子的相关性、位点可达性和反应适用性仍需审查。',
            '局部环境测量是描述性证据，不自动证明半胱氨酸反应活性、结合亲和力或药效。',
            'CovDock 交接和 MD 准入尚未由本报告授予，后续计算需要独立计划与审批。', '',
            '完整质量报告、环境测量、输入/输出校验值和实现版本保存在 report.json。', ''])
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
        raise ValueError('Frozen receptor report differs from current evidence')
    return {'artifact_id': identifier, 'manifest_sha256': sha256_file(destination / 'manifest.json'),
        'report_json': str((destination / 'report.json').relative_to(root)),
        'report_markdown': str((destination / 'report.md').relative_to(root)),
        'prepared_scope': review['prepared_scope'], 'environment_suitability': 'not_established',
        'covdock_ready': False, 'md_ready': False, 'compute_submitted_by_report': False}
