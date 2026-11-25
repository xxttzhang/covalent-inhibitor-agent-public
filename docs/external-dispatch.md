# Ligand 网页派发连接

验收发现：原生审批将 `external-inspire` 任务登记为 `QUEUED` 后，没有宿主调用外部派发器。该状态不证明任务已进入 Inspire 资源队列。

宿主现在可通过安装环境变量 `COVALENT_EXTERNAL_DISPATCH_PROFILE` 启用连接。配置必须位于源码与用户项目之外，是仅所有者可读写的普通 JSON 文件。示例字段如下，路径由部署者填写：

```json
{
  "tool": "covalent-discovery_covalent_ligand_design_step",
  "python": "/absolute/local/python",
  "binding": "/absolute/private/dispatch-binding.json",
  "root": "/inspire/acceptance/data",
  "state": "/absolute/private/dispatch-receipts",
  "approval_key": "/inspire/acceptance/data/.secrets/native-approval.key"
}
```

`binding` 使用现有派发器的四字段合同：`release_id`、`manifest_sha256`、`release_root`、`data_root`，且 `data_root` 必须与 `root` 一致。配置只包含密钥路径，不包含密钥内容。

连接在同一工作流的 `start_pose/pose_status`、`start_af3/af3_status`、`start_covdock/covdock_status` 回执之后调用既有 `dispatch_ligand_smoke.py`。该程序继续核验登记任务的原生签名、固定发布、输入与资源边界；宿主不产生额外审批、不修改科学门禁。其他工具和动作不派发。

`start_md_equilibration/md_equilibration_status` 使用独立的 `dispatch_ligand_openmm.py`，继续核验该派发器原有的单候选 CUDA 平衡协议、原生签名及预算。它不把体系准备或短程 QC 派发到 GPU 平衡执行器。两个派发器的本地 Python 导入路径均固定为当前安装源码的 `science/src`，不继承旧工作区的 `PYTHONPATH` 或用户 site-packages。

首次仅针对登记为 `QUEUED` 且没有本地派发回执的任务调用 `submit`。已有回执只调用 `status`。并发以独占锁隔离；进程中断遗留锁时报告需要核对现场，不自动清锁或重复提交。外部派发结果另列于 `external_dispatch`，不把调度成功当作科学结果通过。私有回执目录保存进程观察结果，网页仅显示状态摘要。

这是受限 Ligand 验收派发连接。其他后端、失败调度器替换及科研重试不通过此连接自动执行。它们必须沿各自现有生命周期接入和单独验收。姿态与平衡连接均尚需部署复验；当前单元与进程错误路径验证不能代替新固定宿主的实际网页派发复验，也不能证明 MD 平衡完成。
