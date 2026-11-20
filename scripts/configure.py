#!/usr/bin/env python3
"""Check the active Kingsoft credentials or initialize them in the user's terminal."""
import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import sys
import warnings

from cloud import profile_config
from create import store_json


REGION_HINT = "请用 --region 指定金山云地域，或设置 KSYUN_REGION。"
TTY_HINT = "请在自己的终端运行 python3 -BS scripts/configure.py 输入 AK/SK（输入隐藏）。"


def save_profile(name, before, updates):
    path = Path(os.environ.get("KSYUN_CONFIG_FILE", "~/.ksyun/config.json")).expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        # Read and merge under the same lock so concurrent setup sessions preserve other profiles.
        fcntl.flock(directory, fcntl.LOCK_EX)
        exists = path.exists()
        config = json.loads(path.read_text(encoding="utf-8")) if exists else {"current": name, "profiles": []}
        rows = config["profiles"]
        matching = [row for row in rows if row["name"] == name]
        if len(matching) > 1 or (matching[0] if matching else {}) != before:
            raise ValueError("配置已变更，请重新运行配置检查。")
        if matching:
            matching[0].update(updates)
        else:
            rows.append({"name": name, **updates})
        store_json(path, config, replace=exists)
    finally:
        os.close(directory)


def configure(profile=None, region=None, check=False):
    selected = profile or os.environ.get("KSYUN_PROFILE")
    region = region or os.environ.get("KSYUN_REGION")
    env_ak = os.environ.get("KSYUN_ACCESS_KEY_ID")
    env_sk = os.environ.get("KSYUN_SECRET_ACCESS_KEY")
    if not selected and (env_ak or env_sk):
        if not (env_ak and env_sk):
            raise ValueError("环境变量凭证不完整。请同时设置 KSYUN_ACCESS_KEY_ID 和 KSYUN_SECRET_ACCESS_KEY，"
                             "或清除这两项后使用 profile。")
        if not region or region == "others":
            raise ValueError(REGION_HINT)
        return f"已找到环境变量凭证，地域 {region}。"

    name, row = profile_config(selected)
    region = region or row.get("region")
    missing_region = not region or region == "others"
    missing_keys = [key for key in ("access_key_id", "access_key_secret") if not row.get(key)]
    if check or not sys.stdin.isatty():
        problems = []
        if missing_keys:
            problems.append(TTY_HINT)
        if missing_region:
            problems.append(REGION_HINT)
        if problems:
            raise ValueError(" ".join(problems))
    if not check:
        updates = {}
        if missing_region:
            region = input("金山云地域（例如 cn-northwest-3）：").strip()
            if not region or region == "others":
                raise ValueError(REGION_HINT)
        if row.get("region") in (None, "", "others"):
            updates["region"] = region
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            for key in missing_keys:
                label = "Access Key ID" if key == "access_key_id" else "Secret Access Key"
                value = getpass.getpass(f"{label}（输入隐藏）：").strip()
                if not value:
                    raise ValueError(f"{label} 不能为空。")
                updates[key] = value
        if updates:
            save_profile(name, row, updates)
    return f"凭证已就绪，profile {name}，地域 {region}。"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report local configuration status without prompting or writing")
    parser.add_argument("--profile", help="Kingsoft CLI profile")
    parser.add_argument("--region", help="Kingsoft region")
    args = parser.parse_args()
    try:
        print(configure(args.profile, args.region, args.check))
    except (ValueError, OSError, EOFError, getpass.GetPassWarning, KeyError, TypeError) as exc:
        if isinstance(exc, getpass.GetPassWarning):
            print(TTY_HINT, file=sys.stderr)
        else:
            print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
