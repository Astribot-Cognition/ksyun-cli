#!/usr/bin/env python3
"""Build a reproducible, portable ksyun-cli installation archive."""
import argparse
import gzip
import hashlib
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
CONTENTS = ("SKILL.md", "agents/openai.yaml", "scripts/*.py", "tests/test_*.py",
            "references/*.md", "references/create-schema.json", "assets/wheels.lock.json", "assets/wheels/*.whl")


def build(output):
    files = sorted(path for pattern in CONTENTS for path in ROOT.glob(pattern))
    with output.open("xb") as dest:
        with gzip.GzipFile(filename="", fileobj=dest, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as tar:
                for src in files:
                    info = tarfile.TarInfo("ksyun-cli/" + src.relative_to(ROOT).as_posix())
                    info.size = src.stat().st_size
                    info.mode = 0o755 if src.name in {"ksyun_cli.py", "remote.py"} else 0o644
                    with src.open("rb") as stream:
                        tar.addfile(info, stream)
    return hashlib.sha256(output.read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, help="new output file")
    args = parser.parse_args()
    print(build(args.output), args.output)
