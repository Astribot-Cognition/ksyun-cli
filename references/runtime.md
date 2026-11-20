# 执行环境

运行脚本时从本 skill 所在目录解析路径。在凭证主机直接执行的示例：

```bash
python3 -BS scripts/ksyun_cli.py --region cn-northwest-3 train list --limit 5
```

查询和提交支持显式 `--profile NAME` 或环境变量 `KSYUN_PROFILE`；配置文件路径可由 `KSYUN_CONFIG_FILE` 覆盖。无显式 profile 时，也可使用同时设置的 `KSYUN_ACCESS_KEY_ID` 和 `KSYUN_SECRET_ACCESS_KEY`。账号选择与地域分开，地域优先级为 `--region`、`KSYUN_REGION`、所选 profile 的有效地域。金山云 CLI 的 `others` 需要显式指定地域。

## 通过远程开发机运行

本地 SSH 包装器只需 Python 3.9+，远端需要 3.10+。它将同一份脚本、schema 和固定依赖传入临时目录，在持有 profile 的主机签名。公网 API 从本机可达时使用 `--relay`，分别转发到官方 AICP 和 KLog 域名。SSH 严格校验主机密钥，TLS 校验证书。远端可直接访问云端时可省略 `--relay`。

在 OpenSSH 配置中选用已验证的开发机别名，并把收据保存到属于当前用户的持久化目录。将示例中的主机别名、状态目录和地域替换成实际值。

```bash
python3 ~/.codex/skills/ksyun-cli/scripts/remote.py \
  --host YOUR_SSH_ALIAS --relay \
  --state-dir /path/to/persistent/ksyun-cli-state \
  -- --region cn-northwest-3 train list --limit 5
```

将最后的 `train list --limit 5` 替换为所需子命令。包装器的 `--spec`、`--save-spec`、`--override`、`--command-file` 路径和收据位于远端。需要重放的文件使用持久化绝对路径。其他机器通过已验证的 SSH 别名使用 `--host HOST`，无须覆盖主机地址。

## 离线复验

在 Python 3.10+ 上从 skill 根目录运行：

```bash
python3 -BS -m unittest discover -s tests
```

依赖原始 wheel 随包存放在 `assets/wheels/`，版本、文件名和 SHA-256 记录在 `assets/wheels.lock.json`。测试包含真实 SDK 请求构造和模拟响应，运行测试无需云凭证。SDK 签名、文件校验和传输失败的退出状态可以与平台请求 ID 一并用于定位问题。

## 打包安装

`python3 scripts/package.py /path/to/ksyun-cli.tar.gz` 生成安装包并打印 SHA-256。包只收录 skill、脚本、schema、依赖与测试。另一台机器校验摘要后，把包内的 `ksyun-cli/` 解压到该用户的 Codex skills 目录即可；SSH 配置和金山云凭证沿用目标机器的配置。
