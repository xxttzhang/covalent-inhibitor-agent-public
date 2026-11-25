> 当前内测合同补充（2026-11-23）：以 [发布清单](RELEASE.md) 和工程AGENTS.md为准。AF3可选、无MD，参考接触保留率不作准入硬门槛。下文按日期保留旧接口合同，旧“筛选→MD”目标不属于本轮上线要求。原生VS→LO、配置化及规模验收仍有未完成项。

# Library-first 接口合同与维护边界

2026-10-29 实际验收补充：017 在同一原生研究完成首次真实软件失败 → 归档 → 独立批准第二次 → 图准入 `COMPLETED / verified`，原会话重开仍同时显示两次结果。12 个产物字节核验通过，无第三次提交。该结论只覆盖单候选图准入及软件失败恢复，不覆盖真实超时/取消恢复、参数化或 MD。见 [验收证据](../../reports/library-native-source-20261029/STATUS.md)。

更新：2026-10-15。这是源码接口说明；部署、真实计算和科学验收分别记录。完整目标仍为靶点输入 → 筛选 → 新候选 MD → 自动分析与报告。

2026-10-29 补充：`covalent_library_research` 增加显式历史输入分支。空研究核定 target/site 后，`attach_historical_md_source(source_artifact_id, source_manifest_sha256, reason)` 只接受操作者已冻结的 Library 原生来源；`read_historical_md_source` 重验来源；`prepare_historical_md_candidate` 将源构建区间与 canonical sequence/site 对齐并冻结既有姿态，再进入现有 `plan_md_admission` 与独立原生审批。没有旧作业、批准或 MD 完成状态的导入；历史补环/清理只作为待重验输入。实现为路线 `historical_md.py` 与共享 `md_historical_library_source.py`，事实与剩余验收见 [017 当前记录](../../reports/library-native-source-20261029/STATUS.md)。下文历史阶段描述按各自日期理解。

2026-10-19 后续整理：本地 1.27.1 / 67 工具已接入统一工作流入口和靶点/结构/受体阶段，新增复用原科学实现的受体证据交接。见 [`WORKFLOW_CONTRACT.md`](../WORKFLOW_CONTRACT.md) 及 [`RECEPTOR_HANDOFF.md`](../../reports/workflow-consolidation-20261019/RECEPTOR_HANDOFF.md)。下述早期工具计数为历史状态，不能用作当前运行版本证据。

2026-10-28 本地诊断修复：Library-first 原研究现在持久显示 MD 阶段的退出码、失败原因及证据核验错误，区分任务失败与完成后产物不合格；完整恢复原证据后可重读核验。此次未发布，未新增自动重试或科学计算，见 [本地验收记录](../../reports/library-readiness-20261028/STATUS.md)。统一研究的生产 MD/分析编排与真实端到端验收仍未完成。

2026-10-28 后续失败恢复（本地发布候选）：`retry_md_stage(reason)` 对原研究当前 MD 阶段重新读取签名任务、终态、冻结候选和全部父证据，仅允许 `FAILED` / `TIMED_OUT` / `CANCELLED` 后生成新计划。原失败记录完整归档，原任务文件不变；新计划沿用原阶段参数，生成新的 request ID 和递增 attempt，不继承旧审批、取消意图或提交状态。该动作不登记或运行新任务；既有 `start_md_*` 仍要求独立原生审批。取消请求、未知提交和完成但证据不合格都不能授权重试。新计划编译失败会保留原任务与诊断；重新规划或释放未提交草案不会重置已有尝试次数。审批前再次编译并比对冻结计划，运行时变化需要显式重新规划。见 [失败恢复本地验收](../../reports/library-failure-recovery-20261028/STATUS.md)；部署和真实验收仍由单独记录确认。

## 2026-10-20 动作合同整理（本地未发布）

`src/covscreen_library_first/workflow_actions.py` 是研究动作、受体动作及动作参数的单一词汇表。工作流 `view()` 仍按实际阶段决定可用动作；MCP schema 从该词汇表生成完整 action 枚举。完整词汇表不是权限，也不意味着后继动作提前可用。旧工作流入口、状态文件、revision 和任务身份保持兼容。

`screening_inputs.py` 的登记方案仍是 EGFR/C797/丙烯酰胺 research pilot：20,000 条文件顺序前缀、最多 5 个 shortlist，非全库覆盖；受体映射仍受当前注册适配器限制。指标含义遵循上级 `METRIC_SEMANTICS.md`，本次没有变更筛选数值、扩大靶点范围或授予 MD 准入。

## 一处实现，一个正式入口

