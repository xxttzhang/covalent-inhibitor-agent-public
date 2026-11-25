# Ligand-first 维护入口

## 2026-10-27 平衡阶段接入候选

新增 `plan_md_equilibration` / `start_md_equilibration` / `md_equilibration_status`，以及未提交计划撤回和失败任务新计划动作。沿用原研究的已核验 QC 坐标与速度、准备体系和参数化父链，平衡计划单独批准后执行。复用共享 NPT 引擎与回执核验器，记录初始化、积分与写出进度；进度本身不授予科学认可。

冻结协议为 500 ps NPT、2 fs 步长、300 K、1.01325 bar，每 10 ps 记录一次；最后 10 帧按现有温度、密度、体积、约束与共价几何门槛复核。`equilibration_protocol_completed` 与 `production_review_ready` 分开表达；完成协议不等于达到平衡收敛，不自动启动生产 MD。CPU affinity 和 `OPENMM_CPU_THREADS` 在导入原生计算库前设置。

部署、具体计划、用户批准及真实验收以 [本轮交付记录](../../reports/ligand-equilibration-20261027/STATUS.md) 为准。代码或软件测试通过不能计为 066 平衡通过。

## 2026-10-27 体系准备与短程 QC 已接入正式平台

已部署 `plan_md_system_preparation` / `start_md_system_preparation` / `md_system_preparation_status` 和对应 short_qc 动作，每阶段独立冻结计划及审批；只允许撤回未提交计划。复用共享 CPU 执行器、产物审计与原任务恢复，不新增任务账本。短程 QC 通过仅允许进入平衡规划复核，不等于平衡、生产 MD 或药效通过。

候选 066 的新图检查、参数化、体系准备与 1 ps 短程 QC 均已真实通过。体系准备 14 项产物核验通过；QC 的 4 项产物独立读回通过，1000 步全部完成，共价 S–C 距离保持 1.746–1.848 Å。重启后同一研究四阶段保持 `verified`，37 个会话及任务列表不变，无重复提交。见 [体系准备真实验收](../../reports/ligand-md-preparation-dependencies-20261027/STATUS.md) 和 [短程 QC 真实验收](../../reports/ligand-native-short-qc-20261027/STATUS.md)。

本次准备运行前段曾出现额外 CPU 占用，已实时限核并保留记录，不能声称全程严格遵守两核上限。启动前 CPU affinity 修复已部署，后续准备/QC 从启动即施加限制；验证与切换见 [CPU 预算约束](../../reports/ligand-md-cpu-budget-20261027/STATUS.md)。

失败后的新尝试必须另行批准，保留原候选、父任务和失败历史，不自动重跑。本段 QC 验收时平衡尚未接入；后续平衡进展见顶部记录，生产 MD 和分析仍待接通。体系准备通过不代表 MD 系统已就绪。

当前原验收会话可查询新结果；路线工作台仍展示 2026-10-14 历史快照，尚未自动投影本轮研究结果。下一阶段为 `equilibration_review`，未开始平衡或生产 MD。

## 历史能力与发布状态（2026-10-26，后续进展以上文为准）

下文各阶段说明保留其验收边界。当前已发布研究工作流已覆盖母体证据、候选生成/姿势、AF3、受体准备、CovDock 与结果读回，并已增加：

- `prepare_md_handoff`：从当前研究产物冻结共享候选交接。
- `select_md_candidate` / `release_md_candidate`：固定候选与理由，保留选择历史。
- `check_md_graph_runtime`：冻结同一候选的原生图检查计划。
- `start_md_graph` / `md_graph_status`：独立原生审批、单任务登记、原任务状态/结果核验；`reconcile` 恢复丢失回执，不自动重交。待定或已登记任务不能改选绕开原任务。
- `plan_md_parameterization` / `start_md_parameterization` / `md_parameterization_status`：绑定当前已核验父图、独立审批、原任务恢复和参数产物核验。
- `release_md_parameterization_plan`：只允许撤回未提交的参数化计划，并保留归档记录。
- `attach_md_source`：把已冻结的历史候选来源链关联到空闲新研究，重新核验原始对接、补环、清理的文件对应关系；不继承历史审批或科学完成状态。
- `prepare_historical_md_candidate`：绑定历史来源与经核验的标准序列，选择同一候选并冻结历史输入；之后使用既有图检查审批和执行动作，原生检查实际构建体、清理链和共价图。

