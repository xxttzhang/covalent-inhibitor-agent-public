# 三路线工作流合同（迁移设计 v1）

状态：设计与实施合同。科研后端 1.26.0 已接入公共结果合同及 Ligand-first；对应不可变发布已在 Inspire 独立目录通过协议验证，未切换共享服务。本地 1.27.0 新增 Library-first 的靶点、结构、受体准备阶段及宿主路由；主筛、MD 和 De novo 尚未完成统一接入。实施证据见 `../reports/workflow-consolidation-20261019/PROTOCOL_MIGRATION.md` 和 `../reports/workflow-consolidation-20261019/LIBRARY_WORKFLOW.md`，软件验证不代替科学验收。

## 维护与执行边界

保留现有目录、计算引擎、任务账本及不可变发布。路线实现位于 `routes/<route>/src/`；公共身份、任务与 MCP 薄适配位于 `science/src/covscreen/`；宿主只负责会话、展示、授权和调用，不复制科学状态机。

工作流维护研究状态，现有执行器维护任务状态。工作流保存执行器引用并核验回执，不建立第二套作业数据库或通过任务状态推断科学成功。已有 study 资产、Hypothesis、LibraryFirstRequest、DeNovoRequest 等继续使用其真实合同，不能靠相似 JSON 字段自动互换。

## 共同协议

科学指标的定义、允许结论、适用化学态与缺失证据处理遵循
[共价候选评价指标语义合同 v1](METRIC_SEMANTICS.md)。这是 2026-10-20
确定的迁移合同，尚不代表所有运行适配已完成。数值阈值需独立校准；
现有工作流不得将新文档当作已执行的额外科学验证。

本地 1.27.20 的 Ligand-first 执行评审阶段新增 `report_pose`，在现有设计阶段
MCP 内重验签名任务及姿势证据，交付绑定策略的探索性候选报告，包括零通过和
评价不完整分支。该报告保留旧门槛标签，明确指标含义及缺口，不授予完整物理
验收、抑制活性或 MD 准入。实现及本批运行版本复核见
[姿势报告验收记录](../reports/workflow-consolidation-20261019/LIGAND_POSE_REPORT.md)。

| 项目 | 合同 |
|---|---|
| 身份 | route、项目作用域、workflow_id、revision；已有 Ligand ID/存储目录保持兼容 |
| 创建 | 宿主绑定 request_id；同请求同输入幂等，不同输入冲突，不能靠重复 start 查询状态 |
| 科研输入 | 明确目标、物种、科学约束；位点可先未决，由科研动作提出并用证据核验 |
| 资产引用 | 绑定类型、ID、来源哈希、目标/位点/构建体/化学身份；模型不填写任意执行路径 |
| 并发 | 在锁内校验 revision；设计/定稿不得自动接到新证据上；只读刷新有明确规则 |
| 执行意图 | 副作用前持久化请求身份；结果不确定时先对账，禁止根据超时自动重提 |
| 证据更新 | 记录依赖关系；相关资产改变后归档受影响分支，使后继交接失效；无关证据不无差别销毁全流程 |
| 回退 | reconsider 需理由，保留历史证据/候选；旧分支只能读取，恢复执行需重新核验 |
| 暂停 | suspend/resume 改变会话路由，不等于取消作业；取消是独立、有明确范围的执行动作 |
| 工具视图 | 后端返回可用工具、action 及字段；宿主投影，后端再次验证，不仅凭隐藏工具控制调用 |
| 授权 | 继续采用宿主签名的精确范围与资源授权；阶段变化、模型布尔值和工具可见性不授予算力 |
| 能力缺口 | 返回 capability_not_implemented/unsupported_scope；不以其他靶点、历史任务或未经验证引擎替代 |

共同结果外层使用 schema_version、route、workflow、execution、assessment、artifacts、blockers。Ligand-first 已接入；其原字段（包括 result）原位保留，兼容期不批量重写底层引擎的状态字段。公共定义位于 `covscreen/workflow_protocol.py`；新增路线适配须显式映射，不靠相似字段自动推断。