| 责任 | 维护真源 |
| --- | --- |
| 路线科研实现 | 本目录 `src/covscreen_library_first/` |
| 跨路线科学能力、任务登记与原生批准协议 | `../../science/src/covscreen/` |
| 现有 covalent-discovery MCP | [registry.py](../../science/src/covscreen/mcp/registry.py) 聚合；各组模块维护自己的 schema 和薄适配；`server.py` 维护通信与批准入口 |
| 正式主筛 MCP 适配 | [mcp.ts](../../openscience-custom/backend/cli/integrations/inspire-screening/executor/mcp.ts) |
| 正式主筛唯一执行器 | [tool.ts](../../openscience-custom/backend/cli/integrations/inspire-screening/executor/tool.ts)；纯计划编译为同目录 `compile.ts` |
| 路线科学判断与参数示例 | [技能包](../../openscience-custom/backend/cli/integrations/library-first-skills/README.md) |

旧 `scripts/openscience_mcp_server.py` 仅作为兼容启动器，不再保存第二份工具定义或分发逻辑。`covscreen.mcp.legacy` 单独维护历史结果读取；`internal` 保留宿主批准所需接口。分组表示代码归属，不表示已经实现模型侧的路线过滤。

主筛插件使用真实 MCP SDK 的进程内 client/server 调用唯一执行器。宿主在协议之外提供当前会话、原始用户消息、取消信号和资源卡回调；模型不能在参数里伪造这些权限。此实现没有新增独立远程 MCP 服务，不会在 `/mcp` 列表中虚构一个新连接。长任务继续由现有持久控制器执行，不依赖 MCP 连接存活。

默认主筛插件只导出 `inspire_screening_task` 和 `inspire_screening_capabilities`。旧 `inspire_library_workflow` / `inspire_screening_smoke` 保留在历史插件内，不再通过正式插件自动合并。既有 30 个 covalent-discovery 工具保留协议兼容。2026-10-15 源码新增十七个 Library-first 工具，覆盖靶点、主筛候选冻结、单候选登记、原生加合物准入、MD 编排到三重复生产轨迹候选级报告，源端共 47 个（1.22.0）；这不表示远端服务已更新，也不表示新候选已有真实生产轨迹。

## 可复用能力及缺口

| 阶段 | 真实输入 → 输出 | 当前接口/实现 | 处置 |
| --- | --- | --- | --- |
| 靶点身份与证据 | gene/taxon → UniProt 身份、序列、Cys 位置及原始响应哈希；明确 target/site/taxon → 证据假设 | 新 `covalent_library_first_resolve_target`；共享 `target_identity.py`；既有 `covalent_target_hypothesis` | 身份解析已本地实测；位点反应性、结构和受体准备仍需补齐 |
| 正式主筛 | 原始请求、target_parameters、library、screening_mode、max_points → `screen-*` task_id、冻结方案、阶段状态 | `inspire_screening_task` / `create`；唯一执行器 `tool.ts` | 已有执行器，新增 MCP 薄适配；当前受体/化学范围由 `target.ts` 的 registeredTarget 定义，worker 也仍受限 |
| 准备/预筛 | 虚拟库 CSV、科学配置、读取/晋级范围 → 候选 SDF/CSV/Parquet、排除表、选择清单 | 路线 `covindb.py`、`scripts/run_covindb_prescreen.py`；控制器 prepare 阶段 | 复用；当前源记录上限是文件前缀，不是随机抽样或全库覆盖 |
| 共价对接 | 经核验受体、参考配体、候选结构与身份、反应位点 → 姿势、分数、失败记录 | 公共 `run_carsidock_registered.py`；控制器 carsidock 阶段 | 复用；正式 worker 仍绑定已验收 EGFR 组合，不能仅删除前端准入检查 |
| 重评分 | 同一受体及成功对接姿势、候选身份 → 分数和覆盖清单 | 公共 `run_rtmscore_existing_poses.py`；控制器 rtmscore 阶段 | 复用；未成功产生姿势的分子保留为未评分 |
| 子集与精筛 | 已核验核心任务 + selection_policy → 有来源和理由的候选子集；计划后返回 workflow/run 身份及核验报告 | `select_refinement`、`plan_refinement`、`approve_refinement` 等已有 actions | 复用同一任务/产物，不能重建另一套提交器或默认重跑主筛 |
| 新候选 MD 准备 | Pareto run/hash → 冻结候选与结构资产；显式选定候选 → 原生图 QC/加合物准入 → 赋参、坐标与原子映射 | delivery、selection、admission、parameterization 及后续 OpenMM MCP 均已登记 | 新链不引用历史脚本；仍需真实新候选原生运行验收 |
| MD 与分析 | 已准备且登记的新候选 → short-QC、平衡、三重复生产、逐轨迹审计、结构分析与固定策略报告 | 新 Library-first JobEngine/MCP 链；历史固定批次仍由原生 `molecular_dynamics` 管理 | 软件合同已覆盖；仍需真实新候选完整运行和恢复策略验收 |

