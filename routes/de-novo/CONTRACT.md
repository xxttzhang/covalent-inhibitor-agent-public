# De novo 接口与维护合同

## PocketXMol 批量首版（2026-11-20）

本节取代下方历史单候选流程作为新研究默认入口。旧接口与旧研究记录继续保留。真实网页验收仍在进行，源码接通不代表全部计算与结果展示已经验收通过。

- 网页研究冻结 EGFR WT Cys797、4G5J 链 A、丙烯酰胺、生成数量、随机种子和预算；生成器仅 PocketXMol。
- `denovo_generate` 的批量动作顺序：`select_batch` → `prepare_batch` → `register_batch` → `plan_batch_vs` → `batch_vs_status` → `read_batch_vs_products` → `prepare_batch_lo` → `plan_batch_lo` → `batch_lo_status` → `evaluate_batch_lo` → `prepare_batch_prereaction` → `plan_batch_prereaction` → `batch_prereaction_status` → `evaluate_batch_prereaction` → `report_batch`。
- 计算分别通过 `denovo_submit_batch_vs`、`denovo_submit_batch_lo`、`denovo_submit_batch_prereaction` 进入精确原生审批。状态和评价动作不创建替代作业；断线或超时后观察原请求，不能据超时推断计算未启动。
- 生成使用单张 H100；CPU 对接使用 asgbie3。VS 最多 16 核，LO 最多 12 候选、最多 12 个并行子任务（准备状态数与唯一分子数分别统计），独立 Glide SP 使用 4 核、最多 5 个候选及最多 2 个 afatinib 对照状态。预算是每次计划的一部分，不能在审批后静默增加。
- 后续操作绑定已经冻结的 LO／反应前输入清单，不重新生成上游输入。生成坐标、VS 坐标均不替代独立 LO 或 Glide 搜索。
- `retry_batch_lo` 仅支持已核实的第一次 LO 启动前单线程监督错误：签名和来源一致、原进程退出、日志和产物为空。它冻结第二次输入并关联原失败任务，保留原输入、计划和审批；再走计划及独立审批。任务仍活跃、其他失败或存在引擎输出时拒绝。它本身不提交计算。
- 科学依赖核验运行在子进程；实例监督进程保持单线程，继续负责 CPU 边界、取消、超时和后代进程回收。
- `report_batch` 保留全部 LO 候选及未测/失败原因，按结构与独立反应前证据分层，同层内按 LO 分数排序；不把不同引擎分数相加，不把计算优先级解释为活性或反应速率。无 MD、AF3 强制证据或 CovaGEN 回退。

主要实现：`batch_lo.py`、`batch_lo_input.py`、`batch_lo_retry.py`、`batch_glide.py`、`batch_report.py`；MCP 为 `covalent_de_novo_batch`。网页动作及提交范围分别由平台 `denovo.ts`、`denovo-selection.ts`、`denovo-batch-execution.ts` 绑定。

## 历史单候选接口

2026-10-29，独立评价适配已联合发布；尚未完成真实端到端验收。

## 维护入口

| 责任 | 真源 | 边界 |
|---|---|---|
| 路线请求、阶段、规划决策 | `src/covscreen_de_novo/de_novo.py` | 纯规划模型，不启动生成器 |
| 项目路径约束、输入检查、规划结果 | `src/covscreen_de_novo/planning.py` | 复用公共项目读取和运行环境检测；不登记计算 |
| MCP schema / 薄适配 | `../../science/src/covscreen/mcp/de_novo.py` | 通用 `covalent_plan_de_novo`；原生研究独立评价 `covalent_de_novo_review` |
| 旧 Python 调用 | `covscreen.tool_api.plan_de_novo_tool` | 委托路线实现，保持签名及旧字段 |
| 原生生成任务 | 平台 `denovo_generate` 及本路线 `runtime/` | 消费 study 冻结方案；与通用规划器是不同输入合同 |
| 科学候选验证 | 注册 worker 与 `evaluation/`、`registry/` | 只能根据实际产物与来源核验判断；规划通过不替代验证 |

## 返回值解释

- `status=PASS`：本次规划调用通过输入检查；不表示分子、口袋或生物机制通过验证。
- `planning_only=true`、`compute_submitted=false`：没有启动模型或提交计算。
- `availability_source=caller_asserted`：调用者传入的假设性资产标志；不是远端预检凭据。
- `availability_source=runtime_detection`：读取现有运行环境配置并检测；仍不证明完整模型执行成功。
- `assessment.state=not_evaluated`：本次调用没有科学候选验收。
- `READY_FOR_*`：规划中的下一阶段，不是原生授权，也不是已实现的自动执行入口。
- 现有阶段文件路径即使存在，也不能被提升为阶段完成凭据；必须核验内容、来源与请求关系。

## 指标语义与范围

遵循上级 `METRIC_SEMANTICS.md`。`MultiObjectivePolicy` 内 MW、cLogP、电荷及各权重是规划策略；该规划函数没有执行这些筛选，也没有把异质分值校准成统一分数。默认数字不是所有靶点通用的科学门槛。

口袋定义、战头可达区域与阶段完成标志目前仍含请求声明。只有独立结构、反应映射和实际产物证据才能支撑候选晋级；不能因为请求中布尔值为真而宣称反应发生或具备活性。

## 尚待完成

固定登记方案已有原生生成、单候选独立输入冻结、受控数据运输、共享 CovDock 计划/同请求观察/真实结构读回及独立 Vina 反应前评价适配，已联合发布。对照受体对齐及对称感知 RMSD、候选完整配体反应图和立体化学检查已接入；尚无本次新研究的真实科学通过结果。

通用 DeNovoRequest 规划与原生 study 注册包编译是不同合同，旧假说 handoff 的 TARGET_NOT_REGISTERED 不能替代后者的输入绑定核验。正式执行需完整封存证据、显式弹头决策、同研究冻结计划及精确原生审批。

共享 MD 交接仍未实现；不得把 AF3 成功标签填入 Vina 证据，也不得以 CovDock 物理或对照几何通过替代独立门槛。所有计算使用共享 JobManager、不可变 request_id 和精确原生审批，无新增作业账本。最新事实与验收缺口见 `../../reports/denovo-closed-loop-20261029/STATUS.md`。
