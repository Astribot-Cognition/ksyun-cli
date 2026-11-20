"""Read-only Kingsoft AICP inventory and distributed-job capacity estimates."""

import argparse
from datetime import datetime, timezone
from itertools import islice
from math import isfinite

from cloud import CloudError


VERSION = "2024-06-12"
# Whitelists are intentional. Storage configurations may contain access credentials.
CATALOG = {
    "pools": ("DescribeResourcePools", "ResourcePoolSet", (
        "ResourcePoolId", "ResourcePoolName", "ResourcePoolType", "Status",
        "GpuNodeNum", "AvailableGpuNodeNum", "GpuNum", "AvailableGpuNum")),
    "nodes": ("DescribeResourcePoolInstances", "ResourcePoolInstanceSet", (
        "InstanceId", "InstanceName", "ResourcePoolId", "InstanceType", "K8sRole",
        "InstanceStatus", "UnSchedulable", "AvailabilityZone", "GpuType", "Cpu", "Gpu", "Memory")),
    "queues": ("DescribeQueues", "QueueSet", (
        "QueueId", "QueueName", "ResourcePoolId", "QueueType", "Status", "Capability",
        "Allocated", "AllowBorrowing", "Reclaimable", "NodeCount", "NodeSpecs", "NodeSelectType")),
    "queue-usage": ("GetQueueMember", "Users", (
        "UserId", "Permission", "CPUAllocated", "GPUAllocated", "MemoryAllocated")),
    "images": ("DescribeImages", "ImageSet", (
        "ImageId", "ImageName", "ImageVersion", "ImageSource", "ImageStatus", "ImageType",
        "ImageDomain", "Namespace", "ImageRepo", "PythonVersion", "CudaVersion")),
    "storage": ("DescribeStorageConfigs", "StorageConfigSet", (
        "StorageConfigId", "StorageConfigName", "Type", "MountPath", "Prefetch")),
}


def _positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return value