以上已进入正式科研后端及平台工具目录。工具接入有软件验证，候选 066 的图检查与参数化另有本轮真实计算验收，详见下文。图检查完成只验证相应共价图证据，需独立执行参数化；参数化通过也不代表 MD 体系就绪。当前 Ligand 阶段菜单尚未接入体系准备、短程检查、平衡、正式 MD 和分析/恢复。共享底层实现已有基础，应复用而非另建引擎。

正式共享后端版本为 `0f6395786b6e05c0a5dcc6a6518856bf801d4c2a061b4eff74a96902692498f6`，已核验远端 schema、平台工具描述及重载后的候选 066 图检查计划读回。[基础发布说明](../../reports/platform-release-20261026/RELEASE_NOTES.md)记录前一版本范围；[参数化接入记录](../../reports/ligand-parameterization-20261026/STATUS.md)、[真实来源接入记录](../../reports/ligand-historical-source-20261026/STATUS.md)与[图检查接入记录](../../reports/ligand-historical-graph-20261026/STATUS.md)记录后续更新。候选 066 已通过真实原生图检查，见[补环连接处修复与验收](../../reports/ligand-graph-junction-20261026/STATUS.md)；同一候选的新参数化已完成五项产物、下游输入接收与重启验收，见[真实参数化验收](../../reports/ligand-native-parameterization-20261026/STATUS.md)。下一项是把体系准备和短程 QC 接入同一研究。

## 参数化接入（已部署，066 真实验收完成）

现新增 `plan_md_parameterization`、`start_md_parameterization`、`md_parameterization_status`、`release_md_parameterization_plan`。工作流从当前候选的已核验图检查生成参数化计划，单独批准后提交；中断只对账原任务，结果检查原始图、候选身份和参数产物哈希。仅未提交计划允许撤回归档。

状态接口能重新读取参数回执和失败原因；参数化通过仍保持 `md_system_ready=false`、`simulation_started=false`。已补 CPU 分派，避免继承只支持 CovDock 的默认 HPC 分派器。正式目录已新增参数化运行登记并通过模块导入探针，本轮原生结构读取、图检查及力场赋参均已实际成功。正式数据根已有验收研究 `ligand-flow-6b05ea15b9657b54c9272385`，关联历史候选 066 的来源包；新父图任务 `20261026T124547-library-first-md-adduct-admission-1e6412a7d7` 已通过登记产物核验，新参数化任务 `20261026T125147-library-first-adduct-parameterization-0755394eb1` 也已完成并通过五项输出核验；共享下游接口接收及重启读回均通过。历史参数不能计作本轮新工作流成功。

## 维护目录与阶段验收说明

专属源码、命令、配置和测试已物理迁入本目录。旧 `covscreen.ligand_*` 导入与旧脚本保留薄兼容入口，不再维护两份实现。Inspire 新任务代码入口已切换，实际版本以 `../../ops/ligand-release.json` 和迁移回执为准。

```text
ligand-first/
├── src/covscreen_ligand_first/  # 输入、执行链、母体共价化、专属评估
├── scripts/                    # 正式命令与现行恢复/审计工具
├── configs/                    # 版本化路线配置；含历史续跑配置
├── tests/                      # 路线与平台集成测试
├── ops/                        # Inspire / HPC 调度器
├── historical-scripts/         # v4/v6 审计及旧 smoke 准备工具
├── historical-configs/         # 日期化请求快照
├── historical-ops/             # 旧实验、审批、恢复及部署记录
└── science/                    # 指向上述真实源码的导航
```

