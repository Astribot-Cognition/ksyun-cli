"""Read-only AICP development and training job commands."""

import argparse
import re
import time
from datetime import datetime, timedelta, timezone

API = "2024-06-12"
TRAIN_API = "2025-12-12"
CHINA_TIME = timezone(timedelta(hours=8))


def _rows(api, family, name=None, job_id=None):
    if family == "dev":
        params = {"Name": name} if name else {}
        if job_id:
            params["NotebookId.1"] = job_id
        return api.pages("DescribeNotebooks", API, params, items="Notebooks")
    params = {"Sort": "DESC"}
    if job_id:
        params["TrainJobId.1"] = job_id
    if name:
        params["TrainJobName"] = name
    return api.pages("DescribeTrainJobs", TRAIN_API, params, items="TrainJobSet")


def _identity(family):
    return ("NotebookId", "NotebookName") if family == "dev" else ("TrainJobId", "TrainJobName")


def find_job(api, family, *, job_id=None, name=None):
    """Return the original job for internal use, requiring an unambiguous exact match."""
    if bool(job_id) == bool(name):
        raise ValueError("Supply exactly one job ID or exact name.")
    id_key, name_key = _identity(family)
    matches = {}
    for job in _rows(api, family, name, job_id):
        if job_id and job[id_key] == job_id:
            return job
        if name and job[name_key] == name:
            matches[job[id_key]] = job
    if len(matches) > 1:
        raise ValueError(f"Multiple jobs named {name!r} in {api.region}. Select an ID: {', '.join(matches)}")
    if not matches:
        raise ValueError(f"No matching {family} job in {api.region}: {job_id or name}")
    return next(iter(matches.values()))


def _select(args, api):
    return find_job(api, args.family, job_id=args.job_id, name=args.name)


def _pick(record, keys):
    return {key: record[key] for key in keys.split() if key in record}


def _summary(job, family):
    id_key, name_key = _identity(family)
    status = (job.get("Status") if family == "dev" else job.get("JobStatus")) or {}
    return {
        id_key: job[id_key], name_key: job[name_key],
        "State": status.get("State" if family == "dev" else "Status"),
        "SubmitTime": status.get("SubmitTime") or job.get("CreateTime"),
        **_pick(job, "QueueName ResourcePoolId GPUNumber GPUType TotalPodNum"),
    }


def _timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=CHINA_TIME)).timestamp()


def list_jobs(args, api):
    cutoff = None
    if args.since_hours is not None:
        cutoff = datetime.now(timezone.utc).timestamp() - args.since_hours * 3600
    matches = {}
    id_key, _ = _identity(args.family)
    for job in _rows(api, args.family, args.name):
        row = _summary(job, args.family)
        if args.status and (row["State"] or "").casefold() != args.status.casefold():
            continue
        timestamp = _timestamp(row["SubmitTime"])
        if cutoff is not None and (timestamp is None or timestamp < cutoff):
            continue
        matches[row[id_key]] = row
    ordered = sorted(matches.values(), key=lambda j: (_timestamp(j["SubmitTime"]) or 0, j[id_key]), reverse=True)
    return {"jobs": ordered[:args.limit], "matching_count": len(ordered)}


def detail(args, api):
    job = _select(args, api)
    result = _summary(job, args.family)
    result.update(_pick(job, "Description StorageConfigs"))
    if args.family == "dev":
        result.update(_pick(job, "CPUNum Memory RunOnCPU ImageId ImageUrl ImageTagName EnableSSH "
                                "EnablePublicNetworkSSH SSHPort PodIp ExternalIp ServiceConfigs Status"))
    else:
        result.update(_pick(job, "JobStatus Framework Priority EntryPointCommand"))
        result["Roles"] = [_pick(r, "RoleName Replicas ImageConfig ResourceConfig RunCommand RestartPolicy")
                           for r in job.get("Roles") or []]
    return result


def connect(args, api):
    if args.minutes > 720:
        raise ValueError("IDE URLs can be issued for at most 720 minutes.")
    if args.kind == "ide":
        # An explicit ID does not require permission to list all notebooks.
        job_id = args.job_id or _select(args, api)["NotebookId"]
        return api.call("GetWebIdeUrl", API, {"NotebookId": job_id, "ExpirationMinute": str(args.minutes)})
    job = _select(args, api)
    result = _pick(job, "NotebookId EnableSSH EnablePublicNetworkSSH SSHPort")
    if job.get("EnableSSH"):
        result.update(_pick(job, "PodIp"))
        if job.get("EnablePublicNetworkSSH"):
            result.update(_pick(job, "ExternalIp"))
    return result


def _pods(api, job_id):
    return api.pages("DescribeTrainJobPods", API, {"TrainJobId": job_id}, items="TrainJobPodSet",
                     page="Marker", size="MaxResults", start=1)


