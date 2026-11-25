# Library-first OpenScience 技能包

本包为现有平台接口提供可加载的路线与分阶段技能，不修改筛选算法、扩大计算范围或创建新科研任务。源码技能放在 `skills/`，通过平台原生 `/skill/:name` 写入用户技能目录并刷新技能目录；参考文件一同安装，无需重启平台。

| 技能 | 工具及内容 |
|---|---|
| `library-first-screening` | 路线入口、适用范围、工具选择、状态与证据 |
| `library-first-prescreen` | CovInDB 已知证据/虚拟库区分、RDKit SMARTS、硬门槛、三通道选样 |
| `library-first-docking` | CarsiDock-Cov、RTMScore、候选身份、失败保留、报告与交付 |
| `library-first-refinement` | 子集选择、AF3、CovDock-LO、结构复核与精筛交付 |
| `library-first-md-analysis` | 固定八候选 OpenMM 批次、轨迹分析、图表与科学解释 |

平台自然语言例子：“请按照 Library-first skill 检查已有任务，解释筛选流程和当前结果，不启动新计算。”也可在技能菜单中按上述名称选择。执行新任务仍使用实际请求和主机资源授权流程。

## 维护与安装

从 `openscience-custom/backend/cli` 运行：

```sh
bun integrations/library-first-skills/validate.ts
bun integrations/library-first-skills/install.ts
```

验证使用平台实际 Markdown/Skill schema、安全检查和计算工具的 Zod 参数 schema；examples.json 含合法与应拒绝的请求，仅解析、不执行。安装调用原生技能写入 API，独立回读技能列表与内容。保留其他技能、插件、模型配置及服务。此安装本身不注册新的计算引擎。

源码真源为本目录。修改技能后重新验证、安装；不要只改用户目录而遗漏源码。历史运行记录位于工作区 `reports/library-first-skills-20261009/`；后续安装写入 `reports/library-first-skills/installation-<时间>.json`，不覆盖历史回执。既有 `science/.claude/skills` 属于旧 MCP 体系，本次不将其旧接口直接启用到当前项目。

## 验证边界

静态格式、参数 schema、真实技能加载和运行平台发现的验收，不等于新的 GPU 任务或任意靶点端到端验证。已验证科学批次及源码位置由入口技能的 references 维护；新工具或范围变更应同时更新该表、参数示例和阶段技能。
