# Library 计划传输的参数长度缺陷

2026-11-03 只读检查原验收任务：原任务已取消，四个阶段均为 WAITING、无调度句柄。其计划有 660 项资产，JSON 为 194865 字节；原 `prepare` 把计划和摘要整体 base64 后作为单个参数，参数为 259936 字节。

在原验收 Notebook 使用不含任务信息的固定字符串调用 `/bin/true`：100 字节成功，260000 字节失败，errno 7 / `Argument list too long`，系统页大小 4096。这证明原传输方式存在确定的提交前缺陷。旧日志只保留通用 `CommandFailed`，不能据此排除其他问题；必须在新版本网页任务上继续复验。

源码修复：宿主使用 zlib 压缩后发送 `freeze-compressed`，压缩前最大 4 MiB，base64 后最大 64 KiB，整个远端命令最大 96 KiB。超限在本地明确转入需审阅状态，不循环发送。worker 限制解压大小，并拒绝截断、尾随内容及错误字段；解压后继续使用原摘要及不可变计划验证。旧 `freeze` 入口保留以读取和测试历史协议，不改变停用引擎策略。

宿主和 worker 必须随统一固定发布一起更新。当前未部署，也未向远端发送本次原私有计划，未恢复旧任务或提交计算。源码外只保存诊断摘要和测试记录。

验证：大计划的 TypeScript 编码 → 实际 Python 解码与摘要核对、发送前超限拒绝两项通过；worker 九项通过，覆盖解压边界、摘要错误、准确写入和重复冻结；后端类型检查及三项停用策略检查通过。

完整旧执行器测试为 34 通过 / 31 失败。失败涉及旧 CarsiDock 流程被现行策略截停后，原测试仍期待完整执行、失败报告或结果交付；未通过移除策略恢复这些测试。它们必须随 Library VS 流程迁移而更新，不能把本次定向测试当作 Library 完整验收。

证据位于源码外 `outputs/source-baseline-20261029/`：`library-staging-regression-20261103.log`、`library-staging-targeted-20261103.log`、`library-staging-worker-20261103.log`、`library-argument-size-diagnostic-20261103.json`。
