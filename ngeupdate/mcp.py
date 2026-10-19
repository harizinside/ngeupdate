"""MCP Streamable HTTP surface as a mixin: tool schemas, dispatch,
`tools/call` handlers (deploy, deploy_status), plus the worker thread
that actually runs the deploy.

`run_deploy` lives in `ngeupdate.deploy` — it's deploy orchestration,
not MCP surface, so it's owned by the module that already has the rest
of the pipeline. The handler composes this mixin in alongside
`OAuthRoutes`.
"""
import json
import threading
import time

from .deploy import _log_event, run_deploy
from .jobs import _gen_job_id
from .project import resolve_project_path, _validate_migrate_seed
from .report import _fmt_duration, format_deploy_result


DEPLOY_STATUS_POLL = 25  # long-poll window; well below Cloudflare's 100s hard cap


SERVER_INFO = {
    "name": "ngeupdate",
    "version": "1.0.0",
    "vendor": "ngeupdate",
}

TOOL_SCHEMAS = [
    {
        "name": "deploy",
        "description": (
            "Start a deploy on the production VPS. The call validates "
            "synchronously (path, mode, migrate/seed headers, path lock) and "
            "returns IMMEDIATELY with a job id (e.g. `🚀 STARTED job a1b2c3 "
            "— /srv/apps/myapp (fast). Panggil deploy_status(...) sampai "
            "DONE/FAILED.`) — long `--no-cache` builds will not hit "
            "Cloudflare's 100s 524 cap. Use deploy again whenever the user "
            "asks to \"pull build production\", \"deploy production\", "
            "\"rebuild production\", \"restart production\", or asks for a "
            "no-cache / force-recreate production deploy. If the user says "
            "'no cache' / 'force recreate' / '--no-cache', pass mode='clean'. "
            "If the user asks to also run database migrations, pass "
            "migrate=true (\"deploy + migrate\" / \"pull + migrate\" / "
            "\"apply migration\" / \"db migrate\"). For seeders pass "
            "seed=true (\"deploy + seed\", \"pull + seed\", \"db seed\"). "
            "Both flags need the matching header (X-Migrate-Cmd / X-Seed-Cmd) "
            "plus X-Compose-Service to be set in .mcp.json — without them "
            "the call errors out before git pull runs. Never set "
            "migrate=true or seed=true just because the project has the "
            "config; only when the user asked. "
            "After calling deploy, IMMEDIATELY call deploy_status(job_id=...) "
            "in a loop until the response is the final ✅ DONE / ❌ FAILED "
            "output. Show that final output's first status line to the user, "
            "plus any ⚠️ / ℹ️ notice lines that follow."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Project path on the VPS. Absolute (/srv/apps/myapp) or "
                        "relative to ALLOWED_ROOT (e.g. 'myapp'). Omit to use "
                        "the X-Project-Path header from .mcp.json."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "clean"],
                    "default": "fast",
                    "description": (
                        "fast = incremental build; clean = --no-cache build + "
                        "--force-recreate."
                    ),
                },
                "migrate": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Run X-Migrate-Cmd via 'docker compose run --rm -T' "
                        "between build and up. Only set when the user "
                        "explicitly asked for a migration (e.g. 'deploy + "
                        "migrate', 'pull production and migrate', 'apply "
                        "migrations'). Requires X-Migrate-Cmd and "
                        "X-Compose-Service headers; errors out otherwise."
                    ),
                },
                "seed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Run X-Seed-Cmd via 'docker compose run --rm -T' "
                        "between migrate and up. Only set when the user "
                        "explicitly asked for a seed (e.g. 'deploy + seed', "
                        "'db seed'). Requires X-Seed-Cmd and X-Compose-Service "
                        "headers; errors out otherwise."
                    ),
                },
            },
        },
    },
    {
        "name": "deploy_status",
        "description": (
            "Poll the status of a deploy job that was started by the deploy "
            "tool. The deploy tool returned a job id; pass it as job_id. "
            "This call LONG-POLLS up to 25 seconds (well below Cloudflare's "
            "100s 524 hard cap). If the deploy finishes within that window, "
            "the response is the SAME final output you would have seen from "
            "a synchronous deploy — first line '✅ DONE ...' on success or "
            "'❌ FAILED ...' on failure, plus any ⚠️ / ℹ️ migrate-seed notice "
            "lines. If the deploy is still running, the response is a "
            "single '⏳ RUNNING job <id> — step N/M: <command> (<elapsed>)' "
            "line; call this tool again immediately to keep waiting. An "
            "unknown job_id (server restart, retention expiry after 1 hour) "
            "returns 'job tidak ditemukan (expired / server restart)'. "
            "Always keep calling deploy_status in a loop until the final "
            "DONE / FAILED / not-found response, then show that final text "
            "to the user."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": (
                        "The job id returned by the deploy tool, e.g. "
                        "'a1b2c3'."
                    ),
                },
            },
            "required": ["job_id"],
        },
    },
]