def pods(args, api):
    job_id = args.job_id or _select(args, api)["TrainJobId"]
    return {"TrainJobId": job_id, "pods": list(_pods(api, job_id))}


def _train_context(args, api):
    if args.job_id and args.pool_id:
        return {"TrainJobId": args.job_id, "ResourcePoolId": args.pool_id}
    job = _select(args, api)
    if args.pool_id and args.pool_id != job.get("ResourcePoolId"):
        raise ValueError("Pool ID conflicts with the selected training job.")
    if not job.get("ResourcePoolId"):
        raise ValueError("The job has no visible ResourcePoolId. Supply --pool-id with --id.")
    return _pick(job, "TrainJobId ResourcePoolId")


def events(args, api):
    if args.family == "dev":
        job_id = args.job_id or _select(args, api)["NotebookId"]
        return api.call("DescribeNotebookEvents", API, {"NotebookId": job_id, "Sort": "DESC", "SortKey": "LastSeen"})
    return api.call("DescribeTrainJobEvents", API, _train_context(args, api))


def _pool(api, pool_id):
    rows = api.pages("DescribeResourcePools", API, {"ResourcePoolId.1": pool_id}, items="ResourcePoolSet")
    for row in rows:
        if row.get("ResourcePoolId") == pool_id:
            return row
    raise ValueError("No accessible resource pool matching the selected job.")


def _klog_lines(api, pool, pod, since, tail, end):
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", pod):
        raise ValueError("Invalid Kubernetes pod name for a KLog query.")
    if not pool.get("LogProjectName") or not pool.get("LogPoolName"):
        raise ValueError("The resource pool has no KLog project or log pool configured.")
    if tail > 10000:
        raise ValueError("KLog accepts up to 10000 results per query. Narrow the time window or --tail.")
    query = {"ProjectName": pool["LogProjectName"], "LogPoolName": pool["LogPoolName"],
             "Query": f'podName:"{pod}"', "From": end - since * 1000, "To": end, "Size": tail, "Offset": 0}
    response = api.call("GetLogs", "2020-07-31", query)
    if not isinstance(response.get("Logs"), list):
        raise ValueError("KLog GetLogs response is missing its Logs array.")
    lines = []
    # GetLogs returns newest records first, including records with equal timestamps.
    records = list(reversed(response["Logs"]))
    if all(isinstance(r, dict) and isinstance(r.get("_timestamp_"), int) for r in records):
        records = sorted(records, key=lambda r: r["_timestamp_"])
    for entry in records:
        if not isinstance(entry, dict) or "podName" not in entry:
            raise ValueError("KLog record has no pod identity field (podName).")
        if entry["podName"] != pod:
            continue
        content = entry.get("message")
        if not isinstance(content, str):
            raise ValueError("KLog record has no log text in its message field.")
        lines.extend(content.splitlines())
    total = response.get("Total")
    if str(total).isdigit():
        truncated = len(records) < int(total)
    else:
        truncated = True if len(records) >= tail else None
    return lines, {"backend": "klog", "PodName": pod, "from_ms": query["From"], "to_ms": end,
                   "effective_since_seconds": since, "truncated": truncated,
                   "returned_records": len(records), "total_records": total}


def _log_target(args, api):
    if args.family == "dev":
        return {"NotebookId": args.job_id or _select(args, api)["NotebookId"]}
    params = _train_context(args, api)
    pods = list(_pods(api, params["TrainJobId"]))
    if args.pod:
        if not any(p.get("PodName") == args.pod for p in pods):
            raise ValueError("Requested pod does not belong to the training job.")
        params["PodName"] = args.pod
    else:
        masters = [p["PodName"] for p in pods if str(p.get("Role")).lower() == "master" and p.get("PodName")]
        if len(masters) != 1:
            raise ValueError("Could not select a unique Master pod. Use train pods, then supply --pod.")
        params["PodName"] = masters[0]
    return params


