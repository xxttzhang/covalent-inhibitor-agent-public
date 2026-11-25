# Ligand-first 接口与资源合同

统一科研入口更新：目录提供 `covalent_ligand_workflow` 时，以本技能主文档的 start/status、阶段 evidence/design、reconsider 和 suspend/resume 为准。下文是早期底层实现快照，不是调用这些旧入口或脚本的授权。site 可省略，先进行目标身份核验与探索性位点选择；不能用历史 EGFR 编号替代新靶点的证据。

核对日期：2026-10-09。下文 `repo` 指历史 `covalent-library-first` 源码布局；它不等于当前正式工程的根目录。历史示例仅供复现，须由部署配置定位相应版本；当前任务使用上文统一科研入口。

## 历史能力分层（2026-10-09 快照，非当前状态）

| 层级 | 事实与用法 |
|---|---|
| 2026-10-09 OpenScience 工具目录 | 无 Ligand-first 原生执行工具；连接 MCP 仅 inspire-covindb。运行时目录优先于本快照。 |
| 旧 MCP 定义 | `scripts/openscience_mcp_server.py`、`src/covscreen/tool_api.py` 有 intake/chain 等实现；未连接时不可调用。 |
| 仓库父链 | `src/covscreen/ligand_chain.py` 提供真实 Pydantic schema、状态检查与阶段计划编译。 |
| 专用外部执行 | `scripts/run_ligand_first_campaign.py`、`src/covscreen/external_inspire.py` 等；依赖冻结输入与原生批准上下文，不是写一个 human_approved 就得到授权。 |
| 网页 | discovery/ligand-first 展示所登记证据；不是提交能力证明。 |

## 开发者本地规划示例

以下仅检查并编译计划，写入新的审阅目录；不带 `--submit`，不会提交科学作业。先激活具备 repo/pyproject.toml 依赖的科学 Python 环境；本地 .venv 当前缺 Pydantic，不能假设可运行。本次轻量 schema 验证环境也不含完整科学依赖。示例配置是固定 EGFR 案例，不是当前最新 campaign 的别名。

```sh
cd "${LEGACY_SCIENCE_REPO:?Set the verified historical repository path}"
PYTHONPATH=src python scripts/compile_ligand_first_chain.py \
  --project-root "$PWD" \
  --config configs/ligand_first/EGFR_C797_erlotinib_v1.yaml \
  --output-dir /tmp/ligand-first-plan-review \
  --execution-mode plan
```

输出 `ligand_first_chain_status.json`：`status`（stages、next_stage、terminal_state）、`next_request` 和 `provenance`。当前状态来自该项目 registry，不代表远端实时状态。terminal_state 为 ready/active/quality_gate_failed/completed；仍要检查每阶段 `quality_pass` 和父 run。

## 阶段脚本输入/输出

这些是开发者 CLI，不是聊天工具名；先用 `--help` 核对当前版本，不自动执行外部计算。

| 功能/入口 | 必需输入及输出 |
|---|---|
| `prepare_ligand_first_inputs.py` | `--campaign --audit-output --parent-pose-output`；输出输入审计 JSON、母体 SDF。可选 restore-missing 只恢复缺失冻结来源。 |
| `generate_real_ligand_first_candidates.py` | `--campaign --candidate-json --candidate-sdf --ligand-table --failure-json`；自动出射位点需额外 `--auto-discover-sites --site-discovery-json`。RDKit 子集。 |
| `run_upstream_ligand_campaign.py` | `--campaign --upstream-repo --output-dir --max-candidates`；原生枚举 manifest、标准候选 CSV/Parquet/SDF 和失败 JSON；空/超预算返回失败。 |
| `compile_ligand_first_chain.py` | `--config --output-dir`，可显式 project-root；默认 plan，不提交。 |
| `run_ligand_first_covdock_grouped.py` | 由父链编译受体、AF3 表、原子/反应类型映射、资源与输出目录；读取实际 `--help`，不复用其他路线启动命令。 |
| `select_library_first_pareto.py` | 共享脚本须显式 `--profile ligand-first`；父链自动提供 candidate-table/final-n 与 trace-project-root/trace-parent-run-id/trace-current-run-id/trace-chain-config；不是 Library-first 原生工具调用。 |
| `evaluate_ligand_first_batch.py` | `--candidate-table --campaign --output-dir`；默认 MCS≥0.60；坐标复核的三个参数必须同时给；返回 manifest，非 completed 返回码 4。 |

CarsiDock/AF3/Pareto 的阶段参数由 `compile_next_ligand_request` 生成，源文件以哈希绑定；不要手写跳过依赖的请求。Python 依赖见 repo/pyproject.toml；RDKit、pandas/PyArrow、Pydantic 2、PyYAML；GPU 引擎另有模型权重/环境；CovDock 与坐标重解析需要许可有效的 Schrödinger。

## 配置和验收门

`LigandFirstChainConfig` 拒绝未知字段；stage_order 必须是完整链或以 pareto 结尾的连续后缀，seed_run_ids 只能引用该范围内阶段。`dispatch_policy=ligand-smoke-v1` 与 dispatch_batch 必须成对，批次名前缀限定；此策略最多三候选、CovDock 最多四 CPU，只允许登记阶段。普通 schema 的大规模默认值不是实际资源许可。

源码默认：CarsiDock 成功率≥0.75、AF3 coverage≥0.95、CovDock 成功率≥0.80、评价 MCS≥0.60。每次实际配置可不同；这些是该流程的覆盖/质量门，不是药效阈值。使用源码和本次 manifest 的真实值，禁止把缺失补成通过。

母体 1M17 author A:CYS773 → EGFR canonical C797；AF3 片段与 CovDock 受体有各自编号，必须独立映射。EGFR native-validation 配置显式 `upstream-native`；默认配置 `rdkit-branch`。不要根据一个历史文件夹替用户切换后端。
