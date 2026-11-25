# Inspire CovInDB 只读查询连接器

> 发布说明：本文历史部署路径已匿名化；`/path/to/` 与 `<account>` 是占位符，实际路径由部署方在源码外配置。历史验收不代表当前部署状态。

科研平台通过 MCP 调用本机回环连接器；连接器使用已安装的 InspireSkill CLI，
在现有 CPU Notebook 上读取数据库。不依赖旧的 `ssh inspire` / localhost:2222 通道。
不访问公共 CovalentInDB 网站、不下载整库、不提交计算任务，也不修改原始 CSV。

## 使用

1. 双击桌面 `/path/to/home/Desktop/打开科研平台.command`。它会复用健康的前后端和数据库连接服务。
2. 在平台发送：`请调用 covindb_query 查询 Inspire 数据库，target=EGFR、site=C797、limit=10，列出真实 CI 编号和来源。`
3. 若出现 MCP 权限确认，只需授权所需的只读工具；本次接入没有关闭沙箱或改变其他工具权限。

模型调用工具后，查询结果会进入当前模型的上下文；如果选择外部 API 模型，结果会发送到该模型网关。
只连接和直接验收 MCP 本身不调用模型 API。

## 工具范围

- `covindb_catalog`：检查四个已知 CSV 的路径、大小、修改时间和字段。
- `covindb_query`：仅查询 `CovInDB_All.csv`。按精确基因符号或 UniProt 编号、位点、抑制剂编号/名称筛选。
  `target` 与 `inhibitor` 至少提供一个；多个条件取交集。
  `C797`、`CYS797`、`CYS-797` 视作同一位点；不会匹配 `CYS-1797`、`SER-797` 或 `C797S` 突变描述。
  默认 20 条，每页最多 50 条，通过返回的 `next_offset` 翻页。

返回真实 CI ID、来源文件 SHA256、原始活性/单位/文献/SMILES。
`total_matching_records` 是证据记录数，`unique_inhibitors` 是不同 Inhibitor ID 数，不能混用。
无命中、读取失败、推测结果严格区分。不对不同 assay 的活性做跨实验排名。
虚拟筛选库与天然产物库缺少靶点/位点标注，本连接器只查看其元数据，不把候选分子假称为已知结合物。
文件名本身不能验证数据库发行版本。

## 位置和启动

- 代码：本目录 `server.ts`、`query.py`。
- 连接设置：`/path/to/workspace/configs/inspire-covindb.json`。
- 原始数据库：`/inspire/hdd/global_user/<account>/1_zhang/CovInDB/`。
- 连接器只绑定 `127.0.0.1:4097`，平台全局 MCP 项名为 `inspire-covindb`。
- 日志：`/path/to/workspace/.cache/platform-launcher/database.log`，不记录查询内容和认证信息。
- 本机连接器凭据自动生成，存于 `.cache/inspire-covindb/token`（权限 0600），不要提交或分享。
  Inspire 登录信息继续由原 CLI 管理，模型和工具参数不接触它们。

桌面启动器先确保连接器健康，再启动/复用平台；首次通过 `server.ts --register` 写入一个 MCP 配置项。
再次启动不会重写已有的正确配置，不会重启健康服务。
固定端口如需更改，须同时调整连接设置、桌面启动器的端口检查和已注册 MCP 地址。
首次注册遇到活跃研究会话会停下，避免配置重载中断任务。

## 故障排查

- `/health` 只表示本机连接器正常，不能证明 Notebook 在线；用 `covindb_catalog` 检查真实远程读取。
- Notebook 已停止/更名：通过 InspireSkill Live 列表确认同账号、同项目现有 CPU Notebook，更新上述连接设置；不要自动创建或启动计算资源。
- 登录失效：按 InspireSkill 提示恢复原账号登录，不把凭据贴入聊天或写进代码。
- 工具消失：检查平台 MCP 列表，重新双击启动器；连接设置冲突会报错，不自动覆盖。
- 查询超时/失败：明确报告失败，不用公共网页、模型记忆或旧结果伪装为数据库结果。

## 验证

在 `openscience-custom/backend/cli` 中运行：

```bash
python3 -B -m unittest discover -s integrations/inspire-covindb -p 'test_*.py'
bun test integrations/inspire-covindb/server.test.ts
bun integrations/inspire-covindb/smoke.ts
```

前两项测试不访问 Inspire。第三项使用当前连接器执行真实只读查询，并生成
`/path/to/workspace/reports/inspire-covindb-acceptance.json`。

`platform-smoke.ts` 是额外的模型端到端验收：会创建平台验收会话，
并将 3 条查询记录发给已配置的 DeepSeek 网关，必须先取得用户对此次外部传输的明确许可。
获得许可后才可传入 `--allow-model-transfer` 参数执行；省略该参数会在创建会话前退出。
它只在该验收会话中授权单一只读查询工具，不修改全局权限。
