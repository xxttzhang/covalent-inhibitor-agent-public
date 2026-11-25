# De novo 证据及开发入口

> 发布说明：本文历史部署路径已匿名化；`/path/to/` 与 `<account>` 是占位符，实际路径由部署方在源码外配置。历史验收不代表当前部署状态。

工作区根 `/path/to/workspace`。记录于 2026-10-09；历史产物不是新候选输出，不应重放批次脚本来查询状态。

| 范围 | 源码/证据路径（相对工作区） |
|---|---|
| 原生生成入口 | `openscience-custom/backend/cli/src/tool/denovo.ts`、`src/session/denovo-entry.ts` |
| worker / QC | `science/denovo/worker.py`、`denovo/egfr_c797_v1/evaluator.py`、`accept.py`、`control/rules.json`、`control/posebusters.yml` |
| 固定生成方案说明 | `science/denovo/README.md` |
| 历史独立 CovDock | `reports/denovo-covdock-20260929/FINAL_REVIEW.md`、`structural_audit.json`、`completion_audit.json` |
| 对接批次脚本 | `reports/denovo-covdock-20260929/run_covdock_review_v3.py`、`science/denovo/run_covdock_candidate_batch.py`，为该批冻结计划，非通用平台接口 |
| 重复 MD 与图表 | `reports/md-replicas-final-20261008-v1/REPEATED_MD_REPORT.md`、`sampling-summary.json`、`manifest.json`、`independent-final-verification.json` |
| MD 分析脚本 | `ops/analyze_md_replica_contacts_20261003.py` 及对应报告目录的冻结版本；新报告需绑定实际脚本哈希 |
| 轨迹/重复间/物理图 | 同目录 `REPLICA_STRUCTURAL_TRACES.png`、`REPLICA_ESTIMATES.png`、`REPLICA_PHYSICAL_TRACES.png`；配套 CSV 保留所有重复 |
| 网页数据含义 | `reports/denovo-workbench-20261008/README.md`：只读本地同步证据的采集刷新，不是远端调度器直接心跳 |

历史生成任务 `denovo-950cd19cedff09505dbf3eac` 请求 8 槽位，raw 7 通过、relaxed 6 通过。CovDock 复核七个 raw 候选（70 姿势，69 有效），匹配阿法替尼对照；004/001/007 入选后续。2026-09-29 对接报告中的“未启动 MD”是当日范围；2026-10-08 的重复 MD 报告已更新此后进展。

最新已验收重复 MD：四体系×三次×10 ns＝120 ns，四体系均提示追加采样。它不是 Ligand-first 三个候选的结果，也不是 Library-first 固定八分子任务。报告中的 001/004/007 研究安排不覆写原对接排序。

结果页面：`http://127.0.0.1:4096/prj_dbfd2a34da2e4c78977238fcf59d39ae/discovery/de-novo`。查看最新状态时核对界面证据时间与实际 run，旧批次报告不能当实时进度。