- execution：not_submitted / submitted / running / completed / failed / outcome_unknown。
- assessment：not_evaluated / evidence_insufficient / passed / rejected / review_required。
- blockers：结构化 code、作用范围、可恢复条件；避免把零候选、缺证据和计算异常都解释为 FAIL。
- artifacts：分型引用和来源；不同评分的单位、方向、受体和化学态必须保留，不自动混分。
- workflow.tools：由服务端状态产生；给出可选科研动作，不代替智能体作科学选择。

这些状态轴需由明确的逐适配器映射产生；禁止用字段名称猜测通用成功。例如 Library task COMPLETED 只表示执行完成；Ligand BRANCH_REPORTED 不表示设计成功。

## 三路线共享候选验证与 MD（目标边界，2026-10-20 修订）

Ligand-first、Library-first、De novo 是候选发现策略，不是三套独立验证体系。三条路线均可将经过评估与明确选择的候选交接到同一共享 MD 工作流；并非所有候选都自动进入 MD。Library-first 当前优先接入的 MD 代码位置是实施现状，不是其他路线的能力边界。

共享交接必须保留 origin_route、原工作流及 revision、候选化学身份、结合姿态、靶点/构建体/位点映射、共价反应与原子映射、父证据及版本。各路线显式适配真实资产，禁止伪造 Library delivery、screening 或 refinement 任务来满足共享接口。上述语义是待落实合同，不代表当前已注册这些字段或接口。

共享阶段为：候选评估与优先级 → 加合物准入 → 参数化 → 体系准备/最小化 → 短程 QC → 平衡 → 生产模拟 → 轨迹分析与证据评审。每一阶段按真实科学条件推进，计算完成不等于科学合格。MD 结果可回到原发现路线形成新设计分支；共价加合物模拟不能单独证明共价反应发生、亲和力或抑制活性。

迁移时将路线无关的阶段生命周期、证据核验及 MD 能力归入公共包，路线内保留候选选择与来源适配；沿用唯一 JobManager、审批和资产账本。已有 Library 入口通过薄适配保持兼容，不新建平行执行器。先完成真实候选交接合同与三路线消费者验证，再移动实现；不得以只改图或通用命名宣称跨路线接通。

验收新增：三条路线的候选分别可进入同一 MD 准入；来源、版本或位点不匹配须拒绝；父证据变化使下游验收失效；原路线可从结果继续迭代；旧任务恢复、独立审批及幂等提交不退化。当前这些跨路线验收尚未完成。

## Ligand-first

正式入口继续使用 `covalent_ligand_workflow`，阶段工具继续使用 `covalent_ligand_evidence`、`covalent_ligand_design_step`。不为了统一名称新建第二套实现。

| 当前阶段 | 智能体可执行的研究动作 | 推进依据 |
|---|---|---|
| TARGET_REVIEW | 核验/修订靶点；选择有理由的位点假说 | 实际 UniProt 身份、序列及 canonical Cys；位点选择不等于反应性验证 |
| INTAKE / EVIDENCE_PENDING | 采集、读取已封存证据 | 目标/物种/位点一致的证据版本 |
| PARENT_REVIEW | 检索、补证、测量、比较母体、定稿 | 现有原始来源、结合模式和比较核验；不降低现有科学门槛 |
| DESIGN_READY | 准备已选母体设计输入 | 冻结提案及真实结构身份 |
| DESIGN_INPUTS_READY | 有界图生成 | 明确附件位点、弹头规则及结构质检 |
| NEEDS_POSE_EVALUATION | 形成姿态评价方案、交付局限、补证或重选 | 图合格不等于姿态/活性合格 |
| POSE_PLAN_READY / POSE_INPUTS_BLOCKED | 检查计划、交付、回退 | 当前 plan_pose 仅导出请求，计算接入未完成 |
| NO_GRAPH_CANDIDATES / DESIGN_REVIEW_FAILED / BRANCH_REPORTED | 解释负分支、补证、重选母体或迭代 | 保留历史失败与不确定性；不声称靶点不可成药 |

