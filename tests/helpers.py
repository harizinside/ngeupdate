"""Shared test fixtures: key/auth headers, free-port helper, fake-compose,
HTTP request helpers, server starter.

Imported by every test_* module — keeps the per-test boilerplate to a
minimum.
"""
import json
import os
import socket
import threading
import time
import urllib.parse
from http.client import HTTPConnection

import ngeupdate
import ngeupdate.handler
import ngeupdate.jobs
from ngeupdate.tokens import _sign


KEY = "test-secret-key-please-rotate-0123456789"
# Every test that talks to /mcp goes through this header. The raw KEY is no
# longer accepted there; we mint a signed access token once at import time so
# individual tests stay one-liners.
AUTH = "Bearer " + _sign({"kind": "at", "exp": time.time() + 3600}, KEY)
BAD_AUTH = "Bearer wrong-key"


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(root, port, key=KEY, timeout=60):
    """Spin up a real NgeupdateHandler on a free port for HTTP tests."""
    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.key = key
    cfg.allowed_root = root
    cfg.deploy_timeout = timeout
    cfg.host = "127.0.0.1"
    cfg.port = port
    cfg.health_timeout = 90

    handler = ngeupdate.handler.NgeupdateHandler
    handler.config = cfg
    handler.deploy_lock = ngeupdate.jobs.DeployLock()
    handler.job_store = ngeupdate.jobs.JobStore()

    httpd = ngeupdate.handler.ThreadingHTTPServer(("127.0.0.1", port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def _post_raw(port, body, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    conn.request("POST", "/mcp", body=body.encode("utf-8"), headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _post_with_declared_length(port, declared_length, headers=None):
    """Send a POST that *claims* declared_length bytes of body but transmits
    almost nothing. Used to verify the server rejects oversized Content-Length
    before reading the body."""
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {
        "Content-Type": "application/json",
        "Content-Length": str(declared_length),
    }
    if headers:
        hdrs.update(headers)
    body = b"{}"
    conn.request("POST", "/mcp", body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _post_json(port, payload, headers=None):
    return _post_raw(port, json.dumps(payload), headers=headers)


def _post_form(port, path, fields, content_type=None):
    """POST application/x-www-form-urlencoded (or override the Content-Type).

    Returns ``(status, headers_dict, body_text)``.
    """
    body = urllib.parse.urlencode(fields).encode("utf-8")
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {
        "Content-Type": content_type or "application/x-www-form-urlencoded",
        "Content-Length": str(len(body)),
    }
    conn.request("POST", path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    headers = {k: v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, headers, data


def _post_json_to(port, path, payload, headers=None):
    """POST JSON. Returns ``(status, headers, body_text)``."""
    body = json.dumps(payload).encode("utf-8")
    conn = HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json", "Content-Length": str(len(body))}
    if headers:
        hdrs.update(headers)
    conn.request("POST", path, body=body, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    rhdrs = {k: v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, rhdrs, data


def _make_fake_compose(dir_):
    # Any of the recognised filenames is enough to pass the gate.
    with open(os.path.join(dir_, "compose.yaml"), "w") as fh:
        fh.write("services: {}\n")
    return dir_