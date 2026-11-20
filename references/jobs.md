# 开发环境与训练任务查询

以下为 CLI 的子命令。地域和账号 profile 使用入口脚本的全局参数。命令使用金山云任务 ID。按名称查详情时，只有唯一精确匹配才会继续；同名结果会列出候选 ID。

| 需求 | 子命令示例 |
|---|---|
| 查看运行中的开发机 | `dev list --status running` |
| 查看开发机的镜像、规格和存储挂载 | `dev get --name NAME` |
| 查 SSH 公网和 VPC 连接参数 | `dev connect --id NOTEBOOK_ID` |
| 获取指定有效期内的 Jupyter 与 VS Code 链接 | `dev connect --id NOTEBOOK_ID --kind ide --minutes 15` |
| 排查开发机启动情况 | `dev events --id NOTEBOOK_ID`、`dev logs --id NOTEBOOK_ID --tail 100` |
| 找到最近 24 小时提交的任务 | `train list --since-hours 24 --limit 10` |
| 按名字和状态找任务 | `train list --name NAME --status running` |
| 核对每个角色的副本数、规格、镜像和启动命令 | `train get --id TRAIN_JOB_ID` |
| 查调度事件和各节点的实际状态 | `train events --id TRAIN_JOB_ID`、`train pods --id TRAIN_JOB_ID` |
| 在 Master 日志中确认第 20 步 | `train logs --id TRAIN_JOB_ID --iteration 20 --tail 1000` |
| 查某个 Worker 里最近一小时的异常 | `train logs --id TRAIN_JOB_ID --pod POD_NAME --since-seconds 3600 --tail 500 --contains Traceback` |

SSH 查询输出平台返回的 IP 和端口。IDE 链接包含临时访问令牌，用于用户本人打开。任务日志和启动命令可能包含应用写入的环境信息，向用户反馈时摘录相关行。

`train get` 的状态来自平台。确认训练进度时，`running`、Pod 就绪、调度成功分别按其原始含义报告。用训练日志中实际出现的 step/iteration 核实训练迭代。`--iteration 20` 采用精确数字匹配，允许 `step:20`、`iter 20/160000` 等格式。程序先将同一 Pod 的多行记录展开为日志行，取最后 `--tail` 行，再执行迭代或文本筛选。`lines_in_window` 表示实际参与筛选的行数，`omitted_lines_from_response` 表示为满足行数限制而从已读响应中移除的较早行数。没有匹配行时说明查过的 Pod、时间窗口和行数。指定更大的时间窗口及行数可检查更早的日志，仍受日志保留时间影响。

两个日志命令都支持 `--source auto|aicp|klog`。默认读取任务所属资源池配置，已开启 KLog 的池按 Pod 名精确查询 KLog。其余池使用 AICP，官方接口最多提供最近 30 分钟。无法读取池配置时会尝试 AICP 并附上覆盖范围。训练日志默认选唯一的 Master。显式指定 `--pod` 支持 Worker；Pod 名可从 `train pods` 获取。KLog 查询随结果返回查询的起止毫秒时间戳和命中记录数。`truncated` 表示当前查询窗口是否受取回行数或 AICP 时间上限影响。查早期 iter 20 时可以用 `--tail 3000`，或用 `--until 2026-11-20T23:00:00+08:00 --since-seconds 900` 定位启动后 15 分钟的绝对时间窗。访问 KLog 需要当前身份拥有相应日志项目和日志池的读取权限。

开发机和训练任务列表默认展示最近 20 项，按提交时间排序，`--limit` 调整结果数。训练事件和日志可选 `--pool-id`；已知训练 ID 和池 ID 时可以直接读取事件。使用名称筛选后仍应核对返回 ID。

接口版本使用训练查询 `DescribeTrainJobs` 2025-12-12、其余 AICP 读取 2024-06-12，以及 KLog `GetLogs` 2020-07-31。[星泓 API 目录](https://docs.ksyun.com/products/262) · [训练任务查询](https://docs.ksyun.com/documents/9269?type=6) · [KLog 日志查询](https://docs.ksyun.com/documents/37697?type=3)