正式入口为 `scripts/compile_ligand_first_chain.py`（编译）、`scripts/run_ligand_first_campaign.py`（执行控制）和共享 MCP 适配器。所有执行仍须满足原有审批和资源合同。历史脚本归档不代表允许自动重跑。

当前本地工作流候选已在同一个 `covalent_ligand_design` MCP 内接入 AF3 阶段动作：`check_af3_runtime` 编译当前研究的精确计划，`start_af3` 经独立原生审批登记，`af3_status` 跟踪原任务；`evaluate_af3` 重读登记产物、重算置信度和有限几何，`report_af3` 保存可追溯报告。恢复追踪同一任务，新计划不启用引擎历史输出复用。结果核验支持冻结的 CSV/Parquet 候选合同，保留全部失败候选，并区分任务失败、产物缺失和分子评价。

此处的产物核验不授予完整物理验收、药效验证或 MD 准入，也未修改数值门槛。报告分别列出 canonical、来源结构、投影构建体及 AF3 序列编号。该连接通过软件集成回归，未由此执行新的 AF3 科学计算或部署主平台；验收见 `../../reports/next-stage-plan-20261022/ligand-af3-readback/ACCEPTANCE.md`。

受体准备的本地候选动作 `prepare_receptor_input` 将当前封存研究的原始结构、原始采集记录、UniProt 记录和 canonical FASTA 交给共享准备读取器；不伪造新的靶点检索或 Library 工作流。`check_receptor_runtime` 使用现有 CPU 依赖探测和准备编译器，配置缺失时保留输入并报告等待，配置补齐后编译同一来源的计划。执行器的历史名称仍为 `library-first-receptor-preparation`，实际路线保留 `ligand-first` 并核验研究身份。`start_receptor_preparation` 需要冻结 CPU 计划的独立原生审批；`receptor_preparation_status` 跟踪同一登记任务，提交回执丢失后用 `reconcile` 查回，结果不明时不自动重交。来源失效也保留已登记任务，避免重新开放提交。任务完成仍为 `NOT_EVALUATED`。`evaluate_receptor` 使用共享审计器重算身份/立体化学、几何和描述性环境测量；`report_receptor` 保存冻结报告。通过范围仅为隔离的指定蛋白链，生物学环境适用性、CovDock 执行和 MD 准入仍未建立。状态/恢复查询核验冻结审查记录、完整文件清单及实现版本，未变化时不重复重算；显式评价仍调用审计器。文件或报告失效会撤回当前认可，保留原任务和核验记录。详见 `../../reports/next-stage-plan-20261022/ligand-receptor-readback/ACCEPTANCE.md`。以上为本地候选，软件验收不表示已执行新的受体修复或更新主平台。

精修靶点可复用封存采集记录中的完整 `receptor_altlocs`，但仍检查每个选择是否真实存在、是否完整，以及是否与目标位点选择一致。缺少选择时不会自动按占有率或首个构象猜测。未映射构建体位置、序列差异和不确定残基仍须独立政策审查；历史 1M17 的五个未映射末端位置在真实入口继续被拒绝。

CovDock 的本地候选动作 `prepare_covdock_input` 冻结已核验受体、精确候选、实际作者位点编号和完整来源清单，同时保留构建体、排除组分及环境适用性待审问题。`check_covdock_runtime` 复用共享编译器；配置缺失时等待，配置齐备时只编译计划，文件存在不代表许可证可用。当前范围为人 EGFR C797、1–3 个 acrylamide 候选；原始研究的变体和构建体检查继续生效。输入被修改会撤回当前输入/计划并保留原准备任务。两项动作均不提交计算；环境适用性评估与原生执行见下段；缺少正向评估或原生批准时，不能直接把计划改成 execute。详见 `../../reports/next-stage-plan-20261022/ligand-covdock-planning/ACCEPTANCE.md`。输入/计划就绪仍不代表执行或 MD 准入。