def _end_ms(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("include an explicit UTC offset (e.g. 2026-11-20T23:00:00+08:00)")
    return int(parsed.timestamp() * 1000)


def logs(args, api):
    params = _log_target(args, api)
    pool = None
    pool_warning = None
    if args.source != "aicp":
        try:
            pool_id = params.get("ResourcePoolId") or _select(args, api).get("ResourcePoolId")
            if not pool_id:
                raise ValueError("Job lacks a resource pool ID for KLog lookup.")
            pool = _pool(api, pool_id)
        except (ValueError, RuntimeError) as exc:
            if args.source == "klog":
                raise
            pool_warning = f"Could not determine KLog availability: {exc}"
    if args.source == "klog" and not (pool and pool.get("EnableKlog")):
        raise ValueError("KLog is disabled for this resource pool.")

    if pool and pool.get("EnableKlog"):
        pod = params["PodName"] if args.family == "train" else params["NotebookId"] + "-0"
        end = args.until if args.until is not None else time.time_ns() // 1000000
        lines, coverage = _klog_lines(api, pool, pod, args.since_seconds, args.tail, end)
    else:
        if args.until is not None:
            reason = f" {pool_warning}" if pool_warning else ""
            raise ValueError("--until requires an accessible KLog-enabled resource pool." + reason)
        action = "DescribeNotebookLog" if args.family == "dev" else "DescribeTrainJobPodLogs"
        seconds = min(args.since_seconds, 1800)
        tail = str(args.tail) if args.family == "dev" else args.tail
        response = api.call(action, API, {**params, "SinceSeconds": seconds, "TailLines": tail})
        content = response.get("PodLogs")
        if not isinstance(content, str):
            raise ValueError(f"{action} returned no PodLogs text (request {response.get('RequestId')}).")
        lines = content.splitlines()
        coverage = {"backend": "aicp", "RequestId": response.get("RequestId"), "effective_since_seconds": seconds,
                    "truncated": True if args.since_seconds > 1800 or len(lines) >= args.tail else False}
        if pool_warning:
            coverage["pool_warning"] = pool_warning

    # KLog limits the number of records; one record may contain many log lines.
    # Both backends expose the same line window to the subsequent text filters.
    omitted = max(0, len(lines) - args.tail)
    if omitted:
        lines = lines[-args.tail:]
        coverage["truncated"] = True
    result = {**params, **coverage, "requested_seconds": args.since_seconds, "requested_tail": args.tail,
              "omitted_lines_from_response": omitted, "lines_in_window": len(lines)}
    if coverage["truncated"] is not False:
        result["coverage_hint"] = ("KLog search is limited to the retained log lines. Increase --tail or use "
                                   "--until and --since-seconds around the submission time."
                                   if coverage["backend"] == "klog" else
                                   "AICP returns at most 30 minutes; increase --tail for more lines, "
                                   "or use an enabled KLog pool for older intervals.")
    if args.iteration is not None:
        # LeRobot and PyTorch metrics commonly use "step:20", "global_step=20" or "Iter 20/…".
        pattern = re.compile(r"\b(?:iter(?:ation)?s?|(?:global[ _-]?)?steps?)\s*[:=#]?\s*0*"
                             + str(args.iteration) + r"(?![\w.])", re.I)
        lines = [line for line in lines if pattern.search(line)]
    if args.contains is not None:
        lines = [line for line in lines if args.contains in line]
    result["lines"] = lines
    return result


def _positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def _by_job(parser):
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", dest="job_id", help="job ID")
    group.add_argument("--name", help="exact, unambiguous job name")


def register(subparsers):
    for family, title in (("dev", "development environments"), ("train", "training jobs")):
        root = subparsers.add_parser(family, help=f"query {title}")
        root.set_defaults(family=family)
        commands = root.add_subparsers(required=True)

        listing = commands.add_parser("list", help="find jobs sorted by most recent submission")
        listing.add_argument("--name", help="platform name filter")
        listing.add_argument("--status", help="platform state, e.g. running")
        listing.add_argument("--limit", type=_positive, default=20, help="max results (default 20)")
        listing.add_argument("--since-hours", type=_positive, help="submitted within this many hours")
        listing.set_defaults(handler=list_jobs)

        show = commands.add_parser("get", help="show job configuration and current status")
        _by_job(show)
        show.set_defaults(handler=detail)

        ev = commands.add_parser("events", help="show platform scheduling and lifecycle events")
        _by_job(ev)
        ev.set_defaults(handler=events)
        log = commands.add_parser("logs", help="read a bounded window of container logs")
        _by_job(log)
        log.add_argument("--source", choices=("auto", "aicp", "klog"), default="auto",
                         help="auto uses the log service configured on the job resource pool")
        log.add_argument("--tail", type=_positive, default=200)
        log.add_argument("--since-seconds", type=_positive, default=86400)
        log.add_argument("--until", type=_end_ms, help="KLog end time as ISO 8601 with UTC offset")
        match = log.add_mutually_exclusive_group()
        match.add_argument("--contains", help="keep lines containing this literal")
        match.add_argument("--iteration", type=_positive, help="match exactly this step/iteration number")
        log.set_defaults(handler=logs)
        if family == "train":
            log.add_argument("--pod", help="exact pod name (defaults to unique Master)")
            for cmd in (ev, log):
                cmd.add_argument("--pool-id", help="resource pool ID; by default resolve from the job")
            ps = commands.add_parser("pods", help="list Master/Worker pods and their states")
            _by_job(ps)
            ps.set_defaults(handler=pods)
        else:
            conn = commands.add_parser("connect", help="get SSH addresses or signed IDE URLs")
            _by_job(conn)
            conn.add_argument("--kind", choices=("ssh", "ide"), default="ssh")
            conn.add_argument("--minutes", type=_positive, default=15, help="IDE URL validity, up to 720")
            conn.set_defaults(handler=connect)
