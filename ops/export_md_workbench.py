"""Publish plots from the registered, audited OPLS replica batch; never run MD."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import zipfile

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from summarize_md_replicas_20261003 import summarize, RULES

SYSTEMS = ['denovo_001', 'denovo_004', 'denovo_007', 'control_afatinib']
COLORS = ['#0072B2', '#C34D00', '#008563']
STYLES = ['-', '--', ':']


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path):
    with path.open() as file:
        return list(csv.DictReader(file))


def event(phase):
    print(json.dumps({'phase': phase}), flush=True)


def export(root, out):
    event('validation')
    summary = summarize(root, RULES)
    if summary['status'] != 'REPEATED_MD_SAMPLING_ASSESSED':
        raise ValueError('Waiting for all registered real replicas')
    if set(summary['systems']) != set(SYSTEMS):
        raise ValueError('Unexpected system identity')
    event('analysis')
    data = {}
    for record in summary['replicas']:
        folder = root / 'runs' / record['run_id'] / 'analysis-v1'
        trace = rows(folder / 'trajectory-measurements.csv')
        values = {k: np.array([float(r[k]) for r in trace]) for k in trace[0]}
        values['contacts'] = 100 * np.array([float(r['initial_residue_contact_retention_no_C797']) for r in rows(folder / 'contact-frames.csv')])
        values['residues'] = {int(r['residue']): float(r['occupancy_4.5A']) * 100 for r in rows(folder / 'residue-contacts.csv')}
        data[(record['candidate'], record['replica'])] = values
    out.mkdir(parents=True, exist_ok=False)
    (out / 'sampling-summary.json').write_text(json.dumps(summary, indent=2))
    charts = []

    def save(fig, system, kind, title, caption):
        name = f'{system}-{kind}'
        for extension in ['png', 'svg']:
            fig.savefig(out / f'{name}.{extension}', dpi=160, facecolor='white')
        plt.close(fig)
        charts.append({'system': system, 'kind': kind, 'title': title, 'caption': caption,
                       'file': name + '.png', 'sha256': sha(out / (name + '.png'))})

    def traces(ax, system, key, ylabel, limits):
        for replica in [1, 2, 3]:
            d = data[(system, replica)]
            ax.plot(d['production_time_ps'] / 1000, d[key], color=COLORS[replica - 1],
                    linestyle=STYLES[replica - 1], lw=.85, label=f'Replica {replica}')
        ax.set(xlabel='Production time (ns)', ylabel=ylabel, xlim=(0, 10), ylim=limits)
        ax.legend(loc='upper left', ncol=3, fontsize=8)

    event('rendering')
    with plt.rc_context({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False,
                         'axes.grid': True, 'grid.alpha': .14, 'svg.fonttype': 'none'}):
        ringmax = max(d['largest_ring_system_RMSD_A'].max() for d in data.values()) * 1.05
        for system in SYSTEMS:
            fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), layout='constrained')
            traces(axes[0], system, 'largest_ring_system_RMSD_A', 'Ring RMSD (Å)', (0, ringmax))
            traces(axes[1], 'control_afatinib', 'largest_ring_system_RMSD_A', 'Ring RMSD (Å)', (0, ringmax))
            axes[0].set_title(system + ' · largest ring system', loc='left', fontweight='bold')
            axes[1].set_title('Afatinib reference control · same axis limits', loc='left')
            save(fig, system, 'rmsd', 'RMSD 轨迹与对照', '上：当前体系；下：阿法替尼。全部 1,000 帧 / 重复，未经平滑。按各自口袋 Cα 对齐，各配体参照其自身起始姿势；跨分子差异不表示亲和力。')

            fig, axes = plt.subplots(2, 1, figsize=(10, 6.5), layout='constrained')
            traces(axes[0], system, 'contacts', 'Input contacts retained (%)', (0, 103))
            axes[0].legend(loc='lower left', ncol=3, fontsize=8)
            axes[0].set_title(system + ' · contact retention', loc='left', fontweight='bold')
            residues = sorted(set().union(*(d['residues'] for d in data.values())))
            # Only recorded residues are shown; missing entries are masked, never zero-filled.
            values = np.array([[data[(system, r)]['residues'].get(res, np.nan) for res in residues] for r in [1, 2, 3]])
            visible = np.any(np.isfinite(values), axis=0)
            values = values[:, visible]
            residues = np.array(residues)[visible]
            palette = plt.get_cmap('cividis').copy()
            palette.set_bad('#e5e7eb')
            heat = axes[1].imshow(values, aspect='auto', cmap=palette, vmin=0, vmax=100, interpolation='nearest')
            axes[1].grid(False)
            ticks = np.arange(len(residues))
            axes[1].set(xticks=ticks, xticklabels=residues[ticks], yticks=[0, 1, 2],
                        yticklabels=['Replica 1', 'Replica 2', 'Replica 3'], xlabel='Protein residue number',
                        title='Residue-contact occupancy · 4.5 Å cutoff')
            axes[1].tick_params(axis='x', labelrotation=90, labelsize=8)
            fig.colorbar(heat, ax=axes[1], label='Occupancy (%)', shrink=.85)
            save(fig, system, 'contacts', '接触保持与残基热图', '上：初始残基接触保持率，排除 C797 和跨共价键邻近原子对。下：已记录残基的接触占有率，距离阈值 4.5 Å；灰色代表未记录。')

            pairs = summary['systems'][system]['HB_replica_estimates']
            fig, ax = plt.subplots(figsize=(10, max(3.7, len(pairs) * .8)), layout='constrained')
            for i, (pair, estimates) in enumerate(pairs.items()):
                for replica, value in enumerate(estimates['occupancy_3_5A_150deg']['replica_estimates']):
                    ax.scatter(value * 100, i + (replica - 1) * .18, color=COLORS[replica],
                               marker=['o', 's', '^'][replica], label=f'Replica {replica + 1}' if i == 0 else None)
            ax.set(yticks=np.arange(len(pairs)), yticklabels=list(pairs), xlim=(-2, 102),
                   xlabel='Hydrogen-bond occupancy (%)', title=system + ' · preselected hydrogen bonds')
            ax.legend(loc='lower right')
            save(fig, system, 'hbonds', '氢键占有率', '每个点是一条独立轨迹的占有率。供体–受体 ≤ 3.5 Å、D–H···A ≥ 150°。仅追踪预定义原子对，不包含完整新生氢键或水桥搜索；未列出不等于不存在。')

            fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), layout='constrained')
            for ax, metric, label, scale in zip(axes, ['largest_ring_system_RMSD_A', 'initial_residue_contact_retention_no_C797'], ['Ring RMSD (Å)', 'Input contacts retained (%)'], [1, 100]):
                for replica in [1, 2, 3]:
                    record = next(r for r in summary['replicas'] if r['candidate'] == system and r['replica'] == replica)
                    y = np.array(record['diagnostics'][metric]['two_ns_block_means']) * scale
                    ax.plot([1, 3, 5, 7, 9], y, color=COLORS[replica - 1], linestyle=STYLES[replica - 1], marker=['o', 's', '^'][replica - 1], label=f'Replica {replica}')
                ax.set(xlabel='2 ns block midpoint (ns)', ylabel=label, xticks=[1, 3, 5, 7, 9], ylim=(0, ringmax if scale == 1 else 103))
                ax.legend(fontsize=8)
            fig.suptitle(system + ' · block diagnostics, not an equilibrium test', fontweight='bold')
            save(fig, system, 'sampling', '采样与分块诊断', '每条轨迹分成五个连续 2 ns 区段，展示区段均值；区段并非独立重复。漂移和重复间差异用于提示追加采样，不能据此证明收敛。')

            fig, axes = plt.subplots(3, 1, figsize=(10, 8), layout='constrained')
            for ax, key, label in zip(axes, ['temperature_K', 'density_g_ml', 'covalent_distance_A'], ['Temperature (K)', 'Density (g/mL)', 'Covalent distance (Å)']):
                allvalues = np.concatenate([d[key] for d in data.values()])
                pad = (allvalues.max() - allvalues.min()) * .05
                traces(ax, system, key, label, (allvalues.min() - pad, allvalues.max() + pad))
            axes[0].set_title(system + ' · physical diagnostics', loc='left', fontweight='bold')
            save(fig, system, 'physical', '温度、密度与共价键', '全部逐帧记录，同一指标在不同体系使用相同坐标范围。已建拓扑中的共价键距离用于数值质检，不证明化学反应能力。')

    event('report')
    report = '# MD 自动分析报告\n\nOpenMM · OPLS_2005 / TIP3P；四体系各三次独立重复，每次 10 ns。\n\n'
    report += '输入：已验收的完整 DCD 关联指标；SHA-256 校验、初速度独立性、时间轴和帧数校验均通过。此步骤不重新运行 MD。\n\n'
    report += '## 采样评估\n\n'
    for system in SYSTEMS:
        s = summary['systems'][system]
        report += f'- {system}: {s["sampling_assessment"]}；{len(s["additional_sampling_flags"])} 项追加采样提示。\n'
    report += '\n## 缺失项与限制\n\n- RMSF：本批压缩结果未导出，需在原始坐标上按指定对齐和原子选择重新分析。\n- 氢键为预定义原子对，不是全量氢键、水桥检测。\n- 新 Amber 轨迹尚未接入此批分析。\n- 图表自动生成不等于专家审阅完成；不作亲和力、活性或已收敛声明。\n'
    (out / 'report.md').write_text(report)
    (out / 'provenance.json').write_text(json.dumps({
        'scripts': {name: sha(Path(__file__).with_name(name)) for name in
                    ['export_md_workbench.py', 'summarize_md_replicas_20261003.py', 'replica_statistics_20261003.py', 'audit_openmm_pilot_20261002.py']},
        'matplotlib': matplotlib.__version__, 'numpy': np.__version__,
        'frame_subsampling': False, 'smoothing': False, 'production_ns_per_replica': 10,
        'block_width_ns': 2, 'missing_contact_values': 'masked_not_zero',
        'source_manifests': {r['run_id']: r['metrics_manifest_sha256'] for r in summary['replicas']}
    }, indent=2))
    with zipfile.ZipFile(out / 'analysis-bundle.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(out.iterdir()):
            if file.suffix != '.zip':
                archive.write(file, file.name)
        for file in sorted(root.rglob('*')):
            if file.is_file() and file.suffix in ['.csv', '.json']:
                archive.write(file, 'source/' + str(file.relative_to(root)))
        for name in ['export_md_workbench.py', 'summarize_md_replicas_20261003.py', 'replica_statistics_20261003.py', 'audit_openmm_pilot_20261002.py']:
            archive.write(Path(__file__).with_name(name), 'code/' + name)
    manifest = {'schema': 'md-analysis-package-v1', 'systems': SYSTEMS, 'charts': charts,
                'replicas': 12, 'frames': 12000, 'rmsf': 'missing', 'review': 'automatic-report-needs-review',
                'matplotlib': matplotlib.__version__, 'numpy': np.__version__,
                'files': {f.name: sha(f) for f in out.iterdir() if f.is_file()},
                'sourceManifests': {r['run_id']: r['metrics_manifest_sha256'] for r in summary['replicas']}}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    event('ready')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    export(args.root, args.output)
