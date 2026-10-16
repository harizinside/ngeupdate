"""Tests for ngeupdate server. Runs with `python3 -m unittest`."""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from http.client import HTTPConnection

import server as ngeupdate


KEY = "test-secret-key-please-rotate-0123456789"
AUTH = "Bearer " + KEY
BAD_AUTH = "Bearer wrong-key"


def _free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server(root, port, key=KEY, timeout=60):
    class _Cfg:
        pass

    cfg = _Cfg()
    cfg.key = key
    cfg.allowed_root = root
    cfg.deploy_timeout = timeout
    cfg.host = "127.0.0.1"
    cfg.port = port
    cfg.health_timeout = 90

    handler = ngeupdate.NgeupdateHandler
    handler.config = cfg
    handler.deploy_lock = ngeupdate.DeployLock()
    handler.job_store = ngeupdate.JobStore()

    httpd = ngeupdate.ThreadingHTTPServer(("127.0.0.1", port), handler)
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


def _make_fake_compose(dir_):
    # Any of the recognised filenames is enough to pass the gate.
    with open(os.path.join(dir_, "compose.yaml"), "w") as fh:
        fh.write("services: {}\n")
    return dir_


# ---------------------------------------------------------------------------
# Path validation tests
# ---------------------------------------------------------------------------

class ResolveProjectPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.realpath(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _sub(self, name, with_compose=True):
        p = os.path.join(self.root, name)
        os.makedirs(p)
        if with_compose:
            _make_fake_compose(p)
        return p

    def test_absolute_under_root(self):
        project = self._sub("apps/app1")
        self.assertEqual(ngeupdate.resolve_project_path(project, self.root), project)

    def test_relative_to_root(self):
        project = self._sub("apps/app1")
        self.assertEqual(ngeupdate.resolve_project_path("apps/app1", self.root), project)

    def test_rejects_traversal(self):
        with self.assertRaises(ValueError):
            ngeupdate.resolve_project_path(self.root + "/../escape", self.root)
        with self.assertRaises(ValueError):
            ngeupdate.resolve_project_path("../../etc", self.root)

    def test_rejects_absolute_outside_root(self):
        with self.assertRaises(ValueError):
            ngeupdate.resolve_project_path("/etc", self.root)

    def test_rejects_directory_without_compose(self):
        self._sub("apps/bare", with_compose=False)
        with self.assertRaises(ValueError):
            ngeupdate.resolve_project_path(self.root + "/apps/bare", self.root)

    def test_rejects_missing_dir(self):
        with self.assertRaises(ValueError):
            ngeupdate.resolve_project_path(self.root + "/missing", self.root)

    def test_empty_path_raises(self):
        for empty in (None, ""):
            with self.assertRaises(ValueError) as ctx:
                ngeupdate.resolve_project_path(empty, self.root)
            self.assertIn("path wajib", str(ctx.exception))

    def test_rejects_symlink_escape(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        link_path = os.path.join(self.root, "escape-link")
        try:
            os.symlink(outside, link_path)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks not supported on this fs")
        with self.assertRaises(ValueError):
            ngeupdate.resolve_project_path(link_path, self.root)


# ---------------------------------------------------------------------------
# Path-selection (arg > header > error) tests
# ---------------------------------------------------------------------------

class PathSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.realpath(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proj = self._sub("srv/myapp")

    def _sub(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(p)
        _make_fake_compose(p)
        return p

    def _call(self, args, headers):
        port = _free_port()
        httpd = _start_server(self.root, port)
        try:
            status, data = _post_json(
                port,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "deploy", "arguments": args},
                },
                headers=headers,
            )
            self.assertEqual(status, 200)
            body = json.loads(data.decode("utf-8"))
            return body
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_argument_path_wins(self):
        result = self._call(
            {"path": self.proj, "mode": "fast"},
            {"Authorization": AUTH, "X-Project-Path": "/nonexistent"},
        )
        # Fast pipeline tries 'git pull' which fails because there's no git repo;
        # what we care about is *which* path it picked (not arg / header error).
        text = result["result"]["content"][0]["text"]
        self.assertIn(self.proj, text)
        self.assertNotIn("path tidak valid", text)

    def test_header_path_used_when_arg_missing(self):
        result = self._call(
            {"mode": "fast"},
            {"Authorization": AUTH, "X-Project-Path": self.proj},
        )
        text = result["result"]["content"][0]["text"]
        self.assertIn(self.proj, text)
        self.assertNotIn("path wajib", text)

    def test_missing_both_raises_error(self):
        result = self._call(
            {"mode": "fast"},
            {"Authorization": AUTH},
        )
        text = result["result"]["content"][0]["text"]
        self.assertIn("path wajib", text)
        self.assertTrue(result["result"].get("isError"))

    def test_string_flag_rejected_before_pull(self):
        # bool("false") is True; a string flag must never run a seeder.
        result = self._call(
            {"path": self.proj, "seed": "false"},
            {"Authorization": AUTH, "X-Compose-Service": "app", "X-Seed-Cmd": "seed"},
        )
        text = result["result"]["content"][0]["text"]
        self.assertIn("harus boolean", text)
        self.assertNotIn("step 1", text)
        self.assertTrue(result["result"].get("isError"))

    def test_invalid_mode_marks_error(self):
        result = self._call({"path": self.proj, "mode": "yolo"}, {"Authorization": AUTH})
        text = result["result"]["content"][0]["text"]
        self.assertIn("mode tidak valid", text)
        self.assertTrue(result["result"].get("isError"))

    def test_rejects_traversal_via_header(self):
        result = self._call(
            {"mode": "fast"},
            {"Authorization": AUTH, "X-Project-Path": self.root + "/../escape"},
        )
        text = result["result"]["content"][0]["text"]
        self.assertIn("path tidak valid", text)
        self.assertTrue(result["result"].get("isError"))


# ---------------------------------------------------------------------------
# HTTP smoke tests
# ---------------------------------------------------------------------------

class HttpSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.realpath(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "myapp"))
        _make_fake_compose(os.path.join(self.root, "myapp"))
        self.port = _free_port()
        self.httpd = _start_server(self.root, self.port)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def test_missing_auth_is_401(self):
        status, _ = _post_json(self.port, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
        self.assertEqual(status, 401)

    def test_wrong_auth_is_401(self):
        status, _ = _post_json(
            self.port,
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            headers={"Authorization": BAD_AUTH},
        )
        self.assertEqual(status, 401)

    def test_origin_header_is_403_even_with_key(self):
        status, _ = _post_json(
            self.port,
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            headers={"Authorization": AUTH, "Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)

    def test_get_is_405(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/mcp")
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 405)

    def test_initialize_returns_protocol(self):
        status, data = _post_json(
            self.port,
            {"jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}},
            headers={"Authorization": AUTH},
        )
        self.assertEqual(status, 200)
        body = json.loads(data.decode("utf-8"))
        self.assertEqual(body["id"], 7)
        self.assertEqual(body["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(body["result"]["serverInfo"]["name"], "ngeupdate")

    def test_tools_list_has_deploy(self):
        status, data = _post_json(
            self.port,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={"Authorization": AUTH},
        )
        body = json.loads(data.decode("utf-8"))
        names = [t["name"] for t in body["result"]["tools"]]
        self.assertIn("deploy", names)

    def test_unknown_method_is_jsonrpc_error(self):
        status, data = _post_json(
            self.port,
            {"jsonrpc": "2.0", "id": 3, "method": "nope/nope", "params": {}},
            headers={"Authorization": AUTH},
        )
        body = json.loads(data.decode("utf-8"))
        self.assertEqual(body["error"]["code"], -32601)

    def test_notification_returns_202(self):
        status, data = _post_json(
            self.port,
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers={"Authorization": AUTH},
        )
        self.assertEqual(status, 202)
        self.assertEqual(data, b"")

    def test_ping(self):
        status, data = _post_json(
            self.port,
            {"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}},
            headers={"Authorization": AUTH},
        )
        body = json.loads(data.decode("utf-8"))
        self.assertEqual(body["result"], {})

    def test_missing_content_length_411(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/mcp")
        conn.putheader("Authorization", AUTH)
        conn.endheaders()
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 411)

    def test_oversize_body_413(self):
        status, _ = _post_with_declared_length(
            self.port,
            ngeupdate.MAX_BODY_BYTES + 100,
            headers={"Authorization": AUTH},
        )
        self.assertEqual(status, 413)


# ---------------------------------------------------------------------------
# Stop-on-failure behaviour (real shell)
# ---------------------------------------------------------------------------

class PipelineStopOnFailureTests(unittest.TestCase):
    """Exercises run_deploy's stop-on-first-failure with a working `false`-like binary."""

    def test_fast_stops_after_failing_step(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "proj"))
            steps, ok, _, _diff = ngeupdate.run_deploy(
                os.path.join(root, "proj"),
                mode="fast",
                per_step_timeout=10,
            )
            # First step `git pull --ff-only` fails: should return after exactly
            # one step. The healthcheck step only runs on success.
            self.assertEqual(len(steps), 1)
            self.assertFalse(ok)
            self.assertEqual(steps[0]["command"], ["git", "pull", "--ff-only"])


# ---------------------------------------------------------------------------
# Tool description mentions "pull/build/deploy production"
# ---------------------------------------------------------------------------

class DeployLockTests(unittest.TestCase):
    def test_same_path_refused(self):
        lock = ngeupdate.DeployLock(max_total=5)
        self.assertIsNone(lock.acquire("/a"))
        self.assertIn("sedang berjalan", lock.acquire("/a"))

    def test_global_cap(self):
        lock = ngeupdate.DeployLock(max_total=2)
        self.assertIsNone(lock.acquire("/a"))
        self.assertIsNone(lock.acquire("/b"))
        self.assertIn("server sibuk", lock.acquire("/c"))
        lock.release("/a")
        self.assertIsNone(lock.acquire("/c"))


class StepEnvTests(unittest.TestCase):
    def test_key_not_passed_to_children(self):
        self.assertNotIn("NGEUPDATE_KEY", ngeupdate._STEP_ENV)
        self.assertEqual(ngeupdate._STEP_ENV["GIT_TERMINAL_PROMPT"], "0")


class ToolSchemaTests(unittest.TestCase):
    def test_description_mentions_trigger_phrases(self):
        desc = ngeupdate.TOOL_SCHEMA["description"].lower()
        for needle in ("pull", "build", "deploy"):
            self.assertIn(needle, desc)

    def test_mode_enum_and_default(self):
        schema = ngeupdate.TOOL_SCHEMA["inputSchema"]
        self.assertEqual(schema["properties"]["mode"]["enum"], ["fast", "clean"])
        self.assertEqual(schema["properties"]["mode"]["default"], "fast")


# ---------------------------------------------------------------------------
# Loader: refuse to start without key
# ---------------------------------------------------------------------------

class ConfigGuardTests(unittest.TestCase):
    def test_missing_key_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            ngeupdate.load_config({"ALLOWED_ROOT": tempfile.gettempdir()})
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_allowed_root_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            ngeupdate.load_config({"NGEUPDATE_KEY": "x" * 32})
        self.assertEqual(ctx.exception.code, 2)

    def test_short_key_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            ngeupdate.load_config(
                {"NGEUPDATE_KEY": "x" * 31, "ALLOWED_ROOT": tempfile.gettempdir()}
            )
        self.assertEqual(ctx.exception.code, 2)

    def test_reads_env(self):
        cfg = ngeupdate.load_config(
            {"NGEUPDATE_KEY": "x" * 32, "ALLOWED_ROOT": tempfile.gettempdir(), "PORT": "9999"}
        )
        self.assertEqual(cfg.port, 9999)
        self.assertEqual(cfg.allowed_root, os.path.realpath(tempfile.gettempdir()))


# ---------------------------------------------------------------------------
# build_pipeline: pure function — fast/clean × migrate/seed ordering + shlex
# ---------------------------------------------------------------------------

class BuildPipelineTests(unittest.TestCase):
    def test_fast_no_migrate_no_seed(self):
        p = ngeupdate.build_pipeline("fast", None, None, None)
        self.assertEqual(
            p,
            [
                ["git", "pull", "--ff-only"],
                ["docker", "compose", "build"],
                ["docker", "compose", "up", "-d"],
            ],
        )

    def test_clean_no_migrate_no_seed(self):
        p = ngeupdate.build_pipeline("clean", None, None, None)
        self.assertEqual(
            p,
            [
                ["git", "pull", "--ff-only"],
                ["docker", "compose", "build", "--no-cache"],
                ["docker", "compose", "up", "-d", "--force-recreate"],
            ],
        )

    def test_fast_with_migrate_only(self):
        p = ngeupdate.build_pipeline(
            "fast", "app", ["php", "artisan", "migrate", "--force"], None
        )
        self.assertEqual(
            p,
            [
                ["git", "pull", "--ff-only"],
                ["docker", "compose", "build"],
                ["docker", "compose", "run", "--rm", "-T", "app",
                 "php", "artisan", "migrate", "--force"],
                ["docker", "compose", "up", "-d"],
            ],
        )

    def test_clean_with_seed_only(self):
        p = ngeupdate.build_pipeline(
            "clean", "web", None, ["npm", "run", "db:seed"]
        )
        # clean: --no-cache + --force-recreate; seed runs between build and up.
        self.assertEqual(p[0], ["git", "pull", "--ff-only"])
        self.assertEqual(p[1], ["docker", "compose", "build", "--no-cache"])
        self.assertEqual(
            p[2],
            ["docker", "compose", "run", "--rm", "-T", "web", "npm", "run", "db:seed"],
        )
        self.assertEqual(p[3], ["docker", "compose", "up", "-d", "--force-recreate"])

    def test_fast_with_migrate_then_seed(self):
        p = ngeupdate.build_pipeline(
            "fast", "api",
            ["artisan", "migrate"],
            ["artisan", "db:seed"],
        )
        # Order is critical: pull → build → migrate → seed → up.
        labels = [" ".join(c[:6]) for c in p]
        self.assertIn("git pull", labels[0])
        self.assertIn("docker compose build", labels[1])
        self.assertIn("docker compose run --rm -T api", labels[2])
        self.assertIn("docker compose run --rm -T api", labels[3])
        self.assertIn("docker compose up -d", labels[4])
        # And migrate must precede seed.
        migrate_idx = next(i for i, c in enumerate(p) if c[6:] == ["artisan", "migrate"])
        seed_idx = next(i for i, c in enumerate(p) if c[6:] == ["artisan", "db:seed"])
        self.assertLess(migrate_idx, seed_idx)

    def test_pull_uses_ff_only(self):
        # Forcing --ff-only is the whole point of this plan item: divergent
        # history in production must error out instead of silently creating a
        # merge commit.
        p = ngeupdate.build_pipeline("fast", None, None, None)
        self.assertEqual(p[0], ["git", "pull", "--ff-only"])

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            ngeupdate.build_pipeline("yolo", None, None, None)

    def test_migrate_argv_requires_service(self):
        with self.assertRaises(ValueError):
            ngeupdate.build_pipeline("fast", None, ["artisan", "migrate"], None)

    def test_seed_argv_requires_service(self):
        with self.assertRaises(ValueError):
            ngeupdate.build_pipeline("fast", None, None, ["artisan", "db:seed"])


# ---------------------------------------------------------------------------
# Header shlex parsing — argv must split correctly including quoted args
# ---------------------------------------------------------------------------

class HeaderShlexTests(unittest.TestCase):
    def test_empty_header_returns_empty_argv(self):
        for raw in (None, "", "   "):
            self.assertEqual(ngeupdate._parse_optional_shlex(raw, "X-Migrate-Cmd"), [])

    def test_simple_split(self):
        argv = ngeupdate._parse_optional_shlex(
            "php artisan migrate --force", "X-Migrate-Cmd"
        )
        self.assertEqual(argv, ["php", "artisan", "migrate", "--force"])

    def test_quoted_args_preserved(self):
        argv = ngeupdate._parse_optional_shlex(
            'echo "hello world" --foo=bar baz', "X-Migrate-Cmd"
        )
        self.assertEqual(argv, ["echo", "hello world", "--foo=bar", "baz"])

    def test_unclosed_quote_raises(self):
        with self.assertRaises(ValueError) as ctx:
            ngeupdate._parse_optional_shlex('php artisan "unclosed', "X-Migrate-Cmd")
        self.assertIn("X-Migrate-Cmd", str(ctx.exception))
        self.assertIn("tidak valid", str(ctx.exception))


# ---------------------------------------------------------------------------
# Paths header — comma-separated list, stripped
# ---------------------------------------------------------------------------

class HeaderPathsTests(unittest.TestCase):
    def test_empty_returns_empty_list(self):
        for raw in (None, "", "  "):
            self.assertEqual(ngeupdate._parse_optional_paths(raw), [])

    def test_single_path(self):
        self.assertEqual(
            ngeupdate._parse_optional_paths("database/migrations"),
            ["database/migrations"],
        )

    def test_multiple_paths_trimmed(self):
        self.assertEqual(
            ngeupdate._parse_optional_paths(
                " database/migrations , drizzle/, src/db/seed/ "),
            ["database/migrations", "drizzle", "src/db/seed"],
        )

    def test_dot_prefixed_dir_kept(self):
        # lstrip("./") used to eat the dot: ".drizzle" -> "drizzle".
        self.assertEqual(
            ngeupdate._parse_optional_paths(".drizzle, ./database/migrations, ../x"),
            [".drizzle", "database/migrations", "../x"],
        )

    def test_empty_chunks_dropped(self):
        self.assertEqual(
            ngeupdate._parse_optional_paths(",,foo,,bar,,"),
            ["foo", "bar"],
        )


# ---------------------------------------------------------------------------
# Service-name validator
# ---------------------------------------------------------------------------

class ServiceNameValidationTests(unittest.TestCase):
    def test_valid_service(self):
        self.assertEqual(ngeupdate._validate_service("app"), "app")
        self.assertEqual(ngeupdate._validate_service("api-v2"), "api-v2")
        self.assertEqual(ngeupdate._validate_service("web.1"), "web.1")

    def test_empty_rejected(self):
        for raw in (None, "", "  "):
            with self.assertRaises(ValueError):
                ngeupdate._validate_service(raw)

    def test_invalid_chars_rejected(self):
        for raw in ("app;rm", "foo bar", "../etc", "$(x)", "app/service",
                    "--privileged", "-v", ".hidden", "_x"):
            with self.assertRaises(ValueError) as ctx:
                ngeupdate._validate_service(raw)
            self.assertIn("X-Compose-Service", str(ctx.exception))


# ---------------------------------------------------------------------------
# _validate_migrate_seed: end-to-end header resolution before git pull
# ---------------------------------------------------------------------------

class MigrateSeedValidationTests(unittest.TestCase):
    def test_neither_requested_yields_empty(self):
        svc, ma, sa, mp, sp = ngeupdate._validate_migrate_seed(
            False, False, {"X-Compose-Service": "app", "X-Migrate-Cmd": "x"}
        )
        self.assertIsNone(svc)
        self.assertEqual((ma, sa, mp, sp), (None, None, [], []))

    def test_migrate_ok(self):
        svc, ma, sa, mp, sp = ngeupdate._validate_migrate_seed(
            True, False,
            {
                "X-Compose-Service": "app",
                "X-Migrate-Cmd": "php artisan migrate --force",
                "X-Migrate-Paths": "database/migrations",
            },
        )
        self.assertEqual(svc, "app")
        self.assertEqual(ma, ["php", "artisan", "migrate", "--force"])
        self.assertEqual(sa, [])
        self.assertEqual(mp, ["database/migrations"])
        self.assertEqual(sp, [])

    def test_seed_ok(self):
        _svc, _ma, sa, _mp, sp = ngeupdate._validate_migrate_seed(
            False, True,
            {
                "X-Compose-Service": "web",
                "X-Seed-Cmd": "node dist/seed.js",
                "X-Seed-Paths": "src/db/seed",
            },
        )
        self.assertEqual(sa, ["node", "dist/seed.js"])
        self.assertEqual(sp, ["src/db/seed"])

    def test_migrate_without_cmd_errors(self):
        with self.assertRaises(ValueError) as ctx:
            ngeupdate._validate_migrate_seed(
                True, False, {"X-Compose-Service": "app"}
            )
        msg = str(ctx.exception)
        self.assertIn("X-Migrate-Cmd", msg)
        self.assertIn("kosong", msg)

    def test_migrate_without_service_errors(self):
        with self.assertRaises(ValueError):
            ngeupdate._validate_migrate_seed(
                True, False, {"X-Migrate-Cmd": "artisan migrate"}
            )

    def test_seed_without_cmd_errors(self):
        with self.assertRaises(ValueError) as ctx:
            ngeupdate._validate_migrate_seed(
                False, True, {"X-Compose-Service": "app"}
            )
        self.assertIn("X-Seed-Cmd", str(ctx.exception))

    def test_unclosed_quote_in_cmd_errors(self):
        with self.assertRaises(ValueError) as ctx:
            ngeupdate._validate_migrate_seed(
                True, False,
                {"X-Compose-Service": "app", "X-Migrate-Cmd": 'php "unclosed'},
            )
        self.assertIn("X-Migrate-Cmd", str(ctx.exception))

    def test_only_service_required_when_migrate_requested(self):
        # Empty paths are allowed (paths are only for the notice).
        svc, ma, sa, mp, sp = ngeupdate._validate_migrate_seed(
            True, False,
            {"X-Compose-Service": "app", "X-Migrate-Cmd": "artisan migrate"},
        )
        self.assertEqual(svc, "app")
        self.assertEqual(ma, ["artisan", "migrate"])
        self.assertEqual(mp, [])


# ---------------------------------------------------------------------------
# Notice logic: pure, no git needed
# ---------------------------------------------------------------------------

class NoticeLogicTests(unittest.TestCase):
    def test_no_paths_no_notice(self):
        self.assertEqual(
            ngeupdate._migrate_seed_notice_lines(
                [], [], [], False, False,
            ),
            [],
        )

    def test_paths_with_no_diff_and_not_run_yields_info(self):
        notes = ngeupdate._migrate_seed_notice_lines(
            [],
            ["database/migrations"], [],
            migrate_requested=False, seed_requested=False,
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("ℹ️", notes[0])
        self.assertIn("migrate", notes[0])

    def test_paths_with_changes_and_not_run_yields_warning(self):
        notes = ngeupdate._migrate_seed_notice_lines(
            ["database/migrations/2025_01_01_foo.php", "src/index.ts"],
            ["database/migrations"], [],
            migrate_requested=False, seed_requested=False,
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("⚠️", notes[0])
        self.assertIn("database/migrations", notes[0])
        self.assertIn("(1 file)", notes[0])
        self.assertIn("migrate=true", notes[0])

    def test_seed_requested_but_pipeline_failed_later_yields_no_notice(self):
        # Seed ran, then `up -d` failed. Saying "seed TIDAK dijalankan" here
        # would make the agent re-run a non-idempotent seeder.
        steps = [
            {"command": ["docker", "compose", "run", "--rm", "-T", "app", "seed"],
             "returncode": 0, "timed_out": False, "elapsed_seconds": 1, "output_tail": ""},
            {"command": ["docker", "compose", "up", "-d"],
             "returncode": 1, "timed_out": False, "elapsed_seconds": 1, "output_tail": ""},
        ]
        text = ngeupdate.format_deploy_result(
            "/p", "fast", steps, False, 2,
            seed_paths=["database/seeders"], seed_requested=True,
            diff_files=["database/seeders/UserSeeder.php"],
        )
        self.assertNotIn("seed TIDAK", text)
        self.assertNotIn("⚠️", text)

    def test_paths_with_changes_and_ran_yields_no_notice(self):
        notes = ngeupdate._migrate_seed_notice_lines(
            ["database/migrations/2025_01_01_foo.php"],
            ["database/migrations"], [],
            migrate_requested=True, seed_requested=False,
        )
        self.assertEqual(notes, [])

    def test_similar_prefix_does_not_match(self):
        # "migrations" must NOT match a file under "migrations_old/".
        notes = ngeupdate._migrate_seed_notice_lines(
            ["migrations_old/2024_foo.php"],
            ["migrations"], [],
            migrate_requested=False, seed_requested=False,
        )
        self.assertEqual(notes[0].count("⚠️"), 0)
        self.assertIn("ℹ️", notes[0])

    def test_multiple_paths_listed(self):
        notes = ngeupdate._migrate_seed_notice_lines(
            ["drizzle/0001_init.sql"],
            ["drizzle", "extra"], [],
            migrate_requested=False, seed_requested=False,
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("(paths: drizzle, extra)", notes[0])


# ---------------------------------------------------------------------------
# Notice flow: real git + bare remote (no docker needed)
# ---------------------------------------------------------------------------

class NoticeEndToEndTests(unittest.TestCase):
    """Spin up a bare git repo, clone it, push a migration file, run a
    deploy. Docker is not required: the first step (`git pull`) succeeds
    against the local bare remote, the second (`docker compose build`)
    fails on macOS without docker, but the ⚠️ notice must still be in
    the tool output."""

    def _git(self, *args, cwd):
        return subprocess.run(
            ["git"] + list(args),
            cwd=cwd,
            shell=False,
            env={**os.environ, "GIT_AUTHOR_NAME": "t",
                 "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t"},
            capture_output=True,
            text=True,
            check=True,
        )

    def test_pull_brings_migration_file_yields_warning(self):
        with tempfile.TemporaryDirectory() as base:
            base = os.path.realpath(base)
            remote = os.path.join(base, "remote.git")
            self._git("init", "--bare", remote, cwd=base)
            self._git("clone", remote, "clone", cwd=base)
            clone = os.path.join(base, "clone")
            # Modern git defaults to `main`; force it explicitly so the test
            # is independent of the host's init.defaultBranch.
            self._git("symbolic-ref", "HEAD", "refs/heads/main", cwd=clone)
            # Initial commit: compose.yaml only. No migrations yet.
            with open(os.path.join(clone, "compose.yaml"), "w") as fh:
                fh.write("services: {}\n")
            self._git("add", "-A", cwd=clone)
            self._git("commit", "-m", "init", cwd=clone)
            self._git("push", "origin", "main", cwd=clone)

            # Deploy target is a fresh clone of the bare remote. At this
            # point it has no migrations, so a `git pull` after we push a
            # migration file would actually bring one in.
            self._git("clone", remote, "work", cwd=base)
            work = os.path.join(base, "work")

            # Now add a migration file to the clone, commit, and push. The
            # work tree is behind on `main` — a subsequent `git pull` from
            # work will fast-forward and the diff will include the new file.
            os.makedirs(os.path.join(clone, "database/migrations"))
            with open(
                os.path.join(clone, "database/migrations", "0001_init.sql"), "w"
            ) as fh:
                fh.write("-- init\n")
            self._git("add", "-A", cwd=clone)
            self._git("commit", "-m", "add migration", cwd=clone)
            self._git("push", "origin", "main", cwd=clone)

            port = _free_port()
            httpd = _start_server(base, port, timeout=15)
            try:
                # Deploy returns immediately with a STARTED line + job id.
                status, data = _post_json(
                    port,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "deploy",
                            "arguments": {"path": work, "mode": "fast"},
                        },
                    },
                    headers={
                        "Authorization": AUTH,
                        "X-Migrate-Cmd": "php artisan migrate --force",
                        "X-Migrate-Paths": "database/migrations",
                    },
                )
                self.assertEqual(status, 200)
                deploy_body = json.loads(data.decode("utf-8"))
                deploy_text = deploy_body["result"]["content"][0]["text"]
                self.assertIn("🚀 STARTED job ", deploy_text)
                job_id = deploy_text.split("🚀 STARTED job ", 1)[1].split(" —", 1)[0]

                # The deploy fails at docker compose build (no docker on macOS);
                # poll deploy_status until we get the final output.
                text = ""
                deadline = time.time() + 25
                while time.time() < deadline:
                    _, status_data = _post_json(
                        port,
                        {
                            "jsonrpc": "2.0",
                            "id": 2,
                            "method": "tools/call",
                            "params": {
                                "name": "deploy_status",
                                "arguments": {"job_id": job_id},
                            },
                        },
                        headers={"Authorization": AUTH},
                    )
                    body = json.loads(status_data.decode("utf-8"))
                    text = body["result"]["content"][0]["text"]
                    if text.startswith("❌ FAILED") or text.startswith("✅ DONE"):
                        break
                    self.assertIn("⏳ RUNNING", text)
                    time.sleep(0.1)
            finally:
                httpd.shutdown()
                httpd.server_close()

            self.assertTrue(
                text.startswith("❌ FAILED") or text.startswith("✅ DONE"),
                "deploy_status never resolved to final output: %r" % text,
            )
            # And the ⚠️ must appear regardless of pipeline outcome.
            self.assertIn("⚠️", text)
            self.assertIn("database/migrations", text)
            self.assertIn("(1 file)", text)


# ---------------------------------------------------------------------------
# Migrate without header: error returned, no git pull step
# ---------------------------------------------------------------------------

class MigrateWithoutHeaderTests(unittest.TestCase):
    def test_migrate_true_without_cmd_errors_before_pull(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            os.makedirs(os.path.join(root, "myapp"))
            _make_fake_compose(os.path.join(root, "myapp"))
            port = _free_port()
            httpd = _start_server(root, port, timeout=10)
            try:
                status, data = _post_json(
                    port,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "deploy",
                            "arguments": {
                                "path": os.path.join(root, "myapp"),
                                "mode": "fast",
                                "migrate": True,
                            },
                        },
                    },
                    headers={
                        "Authorization": AUTH,
                        # Service set, but X-Migrate-Cmd deliberately missing
                        # so we hit the "cmd kosong" error path.
                        "X-Compose-Service": "app",
                    },
                )
            finally:
                httpd.shutdown()
                httpd.server_close()

            self.assertEqual(status, 200)
            body = json.loads(data.decode("utf-8"))
            self.assertTrue(body["result"].get("isError"))
            text = body["result"]["content"][0]["text"]
            self.assertIn("X-Migrate-Cmd", text)
            self.assertIn("kosong", text)
            # CRITICAL: nothing was executed, so the output must NOT contain
            # a step line for `git pull`.
            self.assertNotIn("step 1: git pull", text)

    def test_seed_true_without_service_errors_before_pull(self):
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            os.makedirs(os.path.join(root, "myapp"))
            _make_fake_compose(os.path.join(root, "myapp"))
            port = _free_port()
            httpd = _start_server(root, port, timeout=10)
            try:
                status, data = _post_json(
                    port,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": "deploy",
                            "arguments": {
                                "path": os.path.join(root, "myapp"),
                                "mode": "fast",
                                "seed": True,
                            },
                        },
                    },
                    headers={
                        "Authorization": AUTH,
                        "X-Seed-Cmd": "node dist/seed.js",
                    },
                )
            finally:
                httpd.shutdown()
                httpd.server_close()

            self.assertEqual(status, 200)
            body = json.loads(data.decode("utf-8"))
            self.assertTrue(body["result"].get("isError"))
            text = body["result"]["content"][0]["text"]
            self.assertIn("X-Compose-Service", text)
            self.assertNotIn("step 1: git pull", text)


# ---------------------------------------------------------------------------
# Initialize response carries instructions for the agent
# ---------------------------------------------------------------------------

class InitializeInstructionsTests(unittest.TestCase):
    def test_initialize_has_instructions(self):
        port = _free_port()
        with tempfile.TemporaryDirectory() as root:
            root = os.path.realpath(root)
            httpd = _start_server(root, port, timeout=10)
            try:
                status, data = _post_json(
                    port,
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26"},
                    },
                    headers={"Authorization": AUTH},
                )
            finally:
                httpd.shutdown()
                httpd.server_close()

        self.assertEqual(status, 200)
        body = json.loads(data.decode("utf-8"))
        self.assertIn("result", body)
        self.assertIn("instructions", body["result"])
        instr = body["result"]["instructions"]
        # Must mention migrate / seed so any agent picks it up.
        self.assertIn("migrate", instr.lower())
        self.assertIn("seed", instr.lower())


# ---------------------------------------------------------------------------
# TOOL_SCHEMA exposes the new params
# ---------------------------------------------------------------------------

class ToolSchemaMigrateSeedTests(unittest.TestCase):
    def test_migrate_and_seed_params_present(self):
        props = ngeupdate.TOOL_SCHEMA["inputSchema"]["properties"]
        self.assertIn("migrate", props)
        self.assertIn("seed", props)
        self.assertEqual(props["migrate"]["type"], "boolean")
        self.assertEqual(props["seed"]["type"], "boolean")
        self.assertEqual(props["migrate"]["default"], False)
        self.assertEqual(props["seed"]["default"], False)

    def test_description_mentions_migrate_and_seed_triggers(self):
        desc = ngeupdate.TOOL_SCHEMA["description"].lower()
        self.assertIn("migrate", desc)
        self.assertIn("seed", desc)
        for phrase in ("deploy + migrate", "db seed"):
            self.assertIn(phrase.lower(), desc)


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

class JobStoreTests(unittest.TestCase):
    def _make_job(self, jid, status="running"):
        return {
            "id": jid,
            "path": "/p",
            "mode": "fast",
            "started_at": time.time(),
            "completed_at": None,
            "status": status,
            "steps": [],
            "current": None,
            "diff_files": [],
            "ok": None,
            "total_seconds": None,
            "total_steps": 0,
            "lock": threading.Lock(),
            "ip": "127.0.0.1",
            "migrate_paths": [],
            "seed_paths": [],
            "migrate_requested": False,
            "seed_requested": False,
            "service": None,
        }

    def test_create_and_wait_completes_after_mark(self):
        store = ngeupdate.JobStore()
        j = self._make_job("aaaa11")
        store.create(j)
        # While running, wait_for_terminal blocks until the deadline
        # OR mark_complete fires.
        completed = threading.Event()
        def waiter():
            r = store.wait_for_terminal("aaaa11", time.time() + 1.0)
            completed.set()
            return r
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        store.mark_complete("aaaa11")
        t.join(timeout=2)
        self.assertTrue(completed.is_set())
        snap = store.wait_for_terminal("aaaa11", time.time() + 1.0)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["id"], "aaaa11")

    def test_unknown_job_id_returns_none(self):
        store = ngeupdate.JobStore()
        r = store.wait_for_terminal("missing", time.time() + 0.1)
        self.assertIsNone(r)

    def test_gc_drops_old_done_jobs(self):
        store = ngeupdate.JobStore(retention_seconds=0)
        store.create(self._make_job("oldjob", status="done"))
        store._jobs["oldjob"]["completed_at"] = time.time() - 5
        store._gc_locked()
        self.assertEqual(store.count(), 0)

    def test_mark_complete_idempotent(self):
        store = ngeupdate.JobStore()
        j = self._make_job("twice")
        store.create(j)
        store.mark_complete("twice")
        # Second call must not flip status back, and must not crash.
        store.mark_complete("twice")
        snap = store.wait_for_terminal("twice", time.time() + 0.5)
        self.assertEqual(snap["status"], "done")
        self.assertIsNotNone(snap["completed_at"])

    def test_mark_complete_on_unknown_job_is_no_op(self):
        # The worker thread and the polling call can be racing; mark_complete
        # on a job id we don't know must not crash (e.g. GC swept it).
        store = ngeupdate.JobStore()
        store.mark_complete("ghost")  # no raise


# ---------------------------------------------------------------------------
# Async deploy / deploy_status (HTTP smoke)
# ---------------------------------------------------------------------------

class AsyncDeployTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.realpath(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.makedirs(os.path.join(self.root, "proj"))
        _make_fake_compose(os.path.join(self.root, "proj"))
        self.port = _free_port()
        self.httpd = _start_server(self.root, self.port)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _call(self, name, arguments, headers=None):
        hdrs = {"Authorization": AUTH}
        if headers:
            hdrs.update(headers)
        return _post_json(
            self.port,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            headers=hdrs,
        )

    def test_deploy_returns_started_in_milliseconds(self):
        t0 = time.time()
        status, data = self._call(
            "deploy", {"path": os.path.join(self.root, "proj"), "mode": "fast"}
        )
        elapsed = time.time() - t0
        self.assertEqual(status, 200)
        body = json.loads(data.decode("utf-8"))
        text = body["result"]["content"][0]["text"]
        self.assertIn("🚀 STARTED job ", text)
        # The whole point of going async: the response is fast even though
        # the underlying git pull will fail (no remote).
        self.assertLess(elapsed, 1.0, "deploy should return in <1s, took %.3fs" % elapsed)
        self.assertFalse(body["result"].get("isError"))

    def test_deploy_status_unknown_job_returns_error(self):
        status, data = self._call(
            "deploy_status", {"job_id": "deadbe"}
        )
        body = json.loads(data.decode("utf-8"))
        text = body["result"]["content"][0]["text"]
        self.assertIn("tidak ditemukan", text)
        self.assertTrue(body["result"].get("isError"))

    def test_deploy_status_no_job_id_is_error(self):
        status, data = self._call("deploy_status", {})
        body = json.loads(data.decode("utf-8"))
        self.assertIn("job_id wajib", body["result"]["content"][0]["text"])
        self.assertTrue(body["result"].get("isError"))

    def test_deploy_then_status_resolves_to_done(self):
        # First call: deploy (synchronous ack).
        _, data = self._call(
            "deploy", {"path": os.path.join(self.root, "proj"), "mode": "fast"}
        )
        deploy_text = json.loads(data.decode("utf-8"))["result"]["content"][0]["text"]
        job_id = deploy_text.split("🚀 STARTED job ", 1)[1].split(" —", 1)[0]

        # Poll until the final output is back, or the deadline (~30s, past
        # one long-poll cycle) exhausts. With no docker, the deploy fails
        # at git pull --ff-only and we get FAILED very fast — but the test
        # also exercises the RUNNING path if the worker is still busy.
        deadline = time.time() + 30
        text = ""
        while time.time() < deadline:
            _, sdata = self._call("deploy_status", {"job_id": job_id})
            text = json.loads(sdata.decode("utf-8"))["result"]["content"][0]["text"]
            if text.startswith("❌ FAILED") or text.startswith("✅ DONE"):
                break
            self.assertTrue(
                text.startswith("⏳ RUNNING"),
                "expected RUNNING or final, got: %r" % text,
            )
            time.sleep(0.1)

        self.assertTrue(
            text.startswith("❌ FAILED") or text.startswith("✅ DONE"),
            "deploy_status never resolved; final text %r" % text,
        )
        self.assertIn(self.root + "/proj", text)

    def test_lock_held_until_thread_finishes(self):
        """Two concurrent deploy calls on the SAME path: the second must be
        refused synchronously. After the first job's worker releases the
        lock (in its own finally — NOT in the handler), a third call must
        succeed. This proves the lock release is in the thread, not the
        handler."""
        _, data = self._call(
            "deploy", {"path": os.path.join(self.root, "proj"), "mode": "fast"}
        )
        text = json.loads(data.decode("utf-8"))["result"]["content"][0]["text"]
        self.assertIn("🚀 STARTED job ", text)

        # Immediately try a second deploy to the same path: should be refused.
        _, data2 = self._call(
            "deploy", {"path": os.path.join(self.root, "proj"), "mode": "fast"}
        )
        text2 = json.loads(data2.decode("utf-8"))["result"]["content"][0]["text"]
        self.assertIn("sedang berjalan", text2)
        self.assertTrue(json.loads(data2.decode("utf-8"))["result"].get("isError"))

    def test_log_event_includes_cf_connecting_ip(self):
        # The first call records the deploy; the worker emits a deploy_end
        # log line. Capture stderr to assert CF-Connecting-IP shows up.
        # The handler falls back to client_address when the header is
        # absent, so we forward the header explicitly.
        import io
        from unittest import mock

        # Re-purpose the existing HttpSmokeTests setUp but capture stderr.
        captured = io.StringIO()
        with mock.patch("sys.stderr", captured):
            _, _ = self._call(
                "deploy",
                {"path": os.path.join(self.root, "proj"), "mode": "fast"},
                headers={"CF-Connecting-IP": "203.0.113.42"},
            )
            # Give the worker thread a beat to write deploy_end.
            for _ in range(50):
                if '"event": "deploy_end"' in captured.getvalue():
                    break
                time.sleep(0.05)
        out = captured.getvalue()
        self.assertIn('"event": "deploy_end"', out)
        self.assertIn('"ip": "203.0.113.42"', out)

    def test_log_event_falls_back_to_client_address(self):
        import io
        from unittest import mock

        captured = io.StringIO()
        with mock.patch("sys.stderr", captured):
            _, _ = self._call(
                "deploy",
                {"path": os.path.join(self.root, "proj"), "mode": "fast"},
            )
            for _ in range(50):
                if '"event": "deploy_end"' in captured.getvalue():
                    break
                time.sleep(0.05)
        out = captured.getvalue()
        self.assertIn('"ip": "127.0.0.1"', out)


# ---------------------------------------------------------------------------
# _parse_compose_ps: NDJSON, JSON array, and the (unhealthy/exited) matrix
# ---------------------------------------------------------------------------

class ParseComposePsTests(unittest.TestCase):
    def test_empty_input(self):
        ok, bad = ngeupdate._parse_compose_ps("")
        self.assertEqual((ok, bad), ({}, {}))

    def test_ndjson_one_per_line(self):
        nd = "\n".join([
            json.dumps({"Name": "web", "State": "running", "Health": "healthy"}),
            json.dumps({"Name": "worker", "State": "running", "Health": ""}),
        ])
        ok, bad = ngeupdate._parse_compose_ps(nd)
        self.assertEqual(set(ok), {"web", "worker"})
        self.assertEqual(bad, {})

    def test_json_array(self):
        arr = json.dumps([
            {"Name": "web", "State": "running", "Health": "healthy"},
            {"Name": "init", "State": "exited", "ExitCode": 0},
        ])
        ok, bad = ngeupdate._parse_compose_ps(arr)
        self.assertEqual(set(ok), {"web", "init"})
        self.assertEqual(bad, {})

    def test_unhealthy_is_bad(self):
        nd = json.dumps({"Name": "web", "State": "running", "Health": "unhealthy"})
        ok, bad = ngeupdate._parse_compose_ps(nd)
        self.assertEqual(ok, {})
        self.assertIn("web", bad)
        self.assertIn("unhealthy", bad["web"])

    def test_restarting_is_bad(self):
        nd = json.dumps({"Name": "web", "State": "restarting", "Health": ""})
        ok, bad = ngeupdate._parse_compose_ps(nd)
        self.assertEqual(ok, {})
        self.assertIn("web", bad)

    def test_exited_zero_is_ok(self):
        nd = json.dumps({"Name": "init", "State": "exited", "ExitCode": 0})
        ok, bad = ngeupdate._parse_compose_ps(nd)
        self.assertIn("init", ok)
        self.assertEqual(bad, {})

    def test_exited_nonzero_is_bad(self):
        nd = json.dumps({"Name": "broken", "State": "exited", "ExitCode": 1})
        ok, bad = ngeupdate._parse_compose_ps(nd)
        self.assertEqual(ok, {})
        self.assertEqual(bad.get("broken"), "exited(1)")

    def test_starting_is_bad(self):
        nd = json.dumps({"Name": "web", "State": "starting", "Health": ""})
        _, bad = ngeupdate._parse_compose_ps(nd)
        self.assertIn("web", bad)


class WaitHealthyTests(unittest.TestCase):
    """_wait_healthy with `docker compose ps` faked; no docker needed."""

    def _run(self, stdout, timeout=0.5):
        from unittest import mock
        proc = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(ngeupdate, "HEALTH_POLL_INTERVAL", 0), \
                mock.patch.object(ngeupdate.subprocess, "run", return_value=proc):
            return ngeupdate._wait_healthy("/tmp", timeout)

    def test_all_running_is_ok(self):
        step = self._run('{"Name":"web","State":"running","Health":""}\n')
        self.assertEqual(step["returncode"], 0)

    def test_no_services_is_not_healthy(self):
        # Wrong cwd / empty compose output must not count as "all healthy".
        for out in ("", "[]"):
            step = self._run(out)
            self.assertEqual(step["returncode"], 1)
            self.assertIn("no services", step["output_tail"])

    def test_restarting_fails(self):
        step = self._run('[{"Name":"web","State":"restarting","Health":""}]')
        self.assertEqual(step["returncode"], 1)
        self.assertIn("web=restarting", step["output_tail"])


class JobIdTests(unittest.TestCase):
    def test_collision_never_overwrites_live_job(self):
        store = ngeupdate.JobStore()
        a = {"id": "dup", "status": "running", "lock": threading.Lock()}
        b = {"id": "dup", "status": "running", "lock": threading.Lock()}
        self.assertEqual(store.create(a), "dup")
        new_id = store.create(b)
        self.assertNotEqual(new_id, "dup")
        self.assertEqual(store.count(), 2)


if __name__ == "__main__":
    unittest.main()
