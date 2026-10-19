"""Tests for path resolution + migrate/seed header parsing + config guard."""
import json
import os
import shutil
import tempfile
import unittest

from ngeupdate import config, project

from tests.helpers import (
    AUTH,
    _free_port,
    _make_fake_compose,
    _post_json,
    _start_server,
)


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
        proj = self._sub("apps/app1")
        self.assertEqual(project.resolve_project_path(proj, self.root), proj)

    def test_relative_to_root(self):
        proj = self._sub("apps/app1")
        self.assertEqual(project.resolve_project_path("apps/app1", self.root), proj)

    def test_rejects_traversal(self):
        with self.assertRaises(ValueError):
            project.resolve_project_path(self.root + "/../escape", self.root)
        with self.assertRaises(ValueError):
            project.resolve_project_path("../../etc", self.root)

    def test_rejects_absolute_outside_root(self):
        with self.assertRaises(ValueError):
            project.resolve_project_path("/etc", self.root)

    def test_rejects_directory_without_compose(self):
        self._sub("apps/bare", with_compose=False)
        with self.assertRaises(ValueError):
            project.resolve_project_path(self.root + "/apps/bare", self.root)

    def test_rejects_missing_dir(self):
        with self.assertRaises(ValueError):
            project.resolve_project_path(self.root + "/missing", self.root)

    def test_empty_path_raises(self):
        for empty in (None, ""):
            with self.assertRaises(ValueError) as ctx:
                project.resolve_project_path(empty, self.root)
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
            project.resolve_project_path(link_path, self.root)


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
# Header shlex parsing — argv must split correctly including quoted args
# ---------------------------------------------------------------------------

class HeaderShlexTests(unittest.TestCase):
    def test_empty_header_returns_empty_argv(self):
        for raw in (None, "", "   "):
            self.assertEqual(project._parse_optional_shlex(raw, "X-Migrate-Cmd"), [])

    def test_simple_split(self):
        argv = project._parse_optional_shlex(
            "php artisan migrate --force", "X-Migrate-Cmd"
        )
        self.assertEqual(argv, ["php", "artisan", "migrate", "--force"])

    def test_quoted_args_preserved(self):
        argv = project._parse_optional_shlex(
            'echo "hello world" --foo=bar baz', "X-Migrate-Cmd"
        )
        self.assertEqual(argv, ["echo", "hello world", "--foo=bar", "baz"])

    def test_unclosed_quote_raises(self):
        with self.assertRaises(ValueError) as ctx:
            project._parse_optional_shlex('php artisan "unclosed', "X-Migrate-Cmd")
        self.assertIn("X-Migrate-Cmd", str(ctx.exception))
        self.assertIn("tidak valid", str(ctx.exception))


# ---------------------------------------------------------------------------
# Paths header — comma-separated list, stripped
# ---------------------------------------------------------------------------

class HeaderPathsTests(unittest.TestCase):
    def test_empty_returns_empty_list(self):
        for raw in (None, "", "  "):
            self.assertEqual(project._parse_optional_paths(raw), [])

    def test_single_path(self):
        self.assertEqual(
            project._parse_optional_paths("database/migrations"),
            ["database/migrations"],
        )

    def test_multiple_paths_trimmed(self):
        self.assertEqual(
            project._parse_optional_paths(
                " database/migrations , drizzle/, src/db/seed/ "),
            ["database/migrations", "drizzle", "src/db/seed"],
        )

    def test_dot_prefixed_dir_kept(self):
        # lstrip("./") used to eat the dot: ".drizzle" -> "drizzle".
        self.assertEqual(
            project._parse_optional_paths(".drizzle, ./database/migrations, ../x"),
            [".drizzle", "database/migrations", "../x"],
        )

    def test_empty_chunks_dropped(self):
        self.assertEqual(
            project._parse_optional_paths(",,foo,,bar,,"),
            ["foo", "bar"],
        )


# ---------------------------------------------------------------------------
# Service-name validator
# ---------------------------------------------------------------------------

