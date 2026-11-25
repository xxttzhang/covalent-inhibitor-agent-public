# Ligand-first 源码与历史证据

以下路径相对于部署方保留的历史证据根目录；该目录由外部配置定位，不属于源码包。路径用于查阅，不是任务重放指令；有新任务时读取其独立 manifest。历史报告日期与当前调度器状态分开。历史证据未安装时报告缺失，不猜测本机路径。

- `science/docs/LIGAND_FIRST_ROUTE_SPEC.md`：原始八阶段设计；旧原生入口描述需结合当前工具目录，枚举新增后端见源码。
- `science/src/covscreen/ligand_chain.py`：配置、阶段编译与质量门。
- `science/scripts/run_upstream_ligand_campaign.py`：完整母体原生枚举及 vinyl sulfonamide 区别。
- `science/configs/ligand_first/EGFR_C797_erlotinib_native_validation_v1.yaml`：显式验证配置，不覆盖默认。
- `reports/ligand-af3-recovery-20261008-v2/platform-delivery/LIGAND_AF3_REPORT.md`、`AF3_RAW_OUTPUT_AUDIT.json`：058/060/066 三个 propiolamide 候选新 AF3 推理。实际 1 sample；旧表字段 5 是已知差异。mPAE 0.95/0.91/0.90 Å，不能推定完整物理验收或最终排序。
- `reports/ligand-covdock-serial-recovery-20261008-v2/RECONCILIATION.md`：历史协调记录，本目录恢复提案未激活，不能重放 activate.py；不是当前作业状态。
- 当前路线界面 `http://127.0.0.1:4096/prj_dbfd2a34da2e4c78977238fcf59d39ae/discovery/ligand-first`：解释最近证据时检查数据源、时间和批次。

AF3 的三个结果不能证明 CovDock、独立评价、Pareto 或 MD 已全部完成。状态更新要找该批实际完成证据，不能用 De novo/Library-first 的 MD 报告填补。
