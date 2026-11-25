# 开发与独立启动

使用 Bun 1.3.14、Python 3.11.16。公共目录 `science/`；import 名 `covscreen`；发行名暂保留 `covalent-library-first`，避免破坏路线依赖。

## 安装

在 `openscience-custom/` 用 Bun 1.3.14：

```sh
bun install --frozen-lockfile --ignore-scripts
```

在工程根，用独立 Python 环境安装已验证依赖，再安装四个项目包：

```sh
uv pip install --python /absolute/runtime/venv/bin/python --require-hashes -r configs/examples/requirements-host.lock
uv pip install --python /absolute/runtime/venv/bin/python --no-deps -e ./science -e ./routes/library-first -e ./routes/ligand-first -e ./routes/de-novo
```

锁文件约束了这次验证环境；不宣称为远端 GPU/持证科学软件的统一环境。

## 新环境

`ops/source-baseline/web.py prepare` 需要 `--python` 和 `--bun` 的绝对路径；可用 `--provider-config` 读取安装方配置中指定的 provider/model/billing/sandbox 字段。它不复制旧研究、MCP、插件、凭证或生产指针。默认 runtime 是源码的同级 `covalent-agent-runtime/`。

配置生成后通过正式 `/auth/:providerID` 接口配置凭证，避免直接写文件造成 provider 缓存仍使用旧认证。凭证不进入源码。科研执行环境须另行登记并验证。

```sh
python3 ops/source-baseline/web.py status
python3 ops/source-baseline/web.py start
```

`start` 只启动；端口已有监听时拒绝，不停止或替换服务。当前服务已运行，请先用 `status`，不要重复启动。旧 `ops/local-platform`/launchd 文档不适用于此隔离环境。

## 构建与检查

后端与前端分别在 `openscience-custom/backend/cli/`、`openscience-custom/frontend/workspace/` 运行 `bun run typecheck`。前端 `bun run build`；后端 `bun run script/build.ts --single --skip-install --skip-models-refresh`。实际成功日志见迁移状态。

公开副本不包含私有仓库的历史源码清单。使用
`python3 ops/publication/check_source.py` 检查当前公开工作树；需要可复现发布包时，
优先从当前公开提交直接使用 `git archive` 生成。`ops/release/components.json` 是为公开
副本重建的组件清单，不代表私有发布身份。

科研发布使用 `code_release.py build --runtime-config /absolute/external/local.inspire.yaml`，外部配置被纳入科研发布身份。测试或释放后的源码验证须设置 `PYTHONDONTWRITEBYTECODE=1`，避免 Python 自动生成的 pyc 改变受检发布内容。

工作流回归选择解释器使用 `COVSCREEN_TEST_PYTHON`；历史受体重放使用 `COVSCREEN_RECEPTOR_FIXTURE_ROOT`，必须有来源及哈希清单。没有该外部历史输入时不能声称覆盖其重放。
