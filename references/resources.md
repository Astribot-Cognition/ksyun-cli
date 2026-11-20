# 资源、队列与运行环境

以下是附加在 CLI 入口和全局参数后的子命令。所有命令均为只读，地域取当前 CLI 配置。

| 用途 | 子命令 |
| --- | --- |
| 查看资源池 ID、名称、卡数和健康情况 | `resources pools [--name 名称] [--limit N]` |
| 查看指定池每个节点的 GPU 型号、分配量、角色及封锁状态 | `resources nodes --pool POOL_ID [--limit N]` |
| 查看队列所属池、配额、占用、运行与排队数量 | `resources queues [--pool POOL_ID] [--limit N]` |
| 查看指定队列内各成员的资源占用 | `resources queue-usage --queue QUEUE_ID [--limit N]` |
| 按名字找镜像及其平台 ID、版本、状态 | `resources images [--name 名称] [--limit N]` |
| 查询 KPFS、KS3 等存储配置的 ID、类型和挂载路径 | `resources storage [--limit N]` |
| 按节点与队列配额评估本次申请 | `resources capacity --pool POOL_ID [--queue QUEUE_ID] [--gpu-type 型号] [--nodes N] [--gpus-per-node N] [--cpu-per-node N] [--memory-gib-per-node N]` |

## 评估 4 个节点，每节点 8 卡

把任务实际申请的卡型、每节点 GPU/CPU/内存、队列和资源池带入查询。训练程序内的 batch size 和 steps 无需传给容量命令。

```text
resources capacity --pool POOL_ID --queue QUEUE_ID --gpu-type GM402 --nodes 4 --gpus-per-node 8 --cpu-per-node 160 --memory-gib-per-node 1899
```

- `gpu_headroom.known_free_by_gpu_type` 汇总健康且允许调度的 Worker 上已知空闲 GPU，包括不足整机的零散卡。节点可用量按 `Allocatable - Allocated` 计算。资源池列表的 `AvailableGpuNum` 表示健康卡数。
- `eligible_nodes` 只统计同时满足本次所有单节点要求的节点。每台只计一次。省略 CPU 或内存表示只检查填写的资源项。CPU 任务可传 `--gpus-per-node 0`。
- `pool_fit` 是物理节点的当前快照。指定队列后，`queue.own_quota_fit` 评估普通队列自身剩余配额，`fit` 合并节点和队列结果。`fits` 表示已检查的资源够用，`insufficient` 表示至少有一项已知约束不满足，`unknown` 表示数据不足。仅提供池时，请按资源池范围解释结果。
- 启用借用时，队列自身配额不足可能由其他队列补足。混合卡型队列若仅返回已分配 GPU 总数，指定卡型的剩余配额显示为未知。物理队列的专属节点集合、预约、抢占、亲和性等亦可能影响实际调度。任务提交后，以调度事件和 Pod 状态为准。
- 查询资源池或队列缺少相应权限时，容量报告保留可查部分，`notes` 标注缺失信息。列表仅返回选定的显示字段，存储访问密钥不进入输出。

## 官方接口

上述接口版本均为 `2024-06-12`。节点和队列使用不同的配额字段，定位未知值时可查原始结构。

- 资源组列表与节点分配量。[DescribeResourcePools](https://docs.ksyun.com/documents/9160?type=6) · [DescribeResourcePoolInstances](https://docs.ksyun.com/documents/9161?type=6)
- 队列配额与成员用量。[DescribeQueues](https://docs.ksyun.com/documents/9601?type=6) · [GetQueueMember](https://docs.ksyun.com/documents/9926?type=6)
- 镜像与存储配置。[DescribeImages](https://docs.ksyun.com/documents/8107?type=6) · [DescribeStorageConfigs](https://docs.ksyun.com/documents/8088?type=6)