已实施：原子读写、revision、分支归档、操作意图及公共结果合同；兼容已有 state.json。入口 schema 的 `x-research-workflow` 声明阶段工具集合及旧入口处置，宿主读取声明并投影，旧 1.25 服务仍有兼容回退。后端继续验证实际阶段。待完成：未知操作的完整对账、真实姿态执行交接与宿主独立发布验收；不能靠改名补齐。

## Library-first

本地 1.27.0 已注册正式入口 `covalent_library_workflow`，阶段工具为 `covalent_library_research` 和 `covalent_library_receptor`；尚未部署此版本。

后续本地 1.27.1 在 research 阶段接口加入 `review_receptor` / `read_screening_receptor`：重放现有独立质量/几何/环境检查，将结果绑定为不可变证据；模型不传任务或路径。更换结构归档并使其失效。该证据不替代主筛输入合同、科学范围判断或原生批准，主筛交接仍待完成。详见 `../reports/workflow-consolidation-20261019/RECEPTOR_HANDOFF.md`。

本地 1.27.2 继续在同一阶段接口加入 `screening_profiles`、`prepare_screening_inputs`、`read_screening_inputs`：模型选择登记方案，后端绑定库、受体、参考配体和科学配置，复用原冻结器及实验坐标验证器。当前仅支持现有 20k/5 research pilot 输入合同，不替代全库筛选；科学审核、后端意图来源与宿主执行接线仍待完成。重复准备需提交完成事件，即使复用了相同资产，也不能留下 pending。详见 `../reports/workflow-consolidation-20261019/SCREENING_INPUTS.md`。

本地 1.27.3 在同一 research 工具加入 `prepare_screening_plan` / `read_screening_plan`。计划绑定冻结输入、受体证据、三项科学审核理由及提议预算，在后端提交后才形成可读的稳定操作；相同提议复用旧操作，改提议归档旧分支，父输入变更使计划失效。它不授予计算权限。详见 `../reports/workflow-consolidation-20261019/SCREENING_PLAN.md`。

已实现靶点/物种核验、位点选择、结构发现/获取、受体计划、原生审批与现有 JobManager 提交/状态/对账、环境描述。复用证据时重验原始来源与父链；换结构归档受影响资产；模型不能替换后端绑定的 run/hash。受体准备请求身份按工作流及计划 revision 隔离。中断的本地证据操作可显式恢复已提交资产；不明计算提交只按已存身份核对签名任务，不自动重提。当前 `SCREENING_PLAN` 要求提交科学计划，`SCREENING_PLAN_READY` 通过宿主编译；本地 1.27.6 已接通草案、原生审批、执行状态和当前报告/候选评审；精筛/MD 工作流交接与真实运行验收尚待完成。

