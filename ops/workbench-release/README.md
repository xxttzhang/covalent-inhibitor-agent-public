# 三路线工作台正式维护流程

三个页面使用同一套布局、导航、状态语义和分子展示样式。`workbench-v1.0.0` 封存改造前外观；`workbench-v1.1.0` 为项目配置、统一结果和正式运行服务版本。版本文件代替 Git 标签：当前工作区没有 Git 提交历史，不宣称已经建立 Git tag。

## 为新靶点复用

1. 在平台创建一个项目，获取项目 ID 和工作目录。
2. 运行 `bun ops/workbench-release/configure.ts --directory <工作目录> --project <项目ID> --target <靶点ID> --name <靶点名称>`。缺少 target 时显示“待配置”；靶点存在而无结果时显示“待运行”。此命令拒绝覆盖已有配置。
3. 在该项目 `.discovery/project.json` 里配置名称、位点、标题、阶段说明、候选选择与结果入口。不要复制其他项目的 ID、候选或结构引用。
4. 计算工具将符合 `discovery-result-v1` 的结果原子写入 `.discovery/inbox/<route>.json`。内置服务约每 15 秒校验项目、靶点、路线和数据格式后发布；前端约每 15 秒读取。页面地址为 `/<项目ID>/discovery/library-first`、`ligand-first` 或 `de-novo`。

页面刷新不会启动新的分子生成、对接、GPU 或 MD 计算。

## 标准结果约定

权威可执行定义：`openscience-custom/tooling/util/src/discovery-result.ts`。所有路线使用一个版本化格式。

| 字段 | 约定 |
| --- | --- |
| project / target / route / run | 必填身份；与项目配置严格匹配 |
| collectedAt / evidenceUpdatedAt | 分开记录采集时间和科研证据更新时间，不能用刷新时间伪造新证据 |
| stages | 状态为 pending、running、completed、blocked、failed、skipped；数量、说明、依据；仅有真实总数时给 progress |
| candidates | 不透明且唯一的 ID、SMILES、展示标签、建议、指标、结构和 MD 证据；不从候选编号推断活性 |
| metrics | 数值可为空、单位和定义必填；误差注明 SD/SEM/CI95 与独立样本量 |
| structures | 方法、格式、不可变 artifact ID/version/SHA256 或项目内路径/SHA256、姿势索引及身份映射 |
| analysis / charts | 分析阶段、实际与预期重复数；每张图绑定候选、类型、标题、图注、单位和文件校验值；未验收分析不发布图 |
| reports / sources | 文件引用、哈希和证据时间，用于追溯 |

原有 CovDock、AF3 和 OPLS 多重复研究通过版本化适配器转换；`legacy` 投影仅用于保持已审阅页面，其候选身份与公共字段交叉校验。新工具直接输出公共字段，由通用工作台展示二维结构、PDB/CIF 三维预览、MD 指标和图表；其他已登记结构格式保留校验下载。当前三条研究路线继续支持原有 Maestro 共价构象查看。

现有 MD 适配器适用于已经登记和审计的 OPLS 多重复输出，自动完成输入校验、统计、绘图、报告和缓存恢复。它并非任意 DCD/XTC 文件的通用分析器；新引擎需先完成输入转换与科学验收，或直接发布标准结果。原研究未导出的 RMSF 不会补造，Library-first 与 Ligand-first 无正式轨迹时不会显示 De novo 图表。

## 启动与生命周期

`backend/cli/src/plugin/index.ts` 将 DiscoveryPlugin 列为内置插件。按项目工作目录和项目 ID 启动观察器；配置每轮重新读取，未配置项目不读取其他项目来源。现有研究的外部来源与解释文本位于其 `.discovery/runtime.json`，并与项目和靶点身份绑定。任务结果、配置和 artifact store 是持久数据，不随 UI 发布清空。读取已验收缓存不启动计算；需要重新运行 Python 分析时遵守项目已有信任与沙箱策略，授权不足时报告失败而不绕过执行限制。

冷启动通过 `backend/cli/src/web/assets.ts` 读取 `integrations/live-web-assets/active.json`，逐文件校验当前版本。现有运行进程的更新使用相同服务的不可变编译包；内置与兼容入口共用去重表。历史三个插件入口已转向同一服务，不再是独立全局观察器。

## 发布与恢复

- 前端目录执行 `bun run typecheck` 和 `bun run build -- --outDir dist-workbench`。
- 后端目录运行 typecheck 与 discovery-platform、discovery-monitor、refinement-monitor、md-analysis 的验证。
- 根目录 `bun ops/workbench-release/release.ts` 准备并校验网页及运行服务；加 `--apply` 在会话空闲时更新注册和网页入口，不重启现有服务。原有其他插件保留。版本身份包含网页和运行服务哈希。
- `bun ops/workbench-release/cold-check.ts` 用隔离的数据目录执行两次独立后端冷启动，检查内置服务和正式网页产物，无需临时全局插件，也不触碰生产进程。
- `bun ops/workbench-release/browser-check.ts` 检查三路线桌面、手机、MD 归属及专用复用验收项目。该脚本仅对报告中登记的验收项目配置做变更，不能指向研究项目。
- `python ops/release/release.py build` 封存源码、锁定依赖与构建配置；`baseline.py` 封存源码包、网页包、截图与哈希清单。Python 分析依赖另存锁定清单。

恢复先解包到空目录并校验，禁止直接覆盖研究数据。完整旧版恢复需使用同一标签的源码、网页产物和相容运行配置；不要把旧网页随意拼接到新结果协议。`baseline.py verify <标签>` 已执行临时目录解包和逐文件校验；`restore.py <标签> <空目录>` 输出可检查的恢复目录。运行配置、凭证、原始数据、数据库及 Python/Bun 解释器是外部依赖，应保留并按发布记录单独恢复。当前发布不重启平台。
