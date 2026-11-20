#!/usr/bin/env python3
"""Execute the bundled CLI on the SSH host that already holds the Kingsoft profile."""
import argparse
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = """
import os, pathlib, subprocess, sys, tarfile, tempfile
ports = sys.argv[1:3]
if ports[0]:
    os.environ.update(KSYUN_AICP_RELAY_PORT=ports[0], KSYUN_KLOG_RELAY_PORT=ports[1])
if sys.argv[3]:
    os.environ["KSYUN_CLI_STATE_DIR"] = sys.argv[3]
with tempfile.TemporaryDirectory(prefix="ksyun-cli-") as tmp:
    with tarfile.open(mode="r|", fileobj=sys.stdin.buffer) as archive:
        for entry in archive:
            target = pathlib.Path(tmp, entry.name)
            if not entry.isfile() or not target.resolve().is_relative_to(pathlib.Path(tmp).resolve()):
                sys.exit("invalid runtime archive")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as out:
                out.write(archive.extractfile(entry).read())
    path = pathlib.Path(tmp, "scripts", "ksyun_cli.py")
    rc = subprocess.run([sys.executable, "-BS", str(path), *sys.argv[4:]]).returncode
sys.exit(rc)
"""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--host", required=True, help="trusted OpenSSH host alias")
    p.add_argument("--hostname", help="override a stale IP in the SSH host alias")
    p.add_argument("--host-key-alias", help="verified known_hosts identity for that host")
    p.add_argument("--relay", action="store_true", help="reach the official APIs via the local network")
    p.add_argument("--state-dir", help="persistent private directory on the SSH host for submit receipts")
    p.add_argument("command", nargs=argparse.REMAINDER)
    args = p.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@-]*", args.host):
        p.error("supply --host SSH_ALIAS followed by -- and a CLI command")
    ssh = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
           "-o", "UpdateHostKeys=no", "-o", "ConnectTimeout=10", "-o", "ControlPath=none",
           "-o", "ExitOnForwardFailure=yes", "-o", "IdentitiesOnly=yes"]
    if args.hostname:
        ssh.extend(["-o", "HostName=" + args.hostname])
    if args.host_key_alias:
        ssh.extend(["-o", "HostKeyAlias=" + args.host_key_alias])
    ports = ["", ""]
    if args.relay:
        ports = list(map(str, secrets.SystemRandom().sample(range(35000, 60000), 2)))
        for port, service in zip(ports, ("aicp", "klog")):
            ssh.extend(["-R", f"127.0.0.1:{port}:{service}.api.ksyun.com:443"])
    bootstrap = ["python3", "-B", "-c", BOOTSTRAP, *ports, args.state_dir or "", *command]
    ssh.extend([args.host, shlex.join(bootstrap)])
    # Include only runtime files. Credentials, receipts and user manifests stay on their host.
    files = [*sorted((ROOT / "scripts").glob("*.py")), ROOT / "references/create-schema.json",
             ROOT / "assets/wheels.lock.json", *sorted((ROOT / "assets/wheels").glob("*.whl"))]
    with tempfile.TemporaryFile() as bundle:
        with tarfile.open(fileobj=bundle, mode="w") as archive:
            for f in files:
                archive.add(f, arcname=str(f.relative_to(ROOT)), recursive=False)
        bundle.seek(0)
        return subprocess.run(ssh, stdin=bundle).returncode


if __name__ == "__main__":
    raise SystemExit(main())