| 阶段 | 消费/形成的科学资产 | 复用实现与门槛 |
|---|---|---|
| TARGET_REVIEW | 靶点身份、候选位点、选择理由 | resolve_target + shared site-discovery；序列 Cys 与预测可反应性分别报告 |
| STRUCTURE_REVIEW | 构建体、链/残基映射、受体准备与环境质检 | discover/acquire/prepare_receptor/assess_receptor_context；重新选择结构使依赖结果失效 |
| SCREENING_PLAN | 库版本、反应范围、筛选策略、晋级策略 | plan_from_sites/prepare_pilot 和现有主筛方案合同；小规模 pilot 与正式主筛须有显式类型区别 |
| SCREENING_PLAN_READY | 已提交的科学审核、提议预算与稳定操作身份 | screening_plan 真源；签名宿主交接已接入，后续计算仍需独立原生批准 |
| SCREENING_HANDOFF | 后端锁定计划、原始请求及宿主账本身份 | compile_screening / recover_screening；签名挑战、当前父链与原操作核验，无自动重提 |
| SCREENING_EXECUTION_DRAFT | 现有执行器内唯一未批准草案及后端回执 | 精确计划原生审批；未批准操作可核验封存后返回计划 |
| SCREENING_EXECUTION | 冻结执行范围、原任务身份与账本时点观察 | 原生批准/取消/恢复由 `task-lifecycle.ts` 共用原实现；不能另建任务 |
| SCREENING_STOPPED | 原任务取消或失败及其批准记录 | 读取原账本；仅未批准、从未提交操作可释放，已批准终态的科学分支待接通 |
| SCREENING_REVIEW | 原任务的当前报告与各阶段分母/失败数 | `screening_report` 重验产物；文件不可用时关闭推进，科学评价仍未通过 |
| CANDIDATE_REVIEW | 显式策略产生的绑定候选提案、排名分歧和排除计数 | 复用原选择器及不可变名单回读；保留分母，不把提案当作精筛批准 |
| SCREENING_SELECTION_EMPTY | 同一报告下的零候选及策略/排除数 | 可显式修改策略，不自动补齐；输入方案回退仍待已完成任务分支接线 |
| REFINEMENT | 已冻结子集、精筛任务及来源一致的结果 | 复用既有 refinement actions；不得通过新建主筛任务代替精筛 |
| VALIDATION_PLAN | 待验证候选、明确验证目的与方案 | delivery/select/admit；原生加合物准入不等于完成赋参 |
| MD_VALIDATION | 赋参→体系→short-QC→平衡→生产→轨迹审计→分析 | 工作流绑定全部 run/hash；复用 next_md_action 及现有步骤，保留当前三重复协议及专家复核 |
| BRANCH_REPORTED | 执行事实、科学质量、限制和后续选项 | 报告不能把结构稳定性当成亲和力/抑制活性/反应速率证据 |

首个适配必须解决主筛 host-bound 权限上下文：Python 工作流可以输出分型执行意图，宿主调用唯一执行器并回交可核验回执。不得让模型提供用户原始消息、审批回调或伪造 task_id；不得让 Python 任意调用宿主 HTTP。此交接合同完成前不能宣称 Library 工作流可执行主筛。

宿主基础适配已加入 `executor/workflow-execution.ts`：将工作流操作引用与确切已编译 plan 的 SHA256 关联，在现有任务 SQLite 事务中同时创建 DRAFT 和绑定；相同操作可恢复，改 revision/意图/计划或借用旧任务会被拒绝。MCP 支持宿主在协议外提供引用并绑定 task_id；生命周期经 `task-lifecycle.ts` 复用原 `execute` 的同一审批、取消和观察器调用，原生审批保持独立。回执重放任务及阶段来源，标注本地账本观察而非新鲜远端核验。该基础适配自身不验证科学意图来源；现由 `workflow-host.ts` 和后端 `screening_handoff.py` 补齐签名来源、阶段门及会话接线，模型不能提供宿主签名、任务身份或权限回调。

后续 `executor/workflow-plan.ts` 已接入现有 `compileScreening`：显式冻结输入和受体证据绑定到内部意图，完整意图、原生草稿及操作引用在一个任务库事务内保存。丢失编译回复可按后端操作恢复，不要求调用方先知道计划哈希；并发宿主只产生一个计划。当前显式输入仍限现有 20k/5 research pilot，未拓宽为全库主筛；后端来源验证和阶段调用现已接通；宿主部署及真实运行验收仍是独立验收门。证据见 `../reports/workflow-consolidation-20261019/SCREENING_COMPILER.md`。

本地 1.27.5 的审批/状态/释放合同见 `../reports/workflow-consolidation-20261019/SCREENING_LIFECYCLE.md`。签名观察包含事件序号，明确只代表持久账本；关闭记录与任务编译共用一个 SQLite 事务边界，防止撤回后的迟到编译或批准。

