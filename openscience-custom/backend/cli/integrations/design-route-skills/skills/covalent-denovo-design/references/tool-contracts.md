# De novo 原生合同

> 发布说明：本文历史部署路径已匿名化；`/path/to/` 与 `<account>` 是占位符，实际路径由部署方在源码外配置。历史验收不代表当前部署状态。

核对日期：2026-10-20（本地源码，部署需另行核验）。实际源码位于工作区 `openscience-custom/backend/cli/src/tool/denovo.ts` 和 `src/session/denovo-entry.ts`；工作区根 `/path/to/workspace`。

## 严格参数

```json
{"action":"workflow","study_id":"study-00000000-0000-0000-0000-000000000000"}
{"action":"plan_study","study_id":"study-00000000-0000-0000-0000-000000000000"}
{"action":"create_study","study_id":"study-00000000-0000-0000-0000-000000000000"}
```

```json
{"action":"status","task_id":"denovo-0123456789abcdef01234567"}
{"action":"results","task_id":"denovo-0123456789abcdef01234567"}
```

所有 ID 仅是格式示例，不可查询或执行。task_id 满足 `^denovo-[a-f0-9]{24}$`；各分支拒绝额外字段。没有 target、slots、command、approve 或任意路径参数；这些科学输入在 study 内冻结。`create` 仅返回 NEEDS_SCIENTIFIC_DECISION，不创建任务。`workflow` 恢复研究及其原任务；`plan_study` 读取证据和注册包生成不可变回执，不提交计算；`create_study` 绑定实际用户消息、冻结计划及原生审批。`status` 和 `results` 检查原会话与项目，不要求当前模型包可用。`results` 仅读取已完成任务的发布校验报告，核对审批/请求摘要、包哈希、槽位及计数，保留 raw/relaxed 的全部失败条目；不执行重新 QC、不补样。项目内 `workflow` 也会读取其冻结任务的已完成结果。

## 请求识别

自然语言请求（如“针对 EGFR C797，固定丙烯酰胺，用 PocketXMol 从头生成 32 个候选分子”）进入智能体规划，不直接执行旧 create。通过 discovery_study/list/get 恢复已有研究，必要时 create；明确输入后 start-reasoning、sync-hypotheses，最后 plan_study。缺证据或资产须如实阻断，不生成假回执。槽位和种子必须明确写入研究，不沿用旧解析器默认值。

冻结后仅实际用户的精确命令“执行 de-novo 研究 study-<id>”进入宿主执行快捷入口，支持 research/biology/build；此命令仍不能替代 remote_compute 原生审批。

提问、引文、否定、附带文件/代理部件、synthetic/ignored 文本、C797S、其他靶点/warhead、0/97/小数数量、追加条件均不进入直接执行路径。工具 schema 合法不等于原始请求被授权；入口解析和工具再次检查都必须通过。未识别的正常请求先继续讨论/配置，不能删掉不支持条件再调用。

## 运行与质量标准

执行包 `science/denovo/egfr_c797_v1`；固定 EGFR WT 4G5J A:C797，丙烯酰胺五个重原子、N-only、maskfill 100 steps/batch 2。拒绝审批不提交；不确定提交只协调原作业，不重复创建。最多 1 H100×15 min，当前单位价格上限 1 点券/小时，最多估计 0.25 点券；审批绑定本次输入和执行包哈希。

验收 `control/rules.json`：固定核心 RMSD≤0.01 Å，蛋白 clash VDW 比值≥0.75；反应前 SG–beta 2.8–4.0 Å、SG–beta–alpha 80–140°。主终点 raw all-checks-pass/requested slots；次终点 relaxed all-checks-pass 且优化收敛/requested slots。完整判据还包括 PoseBusters、化学与身份核验，不能只用距离判断通过。规则中另有 adduct 几何范围，不能与反应前结构混用。它们是本方案预登记工程筛查值，不是普适反应性阈值。

任务 `.cache/denovo/tasks/<id>/` 保存 request/status/输入日志和失败槽位；项目 `denovo-results/<id>/` 和 Results 保存报告、CSV、原始/优化后 SDF、结构图、汇总。最终报告读真实 artifacts 链接；远端文件路径不能直接作为浏览器下载链接。

后台 worker 独立于对话轮次，但取消活动生成会写取消请求；本机睡眠/关机影响控制与交付，没有重启自动恢复服务。workflow 读取已有任务但不启动 worker；部分预留或缺失状态返回 OUTCOME_UNKNOWN，不重建。恢复仅沿原任务，不能获得新采样。