环境适用性判断现通过 `read_covdock_environment`、`review_covdock_environment` 接入同一 MCP。前者返回当前审查、待审问题及允许引用的来源清单；后者记录 assembly、excluded_components、construct_and_site、reaction_scope 四方面的决定、理由、局限和 JSON 字段引用。工具核验来源哈希及 JSON pointer，并自行提取被引用值；不把引用存在等同于论证正确。结论区分适合本次探索性精修、需要修改和证据不足，明确标记为智能体判断，不是生物学验证或原生计算批准。审查改变会保留并退役原审查/未执行计划，重新编译的计划绑定新审查；审查失效保留准备任务与原输入。此功能未放宽原有构建体门槛，MD 准入仍需独立完成。详见 `../../reports/next-stage-plan-20261022/ligand-environment-review/ACCEPTANCE.md`。

原生 CovDock 阶段通过 `start_covdock`、`covdock_status` 接入。前者要求已冻结的当前输入、正向环境评估和独立原生签名批准，提交身份随原计划固定；后者核验同一登记任务。回执丢失由 `reconcile` 查找原任务，无法观察到登记时保留结果不明，不自动重交。运行中不允许替换研究；输入或签名失效保留原任务身份。任务完成仍为 `NOT_EVALUATED`；产物独立核验和报告见下段。共享编译器同时检查通用接口提交的当前工作流请求，本地执行也必须绑定原生签名。详见 `../../reports/next-stage-plan-20261022/ligand-covdock-lifecycle/ACCEPTANCE.md`。以上均为本地候选，软件验收不代表新的厂商引擎运行或主平台部署。

公共任务账本、审批、候选身份、引擎适配、Pareto 排序及通用化学工具继续由 `../../science/src/covscreen/` 维护。两条路线使用公共 `chain_contract.py`，各自保留阶段顺序及科学门槛。`parent_template`、`parent_chemical_identity`、`attachment` 和 `covalent_enumerator` 属于公共科学能力，导航见 `../shared/`。

发布包将公共包和两条路线的源码一起固定版本。代码位置与任务数据位置分开；新任务记录发布身份，工作进程核对已批准版本。旧任务保留原有 spec、结果和运行根，不能用迁移后的配置改写历史审批。

迁移清单、原文件备份和验证证据：`../../reports/ligand-layout-migration-20261012/`。真实 CPU 输入检查、单个位点共价化、注册工作进程及 23 个历史任务回读已通过。验收没有重跑 GPU、AF3 或 CovDock 全链。

Inspire 代码根和数据根由部署方在外部配置中登记。数据根包含运行配置、已登记研究输入、任务账本及历史结果；新任务使用明确绑定的发布版本。历史回执缺失目录时，须配置原始目录后才能恢复，配置项见 [配置说明](../../configs/examples/README.md)。源码迁移不改写旧任务的冻结版本。

CovDock 产物通过 `evaluate_covdock`、`report_covdock` 读回：冻结厂商读取器和实现，核验原登记候选/产物/调用及准备后受体，重开成功结构并回算获准计划中的暂定筛选规则。全部候选分母保留；引擎失败、测量缺失和已测结构失败分别表达，缺失姿势一致性保持 NA，局部应变明确为厂商字段而非母体差值。读取运行环境不可用独立表示为可恢复等待。未变化的输入、产物和实现允许状态/报告复用冻结测量；显式评价重新读回。JSON/Markdown 报告带不可变见证；全失败报告不声称已有成功结构。完整化学验收、活性和 MD 准入仍未建立。详见 `../../reports/next-stage-plan-20261022/ligand-covdock-readback/ACCEPTANCE.md`。本地软件验收不表示新的厂商计算或主平台更新。
