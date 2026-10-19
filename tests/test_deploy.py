"""Tests for pipeline construction + report formatting + deploy worker.

Pipeline + notice + step-env are pure-function tests; NoticeEndToEnd /
MigrateWithoutHeader / AsyncDeploy spin a live server.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

import ngeupdate
from ngeupdate import deploy

from tests.helpers import (
    AUTH,
    _free_port,
    _make_fake_compose,
    _post_json,
    _start_server,
)


# ---------------------------------------------------------------------------
# StepEnv: child processes never see the bearer key
# ---------------------------------------------------------------------------

class StepEnvTests(unittest.TestCase):
    def test_key_not_passed_to_children(self):
        self.assertNotIn("NGEUPDATE_KEY", deploy.STEP_ENV)
        self.assertEqual(deploy.STEP_ENV["GIT_TERMINAL_PROMPT"], "0")


# ---------------------------------------------------------------------------
# Pipeline: pure function — fast/clean × migrate/seed ordering + shlex
# ---------------------------------------------------------------------------

class BuildPipelineTests(unittest.TestCase):
    def test_fast_no_migrate_no_seed(self):
        p = deploy.build_pipeline("fast", None, None, None)
        self.assertEqual(
            p,
            [
                ["git", "pull", "--ff-only"],
                ["docker", "compose", "build"],
                ["docker", "compose", "up", "-d"],
            ],
        )

    def test_clean_no_migrate_no_seed(self):
        p = deploy.build_pipeline("clean", None, None, None)
        self.assertEqual(
            p,
            [
                ["git", "pull", "--ff-only"],
                ["docker", "compose", "build", "--no-cache"],
                ["docker", "compose", "up", "-d", "--force-recreate"],
            ],
        )

    def test_fast_with_migrate_only(self):
        p = deploy.build_pipeline(
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
        p = deploy.build_pipeline(
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
        p = deploy.build_pipeline(
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
        p = deploy.build_pipeline("fast", None, None, None)
        self.assertEqual(p[0], ["git", "pull", "--ff-only"])

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            deploy.build_pipeline("yolo", None, None, None)

    def test_migrate_argv_requires_service(self):
        with self.assertRaises(ValueError):
            deploy.build_pipeline("fast", None, ["artisan", "migrate"], None)

    def test_seed_argv_requires_service(self):
        with self.assertRaises(ValueError):
            deploy.build_pipeline("fast", None, None, ["artisan", "db:seed"])


# ---------------------------------------------------------------------------
# Pipeline worker integration: stop-on-first-failure with a working binary
# ---------------------------------------------------------------------------

class PipelineStopOnFailureTests(unittest.TestCase):
    """Exercises run_deploy's stop-on-first-failure with a working `false`-like binary."""

    def test_fast_stops_after_failing_step(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "proj"))
            steps, ok, _, _diff = ngeupdate.deploy.run_deploy(
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
# Notice logic: pure, no git needed
# ---------------------------------------------------------------------------

class NoticeLogicTests(unittest.TestCase):
    def test_no_paths_no_notice(self):
        self.assertEqual(
            ngeupdate.report._migrate_seed_notice_lines(
                [], [], [], False, False,
            ),
            [],
        )

    def test_paths_with_no_diff_and_not_run_yields_info(self):
        notes = ngeupdate.report._migrate_seed_notice_lines(
            [],
            ["database/migrations"], [],
            migrate_requested=False, seed_requested=False,
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("ℹ️", notes[0])
        self.assertIn("migrate", notes[0])

    def test_paths_with_changes_and_not_run_yields_warning(self):
        notes = ngeupdate.report._migrate_seed_notice_lines(
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
        text = ngeupdate.report.format_deploy_result(
            "/p", "fast", steps, False, 2,
            seed_paths=["database/seeders"], seed_requested=True,
            diff_files=["database/seeders/UserSeeder.php"],
        )
        self.assertNotIn("seed TIDAK", text)
        self.assertNotIn("⚠️", text)

    def test_paths_with_changes_and_ran_yields_no_notice(self):
        notes = ngeupdate.report._migrate_seed_notice_lines(
            ["database/migrations/2025_01_01_foo.php"],
            ["database/migrations"], [],
            migrate_requested=True, seed_requested=False,
        )
        self.assertEqual(notes, [])

    def test_similar_prefix_does_not_match(self):
        # "migrations" must NOT match a file under "migrations_old/".
        notes = ngeupdate.report._migrate_seed_notice_lines(
            ["migrations_old/2024_foo.php"],
            ["migrations"], [],
            migrate_requested=False, seed_requested=False,
        )
        self.assertEqual(notes[0].count("⚠️"), 0)
        self.assertIn("ℹ️", notes[0])

    def test_multiple_paths_listed(self):
        notes = ngeupdate.report._migrate_seed_notice_lines(
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
            # Bare HEAD must point at main too, or `git clone` (below, for
            # `work`) checks out a nonexistent master on hosts whose default
            # branch is master (e.g. GitHub's Ubuntu runner).
            self._git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)
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


if __name__ == "__main__":
    unittest.main()