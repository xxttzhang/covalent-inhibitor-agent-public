# 统一源码发布入口

本工具生成可追溯的源码快照。源码快照、网页运行版本和科研执行器版本分别记录，生成源码包不代表服务已经切换。

## 目录分工

| 路径（相对新工程根） | 职责 |
| --- | --- |
| `openscience-custom/` | 网页、智能体运行时、工具与执行器集成 |
| `science/` | 各路线共用的科研实现；Python 包名仍为 `covscreen` |
| `routes/` | Ligand-first、Library-first、De novo 路线实现 |
| `ops/release/components.json` | 正式源码及内部链接的显式登记表 |
| `ops/`、`configs/`、`docs/` | 运维工具、无凭据配置模板和工程说明 |

依赖安装、密钥、会话、任务记录、测试报告和科研结果放在工程外。当前本机运行目录为同级 `covalent-agent-runtime/`，验收证据为同级 `outputs/source-baseline-20261029/`。模型权重、分子库、第三方引擎和许可证仍是显式外部依赖。

## 使用

在新工程 `covalent-agent-platform/` 执行（Python ≥3.10）：

```sh
python3 -m unittest discover -s ops/release -p 'test_*.py'
python3 ops/release/release.py build --output ../outputs/source-packages
python3 ops/release/release.py verify ../outputs/source-packages/<版本ID>.tar.gz
```

`build` 不联网、不安装依赖、不改运行配置、不提交计算、不重启服务。相同内容得到相同版本和归档；文件变化产生新版本。包内 `manifest.json` 记录逐文件 SHA256。版本反映工作树的实际字节，包括未提交修复，因此交付时须另外记录 Git 提交及工作树状态。

白名单排除依赖、编译产物、认证和运行状态。凭据格式检测只是辅助检查，不能代替交付前的内容审查。不要将含密钥的运行配置复制到源码树。

登记的 `source_links` 在归档中展开为普通文件。若从解包目录再次构建源码包，需要先核对字节并恢复登记的内部链接；不要任意将普通文件替换为外部目录链接。

## 发布和验收

### 编译宿主候选版

在独立解包目录安装锁定依赖，不在运行中的源码目录覆盖构建产物：

```sh
cd openscience-custom
bun install --frozen-lockfile --ignore-scripts
cd backend/cli
OPENSCIENCE_VERSION="$(cat ../../../VERSION)" OPENSCIENCE_CHANNEL=rc \
  bun run script/build.ts --single --skip-install --skip-models-refresh
```

使用根 `package.json` 指定的 Bun 版本。构建结果位于 `backend/cli/dist/` 对应平台目录；先运行可执行文件的 `--version` 并核对根 `VERSION`。此步骤只验证宿主构建，科研 Python 环境、外部软件及部署配置仍需独立准备。

1. 修改源码并完成相关回归与构建检查。
2. 生成、校验源码快照，在独立运行目录准备候选版。
3. 科研侧通过现有 `code_release.py` 生成独立的内容寻址版本；记录其清单、环境锁和配置摘要。
4. 记录源码提交、源码包、网页构建、后端进程及科研执行器身份，再进行网页验收。
5. 只有经验证的候选版才能替代验收入口。已有会话、历史任务及证据保留；旧任务继续使用其冻结版本。

不要对运行目录执行全量 `rsync --delete`，也不要为了目录统一改写历史任务的 manifest。源码包不是包含模型、商业软件及科研数据的完整运行镜像。

当前发布身份见 [Ligand-first 发布状态](../../docs/ligand-first-release-status.md)。`docs/web-start.md` 和 `docs/migration-status.md` 保留历史迁移记录，不作为当前入口。实际服务身份以源码树外保存的运行回执为准。

### 固定产物启动

先将 `configs/examples/runtime.env.example` 复制到源码外并填写本安装路径。运行目录须已经通过 `ops/source-baseline/web.py prepare` 或等效部署准备，`binding.json` 的 source、Python、端口、项目和科研连接必须与实际安装一致；模型凭据只保存在外部配置中。不要在另一安装中复用本机运行目录。

以下是当前 macOS arm64 三个服务的启动命令；分别在进程管理器中运行，加载同一份外部环境。后端工作目录设为 binding.json 中的 project，网页构建时显式设置 `VITE_OPENSCIENCE_SERVER_URL` 为本安装后端地址。其他平台使用各自编译产物目录；端口与 binding.json 一致。

```sh
. /absolute/path/to/runtime.env
# Scientific connector (installation owns the transport and exact remote binding)
"$DEPLOY_BUN" --no-env-file run "$DEPLOY_SOURCE/ops/source-baseline/connector.ts"
# Backend, a separate process
"$DEPLOY_SOURCE/openscience-custom/backend/cli/dist/@synsci/openscience-darwin-arm64/bin/openscience" serve --port 4712
# Web, a separate process with this working directory
cd "$DEPLOY_SOURCE/openscience-custom/frontend/workspace"
"$DEPLOY_BUN" --no-env-file run serve --host 127.0.0.1 --port 4752 --strictPort --outDir "$DEPLOY_SOURCE/openscience-custom/frontend/workspace/dist"
```

`web.py start` 是源代码开发启动方式；固定产物上线使用上述编译产物。切换前保存配置、会话数据和旧进程命令，确认会话空闲；切换后必须同时检查健康版本、实际 MCP 工具发现、带原项目作用域的历史会话及 Results。只返回 HTTP 200 不够。失败恢复原配置和旧命令，不改写冻结科研发布。