## 已实现工具的合同

`covalent_library_first_resolve_target({gene, taxon, action})` 是 covalent-discovery 源码 1.6.0 新增的 Library-first 查询入口。`action` 默认为 `plan`，只读参数规划，不联网或写任务；`search` 最多发送一次有上限的公开 UniProt 查询，保存原始响应、数据库版本信息、canonical FASTA 和 manifest。输出 `artifact_id`、相对产物目录及 manifest SHA256。用户不指定输出路径。完整页面中只有唯一 reviewed 主基因名及指定物种匹配时，才返回核验身份；未知总数、分页不完整、歧义、同义名或异物种不会静默放行。

成功查询不是位点或筛选成功：`site_assessment.selected_site=null`、`campaign_ready=false`、`calculation_submitted=false`。canonical Cys 列表仅由序列确定，后续仍需结构映射和反应性证据。网络或数据错误保留 `lookup_failed` 回执，由 MCP 返回错误。该共享身份实现也供 Ligand-first 使用，其原配体和指定 Cys 校验合同保持不变；不维护两份 UniProt 身份解析逻辑。

`covalent_library_first_discover_structures({target_artifact_id, target_manifest_sha256, limit, action})` 消费上述工具的真实产物 ID 与哈希，不接受任意路径、替代 UniProt 编号或默认母体。调用前重读原始 UniProt 响应及 FASTA，重新解析唯一身份并对照 manifest；改写后的候选身份不能仅通过重新计算 manifest 哈希绕过原始证据检查。源目录或文件的符号链接、路径穿越、未消费分页和损坏文件会被拒绝。

默认 `action=plan` 不联网、不写新产物；`search` 复用共享 RCSB 搜索实现，查询一页，`limit` 为 1–100，返回 `structures-*` 产物 ID、父身份绑定、请求及原始响应哈希。它列出包含该 UniProt 引用的候选 PDB 条目，不要求 CCD 母体。条目级引用不能证明目标链、构建体、物种、突变、位点或口袋适用性，也不能把有界第一页称为完整搜索或最佳结构。下一步必须做结构与位点核验；成功和失败回执均不注册 campaign 或提交计算。

`covalent_library_first_acquire_structure` 消费 `discovery_artifact_id` 和 `discovery_manifest_sha256`，重新核验结构搜索与父靶点身份。`entry` 必须在已核验结果中；`cys` 必须是该 canonical 序列中的 Cys。可指定 label chain、模型和替代构象，歧义不默认选第一项。`plan` 离线；`acquire` 最多进行 26 次有界公开数据获取，复用既有 acquisition 和编号映射实现，不要求母体配体。

输出 `receptor-input-*` 产物保留原始 RCSB 数据、PDB、canonical→author 映射、坐标核验及父产物绑定。共享 PDB 解析器供两条路线使用；Library-first 位点检查完整 Cys 重原子、元素、占有率、CB–SG 几何和显式硫连接。成功仅为坐标核验待准备；缺位点或歧义返回失败回执。LINK/SSBOND/CONECT 触发连接状态复核，缺少记录也不证明是游离可反应硫醇。物种、构建体突变、可及性、质子化、补环和反应性仍待核验；`receptor_prepared`、`campaign_ready` 均为 false。

`covalent_library_first_prepare_receptor({action, receptor_input_artifact_id, manifest_sha256, ...})` 是 1.9.0 注册的受体制备任务入口。`plan` 只编译冻结输入、命令、运行时和资源预算；`start` 需要原生用户批准后才提交 CPU 任务；`status` 只读取对应 engine 的任务及流水线回执。输入只接受上述 `receptor-input-*` ID 与 manifest 哈希，不能传任意坐标路径。完整父身份、FASTA、RCSB 原始证据在执行前重放，并作为 JobSpec 输入逐文件冻结。流水线依次运行重原子修复、独立手性计划、三阶段松弛、身份/拓扑/手性审计、几何审计和受限准入；输出仅可声明 `isolated_selected_chain_protein` 已准备，`campaign_ready` 保持 false。

`covalent_library_first_assess_receptor_context({receptor_input_artifact_id, manifest_sha256, preparation_run_id, prepared_receptor_manifest_sha256})` 是 1.10.0 新增的确定性位点环境评估。它只接受已完成的注册受体制备任务，重新核验获取产物、制备任务类型、终态及 prepared manifest 的全链哈希。输出 `receptor-context-*` 工件，记录制备单链中目标 Cys SG/整残基 Shrake–Rupley SASA、6 Å 蛋白壳层，以及原始沉积结构中 8 Å 内的异质分子、水和其他链；4.5 Å 只用于单列近邻，不是接受阈值。该工具不自动选择保留组分，不把 SASA/距离解释为反应性或口袋适用性，`campaign_ready=false`。

