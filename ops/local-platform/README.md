# 本机平台常驻服务

> 历史部署记录，非当前安装或服务入口。当前 ligand-first 状态见 [发布状态](../../docs/ligand-first-release-status.md)；以下路径已匿名化，不能直接执行。

网页主服务由 macOS 用户级 launchd 托管，不依赖 Codex 对话或终端进程。

- 服务名：`com.openscience.platform`
- 配置：`/path/to/home/Library/LaunchAgents/com.openscience.platform.plist`
- 配置源码副本：本目录 `com.openscience.platform.plist`
- 固定地址：http://127.0.0.1:4096/
- 桌面入口：`/path/to/home/Desktop/打开科研平台.webloc`
- 工作目录：`/path/to/workspace/openscience-custom`
- stdout：`/path/to/home/Library/Logs/OpenScience/platform.stdout.log`
- stderr：`/path/to/home/Library/Logs/OpenScience/platform.stderr.log`

## 日常使用

登录此 Mac 后自动启动。关闭浏览器、终端或 Codex 不会主动关闭服务。服务进程退出后 launchd 自动拉起；设置 30 秒重启节流，避免持续错误时高速重启。

只绑定 127.0.0.1，不向局域网/公网开放。沿用用户原有平台配置、数据库、结果和 Inspire 凭据，不把凭据写入 plist。已有 `com.openscience.inspire-covindb` 查询服务保持不变。

电脑休眠、关机或用户退出登录时，本机平台不能持续工作。这不是全天候服务器；远程已经提交的作业与本地流程控制器是不同进程。未设置防休眠、未更改能源策略。KeepAlive 恢复退出的进程，不等于诊断所有 HTTP 卡顿，更不保证模型 API 或 Inspire 始终可用。

## 只读检查

```sh
launchctl print gui/501/com.openscience.platform
curl --fail --max-time 10 http://127.0.0.1:4096/global/health
lsof -nP -iTCP:4096 -sTCP:LISTEN
```

健康检查通过后还应检查实际页面、项目和工具接口；仅有进程或 health=200 不代表科研任务成功。故障时保留日志，不删除任务数据库。

## 维护与停用

不要再用临时终端启动第二个 4096 服务。维护前确认所有项目没有活跃会话、待审批/待澄清卡，再在用户授权范围内操作；不要停止独立的科研作业或数据库查询服务。

暂时卸载常驻进程（下一次登录仍自动加载）：

```sh
launchctl bootout gui/501/com.openscience.platform
```

恢复：

```sh
launchctl bootstrap gui/501 /path/to/home/Library/LaunchAgents/com.openscience.platform.plist
```

如需长期停用，应先通过明确的维护操作停止服务，再将此 plist 移出 LaunchAgents 并保留备份；本次安装没有创建定时任务、远程作业或电源配置。

代码变更仍需构建/校验与受控重载；自动恢复不负责自动升级代码。系统重启后的自动启动以实际登录和系统后台项目权限为前提；本次验证不通过重启用户电脑来测试。

## 2026-09-28 审批与最终排名配置

经用户授权，launchd 已配置 `COVALENT_APPROVAL_KEY_FILE`（只保存专用密钥路径）、`COVALENT_PIPELINE_ROOT` 和 `COVALENT_POSTPROCESS_PYTHON`。密钥为外部 0600 文件，不包含在 plist 或源码包中；勿把它作为模型工具输入。交付解释器必须包含 pandas、RDKit、pyarrow，缺失时应阻断交付而非输出替代排名。

激活和回退证据见 `../../reports/platform-deployment-20260928.md`；本次没有停止科研计算任务。
