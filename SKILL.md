---
name: ksyun-cli
description: 通过金山云星泓 OpenAPI 和 KLog 查询资源池、节点 GPU 余量、队列、镜像和存储，创建或复用开发环境和分布式训练任务，查询任务状态、SSH/IDE 入口、Pod、事件和训练日志。用于金山云开发机和训练任务的自然语言操作。
---

# ksyun-cli

使用随包的 Python CLI，所有子命令输出 JSON。执行主机需要 Python 3.10+。依赖随包提供，每次启动联网操作前校验 SHA-256。地区和 profile 是子命令前的全局参数。先确定账号和地域，示例地域为 `cn-northwest-3`。

## 选择执行入口

- 已有金山云凭证的主机，运行 `python3 -BS scripts/ksyun_cli.py --region REGION ...`，脚本路径相对于本 skill。支持金山云 CLI 的 `~/.ksyun/config.json` 当前 profile 或显式 `--profile NAME`。其他配置和远程用法见 [执行环境](references/runtime.md)。
- 如果凭证保存在远端开发机，使用已验证的 SSH 主机别名调用包装器。SSH 模式中的文件路径在远端解析。保持同一持久目录保存提交收据。[远程调用示例](references/runtime.md#通过远程开发机运行)
- 用户说“给我提交命令”“我来提交”或只要求预览时，输出命令或预览结果。用户要求创建或提交时，可使用 `--submit`。创建示例和测试只运行预览或模拟传输。

## 按任务使用命令

| 用户需求 | 操作 |
|---|---|
| 查余量，能否开 4 节点 × 8 卡 | 用 `resources pools`、`resources queues --pool POOL` 确定目标，再用 `resources capacity --pool POOL --queue QUEUE --gpu-type GM402 --nodes 4 --gpus-per-node 8`。已知 CPU/内存也传入。报告查询时间、够用的整节点数及队列结论。[资源查询](references/resources.md) |
| 查镜像、存储、节点和队列使用量 | 用 `resources images`、`storage`、`nodes`、`queue-usage` 子命令。按名称选择资产时核实唯一 ID；创建参数里的 QueueName 使用队列名称。[资源查询](references/resources.md) |
| 新建或复用开发环境 | 用 `create dev --spec FILE` 或 `create dev --from-id ID --name NAME`。返回配置摘要，提交成功后交付 NotebookId。[创建配置](references/create.md) |
| 查找或连接开发环境 | 用 `dev list`、`dev get`、`dev connect --id ID --kind ssh` 获取状态或当前连接信息。需要 IDE 时用 `--kind ide`，签名链接仅交给提出连接请求的用户。[开发机查询](references/jobs.md) |
| 新建单机或多机训练 | 用 `create train --spec FILE` 或基于历史任务 `--from-id ID --name NAME`。核对 Master/Worker 数量和每角色命令，支持 `--nodes`、`--gpus-per-node`、`--override`、`--command-file`。要精确重放可用 `--save-spec FILE`，再按该文件提交。[创建与恢复](references/create.md) |
| 查最近提交的训练 | 用 `train list --since-hours 24`，按 ID 或确切名称 `train get`。同名任务结合提交时间定位。[训练查询](references/jobs.md) |
| 排查调度或查看训练进度 | 用 `train events`、`train pods` 查调度和各角色状态，`train logs --id ID --iteration 20 --tail 3000` 定位启动日志。日志支持 AICP/KLog 和绝对时间窗口，报告窗口或截断情况。[日志和事件](references/jobs.md) |

当前训练步数以匹配到的日志为准；调度、容器运行状态和训练进度各自引用对应证据。创建结果不确定时按收据和任务查询核对。回复只包含用户需要的配置或日志片段，省略 AK/SK、环境变量中的秘密以及非必要的签名链接。