坐标获取成功后，当前源端还调用共享 `construct_audit.py`，将已核验的 canonical FASTA 与下载的实体序列按 RCSB 对齐区域逐位比较。`construct_audit` 保存双重编号的序列差异、不确定残基、未对齐构建体位置、canonical 未覆盖位置以及来源物种一致性。冲突、越界或非单调对齐不能通过；源文件与 FASTA 在读取时再次核对哈希。缺失或混合物种记录不会标为物种已核验。未覆盖 canonical 位置不能直接解释成需补建的缺失环，未对齐构建体残基也不会自动删除。该报告始终要求构建体适用性复核，不赋予准备或计算准入；旧回执可能没有该字段，后续准备不得将字段缺失视为通过。

`inspire_screening_capabilities({})` 返回源码支持范围、正式入口、准备/MD 缺口。它不访问远端，不创建任务；`runtime_verified=false` 明确表示没有检查当前引擎和资源可用性。

`inspire_screening_task` 的参数真源是执行器 `tool.ts` 的 Zod schema；MCP 和插件复用同一份结构，跨字段规则由执行器校验。不要维护另一份手写参数 schema。

- `create` 需要明确研究输入和宿主绑定的原始消息；预检后展示一次具体资源卡，批准才可提交。返回的是任务及其实际状态，不是预测命中。不能把这个动作称作纯只读规划。
- `list/status/report` 使用实际 task_id；`download` 只接受本任务清单中的 phase/name，不能传任意路径。保留任务状态、科学质量和交付状态的区别。
- `wake` 恢复观察，`resume_delivery` 恢复交付，都不重提计算；`cancel` 返回取消意图，只有远端回读后才可报告停止。
- 精筛同样使用原核心 task_id 和冻结子集。其 workflow/run 身份由实际执行器返回；模型不能根据名称猜测。
- 未支持靶点返回 `TARGET_CONFIGURATION_REQUIRED` 且不提交。输入/协议错误作为 MCP error 返回；引擎失败、缺证据、无合格候选和交付受阻保留执行器的不同状态，不统一伪装成成功。
- MCP 结构化结果为 `{ "result": 原执行器结果 }`；文本内容保留原格式，包括 list 的数组。能力查询返回自身结构化对象。

模型提出的后续步骤必须消费已核验的真实产物，不能仅凭任务执行结束推定下一步输入合格。新版协议接入通过软件测试，不等于新靶点的科学闭环已完成。

## 整理规则

### 通用受体修复清单（开发中）

共享 `covscreen/receptor_preparation.py` 维护构建体投影与真实 PDBFixer 检查。它使用完整实体序列和 author→entity 映射建立内部连续编号，保留原链、author 编号和插入码；不使用固定 EGFR 偏移。整条目标链的替代构象须明确选择。原始坐标不修改，其他链、异质分子、零占有率坐标和氢的排除理由全部记录；这些只是检查视图，不是批准删除这些组成部分。

PDBFixer 返回的缺失残基清单须与实体映射推导的清单一致；保留原子的身份、顺序和坐标也必须一致。结果记录缺失重原子、末端原子、依赖版本及源码哈希，状态为 `repair_plan_requires_policy`。该检查清单本身不是已准备受体。

当前 1.22.0 注册实现只有一个受体修复后端：随不可变发布包冻结的 PDBFixer 1.12.0，后续松弛使用 OpenMM 8.6.1。Schrödinger Protein Preparation Wizard/Prime 能补侧链和环区，但目前没有实现成该工具合同中的可选后端；检测到 Schrodinger、CovDock 可用或发现历史 Prime 脚本，都不能让模型声称 Prime 已接入。每条受体谱系只能选择一个修复后端，不允许把 PDBFixer 和 Prime 串联后仍称为同一可追溯准备结果。

PDBFixer 与 InterMol 从当前不可变源码发布包的 `science/vendor/` 解析；可写 campaign/data root 只保存配置、任务账本和科研产物，不复制第二份 vendor 或执行脚本。这样源码身份由 release manifest 固定，任务恢复使用同一代码发布 ID，历史研究目录也不会反向覆盖当前实现。

若以后增加 `backend=prime`，必须实现独立的冻结命令、版本/许可证环境记录和输出解析，并满足与现后端相同的父证据、author/canonical 编号、观测重原子保留、目标 Cys 预反应化学状态、原子图/手性/几何审计和 scoped admission 合同。靠近结合口袋或目标 Cys 的缺失环应优先标记为 Prime/专家建模候选；在 Prime 后端正式实现前，这类输入报告 `capability_not_implemented` 或受体不准入，不能由 PDBFixer 自动成功掩盖。

