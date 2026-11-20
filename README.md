# ksyun-cli

通过 Codex 或命令行访问金山云星泓训推平台。可以查看队列配额和节点余量，创建或复用开发环境和分布式训练任务，以及查询 Pod、事件和训练日志。

## 使用

在 macOS 或 Linux 上使用 Python 3.10 或更高版本。将仓库克隆到 Codex 的 skills 目录，即可在对话中使用 `$ksyun-cli`。

```bash
git clone https://github.com/Astribot-Cognition/ksyun-cli.git ~/.codex/skills/ksyun-cli
cd ~/.codex/skills/ksyun-cli
python3 -BS scripts/ksyun_cli.py --region cn-northwest-3 resources pools
```

联网操作读取当前账号的金山云 CLI profile，也可以通过 SSH 在已有凭证的开发机运行。Python 依赖随仓库提供并通过 SHA-256 校验。[凭证与远程执行](references/runtime.md)

| 场景 | 说明 |
|---|---|
| 检查一项多机训练能否调度 | [资源池、节点与队列配额](references/resources.md) |
| 根据配置或历史任务创建开发机和训练任务 | [预览、提交与重试](references/create.md) |
| 查训练进度、调度失败原因或开发机连接信息 | [状态、事件与日志](references/jobs.md) |

## 离线验证

```bash
python3 -BS -m unittest discover -s tests
```