本地 1.27.6 的报告/候选交接见 `../reports/workflow-consolidation-20261019/SCREENING_RESULTS.md`。后端保存确切 selection/manifest/policy；失败读取不采用最新其他名单，状态刷新保留候选评审阶段。报告和候选的当前可用性分别报告；候选失效但报告仍有效时允许显式新策略。精筛/MD 工作流与真实运行验收仍待完成。

精筛宿主内部适配支持绑定研究操作、原名单、审核理由及独立提议预算，按操作保留唯一提案与草案，审批需确切引用；过期计划不能隐式重建，也不能接管其他精筛。本地 1.27.7 已接通后端持久交接、`plan_refinement`、`recover_refinement` 和 `release_refinement`：先保存再规划，先在原生账本封存未激活规划再返回候选评审。迟到写入和确认创建均拒绝已封存操作。详见 `../reports/workflow-consolidation-20261019/REFINEMENT_HANDOFF.md`。提议预算不构成运行授权；精筛审批/执行见下述 1.27.8 接入记录。

后续 1.27.8 已接通 `approve_refinement`、`refinement_status`、`cancel_refinement`、`wake_refinement`。宿主与历史工具共用原生生命周期适配；后端按验真的双引擎账本事实推进审批等待、执行、停止和结果评审阶段。提交预留不是已确认作业，取消请求不是终止，账本内结果记录核验不是本次远端文件重验。拒绝后未批准确认可封存，已批准操作不能作为未提交规划释放。详见 `../reports/workflow-consolidation-20261019/REFINEMENT_LIFECYCLE.md`；精筛新鲜报告评审、异常证据对账和 MD 仍待完成。

本地 1.27.9 新增阶段动作 `refinement_report`，按原生草案身份调用既有报告读取与质量判定，成功后进入 `REFINEMENT_CANDIDATE_REVIEW`；文件不可读或证据变化时撤下当前候选报告，保留私有见证恢复。零通过、分歧及失败均保留，读取不产生审批、计算或 MD 候选决策。详见 `../reports/workflow-consolidation-20261019/REFINEMENT_RESULTS.md`。

1.27.10 在同一报告回执中增加可选 `delivery`。COMPLETE 只表示发布历史；只有确切工作流、报告、批准清单、原生当前版本和实际字节均通过复核，才返回文件引用。后端保留文件身份见证，核验失败时撤下当前引用，不产生发布/计算或 MD 准入。旧批准不补交付策略。详见 `../reports/workflow-consolidation-20261019/PUBLICATION_INSPECTION.md`。

1.27.11 按后端交付状态开放 `resume_refinement_delivery`。只恢复当前批准草案的文件队列及原文件观察器，保留不确定发布记录，不重跑精筛、不重发未对账文件、不扩展历史授权。完成后隐藏该动作；候选选择与 MD 输入仍另行接通。详见 `../reports/workflow-consolidation-20261019/DELIVERY_RECOVERY.md`。

1.27.12 增加阶段动作 `select_md_candidate` / `release_md_candidate`：先提交新鲜报告及原生文件观察，再保存明确候选身份和理由，并自动绑定文件版本与受体父记录。换选通过既有分支机制归档；证据失效时保留选择但要求重新核验。选择不是 MD 就绪或运行授权，资产传输及 pose 冻结仍待接通。详见 `../reports/workflow-consolidation-20261019/MD_SELECTION.md`。

1.27.13 在原研究入口接通 `prepare_md_input`：宿主先后复核原生文件与报告，通过私有签名分块交换传输确切版本，后端按持久偏移恢复并重验实际字节；冻结复用既有候选姿态实现及注册目录，不模拟旧 delivery run。输出仍待原生图准入及参数化，不授予 MD 运行或就绪。交接实现见 `../reports/workflow-consolidation-20261019/MD_TRANSFER.md`。后续完整宿主软件联合测试已覆盖真实文件 API、MCP、传输及冻结恢复，见 `../reports/workflow-consolidation-20261019/MD_FULL_HOST.md`；真实受体/结构来源验收仍未完成。