def _nonnegative(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return value


def register(subparsers):
    group = subparsers.add_parser("resources", help="Resource pools, quotas, images and storage")
    modes = group.add_subparsers(dest="resource_kind", required=True)
    for kind in CATALOG:
        parser = modes.add_parser(kind)
        parser.add_argument("--limit", type=_positive, help="Return up to N matching records")
        if kind == "nodes":
            parser.add_argument("--pool", required=True)
        elif kind == "queues":
            parser.add_argument("--pool", help="Filter by exact resource pool ID")
        elif kind == "queue-usage":
            parser.add_argument("--queue", required=True)
        elif kind in ("images", "pools"):
            parser.add_argument("--name", help="Filter by name using the provider's search")
        parser.set_defaults(handler=list_resources)
    capacity = modes.add_parser("capacity", help="Estimate whether a job fits a pool and queue")
    capacity.add_argument("--pool", required=True)
    capacity.add_argument("--queue", help="Optionally check the queue's own quota")
    capacity.add_argument("--gpu-type", help="Platform model code, e.g. GM402")
    capacity.add_argument("--nodes", type=_positive, default=1)
    capacity.add_argument("--gpus-per-node", type=_nonnegative, default=1)
    capacity.add_argument("--cpu-per-node", type=_positive)
    capacity.add_argument("--memory-gib-per-node", type=_positive)
    capacity.set_defaults(handler=capacity_report)


def _rows(api, kind, params):
    action, key, fields = CATALOG[kind]
    for row in api.pages(action, VERSION, params, items=key):
        yield {field: row[field] for field in fields if field in row}


def list_resources(args, api):
    kind = args.resource_kind
    params = {}
    if kind == "nodes":
        params["ResourcePoolId"] = args.pool
    elif kind == "queue-usage":
        params["QueueId"] = args.queue
    elif kind in ("images", "pools") and args.name:
        params["ImageName" if kind == "images" else "ResourcePoolName"] = args.name
    rows = _rows(api, kind, params)
    if kind == "queues" and args.pool:
        rows = (row for row in rows if row.get("ResourcePoolId") == args.pool)
    return list(islice(rows, args.limit))


def _number(value, resource):
    """Read numeric API values; also handle suffixed Kubernetes quantities."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    multiplier = 1
    if isinstance(value, str):
        value = value.strip()
        units = {"cpu": {"m": 0.001}, "memory": {"Ki": 2**-20, "Mi": 2**-10, "Gi": 1, "Ti": 1024}}
        for suffix, scale in units.get(resource, {}).items():
            if value.endswith(suffix):
                value = value[:-len(suffix)]
                multiplier = scale
                break
    try:
        n = float(value) * multiplier
    except (ValueError, OverflowError):
        return None
    if not isfinite(n) or n < 0:
        return None
    return int(n) if n.is_integer() else n


def _remaining(cap, used, unit):
    total, allocated = _number(cap, unit), _number(used, unit)
    return None if total is None or allocated is None else max(0, total - allocated)


def _all_known(checks):
    if False in checks:
        return False
    return None if None in checks else True


def _fit(checks):
    return {False: "insufficient", True: "fits", None: "unknown"}[_all_known(checks)]


def _at_least(available, needed):
    if needed == 0:
        return True
    return None if available is None else available >= needed


def _flag(value):
    if value is True or str(value).lower() == "true":
        return True
    if value is False or str(value).lower() == "false":
        return False
    return None


def _node(row, args):
    free = {}
    for key, unit in (("Gpu", "gpu"), ("Cpu", "cpu"), ("Memory", "memory")):
        usage = row.get(key) or {}
        free[unit] = _remaining(usage.get("Allocatable"), usage.get("Allocated"), unit)
    status = row.get("InstanceStatus")
    role = row.get("K8sRole")
    cordon = _flag(row.get("UnSchedulable"))
    healthy_worker = _all_known([
        status.lower() == "normal" if status else None,
        role.lower() == "worker" if role else None,
        not cordon if cordon is not None else None,
        True if row.get("InstanceId") else None,
    ])
    checks = [healthy_worker, _at_least(free["gpu"], args.gpus_per_node)]
    model = row.get("GpuType")
    if args.gpu_type:
        checks.append(model == args.gpu_type if model else None)
    if args.cpu_per_node:
        checks.append(_at_least(free["cpu"], args.cpu_per_node))
    if args.memory_gib_per_node:
        checks.append(_at_least(free["memory"], args.memory_gib_per_node))
    return {
        "id": row.get("InstanceId"), "gpu_type": model, "status": status, "role": role,
        "cordoned": cordon, "healthy_worker": healthy_worker, "free_gpu": free["gpu"],
        "free_cpu": free["cpu"], "free_memory_gib": free["memory"], "fit": _fit(checks),
    }


def _queue(row, args):
    """Ordinary queue quota headroom; aggregate usage cannot identify a model in a mixed queue."""
    caps = row.get("Capability") or {}
    used = row.get("Allocated") or {}
    gpu_caps = caps.get("GPUInfos")
    gpu_free = None
    if isinstance(gpu_caps, list) and gpu_caps:
        selected = [i for i in gpu_caps if not args.gpu_type or i.get("GPUType") == args.gpu_type]
        amounts = [_number(i.get("GPUNum"), "gpu") for i in selected]
        if not selected and all(i.get("GPUType") for i in gpu_caps):
            gpu_free = 0
        elif selected and len(selected) == len(gpu_caps) and None not in amounts:
            gpu_free = _remaining(sum(amounts), used.get("GPUNum"), "gpu")
    cpu_free = _remaining(caps.get("CPUNum"), used.get("CPUNum"), "cpu")
    mem_free = _remaining(caps.get("MemoryNum"), used.get("MemoryNum"), "memory")
    state = (row.get("Status") or {}).get("State")
    checks = [_at_least(gpu_free, args.gpus_per_node * args.nodes)]
    if args.cpu_per_node:
        checks.append(_at_least(cpu_free, args.cpu_per_node * args.nodes))
    if args.memory_gib_per_node:
        checks.append(_at_least(mem_free, args.memory_gib_per_node * args.nodes))
    own_quota = _fit(checks)
    fit = own_quota
    if state and state.lower() != "normal":
        fit = "insufficient"
    elif (row.get("QueueType") != "normal" or not state
          or (fit == "insufficient" and _flag(row.get("AllowBorrowing")) is not False)):
        fit = "unknown"
    return {"id": args.queue, "type": row.get("QueueType"), "status": row.get("Status"),
            "allow_borrowing": row.get("AllowBorrowing"), "free_quota_gpu": gpu_free,
            "free_quota_cpu": cpu_free, "free_quota_memory_gib": mem_free,
            "own_quota_fit": own_quota, "fit": fit}


def _gpu_totals(nodes):
    """Known free cards on healthy uncordoned workers, counting partially free nodes too."""
    by_model = {}
    unmeasured = 0
    for node in nodes:
        if node["healthy_worker"] is False:
            continue
        if node["free_gpu"] == 0 and not node["gpu_type"]:
            continue
        if node["healthy_worker"] is None or node["free_gpu"] is None:
            unmeasured += int(node["free_gpu"] != 0)
            continue
        model = node["gpu_type"] or "(unreported)"
        by_model[model] = by_model.get(model, 0) + node["free_gpu"]
    return {"known_free_by_gpu_type": by_model, "unmeasured_nodes": unmeasured}


def _unavailable(error):
    return error.status == 403 or error.code in {"AccessDenied", "UnauthorizedOperation", "InvalidAction", "UnsupportedOperation"}


def capacity_report(args, api):
    notes = []
    try:
        raw_nodes = list(_rows(api, "nodes", {"ResourcePoolId": args.pool}))
    except CloudError as error:
        if not _unavailable(error):
            raise
        raw_nodes = []
        notes.append(str(error))
    nodes = []
    seen = set()
    for row in raw_nodes:
        node_id = row.get("InstanceId")
        if node_id and node_id in seen:
            continue
        seen.add(node_id)
        nodes.append(_node(row, args))
    matches = sum(n["fit"] == "fits" for n in nodes)
    unknown = sum(n["fit"] == "unknown" for n in nodes)
    if not nodes:
        pool_fit = "unknown"
        if not notes:
            notes.append("No nodes visible for this resource pool")
    elif matches >= args.nodes:
        pool_fit = "fits"
    elif matches + unknown < args.nodes:
        pool_fit = "insufficient"
    else:
        pool_fit = "unknown"
    report = {
        "region": api.region, "pool": args.pool, "observed_at": datetime.now(timezone.utc).isoformat(),
        "requested": {"nodes": args.nodes, "gpu_type": args.gpu_type, "gpus_per_node": args.gpus_per_node,
                      "cpu_per_node": args.cpu_per_node, "memory_gib_per_node": args.memory_gib_per_node},
        "gpu_headroom": _gpu_totals(nodes) if nodes else None,
        "eligible_nodes": matches if nodes else None, "indeterminate_nodes": unknown if nodes else None,
        "pool_fit": pool_fit, "queue": None, "fit": pool_fit, "nodes": nodes, "notes": notes,
    }
    if args.queue:
        try:
            queue = next((q for q in _rows(api, "queues", {}) if q.get("QueueId") == args.queue), None)
        except CloudError as error:
            if not _unavailable(error):
                raise
            queue = None
            notes.append(str(error))
        if queue is None:
            notes.append("Queue ID is not visible in this region")
            report["queue"] = {"id": args.queue, "fit": "unknown"}
        elif queue.get("ResourcePoolId") != args.pool:
            raise ValueError("Queue does not belong to the selected resource pool")
        else:
            report["queue"] = _queue(queue, args)
        states = (pool_fit, report["queue"]["fit"])
        report["fit"] = next((state for state in ("insufficient", "unknown") if state in states), "fits")
    return report
