# De novo 路线维护入口

> 发布说明：本文历史部署路径已匿名化；`/path/to/` 与 `<account>` 是占位符，实际路径由部署方在源码外配置。历史验收不代表当前部署状态。

## 首版主线（2026-11-19 用户确认）

首版生成器仅使用已跑通的 **PocketXMol**。CovaGEN 从首版接入与验收范围移除，不作为备选或失败回退；历史调研与实验记录保留。其他生成器也不属于首版主流程。

**固定靶点与丙烯酰胺 → PocketXMol 生成 → 化学检查与去重 → 多样性筛选 → 独立受体／配体准备 → CovDock-VS → 少量候选 CovDock-LO → 结构与反应前姿态评价 → 候选排序。**

- 当前试点沿用 EGFR Cys797；每批冻结靶点结构、位点、丙烯酰胺原子映射、生成种子和预算。
- 化学检查保留不合格与重复记录；多样性筛选保留可追溯的候选身份。逐阶段分别统计唯一分子数、准备后的状态数和姿态数，不混用分母。
- 生成坐标用于记录生成器表现；下游对接使用独立准备的受体和配体，不把生成成功视为对接或结构验收通过。
- VS 用于批量预筛；入选的少量候选独立运行 LO。数量与预算在运行前确定，不承诺固定数量全部通过。
- LO 共价加合物结构评价与反应前姿态评价分别记录。共价键长不能替代反应前距离与攻击几何证据；最终排序呈现两类证据、失败原因及评价规则。
- 首版交付候选排序和结构展示，不包括 MD 或活性验证。此处固定的是产品路线，不表示平台接入、部署或网页端到端验收已经完成。

下文带日期的迁移、模型选型与验收记录保留历史语义；与首版范围冲突时，以本节为准。

当前接口、规划结果语义及执行缺口见 [接口合同](CONTRACT.md)。

专属代码已经物理迁入本目录。通用引擎、审批、任务账本和结果清单由公共包维护；不可变发布包同时包含三条路线与公共实现。

| 目录 | 维护内容 |
| --- | --- |
| `src/covscreen_de_novo/` | De novo 路线规划、阶段与门槛 |
| `scripts/` | PocketXMol、REINVENT4 正式适配器 |
| `registry/egfr_c797_v1/` | 固定 EGFR C797 丙烯酰胺方案、配置、小型输入、生成与结构验收代码 |
| `runtime/` | 原生任务控制、校准、结构展示与受控来源包运输 |
| `evaluation/` | 独立输入、原始对照重建、来源核验；不冒充已完成科学评价 |
| `tests/` | 路线模型及身份、来源、归档边界测试 |
| `historical-scripts/`、`historical-ops/` | 历史审计、CovDock 复核与 MD 实验；禁止批量执行 |

DiffLinker 同时供 Ligand-first 使用，正式代码保持在 `science/scripts/run_difflinker_registered.py`，这里仅提供导航链接。`generator_adapter_common.py`、任务注册、候选读取、审批和版本校验同样只有公共维护来源。平台 TypeScript 的消息入口、审批 UI 与 Results 适配保留在 `openscience-custom/`。

新任务原生代码选择由 `ops/denovo-release.json` 指定，逐文件校验本地安装版本；共享 MCP 科学代码由 `ops/ligand-release.json` 指定。已经冻结的任务继续读取自己的源码、输入和结果。新远端根：

`/inspire/hdd/project/chemicalreaction/<account>/covalent-agent-platform/routes/de-novo/`

`releases/<ID>/` 保存不可变代码，`runs/` 保存新任务和独立验收，`registry/` 保存发布、方案及迁移证据。旧全局个人盘任务、模型、环境与权重原位保留。

本次使用 REINVENT4 的 4 分子 CPU 真实生成功能验证身份、任务登记和版本绑定。它不验证 EGFR 定向生成、固定 warhead 或 GPU 工作负载；PocketXMol 和 DiffLinker 的资产检查不等于本次重新运行成功。完整证据及回滚见 [迁移报告](../../reports/denovo-layout-migration-20261012/README.md)。

2026-10-29：单候选独立反应前与共价评价适配已联合发布，新增研究正在整理可核验的证据与生成计划；尚无本研究的真实候选评价或 MD 结果。正式 MD 交接仍未接通。活动版本、原生研究身份和验收缺口见 [闭环状态](../../reports/denovo-closed-loop-20261029/STATUS.md)。历史对照与候选化学设计见 [对照验收](../../reports/denovo-readiness-20261028/CONTROL_REVIEW.md) 与 [候选化学检查](../../reports/denovo-readiness-20261028/CHEMICAL_REVIEW.md)。

2026-11-16：按用户要求将 CovaGEN 纳入独立生成后端接入计划。模型定位、官方来源、与 PocketXMol/REINVENT4 的分工和未完成验收项见[模型选型与接入方案](docs/generator-selection-20261116.md)。当前仅完成调研与方案登记，未启用 CovaGEN。