1.27.14 修复生产者—消费者的靶点字段缺口：`prepare_md_input` 从确切原生批准计划读取 CovDock 靶点/反应并签名绑定，CSV 可不含上下文；若已有上下文则必须一致。冻结器对照受体科学身份，保留原生上下文并在加载时重验。未证明的 canonical/author 编号映射不得自动补造。见 `../reports/workflow-consolidation-20261019/MD_NATIVE_CONTEXT.md`。

MD 下一步工具当前有 30 个顶层字段。迁移后由后端绑定资产图及版本，模型显式决定候选/协议覆盖值；执行层仍核验哈希。失败轨迹不能为了凑齐三重复自动补跑；补跑需符合既有恢复和授权策略。

## De novo

拟新增正式入口 `covalent_denovo_workflow`；这是待实现接口，尚未注册为可调用工具。

| 阶段 | 智能体的科研动作 | 后端核验/复用 |
|---|---|---|
| TARGET_POCKET_REVIEW | 选择靶点、位点及口袋，并说明依据 | 使用真实受体、坐标系、canonical/author 映射及口袋资产 |
| CONSTRAINT_REVIEW | 明确固定片段、弹头、生长/连接位点、化学与空间约束 | 检查分子图/原子映射；不得把模板默认值当成用户要求 |
| GENERATION_PLAN | 比较生成策略与支持范围，选择有界样本/预算 | 复用 DeNovoRequest、已冻结 study/hypothesis/plan；模型可提出引擎偏好，后端检查适用性 |
| GENERATION | 执行已批准方案 | 复用 `denovo_generate(create_study)` 和现有 worker；既有 task_id 保留 |
| CHEMISTRY_REVIEW | 检查结构、约束、重复、化学可行性 | 原始/优化后候选分别统计，保留失败与对应关系 |
| POSE_PROPERTY_REVIEW | 比较姿态、反应几何、性质及多样性 | 仅调用有可用资产的评价实现；计划中出现引擎名不等于实际可运行 |
| ITERATION_REVIEW | 修改约束/策略、保留候选或终止分支 | 新方案创建新分支并保留父谱系；不静默补样或反复采样直到通过 |
| VALIDATION / BRANCH_REPORTED | 选择进一步验证，交付事实与限制 | 仅在满足共享验证准入时交接；结构合格不等于命中或共价反应得到验证 |

已核实的原生生成入口仅支持冻结 EGFR WT C797 acrylamide/PocketXMol profile，最多 96 槽位；通用规划器还列有其他引擎。通用接口必须返回各组合的支持状态，不能由规划器推定 REINVENT4/DiffLinker 已具备与原生路径相同的在线闭环。

## 目录迁移规则

先把工作流职责与接口整理清楚，再决定是否将公共包内的 Ligand 专属实现物理归位。迁移采用原模块兼容导入、单一实现与同一源码快照；不得从 reports 或历史发布搜索代码作为运行时回退。公共包暂不改名。原始科学数据、凭据、权重、任务账本和冻结发布不移动。

## 验收门

1. 合同：注册表、处置清单、宿主阶段视图和实际 handler 覆盖一致；新工具未分类即失败。
2. 科学门：跨目标/物种/位点/化学身份、篡改哈希、缺失证据、跳阶段、旧 revision 均被拒绝。
3. 探索：补证、母体重选、生成约束修改、候选换选保留历史并仅使受影响后继失效。
4. 执行：断线/超时/进程恢复不重复提交；必须区分观察失败和计算失败；原生授权范围继续有效。
5. 兼容：旧工具和历史任务能读取/恢复；不把现有冻结任务迁移到新的源码版本。
6. 部署：各项目独立核对主入口及唯一主工作流服务；验证实际工具列表、版本与模型调用轨迹。
7. 科学验证：分别记录夹具/本地软件/真实计算/科学结论；软件通过不替代新候选完整计算验证。