同一模块的 `build_receptor_heavy_atoms` 现可调用实际 PDBFixer 建立完整构建体的重原子提案；要求显式单次 seed、缺失残基预算、新输出目录和 `isolated_protein_proposal` 环境策略，须在有 CPU/墙钟上限的独立进程运行。物种或序列差异、目标硫显式连接需要另行定义策略，不会静默建模。输出恢复 author 编号及插入码，建模原子占有率为零，原子映射、拓扑、输入清单和回执均保留。该函数只产生待质检提案，不授予 receptor/campaign 准入。

`receptor_quality.py` 使用 RDKit 标准肽模板独立检查序列、原子身份、原始坐标、化学图和坐标手性；逐项核对发布产物哈希。最初 1M17 重原子提案的图与身份通过，但有 12 处手性不符或不确定，该失败产物被保留且不能用于对接或 MD。

标准肽原子命名和手性方向的唯一维护实现为 `peptide_templates.py`，质检也使用此模板。修复约束方向从标准三肽的立体标签与独立测试坐标推导，不从待修复坐标猜测；明确覆盖 Cys、Ile、Thr 的特殊构型。实际提案有 350 个中心，其中 27 个涉及建模原子，可建立准备阶段约束；完全观测的中心不加此约束。OpenMM 能量和有限差分力已本地验证，但约束项验证不等于蛋白修复完成。正式验收仍要求移除人工约束、物理松弛和独立质检。

`receptor_relaxation.py` 维护唯一的通用质子化与三阶段松弛实现：带软排斥和手性约束、Amber 加手性约束、重建氢后无人工约束 Amber 松弛。目标 Cys 以显式中性 `CYS` 变体处理；选择的变体、氢身份、固定 seed、每阶段坐标、能量、力、手性体积和产物哈希均保存。最后阶段没有人工手性或软排斥势，2460 个观测重原子固定不动。约束计划由独立 RDKit 环境生成；OpenMM 运行时按原子身份重新绑定，避免混装两个 Python ABI 环境。

`receptor_geometry.py` 在身份/图/手性质检通过后，独立核对 RDKit 化学图界限、键/角/非局部距离、芳环平面、范德华碰撞及目标 Cys 的 `CB-SG-HG` 预反应状态。真实 1M17 回归使用已冻结历史 seed（重原子 20261013、氢 0）后，350 个立体中心、键、角、内部距离、芳环和碰撞检查均通过；错误 seed 的失败产物保留为故障证据，不能解释为 seed 搜索或新靶点通用成功。

`receptor_admission.py` 将通过全部审计的坐标登记为范围受限的 `prepared_isolated_protein_scope` 产物，并明确记录移除的异质分子与结晶水。这里的 `receptor_prepared=true` 只适用于单条所选蛋白链；生物组装体、辅因子依赖、口袋适用性和位点反应性仍为待评估，因此 `campaign_ready=false`。这套实现已接入上述异步 MCP/JobManager，并完成 1M17/EGFR 与 4Z3V/BTK 两个真实回归；两个实例不能证明任意新靶点都能通过补环。

`receptor_site_context.py` 是位点环境测量的唯一真源；`routes/library-first/scripts/assess_receptor_context.py` 只是命令适配。它同时绑定 preserved deposited coordinates 与 prepared isolated protein，避免从删去环境的对接受体推断原始配体、水、其他链。真实回归中 EGFR C797 SG SASA 为 2.28 Å²、BTK C481 为 10.46 Å²；这些值仅验证程序能处理不同暴露环境，不构成通用阈值或科学 PASS。

### 新候选赋参的维护入口（开发中）

`covalent_library_first_deliver_md_candidates({action, pareto_run_id, pareto_manifest_sha256, ...})` 与 `library-first-md-candidate-delivery` JobEngine 是主筛到新候选 MD 的首个正式边界。它只接受已完成的 Library-first Pareto 任务 ID 与精确 manifest 哈希，沿 `parent_run_id` 自动取得已完成的 Library-first CovDock 任务。计划阶段重放两个任务的 spec/result、Pareto 质量门和候选表哈希，核对每个 CovDock 结构确实登记在父 manifest，并把 AF3/CovDock 结构逐文件纳入 JobSpec 哈希。执行产物为 `library-first-md-candidate-delivery-v1`，资产复制到独立目录且 manifest 不暴露源路径；此时 `candidate_selected_for_md=false`、`native_graph_qc_completed=false`。模型不能传候选表或 MAEGZ 路径绕过父链。