class ServiceNameValidationTests(unittest.TestCase):
    def test_valid_service(self):
        self.assertEqual(project._validate_service("app"), "app")
        self.assertEqual(project._validate_service("api-v2"), "api-v2")
        self.assertEqual(project._validate_service("web.1"), "web.1")

    def test_empty_rejected(self):
        for raw in (None, "", "  "):
            with self.assertRaises(ValueError):
                project._validate_service(raw)

    def test_invalid_chars_rejected(self):
        for raw in ("app;rm", "foo bar", "../etc", "$(x)", "app/service",
                    "--privileged", "-v", ".hidden", "_x"):
            with self.assertRaises(ValueError) as ctx:
                project._validate_service(raw)
            self.assertIn("X-Compose-Service", str(ctx.exception))


# ---------------------------------------------------------------------------
# _validate_migrate_seed: end-to-end header resolution before git pull
# ---------------------------------------------------------------------------

class MigrateSeedValidationTests(unittest.TestCase):
    def test_neither_requested_yields_empty(self):
        svc, ma, sa, mp, sp = project._validate_migrate_seed(
            False, False, {"X-Compose-Service": "app", "X-Migrate-Cmd": "x"}
        )
        self.assertIsNone(svc)
        self.assertEqual((ma, sa, mp, sp), (None, None, [], []))

    def test_migrate_ok(self):
        svc, ma, sa, mp, sp = project._validate_migrate_seed(
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
        _svc, _ma, sa, _mp, sp = project._validate_migrate_seed(
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
            project._validate_migrate_seed(
                True, False, {"X-Compose-Service": "app"}
            )
        msg = str(ctx.exception)
        self.assertIn("X-Migrate-Cmd", msg)
        self.assertIn("kosong", msg)

    def test_migrate_without_service_errors(self):
        with self.assertRaises(ValueError):
            project._validate_migrate_seed(
                True, False, {"X-Migrate-Cmd": "artisan migrate"}
            )

    def test_seed_without_cmd_errors(self):
        with self.assertRaises(ValueError) as ctx:
            project._validate_migrate_seed(
                False, True, {"X-Compose-Service": "app"}
            )
        self.assertIn("X-Seed-Cmd", str(ctx.exception))

    def test_unclosed_quote_in_cmd_errors(self):
        with self.assertRaises(ValueError) as ctx:
            project._validate_migrate_seed(
                True, False,
                {"X-Compose-Service": "app", "X-Migrate-Cmd": 'php "unclosed'},
            )
        self.assertIn("X-Migrate-Cmd", str(ctx.exception))

    def test_only_service_required_when_migrate_requested(self):
        # Empty paths are allowed (paths are only for the notice).
        svc, ma, sa, mp, sp = project._validate_migrate_seed(
            True, False,
            {"X-Compose-Service": "app", "X-Migrate-Cmd": "artisan migrate"},
        )
        self.assertEqual(svc, "app")
        self.assertEqual(ma, ["artisan", "migrate"])
        self.assertEqual(mp, [])


# ---------------------------------------------------------------------------
# Loader: refuse to start without key
# ---------------------------------------------------------------------------

class ConfigGuardTests(unittest.TestCase):
    def test_missing_key_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            config.load_config({"ALLOWED_ROOT": tempfile.gettempdir()})
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_allowed_root_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            config.load_config({"NGEUPDATE_KEY": "x" * 32})
        self.assertEqual(ctx.exception.code, 2)

    def test_short_key_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            config.load_config(
                {"NGEUPDATE_KEY": "x" * 31, "ALLOWED_ROOT": tempfile.gettempdir()}
            )
        self.assertEqual(ctx.exception.code, 2)

    def test_reads_env(self):
        cfg = config.load_config(
            {"NGEUPDATE_KEY": "x" * 32, "ALLOWED_ROOT": tempfile.gettempdir(), "PORT": "9999"}
        )
        self.assertEqual(cfg.port, 9999)
        self.assertEqual(cfg.allowed_root, os.path.realpath(tempfile.gettempdir()))


if __name__ == "__main__":
    unittest.main()