1.27.15 增加阶段动作 `plan_md_admission`、`start_md_admission`、`md_admission_status`，复用原生图检查执行器、独立签名审批和 JobManager。后端绑定冻结姿态，先保存审批和提交身份，再提交任务；丢失回复由 `reconcile` 查询原任务，无记录不重放。COMPLETED 不自动成为证据 verified；输入、输出、候选、pose、受体和签名任务均复核，字节变化撤销当前验收并保留历史见证。阶段取消、参数化和 MD 运行恢复仍待接入。见 `../reports/workflow-consolidation-20261019/MD_ADMISSION_WORKFLOW.md`。

1.27.16 在同一后端生命周期接入 `plan_md_parameterization`、`start_md_parameterization`、`md_parameterization_status`。规划/启动/观察均绑定并复核原 admission 父链；参数化成功仅凭实际登记产物及现有参数合同验收，MD 就绪仍为 false。`release_md_stage` 封存未批准未提交计划；`cancel_md_stage` 独立审批原任务并复用合作式取消，`reconcile` 恢复取消记录，未确认终止不能标记已取消。外部调度器取消保持原有拒绝边界。见 `../reports/workflow-consolidation-20261019/MD_PARAMETERIZATION_WORKFLOW.md`。

1.27.17 在原研究入口增加 `plan_md_system_preparation`、`start_md_system_preparation`、`md_system_preparation_status`，自动绑定并重新验证参数化父链，复用现有 OpenMM 体系构建引擎。计划仅开放资源预算与最小化步数，独立原生审批、撤回、取消和未知提交恢复沿用统一阶段生命周期。登记产物、转换/力场审计/最小化回执与父哈希不一致时不允许消费结果。成功只表示体系准备回执通过，短程 QC 和 MD 就绪仍为 false。见 `../reports/workflow-consolidation-20261019/MD_SYSTEM_PREPARATION_WORKFLOW.md`。

1.27.18 增加 `plan_md_short_qc`、`start_md_short_qc`、`md_short_qc_status`，由后端绑定体系准备父链，复用既有短程 QC 引擎和同一审批/撤回/取消/恢复机制。阶段字段的接受与展示共享 `md_execution.PLAN_FIELDS`。结果重新核对固定协议、登记坐标/速度/帧文件及帧摘要；短程 QC 通过不代表平衡完成或 MD 就绪。见 `../reports/workflow-consolidation-20261019/MD_SHORT_QC_WORKFLOW.md`。

### Ligand-first registered pose lifecycle (local 1.27.20 development)

`POSE_PLAN_READY` exposes `check_pose_runtime`; a verified frozen plan then exposes
`start_pose` through the existing native approval boundary. The backend persists
the exact workflow request before submission. Pending submission exposes entry
`reconcile`, which looks up the original receipt without resubmitting. Registered
jobs expose `pose_status`; active jobs retain their branch and block evidence
mutation/reselection. Terminal status enters `POSE_EXECUTION_REVIEW` with scientific
assessment still `not_evaluated`. Independent output acceptance and downstream
refinement/MD are not yet connected. Software fixture verification is documented
in `reports/workflow-consolidation-20261019/LIGAND_POSE_RUNTIME.md`.

For local pose lifecycle development, `evaluate_pose` now revalidates registered
Michael Addition raw/no-dummy structures and runs the existing geometry evaluator.
It reports mapped product/geometry review separately from execution completion,
incomplete evidence, independent refinement and MD readiness. `pose_status` rechecks
any saved evaluation and invalidates it if evidence changes. New pose exports
include the CarsiDock-required `COMPOUND_ID`; old sealed exports missing that identity
are rejected before execution. See `LIGAND_POSE_GEOMETRY.md` in the audit report.

`prepare_refinement` now freezes a freshly revalidated geometry-passing subset
under the shared count/file-hash/identity-hash contract. The original input indexes
and global registered pose record indexes are retained. It creates a candidate
input asset only: AF3 target/seed identity and CovDock prepared receptor binding
are still required before refinement execution, and no MD readiness is implied.
