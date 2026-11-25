# 已验证范围与证据索引

> 发布说明：本文历史部署路径已匿名化；`/path/to/` 与 `<account>` 是占位符，实际路径由部署方在源码外配置。历史验收不代表当前部署状态。

以下为历史验收，不是实时任务状态。工作区根为 `/path/to/workspace`；核对时读取该根下列路径。

| 阶段 | 已验证范围 | 证据 |
|---|---|---|
| 核心筛选 | EGFR C797，test100：100 输入、99 成功、1 失败保留；33/33 结果自动交付 | `reports/inspire-test100-envfix-v1-final-verdict.md` |
| AF3 / CovDock | 同任务的八候选精筛与 50/50 文件交付验收；包含失败/恢复历史 | `reports/refinement-final-acceptance-20260922.md` |
| 后续结构复核 / MD | 八候选姿势复核后保留原 A/B1 标签；24 条 10 ns 轨迹，240 ns，七类逐候选图和批次汇总 | `reports/library-workbench-update-20261009/UPDATE.md`、`reports/library-md-20261008-v1/review-v1/SCIENTIFIC_REVIEW.md` |
| 固定批自动执行与交付 | 已准备体系 → MD → 分析 → Results，原生交付回执 | `reports/PLATFORM_MD_AUTOMATION_INTEGRATION_20261008.md` 末尾更新、`reports/library-md-20261008-v1/platform/publication-receipt.json` |
| 通用受体制备入口回归 | PDBFixer 1.12.0 + OpenMM 8.6.1 后端；1M17 A 链 / EGFR C797 与 4Z3V A 链 / BTK C481；注册 CPU 任务完成 repair → relaxation → 独立质量/几何审计 → scoped admission；不含 Prime 后端、口袋/反应性放行 | `reports/library-first-target-identity-20261014/README.md`、`second-target-receptor-regression.json` |
| 位点环境描述回归 | 同两套受体；prepared 单链 SASA/蛋白壳层与 preserved deposited environment 联合审计；EGFR SG SASA 2.28 Å²，BTK 10.46 Å²；不含反应性/口袋准入 | `reports/library-first-target-identity-20261014/site-context-regression-v1/` |
| 新候选加合物交接与赋参接口 | 五类登记 warhead/reaction 合同、不可变 `covalent-adduct-input-*`、单候选 JobEngine/MCP、原生批准边界与运行时探测的软件回归 | `routes/library-first/tests/test_md_adduct_input.py`、`science/tests/test_adduct_parameterization_job.py` |
| OpenMM 体系准备接口 | 完成赋参父任务重放、CMS 转换、独立力场审计、有界 CPU 最小化、单候选 JobEngine/MCP 与批准边界的软件回归；另有一份真实历史 CMS 输入回归 | `routes/library-first/tests/test_md_system_*.py`、`science/tests/test_openmm_system_preparation_job.py`、`reports/library-first-md-conversion-regression-20261014/` |
| OpenMM short-QC 接口 | 完成体系准备父链重放、最小化能量/力复核、固定种子无约束 CPU 积分稳定性、单候选 JobEngine/MCP 与批准边界；真实历史 CMS 体系完成 1 ps/10 帧回归 | `routes/library-first/tests/test_md_short_qc.py`、`science/tests/test_openmm_short_qc_job.py`、`reports/library-first-md-conversion-regression-20261014/library_038-short-qc/` |
| OpenMM 平衡接口 | 完成 short-QC 父链重放、固定 500 ps NPT 合同、部署平台绑定、热力学/约束/共价键门槛、单候选 JobEngine/MCP 与批准边界；真实体系仅完成 0.02 ps 诊断，明确未准入 | `routes/library-first/tests/test_md_equilibration.py`、`science/tests/test_openmm_equilibration_job.py`、`reports/library-first-md-conversion-regression-20261014/library_038-equilibration-diagnostic/` |

核心任务 `screen-9b56c32831224a76a3848d43`；原会话 `ses_f6007fb23ffe8VLlztrSdnvB6b`；项目 `prj_dbfd2a34da2e4c78977238fcf59d39ae`。这些 ID 只用于解释该批次，不可作为新用户任务的默认输入。

本批精筛八候选为 17、19、29、30、33、38、56、81，晋级依据包含排序分歧、共识与多样性，不是简单分数 Top 8。原 9 月精筛报告中的 B2/NA 与后续 A/B1 复核属于不同时间和评价定义；不能改写原报告，也不能用旧标签覆盖新复核。

初始集成 README 及部分旧提示中“AF3/CovDock unfinished”“MD 尚待运行”是早期进度。固定批次验收已推进；同时仍未建立任意靶点、任意 warhead 或任意新候选端到端自动化。临床/实验活性、选择性、反应动力学与采样收敛均不能由上述验收推出。

新参数化、OpenMM 体系准备、short-QC 和平衡 MCP 的软件回归不构成真实新候选验收。本机项目将 Schrodinger 原生 runtime 明确禁用；首个新候选的原生 pose 提取、Schrodinger 实际赋参、基于该候选的 OpenMM 体系准备、short-QC、正式 500 ps 平衡和新轨迹仍缺失。

运行时工具可见、Notebook 在线、资源可用、源工件完整是不同条件。查询失败应报告失败，不用历史快照填充当前状态。
