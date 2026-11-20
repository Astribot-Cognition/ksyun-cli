# 创建与复用

所有示例中的 `create` 都是本 skill CLI 的子命令，地域和 profile 使用主命令的全局参数。输入为 UTF-8 JSON。开发机使用 [CreateNotebook 2024-06-12](https://docs.ksyun.com/documents/8096?type=6)，训练使用 [CreateTrainJob 2025-12-12](https://docs.ksyun.com/documents/9268?type=6)。本地字段快照 [create-schema.json](create-schema.json) 包含两种请求的字段、类型、嵌套结构。键尾的 `!` 表示必填，实际请求中省略该标记。

## 从已有任务创建

1. 按用户给出的名称或 ID 定位源任务。核实地域、资源组和队列。
2. 使用 `--from-id` 和新名称复用平台返回的创建配置。用 `--override patch.json` 覆盖字段，使用 JSON Merge Patch 语义，对象递归合并，数组整体替换，`null` 删除字段。
3. 默认输出预览。指定 `--save-spec new.json` 会把完整请求保存到新文件（权限 0600），并输出同一指纹。只有 `--submit` 才会发出创建请求。用户明确要求提交时，使用冻结后的 spec 以确保提交内容与已核对内容一致。

```text
create dev --from-id NOTEBOOK_ID --name debug-v2 --override dev-overrides.json --save-spec dev-v2.json
create dev --spec dev-v2.json --submit
create train --from-id TRAIN_JOB_ID --name train-run-02 --nodes 4 --gpus-per-node 8 --command-file launch.txt --save-spec train-v2.json
create train --spec train-v2.json --submit
```

`--nodes` 是含 Master 在内的 PyTorch 总节点数。只有 Master 的源任务扩为多节点时，Worker 会复制 Master 的资源、命令、环境。平台返回的 IsChiefRole、DefaultPort、AdditionalPort 属于查询字段，创建请求按 RoleName 交由平台分配。手工指定的 rank 环境变量无法直接复用于新增 Worker，需用 patch 提供单独的角色环境。若旧命令写死了节点数、rank 或端口，扩容前应同步调整启动命令。如果两类角色需要不同命令，应在 spec 中分别设置 `Roles[].RunCommand`。`--command-file` 是**完整**的入口命令文本，会作用于所有非 Ray 角色；Ray 使用 `EntryPointCommand`。例如每卡 `--batch_size=12`、`--steps=160000` 应写入实际训练命令，连同目录切换、环境初始化和脚本路径一起保留。预览输出每个角色的命令 SHA-256 以便核对。要查看完整命令，在本地读取冻结的 spec。

复用时会保留允许提交的镜像、挂载、资源和 SSH 设置。环境变量和命令可能含敏感信息，保存 spec 到用户私有目录。公网 SSH 或公网自定义服务需要可绑定的 `AllocationId`。克隆占用该 EIP 的开发机时，预览的 `submit_blockers` 会要求指定新的可用 EIP，或关闭相应公网入口。源已停止且原 EIP 经查已释放时，可在 patch 中显式指定该 EIP。企业版镜像自动保存所用的密码如在历史结果中脱敏或缺失，需在私有 patch 中提供有效凭据，或关闭自动保存并移除对应配置。有待补字段时可导出私有草稿，根据预览提示修改后再提交。第三方镜像要提供三个镜像仓库 ID。若查询结果只有旧版数字镜像来源，依据镜像列表用 `--override` 明确 `ImageSource`。已有开发机的云盘按原类型和大小申请新盘；复用已有 KPFS/KS3 存储需保留相应挂载。

## 从零创建

开发机 spec 示例，先从资源查询结果填入可用的资源组、队列和镜像 ID。

```json
{
  "NotebookName": "debug-v2",
  "ResourcePoolId": "<POOL_ID>",
  "QueueName": "<QUEUE_NAME>",
  "AccessType": "Creator",
  "ImageSource": "Personal",
  "ImageId": "<IMAGE_ID>",
  "GPUType": "GM402",
  "GPUNumber": "1",
  "CPUNum": 16,
  "Memory": 128
}
```

训练 spec 示例，`GPUNumber` 在训练角色内是整数。角色可能使用不同的镜像、资源和命令。

```json
{
  "TrainJobName": "train-v2",
  "ResourcePoolId": "<POOL_ID>",
  "QueueName": "<QUEUE_NAME>",
  "Framework": "pytorch",
  "Roles": [{
    "RoleName": "Master",
    "Replicas": 1,
    "ImageConfig": {"ImageSource": "Personal", "ImageId": "<IMAGE_ID>"},
    "ResourceConfig": {"GPUType": "GM402", "GPUNumber": 8, "CPUNum": 160, "Memory": 1024},
    "RunCommand": "cd /your/repository && bash train.sh"
  }]
}
```

用 `create dev --spec dev.json` 或 `create train --spec train.json` 进行离线预览，覆盖参数时同样可以加 `--nodes`、`--override` 或 `--command-file`。离线预览验证请求的字段、类型、必填项和核心条件，账户授权、镜像可用性及队列配额由云端判定。

## 提交与中断恢复

每次提交先在状态目录中持久化请求指纹，配置指纹由地域、接口、版本和规范化请求内容计算，离线和在线预览一致。请求收据另按 AK 身份摘要及配置指纹加锁去重。成功后重复执行相同请求直接返回已保存的任务 ID。创建接口不提供服务端幂等 token。使用同一 AK、状态目录和冻结 spec 重试可复用收据，同一 AK 的不同 profile 别名共享结果。切换 AK 或状态目录后无法用原收据去重，需先按原任务名和时间核实云端结果。

状态目录可通过 `KSYUN_CLI_STATE_DIR` 指定，默认 `${XDG_STATE_HOME:-~/.local/state}/ksyun-cli`。在可能重建的容器中，请选择该用户可写、支持 POSIX 文件锁与原子重命名的持久化目录。收据包含提交时间、任务名、指纹、ID 和状态，不包含请求正文。云端明确拒绝的请求允许修正后再次提交。

遇到中断、超时、5xx 或响应缺少资源 ID 时，同一指纹会阻止再次发送。使用收据中的时间、任务名、地域、资源组、角色和命令比对查询结果。找到匹配记录就使用其 ID。结果仍不确定时保留原收据，稍后重查。只有已独立确认原请求未创建任务，且用户仍要提交时，才能将该条收据移到备份路径，再按相同 spec 提交。