`covalent_library_first_select_md_candidate({action, delivery_run_id, delivery_manifest_sha256, compound_id, input_sdf_index, preparation_run_id, prepared_receptor_manifest_sha256, receptor_context_artifact_id, receptor_context_manifest_sha256})` 从上述冻结交付显式选择一项。`plan` 完整重放候选、结构、受体与环境父证据，但不在项目中登记工件；`select` 确定性创建 `candidate-pose-input-artifact-v2`。工具核对 AF3/CovDock 成功状态、物理有效性、支持的 warhead/reaction、Cys 链号与 author residue、零基 pose index 和冻结结构哈希；LLM 只提供候选身份与正式任务/工件 ID，不提供任何文件路径。输出仍为 `native_graph_qc_pending`，选择动作不等于 Maestro 图检查通过。

`covalent_library_first_admit_md_candidate({action, candidate_pose_artifact_id, manifest_sha256, ...})` 与 `library-first-md-adduct-admission` JobEngine 是原生图检查和赋参之间的正式边界。`plan` 重放候选 pose 工件并探测唯一登记的 Schrodinger runtime；`start` 需要原生批准。受控任务从冻结 MAEGZ 按零基 pose index 提取结构，核对候选身份、目标 Cys SG、唯一跨残基 S–C 键、SG 去氢、配体选择与键长，再写出单结构 MAE 并通过 `StructureReader` 独立回读原子/键图；随后由 `md_adduct_input.py` 绑定 candidate evidence、graph QC、prepared receptor 和 receptor context。输出状态仅为 `ADDUCT_ADMITTED_PARAMETERIZATION_PENDING`。

`covalent_library_first_next_md_action({stage, ...当前阶段证据})` 是只读续跑编排器。它重放当前任务/工件及精确哈希，并调用下一正式工具的 `plan` 分支，因此会沿用各阶段已有的父链、状态、资源和运行时检查；它从不提交任务。候选选择返回可执行的确定性 `select` 调用，所有计算阶段只返回 `action=plan` 及 `start_requires_native_approval`，不伪造批准。平衡后强制生成 replica 1/2/3 三个生产计划，生产后要求三个轨迹审计，随后才允许三重复分析和固定策略报告；最终状态仍要求专家复核。

`src/covscreen_library_first/md_candidate_pose.py` 定义筛选结果到原生图检查之间的独立边界。它只从正式 `candidate-results-v1` 表选择 `BOTH_STRUCTURAL_QC_PASSED` 的单个候选，逐项绑定 compound/input identity、CovDock artifact/version/hash、pose index、warhead、reaction type、prepared receptor 和 receptor-context，然后把四类输入复制进新的 `candidate-pose-input-artifact-v1`。该工件仍是 `native_graph_qc_pending`，不因表中有 pose 编号就声称已经提取或验证共价图。

`scripts/inspect_covdock_md_handoff.py` 是该工件唯一的 Schrodinger 原生检查入口。它按冻结的零基 pose index 提取完整复合物，核对目标 Cys SG、唯一跨残基 S-C 键、SG 去氢、配体重原子选择和键长，并把写出的 MAE 再读一次比较完整原子/键图；输出 `candidate-md-handoff-v1` 与 `covalent-adduct-graph-qc-v1`。它不读取 `historical-ops/`、固定八体系 manifest 或日期目录。当前本机未登记 Schrodinger runtime，因此这段新候选原生提取尚未真实运行，不能将脚本语法和单元测试当作结构验证。

`src/covscreen_library_first/md_adduct_input.py` 定义新候选进入原生赋参前的正式交接合同。上游必须提供 `candidate-md-handoff-v1`、完整 `.mae` 加合物、`covalent-adduct-graph-qc-v1`、prepared receptor 与 receptor-context 的逐字节哈希。合同当前支持 acrylamide、vinyl sulfonamide、vinyl sulfone、propiolamide 的 Michael Addition，以及 chloroacetamide 的 Nucleophilic Substitution；都限定为 Cys-S-C 加合物。候选目标、反应类型、电亲原子名、跨键索引和所有父哈希必须一致。输出为 `covalent-adduct-input-artifact-v1`，状态只到 `native_parameterization_pending`；不会把图 QC 代替厂商运行时的原生图重检。

`covalent_library_first_parameterize_adduct` 与 `library-first-adduct-parameterization` JobEngine 优先消费完成的 admission run ID 与 `adduct-input.json` 哈希；旧 state artifact ID/hash 仅保留兼容读取。`plan` 在提交前重放全部父证据并探测已登记 Schrodinger launcher；`start` 需要原生批准，且只允许单候选、CPU、60 分钟以内的资源上限。受控 wrapper 设置厂商根、license、运行库和线程上限，再调用下述唯一参数化实现。当前本机项目配置未登记可运行的原生环境，因此真实新候选原生赋参尚未执行。

