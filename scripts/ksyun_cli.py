#!/usr/bin/env python3
"""Run a versioned Kingsoft AI Platform command."""
import argparse
import json
import sys

from cloud import Api
import create
import jobs
import resources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", help="Kingsoft region, e.g. cn-northwest-3")
    parser.add_argument("--profile", help="named profile in ~/.ksyun/config.json")
    subs = parser.add_subparsers(dest="command", required=True)
    for module in (jobs, create, resources):
        module.register(subs)
    args = parser.parse_args()
    try:
        result = args.handler(args, Api(args.region, args.profile))
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    except (RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