# Back-compat alias — some existing tests reach TOOL_SCHEMA["description"]
# / TOOL_SCHEMA["inputSchema"] assuming the dict shape. Keep the FIRST tool's
# schema on the old name so that surface stays a single deploy-tool dict.
TOOL_SCHEMA = TOOL_SCHEMAS[0]


def _jsonrpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_call_text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class McpRoutes:
    """Mixin: MCP HTTP surface (initialize, tools/list, tools/call).

    Composed into the handler alongside `OAuthRoutes`. Methods reach back
    into `self` for `config`, `deploy_lock`, `job_store`, and the HTTP
    helpers (`_send_json`, `_send_text`, `_read_body`, `_auth_ok`,
    `_www_authenticate`).
    """

    # ------------------------------------------------------------------
    # /mcp entry point (called by the handler's do_POST router)
    # ------------------------------------------------------------------
    def _handle_mcp(self):
        # MCP spec: validate Origin against DNS rebinding. CLI/IDE clients
        # send none; a browser always does, and no browser should talk to
        # this. OAuth flow POSTs from /token don't carry an Origin (CLI),
        # so this stays correct for the actual MCP surface.
        from .tokens import _auth_ok
        if self.headers.get("Origin"):
            self._send_json(403, {"error": "origin not allowed"})
            return

        if not _auth_ok(self.headers, self.config.key):
            self._send_json(
                401,
                {"error": "unauthorized"},
                extra_headers={"WWW-Authenticate": self._www_authenticate()},
            )
            return

        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "invalid json-rpc envelope"})
            return

        method = payload.get("method")
        req_id = payload.get("id")
        params = payload.get("params") or {}

        # Notifications: acknowledge with 202 + empty body, no JSON-RPC envelope.
        if isinstance(method, str) and method.startswith("notifications/"):
            self._send_text(202, "")
            return

        if req_id is None:
            # Spec says notifications omit id; we already handled them above.
            self._send_json(400, {"error": "missing id"})
            return

        result, error = self._dispatch(method, params, self.headers)
        if error is not None:
            self._send_json(200, _jsonrpc_error(req_id, error["code"], error["message"]))
        else:
            self._send_json(200, _jsonrpc_result(req_id, result))

    # ------------------------------------------------------------------
    # JSON-RPC dispatch
    # ------------------------------------------------------------------
    def _dispatch(self, method, params, headers):
        if method == "initialize":
            client_version = params.get("protocolVersion") if isinstance(params, dict) else None
            version = client_version if isinstance(client_version, str) else "2025-03-26"
            return (
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                    "instructions": (
                        "The deploy tool runs asynchronously: it validates "
                        "the request synchronously then returns "
                        "immediately with a job id like "
                        "'🚀 STARTED job a1b2c3 — /srv/apps/myapp (fast). "
                        "Panggil deploy_status(job_id=\"a1b2c3\") sampai "
                        "DONE/FAILED.' Call deploy_status(job_id=...) "
                        "in a tight loop until the response is the final "
                        "'✅ DONE ...' / '❌ FAILED ...' (or the unknown-job "
                        "error). Always show that final output's first "
                        "status line to the user, plus any ⚠️ warning or "
                        "ℹ️ info line that follows. The deploy tool also "
                        "supports optional migrate=true and seed=true "
                        "flags. Set them ONLY when the user explicitly "
                        "asked to run migrations or seeders (e.g. "
                        "\"deploy + migrate\", \"db seed\", \"apply "
                        "migration\"). Never set them by default — the "
                        "project has to opt in via X-Migrate-Cmd / "
                        "X-Seed-Cmd / X-Compose-Service headers in "
                        ".mcp.json."
                    ),
                },
                None,
            )

        if method == "ping":
            return ({}, None)

        if method == "tools/list":
            return ({"tools": list(TOOL_SCHEMAS)}, None)

        if method == "tools/call":
            return self._call_tool(params, headers)

        if isinstance(method, str) and method.startswith("notifications/"):
            # Already handled in do_POST, but keep the door closed here too.
            return (None, {"code": -32601, "message": "notification has no response"})

        return (None, {"code": -32601, "message": "method not found: %s" % method})

    # ------------------------------------------------------------------
    # tools/call
    # ------------------------------------------------------------------
    def _call_tool(self, params, headers):
        if not isinstance(params, dict):
            return (None, {"code": -32602, "message": "params must be object"})
        name = params.get("name")
        if name == "deploy":
            return self._call_deploy(params, headers)
        if name == "deploy_status":
            return self._call_deploy_status(params, headers)
        return (
            None,
            {"code": -32602, "message": "unknown tool: %s" % name},
        )

    def _call_deploy(self, params, headers):
        """Validate synchronously, then kick the deploy into a background
        thread and return immediately with a job id. The result is read
        back via ``_call_deploy_status``.

        Pre-flight errors (bad path, bad mode, missing migrate headers,
        busy path, …) are returned here, INSIDE the JSON-RPC response — no
        job is created for those, so ``deploy_status`` wouldn't have
        anything to look up.
        """
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return (None, {"code": -32602, "message": "arguments must be object"})

        arg_path = args.get("path")
        if arg_path in (None, ""):
            arg_path = headers.get("X-Project-Path", "")
        mode = args.get("mode", "fast")
        if mode not in ("fast", "clean"):
            return (
                _tool_call_text(
                    "mode tidak valid: %r (pilih 'fast' atau 'clean')" % mode,
                    is_error=True,
                ),
                None,
            )

        # Strict: "false" (a string) must not turn into True and run a seed.
        migrate = args.get("migrate", False)
        seed = args.get("seed", False)
        if not isinstance(migrate, bool) or not isinstance(seed, bool):
            return (
                _tool_call_text("migrate / seed harus boolean true/false", is_error=True),
                None,
            )

        try:
            project_path = resolve_project_path(arg_path, self.config.allowed_root)
        except ValueError as exc:
            return (_tool_call_text("path tidak valid: %s" % exc, is_error=True), None)

        # Validate migrate / seed config BEFORE any side-effecting step
        # (git pull). All errors here mean nothing ran.
        try:
            service, migrate_argv, seed_argv, migrate_paths, seed_paths = (
                _validate_migrate_seed(migrate, seed, headers)
            )
        except ValueError as exc:
            return (_tool_call_text(exc.args[0], is_error=True), None)

        refused = self.deploy_lock.acquire(project_path)
        if refused:
            return (_tool_call_text(refused, is_error=True), None)

        job_id = _gen_job_id()
        job = {
            "id": job_id,
            "path": project_path,
            "mode": mode,
            "migrate_requested": migrate,
            "seed_requested": seed,
            "service": service,
            "migrate_paths": list(migrate_paths or []),
            "seed_paths": list(seed_paths or []),
            "started_at": time.time(),
            "completed_at": None,
            "status": "running",
            "steps": [],
            "current": None,
            "diff_files": [],
            "ok": None,
            "total_seconds": None,
            "total_steps": 0,
            "lock": threading.Lock(),
            "ip": headers.get("CF-Connecting-IP") or self.address_string() or "",
        }

        job_id = self.job_store.create(job)
        started_at = job["started_at"]

        def _run_in_thread():
            try:
                try:
                    steps, ok, total, diff_files = run_deploy(
                        project_path,
                        mode,
                        per_step_timeout=self.config.deploy_timeout,
                        service=service,
                        migrate_argv=migrate_argv,
                        seed_argv=seed_argv,
                        health_timeout=self.config.health_timeout,
                        job=job,
                    )
                    with job["lock"]:
                        job["steps"] = steps
                        job["ok"] = ok
                        job["total_seconds"] = total
                        job["diff_files"] = diff_files
                except Exception as exc:  # pragma: no cover - defensive
                    err_step = {
                        "command": ["internal-error"],
                        "returncode": -1,
                        "timed_out": False,
                        "elapsed_seconds": 0,
                        "output_tail": "internal error saat deploy %s: %r"
                        % (project_path, exc),
                    }
                    with job["lock"]:
                        job["steps"] = [err_step]
                        job["ok"] = False
                        job["total_seconds"] = round(time.time() - started_at, 2)
            finally:
                # Lock release happens here (NOT in the handler), so the
                # path is busy until the deploy actually finishes — that's
                # what protects the production box from a stampede.
                try:
                    self.deploy_lock.release(project_path)
                except Exception:
                    pass
                self.job_store.mark_complete(job_id)
                _log_event({
                    "event": "deploy_end",
                    "job": job_id,
                    "path": project_path,
                    "mode": mode,
                    "migrate": bool(migrate),
                    "seed": bool(seed),
                    "ip": job.get("ip", ""),
                    "ok": job.get("ok"),
                    "failed_step": (
                        " ".join(job["steps"][-1]["command"])
                        if job.get("steps") and not job.get("ok")
                        else None
                    ),
                    "seconds": round(time.time() - started_at, 2),
                })

        t = threading.Thread(
            target=_run_in_thread,
            daemon=True,
            name="deploy-%s" % job_id,
        )
        t.start()

        msg = (
            "🚀 STARTED job %s — %s (%s). Panggil "
            'deploy_status(job_id="%s") sampai DONE/FAILED.'
            % (job_id, project_path, mode, job_id)
        )
        _log_event({
            "event": "deploy_start",
            "job": job_id,
            "path": project_path,
            "mode": mode,
            "migrate": bool(migrate),
            "seed": bool(seed),
            "ip": job["ip"],
        })
        return (_tool_call_text(msg, is_error=False), None)

    def _call_deploy_status(self, params, headers):
        """Long-poll the job store. Returns the formatted final output if
        the job finished during the wait, or a "⏳ RUNNING ..." line if the
        25-second window expired first."""
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return (None, {"code": -32602, "message": "arguments must be object"})
        job_id = args.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return (
                _tool_call_text(
                    "job_id wajib diisi (string)",
                    is_error=True,
                ),
                None,
            )

        snapshot = self.job_store.wait_for_terminal(
            job_id, time.time() + DEPLOY_STATUS_POLL
        )
        if snapshot is None:
            return (
                _tool_call_text(
                    "job tidak ditemukan (expired / server restart): %s" % job_id,
                    is_error=True,
                ),
                None,
            )

        if snapshot["status"] != "running":
            text = format_deploy_result(
                snapshot["path"],
                snapshot["mode"],
                snapshot["steps"],
                bool(snapshot["ok"]),
                snapshot["total_seconds"] or 0.0,
                migrate_paths=snapshot.get("migrate_paths") or [],
                seed_paths=snapshot.get("seed_paths") or [],
                migrate_requested=snapshot.get("migrate_requested", False),
                seed_requested=snapshot.get("seed_requested", False),
                diff_files=snapshot.get("diff_files") or [],
            )
            return (_tool_call_text(text, is_error=not snapshot["ok"]), None)

        # Still running after the wait window — show current step.
        current = snapshot.get("current")
        if current:
            elapsed = max(0, time.time() - current["started"])
            line = "⏳ RUNNING job %s — step %d/%d: %s (%s)" % (
                job_id,
                current["idx"],
                current["total"],
                current["label"],
                _fmt_duration(elapsed),
            )
        else:
            steps_done = len(snapshot.get("steps") or [])
            total = snapshot.get("total_steps") or steps_done
            line = "⏳ RUNNING job %s — %d/%d step selesai" % (
                job_id, steps_done, total
            )
        return (_tool_call_text(line, is_error=False), None)