原生 OPLS_2005 赋参核心已提取到 `src/covscreen_library_first/md_parameterization.py`；命令入口为 `scripts/parameterize_covalent_adduct.py`。历史 `parameterize_library_single_adduct_20261009.py` 仅适配原八体系目录，历史控制包打包器从同一真源带入实现。线上冻结版本不会因此自动更新。

入口接受显式 `source`、`qc`、`qc_sha256`、新建 `output` 目录和厂商 `vendor` 根；输入仍须为已制备的完整 Cys 硫—碳加合物，并具有现有格式的补环/化学图 QC。它不生成共价反应产物、不自动补蛋白、不优化质子化，也不意味着仅输入靶点即可调用。

程序使用输入共价原子对与完整化学图识别溶质，已移除 A:797 的赋参识别限制；保留并加强输入哈希、配体完整选择、唯一跨残基连接、硫配位、力场项、原子顺序和电荷审计。原子映射包含插入码。质检使用显式异常，在 Python 优化模式下也不失效。

连接处参数检查由同一模块的 `audit_covalent_terms` 维护：根据结构图枚举跨共价键的角度及 proper 二面角，逐项核对覆盖，拒绝缺项、重复项、错误连接顺序和非有限系数；improper 项不能代替 proper 项。显式为零的二面角系数可保留，不能用缺项代替。该检查已在本地八份带哈希的历史 CMS 产物上通过，但参数覆盖完整不等于参数精度或反应可行性得到证明。

输出仍包括 `parameterized.cms`、`atom-mapping.json`、`parameterization-audit.json` 和 `finished.json`；必须检查终态与产物哈希，不能把输出目录存在视为成功。审计成功也仍需转换、最小化、短程质检和后续 MD 验证。当前已验证本地输入门槛和打包；提取后的原生程序尚待远端真实回归，未注册为已验收的通用 MCP 能力。

### OpenMM 体系转换与最小化门槛（开发中）

`md_system_conversion.py`、`md_system_audit.py` 与 `md_system_minimization.py` 是参数化 CMS 之后的新维护链。转换器只接受 `COVALENT_PARAMETERIZATION_AUDITED` 的 CMS/hash/audit，修复 InterMol 对重复组分标题和 FFIO 约束的已知表示问题，输出 `openmm-conversion-artifact-v1`；其状态明确为 `forcefield_audit_pending`。独立审计器重新从原 CMS 表解析质量、电荷、LJ、排除、Coulomb/LJ 1–4、键、角、OPLS proper/improper 二面角及所有约束，并与由 TOP/GRO 构造的 OpenMM System 逐项比较。只有全部一致才输出 `openmm-forcefield-audit-artifact-v1`。

最小化器只消费上述两份完整父链及原参数审计，在 OpenMM CPU 上启用 LJ 长程修正，执行有次数和容差上限的最小化，要求初末能量/力均有限、能量下降且 Cys-S-C 距离仍在 1.5–2.2 Å。输出 `openmm-minimization-artifact-v1`，状态仍是 `SHORT_QC_PENDING`；不声称收敛、平衡、MD-ready 或 campaign-ready。

`covalent_library_first_prepare_openmm_system({action, parameterization_run_id, parameterization_audit_sha256, ...})` 与 `library-first-openmm-system-preparation` JobEngine 将转换、独立力场审计和有界最小化串成一个可追踪任务。它只接受已完成赋参任务的 ID 与精确审计哈希，执行前重放父任务终态、CMS、原子映射和完成回执；不能传任意 CMS 路径。`plan` 探测登记的 OpenMM/NumPy/ParmEd 与 InterMol 源，`start` 需要原生批准，只允许单候选 CPU 任务。成功终态仍为 `OPENMM_SYSTEM_MINIMIZED_SHORT_QC_PENDING`，不能进入平衡或生产模拟。

`md_short_qc.py` 和 `covalent_library_first_run_openmm_short_qc` 定义下一道独立门槛。工具只消费完成的 OpenMM 体系准备 run ID 与 `openmm-preparation.json` 哈希，重放全部父 manifest、最小化坐标、System XML、CPU 能量和力；随后用固定种子在 CPU 上执行 1 fs、默认 1 ps 的无位置约束 NVT 积分。每帧要求坐标、能量、温度有限，约束相对误差小于 `1e-4`，Cys-S-C 保持在 1.4–2.4 Å。通过状态为 `UNRESTRAINED_SHORT_QC_PASSED_EQUILIBRATION_PENDING`，只证明积分稳定性，不证明平衡、结合稳定性、亲和力或动力学。

