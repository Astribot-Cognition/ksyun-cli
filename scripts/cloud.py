"""Versioned, bounded Kingsoft OpenAPI transport. All operations go to fixed TLS hosts."""
import copy
import hashlib
import http.client
import importlib
import json
import os
from pathlib import Path
import re
import socket
import ssl
import sys
import time
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
READ = {
    "DescribeNotebooks", "GetWebIdeUrl", "DescribeNotebookEvents", "DescribeNotebookLog",
    "DescribeTrainJobPods", "DescribeTrainJobEvents", "DescribeTrainJobPodLogs",
    "DescribeResourcePools", "DescribeResourcePoolInstances", "DescribeQueues", "GetQueueMember",
    "DescribeImages", "DescribeStorageConfigs", "DescribeTrainJobAndPodEvents",
}
CREATE = {"CreateTrainJob", "CreateNotebook"}


class CloudError(RuntimeError):
    def __init__(self, message, *, code=None, status=None, definitely_rejected=False, retryable=False):
        super().__init__(message)
        self.code = code
        self.status = status
        self.definitely_rejected = definitely_rejected
        self.retryable = retryable


def load_wheels():
    """Load the exact pure-Python distributions included with the skill."""
    if sys.version_info < (3, 10):
        raise ValueError("ksyun-cli needs Python 3.10+ on the execution host")
    records = json.loads((ROOT / "assets/wheels.lock.json").read_text())
    paths = []
    for name, expected in records.items():
        path = ROOT / "assets/wheels" / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"bundled dependency failed SHA-256 verification: {name}")
        paths.append(str(path))
    sys.path[:0] = paths


def route(action, version):
    if action == "GetLogs" and version == "2020-07-31":
        return "klog", "POST", "/GetLogs"
    api_version = "2025-12-12" if action in {"DescribeTrainJobs", "CreateTrainJob"} else "2024-06-12"
    if version != api_version or action not in READ | CREATE | {"DescribeTrainJobs"}:
        raise ValueError(f"unsupported API {action} {version}")
    method = "POST" if action in CREATE or action == "DescribeTrainJobAndPodEvents" else "GET"
    return "aicp", method, "/"


def profile_config(selected):
    path = Path(os.environ.get("KSYUN_CONFIG_FILE", "~/.ksyun/config.json")).expanduser()
    if not path.exists():
        return selected or "default", {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        name = selected or config["current"]
        row = next((p for p in config["profiles"] if p["name"] == name), {})
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"invalid Kingsoft CLI profile in {path}") from exc
    return name, row


def provider_error(action, status, result):
    error = result.get("Error")
    nested = result.get("Response")
    if not error and isinstance(nested, dict):
        error = nested.get("Error")
    code = result.get("ErrorCode") or result.get("error_code")
    error_present = bool(error or code not in (None, 0, "0", "", "Success"))
    if 200 <= status < 300 and not error_present:
        return None
    if isinstance(error, dict):
        code = error.get("Code") or error.get("Type") or code
    # Error text from gateways can include the full signed URL or a create payload.
    # Only expose identifiers matching the provider's code/RequestId format.
    def safe_id(value):
        return str(value) if value and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", str(value)) else None
    code = safe_id(code) or "APIError"
    rid = safe_id(result.get("RequestId") or result.get("RequestID"))
    retry = status in (408, 429, 500, 502, 503, 504) or code.startswith(("Throttl", "RequestLimit"))
    rejected = status in (400, 401, 403, 404, 405, 422) and not retry
    return CloudError(f"{action}: HTTP {status} {code}" + (f" (RequestId {rid})" if rid else ""),
                      code=code, status=status, definitely_rejected=rejected, retryable=retry)


