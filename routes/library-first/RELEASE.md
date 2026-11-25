# Library-first 内测发布清单

更新：2026-11-23。当前为发布准备，未完成内测上线。本文的状态来自本地源码与已留存回执，不代替实时服务检查。

## 本阶段范围

自然语言需求 → 文献/数据库证据 → 靶点、位点、反应配置 → 受体与配体准备 → CarsiDock-Cov/RTMScore → 约1000–2000个分子 → CovDock-VS → 约100个分子 → CovDock-LO → 结构质检、多样性与报告。

AF3可选，MD不在本阶段。CPU执行固定使用CPU资源空间/asgbie2；asgbie3保留给De novo。先测实际配额、负载与许可证，再决定并发。CarsiDock设备另行核验。

按分子筛选、按姿态评价、按化学状态记录。两个通过姿态属于同一个分子时，只占一个分子名额。参考接触保留率只作辅助信息；共价几何、碰撞和应变继续核验。

## 代码导航

以下路径以工程根 `covalent-agent-platform/` 为基准。

| 层 | 维护位置 | 职责 |
|---|---|---|
| MCP入口 | `science/src/covscreen/mcp/library_workflow.py` | 对外工具与动作合同 |
| 路线状态机 | `routes/library-first/src/covscreen_library_first/workflow.py` | 研究阶段、恢复与动作路由 |
| 研究输入 | 同目录 `screening_inputs.py`、`screening_plan.py`、`receptor_handoff.py` | 库、靶点、受体和来源绑定 |
| 化学预筛 | `science/src/covscreen/library_vs_prescreen_*.py` | 输入、执行及候选证据；不是已完成CarsiDock主链的证明 |
| CarsiDock | `science/src/covscreen/adapters/carsidock.py`、`carsidock_results.py`、`science/scripts/parse_carsidock_scores.py` | 引擎调用、输出身份与有限评分审计 |
| VS | `science/src/covscreen/library_vs_input.py`、`library_vs_execution.py`、`library_vs_evidence.py`、`library_vs_geometry.py` | 准备、注册、读回和质量评价 |
| LO | `science/src/covscreen/library_lo_input.py`、`library_lo_execution.py` | 按候选ID选择、独立精修、评价与报告 |
| 实例执行 | `science/src/covscreen/library_instance.py`、`routes/library-first/ops/dispatch_library_vs.py` | asgbie2边界、任务分派及观察 |
| 连接器 | `ops/library_jupyter_mcp.py` | Library专用Jupyter MCP通道 |
| 宿主 | `openscience-custom/backend/cli/src/session/covalent-dispatch.ts` | 原生工具结果与外部执行器衔接 |
| 审批与去重 | `science/src/covscreen/native_approval.py`、`submission_identity.py`、`instance_claim.py` | 新任务授权、登记、原任务恢复；不得伪造历史批准 |
| 发布 | `ops/release/`、`openscience-custom/backend/cli/integrations/inspire-screening/executor/code_release.py` | 源码快照、科学版本构建与哈希核验 |

`science/src/covscreen/library_chain.py` 是兼容入口。旧AF3/MD链及日期化脚本只按其历史合同使用，不能把它们当作本阶段已上线的默认流程。

## 当前事实与发布阻断项

| 项目 | 当前状态 | 放行所需证据 |
|---|---|---|
| 新接触质量门 | 已有独立版本及远端再评价 | 活动服务使用该版本，旧冻结规则可追溯 |
| 参考输入可选 | 评价层异常隔离完成；上游仍有必需引用 | 缺参考配体/接触结果仍能执行必要结构检查 |
| 真实VS | 单分子独立短测完成 | 原生登记或显式审计导入，不能冒充原生历史任务 |
| 原生LO与报告 | 未完成 | 新LO任务、原始结构、质量结论、报告下载与恢复验收 |
| CarsiDock/RTMScore | 资产与适配器存在，真实上游未贯通 | 设备、模型、输入分子与输出评分/姿态来源闭环 |
| 配置化与规模 | 仍有EGFR/1M17与5候选限制 | 第二靶点仅靠配置接入，目标规模真实验收 |
| 网页入口 | 未完成本轮切换验收 | 自然语言需求到候选报告，无需用户填写服务器路径 |

现有原生输入/评价绑定冻结版本及哈希；修改默认Policy不能自动迁移旧任务。历史产物再评价须单独登记，保留旧报告，不重跑对接来修改门禁。

## 最近证据位置

证据位于工程外的工作区目录：

- `reports/library-beta-20261121/`：真实准备/VS短测、原始产物包、逐姿态质量表。
- `reports/library-rollout-20261123/`：新版本安装与独立再评价回执、当前未完成事项。
- 已安装科学版本：`a8d6700c3253750fa6f6db9f78d7b8a22c2e034faa7e6cd1ea3ab20d21e7b2eb`。它是独立安装，不等于活动网页已切换。
- 再评价计数：1个分子、1个化学状态、3个姿态；2个姿态通过，对应1个分子。

## 发布操作顺序

1. 完成上表阻断项，运行相应路线、公共科学组件、宿主与连接器回归。测试fixture不得算真实科研验收。
2. 按 [统一发布说明](../../ops/release/README.md) 构建源码快照。科学包使用显式、最小、无凭据的运行配置；构建输出目录需预先创建。
3. 在asgbie2安装独立内容寻址版本，核验清单；为新任务绑定独立数据根与发布版本。
4. 完成真实原生VS→LO→报告、失败记录、断线恢复及不重复提交验收，再验证第二靶点和目标规模。
5. 保存宿主、网页、连接器、科学执行器的版本与健康回执后切换入口。回滚恢复原绑定与进程；不改写或删除任务账本及历史产物。

不得把完整旧运行配置或许可证内容打进源码包。科研结构、任务、测试日志、凭据、权重留在外部状态目录。