`md_equilibration.py` 与 `covalent_library_first_equilibrate_openmm_system` 只消费完成 short-QC 的 run ID 和 manifest 哈希。正式任务固定为无位置约束 500 ps NPT、2 fs 步长、10 ps 采样；CPU/CUDA 和设备属性由部署配置登记，工具参数不能替换运行平台。输出保存最终坐标、速度、OpenMM State、checkpoint 和逐帧热力学记录，并检查末窗温度、密度、体积波动、约束和共价键。通过状态为 `NPT_EQUILIBRATION_COMPLETED_PRODUCTION_REVIEW_PENDING`；`equilibrium_convergence_claimed=false`，生产尚未开始。低于 500 ps 或少于 20 帧的开发回归只能产生 `EQUILIBRATION_DIAGNOSTIC_COMPLETE_NOT_ADMITTED`。

`md_production.py` 与 `covalent_library_first_run_openmm_production` 只接受 `production_review_ready=true` 的正式平衡 run ID/manifest。每次调用固定执行一个 10 ns NPT replica，2 fs 步长、10 ps DCD 间隔；replica 1–3 从冻结 seed base 推导互不相同的速度、积分器和 barostat 种子。输出 DCD、最终坐标/速度/周期盒、State、checkpoint、逐帧热力学记录及哈希。完成状态仍是 `PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING`，不能在轨迹审计前声称稳定、亲和力或动力学。开发诊断模式不由正式 CLI/MCP 暴露，且只能产生 `NOT_SCIENTIFIC_TRAJECTORY`。

`md_trajectory_audit.py` 与 `covalent_library_first_audit_openmm_trajectory` 只消费完成的正式生产 run ID 和 `production.json` 哈希。独立读取 DCD Fortran records，校验字节序、头、原子/帧/步数、帧时间、坐标有限性、轨迹与 frame log 哈希以及 Cys-S-C 距离；正式 CLI/MCP 不暴露诊断豁免。通过状态是 `TRAJECTORY_INTEGRITY_VERIFIED_ANALYSIS_PENDING`，仍未完成结构指标、跨三重复汇总或候选判定。

`md_structural_analysis.py` 与 `covalent_library_first_analyze_openmm_replicas` 强制同时消费同一平衡父任务的 replica 1、2、3 及三份正式轨迹审计。选择由参数审计、原子映射和最小化参考坐标确定，输出逐帧蛋白/口袋 Cα RMSD、配体重原子 RMSD、配体质心位移、参考接触保留率和共价距离以及逐重复描述统计。终态是 `THREE_REPLICA_STRUCTURAL_ANALYSIS_COMPLETE_REPORT_PENDING`；不声明收敛、亲和力、动力学，也不自动作筛选决策。

`md_candidate_report.py` 与 `covalent_library_first_build_md_report` 只消费完成的三重复结构分析 run ID 和 manifest 哈希。固定策略 `library-first-covalent-md-structural-triage-v1` 先要求每重复至少 1000 帧且 Cys-S-C 全程处于 1.4–2.4 Å，再以 protein Cα p95 ≤5 Å、pocket Cα p95 ≤3 Å、ligand heavy RMSD p95 ≤4 Å、ligand COM p95 ≤4 Å、接触保留中位数 ≥0.30 形成逐重复结果和三重复共识。输出 JSON 与 Markdown；这些是可版本追踪的操作性分流标准，不是通用生物物理定律，所有类别都要求跨证据专家复核。

真实规模回归保存在 `reports/library-first-md-conversion-regression-20261014/`。新代码从一份既有 `library_038` CMS 重新生成了 65,218 原子的转换，独立核对 62,510 个约束、88,416 个排除、13,722 个 pair、45,188 个键、29,506 个角及源二面角键，然后完成 100 步上限 CPU 最小化；能量由 -663,532.51 降至 -981,154.54 kJ/mol，最终 Cys-S-C 为 1.736 Å。该回归只证明新实现可处理一个真实历史夹具，不表示新候选已通过，也不允许新代码读取历史转换结果作为运行时输入。

### 代码维护要求

1. 路线与公共实现各有唯一真源；兼容入口仅转发，不增加业务逻辑。
2. 数据、任务账本、冻结部署及验收报告独立保存，不复制进源码包充当默认输入。
3. `historical-ops/` 部分仍被固定 MD 调用；迁移前检查调用方，不能按日期或目录名批量删除。
4. 通用能力实际实现并验收后，才更新 capability 和 Skill；缺失能力不以空函数或假成功返回占位。
5. 正式路线测试须包括新候选体系准备及真实 MD/分析；协议、打包和单元测试仅证明对应的软件行为。