class Api:
    def __init__(self, region=None, profile=None, timeout=12):
        selected = profile or os.environ.get("KSYUN_PROFILE")
        # An explicitly selected profile must never silently use a different account.
        env_auth = not selected and any(os.environ.get(k) for k in ("KSYUN_ACCESS_KEY_ID", "KSYUN_SECRET_ACCESS_KEY"))
        if env_auth:
            self.profile, cfg = "env", {}
            self._ak = os.environ.get("KSYUN_ACCESS_KEY_ID")
            self._sk = os.environ.get("KSYUN_SECRET_ACCESS_KEY")
        else:
            self.profile, cfg = profile_config(selected)
            self._ak = cfg.get("access_key_id")
            self._sk = cfg.get("access_key_secret")
        self.receipt_identity = hashlib.sha256(self._ak.encode()).hexdigest() if self._ak else None
        self.region = region or os.environ.get("KSYUN_REGION") or cfg.get("region")
        if not self.region or self.region == "others":
            raise ValueError("select a region with --region or KSYUN_REGION (e.g. cn-northwest-3)")
        self.timeout = timeout
        self._sdk_ready = False

    def _send(self, action, service, req):
        import requests
        from ksyun.common.http.request import ResponseInternal
        host = service + ".api.ksyun.com"
        query = req.data if req.method == "GET" else req.uri_params
        url = "https://" + host + req.uri + ("?" + query if query else "")
        prepared = requests.Request(req.method, url, headers={**req.header, "Host": host},
                                    data=req.data if req.method == "POST" else None, auth=req.auth).prepare()
        url_parts = urlsplit(prepared.url)
        path = url_parts.path + ("?" + url_parts.query if url_parts.query else "")
        conn = http.client.HTTPSConnection(host, timeout=self.timeout, context=ssl.create_default_context())
        # An optional loopback SSH forward changes the TCP destination only. TLS,
        # SNI, certificate validation, Host and SigV4 continue to use the official host.
        port = os.environ.get("KSYUN_" + service.upper() + "_RELAY_PORT")
        if port:
            conn._create_connection = lambda address, *a, **kw: socket.create_connection(("127.0.0.1", int(port)), *a, **kw)
        try:
            conn.request(req.method, path, body=prepared.body, headers=dict(prepared.headers))
            response = conn.getresponse()
            raw = response.read()
            try:
                result = json.loads(raw)
            except (UnicodeDecodeError, ValueError):
                result = None
            err = provider_error(action, response.status, result if isinstance(result, dict) else {})
            if err:
                raise err
            if not isinstance(result, dict):
                raise CloudError(f"{action}: unexpected response format", retryable=True)
            # The SDK accepts only 200 internally. The transport has already
            # checked the real status, including 201 Created and error envelopes.
            return ResponseInternal(200, {}, raw.decode("utf-8"))
        except ssl.SSLCertVerificationError:
            raise CloudError(f"{action}: TLS certificate verification failed") from None
        except (OSError, http.client.HTTPException) as exc:
            raise CloudError(f"{action}: {type(exc).__name__} contacting {host}", retryable=True) from None
        finally:
            conn.close()

    def call(self, action, version, params):
        service, method, path = route(action, version)
        if not self._ak or not self._sk:
            raise CloudError(f"credentials unavailable for profile {self.profile}", definitely_rejected=True)
        try:
            if not self._sdk_ready:
                load_wheels()
                self._sdk_ready = True
            from ksyun.common.credential import Credential
            from ksyun.common.profile.client_profile import ClientProfile
            from ksyun.common.profile.http_profile import HttpProfile
            module = importlib.import_module(f"ksyun.client.{service}.v{version.replace('-', '')}.client")
            client_cls = module.AicpClient if service == "aicp" else module.KlogClient
            client = client_cls(Credential(self._ak, self._sk), self.region, ClientProfile(httpProfile=HttpProfile(
                reqMethod=method, endpoint=service + ".api.ksyun.com", reqTimeout=self.timeout, path=path)))
        except (ImportError, ValueError, OSError) as exc:
            raise CloudError(f"local SDK setup failed: {exc}", definitely_rejected=True) from None
        client.request.send_request = lambda request: self._send(action, service, request)
        ctype = "application/json" if method == "POST" else "application/x-www-form-urlencoded"
        attempts = 1 if action in CREATE else 3
        for i in range(attempts):
            try:
                raw = client.call_judge(action, copy.deepcopy(params), ctype)
                return json.loads(raw)
            except CloudError as err:
                if not err.retryable or i + 1 == attempts:
                    raise
                time.sleep(0.5 * (2 ** i))

    def pages(self, action, version, params, *, items, page="Page", size="PageSize", limit=None, page_size=50, start=1):
        number = start
        count = 0
        seen = set()
        if page_size <= 0 or (limit is not None and limit < 0):
            raise ValueError("invalid pagination size/limit")
        while limit is None or count < limit:
            result = self.call(action, version, {**params, page: number, size: page_size})
            rows = result
            for key in items.split("."):
                rows = rows.get(key) if isinstance(rows, dict) else None
            total = result.get("TotalCount")
            if rows is None and str(total) == "0" and count == 0:
                return
            if not isinstance(rows, list):
                raise CloudError(f"{action}: expected list at {items}")
            if not rows:
                if str(total).isdigit() and count < int(total):
                    raise CloudError(f"{action}: empty page before TotalCount; results are incomplete")
                break
            signature = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).digest()
            if signature in seen:
                raise CloudError(f"{action}: server repeated a page; results are incomplete")
            seen.add(signature)
            for row in rows:
                yield row
                count += 1
                if limit is not None and count >= limit:
                    return
            if total is not None and str(total).isdigit():
                if count >= int(total):
                    break
            elif len(rows) < int(result.get(size) or page_size):
                break
            number += 1
