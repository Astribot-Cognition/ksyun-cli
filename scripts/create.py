"""Prepare and submit a single versioned AICP create request."""
import argparse
import copy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

SCHEMAS = json.loads((Path(__file__).resolve().parents[1] / "references/create-schema.json").read_text())
ACTIONS = {
    "dev": ("CreateNotebook", "2024-06-12", "NotebookName", "NotebookId"),
    "train": ("CreateTrainJob", "2025-12-12", "TrainJobName", "TrainJobId"),
}


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def register(subparsers):
    parser = subparsers.add_parser("create", help="Preview, export or submit one AICP job")
    kinds = parser.add_subparsers(dest="create_kind", required=True)
    for kind in ACTIONS:
        sub = kinds.add_parser(kind)
        source = sub.add_mutually_exclusive_group(required=True)
        source.add_argument("--spec", type=Path, help="JSON Create request body")
        source.add_argument("--from-id", help="Reuse a job in this profile and region")
        sub.add_argument("--override", type=Path, help="JSON merge patch; arrays replace in full")
        sub.add_argument("--name", help="Override the new task name (required when cloning)")
        if kind == "train":
            sub.add_argument("--nodes", type=positive, help="Total PyTorch replicas including Master")
            sub.add_argument("--gpus-per-node", type=positive, help="Set GPUNumber for every role")
            sub.add_argument("--command-file", type=Path, help="Set all role commands (Ray: entrypoint)")
        mode = sub.add_mutually_exclusive_group()
        mode.add_argument("--save-spec", type=Path, help="Export the exact request body, mode 0600")
        mode.add_argument("--submit", action="store_true", help="Issue one create request")
        sub.set_defaults(handler=handle)


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"duplicate JSON field: {name}")
        result[name] = value
    return result


