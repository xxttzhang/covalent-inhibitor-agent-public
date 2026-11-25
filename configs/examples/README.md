# 配置与外部状态

本目录只放模板。不要复制生产 `openscience.json`、模型提供商凭证、原生批准密钥或活动发布指针。

De novo 内置 `routes/de-novo/registry/egfr_c797_v1/profile.json` 是未激活的安装模板：`allowed_directories` 为空，`/absolute/` 和 `/inspire/deployment/` 路径必须在独立部署副本配置。保留资产哈希、靶点和科学参数；配置目录、算力及模型位置后通过既有内容寻址发布流程登记 `ops/denovo-release.json`，不能直接改写运行中的冻结包或回执。`assets/pocketxmol.yml` 的输入路径须与配置中资产哈希键一致。此整理不撤销已冻结任务的原包，旧包应保留在运行目录。

旧 CovInDB 查询连接器的安装配置需显式提供 `database_root`（远端 `/inspire/` 下的数据库目录）；可用 `COVINDB_CONFIG` 指向源码外的 `inspire-covindb.json`。连接器仅将此安装配置传给远端 `COVINDB_ROOT`，模型查询参数仍不能指定路径。迁移时补入原数据库目录；缺失配置表示连接未就绪，不代表零条查询结果。

SSH MCP 桥接读取 `COVSCREEN_BRIDGE_CONFIG` 指定的外部 JSON，格式见 `bridge.example.json`。将模板复制到源码外，填入本部署的精确数据根—发布根配对、SSH 参数及 Results 目录；SSH 主机别名在本机 SSH 配置中设置代理、端口和私钥路径。桥接不猜测账号、Notebook 或个人路径；未配置时拒绝连接。迁移历史部署须保留原配对，不得只换数据根或覆盖旧发布指针。配置只存路径，不存私钥内容，不提交到 Git。

历史浏览器验收脚本使用 Playwright 默认安装的 Chromium；非默认安装可通过 `PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` 指定程序。需要历史回执或数据库的脚本要求 `COVSCREEN_ACCEPTANCE_ROOT` 指向源码之外的原证据工作区。带日期或固定工作流ID的脚本只用于原案例复查，不作为新版上线验收入口。

- 科研运行参数模板：`science/configs/local.example.yaml`。
- 两条路线的登记科学参数：`routes/*/configs/`；有固定适用范围，不等于通用靶点支持。
- 新网页环境必须使用独立的会话/研究/任务与结果目录，绑定本次明确核验的科研发布。
- 既有远端权重、库和持证软件可以作为显式外部资产使用，必须记录路径和身份。
- 旧任务继续用原来冻结的环境，不因新工程建立而改写。

隔离服务启动配置由 `ops/source-baseline/web.py prepare` 在外部 runtime 生成。当前服务已启动；科研执行配置仍待登记验证。不要执行旧 launchd 安装文件。

`requirements-host.lock` 固定已验证的本地 Python 依赖（含测试与受体准备）；`requirements-host.constraints` 记录锁定时需保留的已测试版本。

Ligand-first 调度器默认从 PATH 调用 `inspire`。如安装位置不在 PATH，可设置 `INSPIRE_CLI` 为安装方的可执行文件绝对路径；该值仅是程序路径，不包含命令参数。新任务仍需显式绑定数据根和科研发布，不能由 CLI 路径推断任务目录。

旧版回执如未保存数据根，须由部署方显式配置 `COVSCREEN_LEGACY_ROOT`；旧运行库布局通过 `COVSCREEN_LEGACY_BASE` 配置；无显式发布绑定的历史任务还需 `COVSCREEN_LEGACY_RELEASES` 指定原发布目录。缺失配置时拒绝恢复，不猜测个人账户目录。切换新部署前须从原外部配置迁移这些值，历史冻结代码和回执保持原样。

旧筛选 worker 没有显式 `research_inputs` 的历史计划，还需在宿主安装环境配置 `COVSCREEN_LEGACY_SHARED_ROOT` 为原共享根；宿主仍按原布局核对精确库、受体和科学配置路径及哈希。带显式输入角色的计划继续依据其冻结角色校验，不使用此兼容设置。该值必须与历史 worker 的原部署一致，不用于把旧任务改指到其他数据。

旧筛选连接器可通过 `COVSCREEN_SCREENING_CONFIG` 读取源码外配置，其中 `shared_root` 显式登记共享根；新计划冻结此字段，旧计划只在明确的兼容环境中恢复。远端 worker 同样要求 `COVSCREEN_LEGACY_SHARED_ROOT`，不能独立猜测账号目录。

历史 Library MD 注册模块使用 `COVSCREEN_MD_ROOT` 选择外部安装根，且要求 `COVSCREEN_MD_REMOTE_PREFIX` 为原远端根前缀（以 `/` 结尾）。目录注册、输入哈希、固定体系与预算检查仍生效。两项配置未迁移并核验前不能启动历史批次；本次 ligand-first 发布仍不启用 MD。

使用旧 CarsiDock 运行库激活脚本时，部署环境必须提供 `COVSCREEN_RUNTIME_ROOT`，其目录包含已安装的 `python311_runtime`、`carsidock_gpu_env` 和 `carsidock_runtime_libs`。`run_chunk.py` 要求显式 `--carsidock-repo` 与 `--checkpoint`；注册运行器已传递这些参数。两个历史 CovDock 分区脚本要求部署环境提供 `PROJECT_DIR`、`SCHRODINGER_ROOT`、`SCHROD_LICENSE_FILE` 和 `PYTHON_BIN`。这些脚本不是新的自动科研入口，不能用它们绕过工作流冻结和原生授权。
