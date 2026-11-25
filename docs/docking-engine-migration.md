# 构象引擎策略与 CovDock-VS 接入状态

2026-11-02 用户决定：后续构象暂停使用 CarsiDock-Cov，优先采用 CovDock-VS，不自动回退到 CarsiDock-Cov。

2026-11-03 固定候选更新：Ligand 与 Library 的原生 VS 规划、审批、CPU 调度、原始产物评估和报告已进入源码，候选源码的执行开关已启用。科研发布现在显式打包同一引擎策略 JSON。仍需统一部署并从网页复验，不能把开关或软件测试当作部署证明。用户澄清的路线为 VS 筛选、LO 精修、再通过既有准入进入 MD；直接 VS→MD 接入已撤销。下面的首次迁移记录保留作历史说明，当前契约以 [VS 原生契约](covdock-vs-native-contract.md) 与 [Library 迁移说明](library-vs-migration.md) 为准。

## 本次完成的文件修改

- 共用策略：`science/src/covscreen/docking-engine-policy.json`。宿主 TypeScript 与科研 Python 读取同一策略；明确禁用引擎、首选方法、禁止回退、VS 原生工作流尚未就绪。该文件中的首选方法是迁移目标，不代表已注册的执行引擎。
- 智能体：路由系统提示、Ligand 共价化/精修技能、Library 主筛/对接技能均写入当前策略，并将旧 CarsiDock 阶段标记为历史说明。rc2 的 `config/openscience.json` 显式引用 `config/docking-policy.md`，使指令不只限于 acceptance 项目；缓存配置须重新加载后生效。
- 宿主保护：旧 Ligand 姿态计划/执行动作在审批与 MCP 调用前被拒绝；底层请求和后端解析出的重试审批计划检查实际引擎。Library 新建旧主筛任务被拒绝，后台到达尚未提交的 CarsiDock 阶段时记录 `NEEDS_REVIEW`，不会预约或提交计算。
- 科研保护：JobManager 编译/提交/重试、旧姿态规划、外部计算预约/调度替换和 worker 启动入口阻止 CarsiDock-Cov。已有排队任务在 worker 校验通过后以 `docking_engine_disabled` 结束，不能误解为分子科学失败。资产存在不再使 CarsiDock 被报告为可执行。
- 历史查询、报告和取消入口保留；本次没有删除模型、资产、旧任务或计算结果，没有对正在运行的任务发送取消命令。
- Ligand 工作流移除当前已停用的姿态动作，在相应阶段明确给出 `covdock_vs_pose_workflow` 接入缺口，不暗示已经换成 VS。

## 尚未完成，不能据此宣布网页流程就绪

本次是智能体指引与执行策略修改。新的宿主和科研代码尚未发布、重启或完成网页验收；正在运行的旧构建不能视为已获得新的代码保护。指令文件的修改也不能替代代码部署。

当前已注册的 Schrödinger 接口是 **CovDock-LO**，仍按 `leadopt` 编译、评估和绑定上游来源。不得将其改名为 VS，也不得沿用 `carsidock_run_id` 等旧身份伪造迁移。

2026-11-03 更新：源码已增加独立 VS JobSpec、准备/执行器、Ligand 输入和环境审阅、原生登记恢复及专用 CPU 调度连接；原运行环境仍未部署这些代码，VS 执行开关保持关闭。以下清单保留原迁移目标，逐项最新状态以 [VS 原生契约](covdock-vs-native-contract.md) 为准，不能将“已有代码”视为“网页已验收”。

要让网页直接以 CovDock-VS 生成构象，还需要完成：

1. 独立的 VS 任务契约和 `enrichment` 执行适配，绑定受体准备、原子/候选身份、正式许可证与 CPU 预算。
2. 原生输入和产物契约：每个姿态必须绑定自身的受体坐标、CYS–配体拓扑、原始分数及源文件哈希。
3. 将 VS 接入 Ligand 状态机及结果评估，复核应变、母核约束、关键相互作用和完整 MD 准入；不得仅以零碰撞放行。
4. Library 的旧 CarsiDock→RTMScore 自动链单独迁移，不能借 Ligand 的三个候选试验宣称全库流程已验收。
5. 发布新宿主/科研版本并绑定运行环境，从网页核实新建、审批、执行、恢复、结果展示与“不回退”行为。

CovDock-VS 三候选独立试验已完成，但不是原生工作流接入证明：[试验报告](/path/to/workspace/outputs/covdock-vs-20261102/REPORT.md)。

## 验证范围

- 38 项 Python 定向测试通过：停用引擎编译/提交/重试与排队启动、历史读取、取消、worker 恢复、状态提示、调度参数与节点替换保护。
- 13 项 Bun 定向测试通过：宿主拦截、后端重试计划检查、既有原生审批/取消边界及 Library 后台暂停。
- `bun run typecheck` 通过。
- Ligand/De novo 与 Library 技能校验脚本通过。
- `git diff --check` 通过。

未运行完整仓库回归，也未进行真实计算或新网页验收。旧测试中假定 CarsiDock 可以新提交/推进的场景需在 VS 迁移时更新，不能再作为当前路径的成功验收条件。