def read_object(path):
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def merged(original, patch):
    """RFC 7396 JSON merge patch."""
    if not isinstance(patch, dict):
        return patch
    result = dict(original) if isinstance(original, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = merged(result.get(key), value)
    return result


def project(value, schema):
    """Retain only fields accepted by Create, recursively, including falsy values."""
    if isinstance(schema, dict) and isinstance(value, dict):
        result = {}
        for key, entry in schema.items():
            name = key.rstrip("!")
            if value.get(name) is not None:
                result[name] = project(value[name], entry)
        return result
    if isinstance(schema, list) and isinstance(value, list):
        return [project(item, schema[0]) for item in value]
    return value


def masked_password(value):
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(re.fullmatch(r"[＊*•●]{3,}", text)) or text.strip("<>[]*()").lower() == "redacted"


def needs_eip(body):
    return body.get("EnablePublicNetworkSSH") or any(
        item.get("EnablePublicNetwork") for item in (body.get("ServiceConfigs") or [])
        if isinstance(item, dict))


def clone_body(kind, source):
    body = project(source, SCHEMAS[kind])
    if kind == "dev":
        if isinstance(body.get("GPUNumber"), (int, float)):
            body["GPUNumber"] = str(body["GPUNumber"])
        if isinstance(body.get("RunOnCPU"), bool):
            body["RunOnCPU"] = str(body["RunOnCPU"]).lower()
        if not body.get("EnableVolume"):
            body.pop("VolumeConfig", None)
        autosave = body.get("AutoSaveConfig")
        if isinstance(autosave, dict) and masked_password(autosave.get("Password")):
            autosave.pop("Password")
    elif not body.get("EnableDeviceHealthCheck"):
        body.pop("DeviceHealthCheckConfig", None)
    return body


def check_schema(value, schema, path):
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        allowed = {key.rstrip("!") for key in schema}
        unknown = sorted(value.keys() - allowed)
        if unknown:
            raise ValueError(f"{path}: unknown fields {', '.join(unknown)}")
        for key, field in schema.items():
            name = key.rstrip("!")
            child = f"{path}.{name}"
            if key.endswith("!") and (name not in value or value[name] in ("", [], None)):
                raise ValueError(f"{child} is required")
            if name in value:
                check_schema(value[name], field, child)
    elif isinstance(schema, list):
        if not isinstance(value, list):
            raise ValueError(f"{path} must be an array")
        for index, item in enumerate(value):
            check_schema(item, schema[0], f"{path}[{index}]")
    elif type(value) is not {"String": str, "Boolean": bool, "Int": int, "Long": int}[schema]:
        raise ValueError(f"{path} must be {schema}")


def require(value, *names):
    for key in names:
        if not value.get(key):
            raise ValueError(f"{key} is required for this configuration")


def check_image(image):
    source = image["ImageSource"]
    if source in ("Official", "Personal"):
        require(image, "ImageId")
    elif source == "ThirdParty":
        require(image, "ImageRegistryId", "ImageRepoId", "ImageTagId")
    else:
        raise ValueError("Set ImageSource explicitly to Official, Personal or ThirdParty")


def check(kind, body):
    check_schema(body, SCHEMAS[kind], kind)
    name = body[ACTIONS[kind][2]]
    if not re.fullmatch(r"[a-zA-Z0-9\u3400-\u9fff_./()-]{1,64}", name):
        raise ValueError("task name must be 1–64 letters, Chinese characters, digits or _./()-")
    if body.get("AccessType", "Creator") not in ("Creator", "QueueMember"):
        raise ValueError("AccessType must be Creator or QueueMember")
    if kind == "dev":
        check_image(body)
        if body.get("EnableSSH"):
            require(body, "SSHAuthorizedKeys")
        if body.get("EnablePublicNetworkSSH"):
            require(body, "EnableSSH")
        if needs_eip(body):
            require(body, "AllocationId")
        if body.get("EnableVolume"):
            require(body, "VolumeConfig")
        gpu_count = body.get("GPUNumber")
        if body.get("GPUType") and not gpu_count:
            raise ValueError("GPUNumber is required when GPUType is set")
        if gpu_count is not None:
            try:
                count = Decimal(gpu_count)
            except InvalidOperation:
                raise ValueError("GPUNumber must be a numeric string") from None
            if not count.is_finite() or not 0 <= count <= 10000:
                raise ValueError("GPUNumber must be between 0 and 10000")
            if count != count.to_integral_value() and not Decimal("0.1") <= count <= Decimal("0.9"):
                raise ValueError("fractional GPUNumber must be between 0.1 and 0.9")
            if count > 0:
                require(body, "GPUType")
        if any(not 0 <= body.get(field, 0) <= 10000 for field in ("CPUNum", "Memory")):
            raise ValueError("CPUNum and Memory must each be between 0 and 10000")
        if not 1 <= body.get("SSHPort", 22) <= 65535:
            raise ValueError("SSHPort must be between 1 and 65535")
        return
    framework = body["Framework"]
    if framework not in ("pytorch", "tensorflow", "custom", "ray"):
        raise ValueError("unknown Framework")
    if framework == "ray":
        require(body, "EntryPointCommand")
    names = set()
    for role in body["Roles"]:
        name = role["RoleName"]
        if name in names:
            raise ValueError(f"duplicate role {name}")
        names.add(name)
        if framework == "pytorch":
            if name not in ("Master", "Worker") or role["Replicas"] < 1:
                raise ValueError("PyTorch requires Master/Worker roles with positive replicas")
            if name == "Master" and role["Replicas"] != 1:
                raise ValueError("PyTorch Master requires exactly one replica")
        if framework != "ray":
            require(role, "RunCommand")
        check_image(role["ImageConfig"])
        resources = role["ResourceConfig"]
        if resources.get("GPUType") and "GPUNumber" not in resources:
            raise ValueError("ResourceConfig.GPUNumber required for GPUType")
        if resources.get("GPUNumber", 0) < 0 or min(resources["CPUNum"], resources["Memory"]) < 0:
            raise ValueError("negative resource request")
    if framework == "pytorch" and "Master" not in names:
        raise ValueError("PyTorch requires a Master role")
    if body.get("JobRunOnCPU") and any(r["ResourceConfig"].get("GPUType") for r in body["Roles"]):
        raise ValueError("JobRunOnCPU conflicts with a role's GPUType")
    cards = [r["ResourceConfig"].get("GPUNumber", 0) for r in body["Roles"]]
    total = sum(n * r["Replicas"] for n, r in zip(cards, body["Roles"]))
    if total > 8 and (total % 8 or any(n not in (1, 2, 4, 8) for n in cards)):
        raise ValueError("Above 8 GPUs, AICP requires a multiple of 8 total and 1, 2, 4 or 8 GPUs per replica")


def train_overrides(body, args):
    if args.nodes is not None:
        if body.get("Framework") != "pytorch":
            raise ValueError("--nodes supports PyTorch; use --override for other frameworks")
        roles = body.get("Roles")
        if not isinstance(roles, list) or not all(isinstance(r, dict) for r in roles):
            raise ValueError("--nodes requires a Roles array")
        names = [r.get("RoleName") for r in roles]
        if sorted(str(n) for n in names) not in (["Master"], ["Master", "Worker"]):
            raise ValueError("--nodes requires one Master and optionally one Worker role")
        master = next(r for r in roles if r["RoleName"] == "Master")
        worker = next((r for r in roles if r["RoleName"] == "Worker"), None)
        if args.nodes == 1:
            roles[:] = [master]
        else:
            if worker is None:
                rank_keys = {"RANK", "NODE_RANK", "GROUP_RANK", "PET_NODE_RANK"}
                fixed_ranks = [env.get("Name") for env in master.get("Envs", [])
                               if isinstance(env, dict) and env.get("Name") in rank_keys]
                if fixed_ranks:
                    raise ValueError("Master sets an explicit rank (" + ", ".join(fixed_ranks)
                                     + "). Provide distinct Worker/Master Envs in --override Roles.")
                worker = copy.deepcopy(master)
                worker["RoleName"] = "Worker"
                roles.append(worker)
            worker["Replicas"] = args.nodes - 1
        master["Replicas"] = 1
    if args.gpus_per_node is not None or args.command_file:
        check_schema(body, SCHEMAS["train"], "train")
        cmd = args.command_file.read_text(encoding="utf-8") if args.command_file else None
        if cmd is not None and not cmd.strip():
            raise ValueError("empty command file")
        for role in body["Roles"]:
            if args.gpus_per_node is not None:
                require(role["ResourceConfig"], "GPUType")
                role["ResourceConfig"]["GPUNumber"] = args.gpus_per_node
            if cmd is not None and body["Framework"] != "ray":
                role["RunCommand"] = cmd
        if cmd is not None and body["Framework"] == "ray":
            body["EntryPointCommand"] = cmd


def prepare(args, api):
    kind = args.create_kind
    source = None
    patch = {}
    if args.from_id:
        if not args.name:
            raise ValueError("provide --name for a cloned job")
        from jobs import find_job
        source = find_job(api, kind, job_id=args.from_id)
        body = clone_body(kind, source)
    else:
        body = read_object(args.spec)
    if args.override:
        patch = read_object(args.override)
        body = merged(body, patch)
    if args.name:
        body[ACTIONS[kind][2]] = args.name
    if kind == "train":
        train_overrides(body, args)
    check(kind, body)
    blockers = []
    if kind == "dev":
        config = body.get("AutoSaveConfig", {})
        if masked_password(config.get("Password")) or (
            body.get("AutoSave") and config.get("ImageType") == "Official" and not config.get("Password")
        ):
            blockers.append("Replace the redacted or missing AutoSaveConfig.Password in a private spec, "
                            "or disable AutoSave and remove AutoSaveConfig.")
        if source and not needs_eip(body):
            body.pop("AllocationId", None)
        elif source and needs_eip(source) and body.get("AllocationId") == source.get("AllocationId"):
            state = str((source.get("Status") or {}).get("State", "")).lower()
            explicit = bool(patch.get("AllocationId"))
            if state not in {"stopped", "deleted"} or not explicit:
                blockers.append("The source notebook already uses this AllocationId. Choose an available EIP "
                                "in --override or disable public SSH/services. For a stopped source, verify "
                                "the EIP was released before explicitly specifying that same AllocationId.")
    return body, blockers


def digest(obj):
    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def store_json(path, data, *, replace):
    """Persist a complete private file. Export never replaces an existing name."""
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(tmp, path)
        else:
            os.link(tmp, path)
        sync_directory(path.parent)
    finally:
        Path(tmp).unlink(missing_ok=True)


def submit(api, action, version, id_key, body, info):
    xdg = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    directory = Path(os.environ.get("KSYUN_CLI_STATE_DIR") or xdg / "ksyun-cli") / "submissions"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fp = digest([getattr(api, "receipt_identity", api.profile), info["fingerprint"]])
    receipt = directory / f"{fp}.json"
    with os.fdopen(os.open(directory / f"{fp}.lock", os.O_CREAT | os.O_RDWR, 0o600), "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if receipt.exists():
            previous = read_object(receipt)
            if previous.get("status") == "submitted" and previous.get("id"):
                return dict(previous, reused_receipt=True)
            if previous.get("status") != "rejected":
                raise ValueError(f"An earlier submission is unresolved. Query job name {info['name']} "
                                 f"and reconcile receipt {receipt} before retrying.")
        record = dict(info, status="pending", submitted_at=timestamp(), receipt=str(receipt))
        store_json(receipt, record, replace=True)
        try:
            response = api.call(action, version, body)
            job_id = response.get(id_key)
            if not isinstance(job_id, str) or not job_id.strip():
                raise ValueError(f"Create response missing {id_key}")
        except Exception as exc:
            if getattr(exc, "definitely_rejected", False):
                record["status"] = "rejected"
                store_json(receipt, record, replace=True)
                raise
            record["status"] = "unknown"
            store_json(receipt, record, replace=True)
            raise RuntimeError(f"Create outcome unconfirmed; query {info['name']} before retrying. "
                               f"Receipt: {receipt} ({type(exc).__name__})") from None
        record.update(status="submitted", id=job_id, request_id=response.get("RequestId"))
        store_json(receipt, record, replace=True)
        return record


def handle(args, api):
    kind = args.create_kind
    action, version, name_key, id_key = ACTIONS[kind]
    body, blockers = prepare(args, api)
    if blockers and args.submit:
        raise ValueError(" ".join(blockers))
    info = dict(action=action, version=version, region=api.region, profile=api.profile, name=body[name_key])
    info["fingerprint"] = digest([api.region, action, version, body])
    if args.submit:
        return submit(api, action, version, id_key, body, info)
    info.update(status="preview", pool=body["ResourcePoolId"], queue=body["QueueName"])
    if blockers:
        info["submit_blockers"] = blockers
    if kind == "dev":
        info["resources"] = {k: body[k] for k in ("GPUType", "GPUNumber", "CPUNum", "Memory") if k in body}
        info.update(image_source=body["ImageSource"], ssh=body.get("EnableSSH", False))
    else:
        info["roles"] = [{
            "name": r["RoleName"], "replicas": r["Replicas"], "resources": r["ResourceConfig"],
            "command_sha256": hashlib.sha256(r.get("RunCommand", "").encode()).hexdigest(),
        } for r in body["Roles"]]
    if args.save_spec:
        store_json(args.save_spec, body, replace=False)
        info["spec_file"] = str(args.save_spec)
    return info
