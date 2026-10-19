"""Tests for the container healthcheck parser + waiter."""
import unittest

from ngeupdate import health


# ---------------------------------------------------------------------------
# _parse_compose_ps: NDJSON, JSON array, and the (unhealthy/exited) matrix
# ---------------------------------------------------------------------------

class ParseComposePsTests(unittest.TestCase):
    def test_empty_input(self):
        ok, bad = health._parse_compose_ps("")
        self.assertEqual((ok, bad), ({}, {}))

    def test_whitespace_only_input(self):
        ok, bad = health._parse_compose_ps("   \n  \n")
        self.assertEqual((ok, bad), ({}, {}))

    def test_ndjson_running_healthy(self):
        nd = '{"Name":"web","State":"running","Health":"healthy"}'
        ok, bad = health._parse_compose_ps(nd)
        self.assertIn("web", ok)
        self.assertEqual(bad, {})

    def test_running_no_health_is_ok(self):
        nd = '{"Name":"web","State":"running","Health":""}'
        ok, bad = health._parse_compose_ps(nd)
        self.assertIn("web", ok)

    def test_unhealthy_is_bad(self):
        nd = '{"Name":"web","State":"running","Health":"unhealthy"}'
        ok, bad = health._parse_compose_ps(nd)
        self.assertEqual(ok, {})
        self.assertIn("web", bad)
        self.assertIn("unhealthy", bad["web"])

    def test_restarting_is_bad(self):
        nd = '{"Name":"web","State":"restarting","Health":""}'
        ok, bad = health._parse_compose_ps(nd)
        self.assertEqual(ok, {})
        self.assertIn("web", bad)

    def test_exited_zero_is_ok(self):
        nd = '{"Name":"init","State":"exited","ExitCode":0}'
        ok, bad = health._parse_compose_ps(nd)
        self.assertIn("init", ok)
        self.assertEqual(bad, {})

    def test_exited_nonzero_is_bad(self):
        nd = '{"Name":"broken","State":"exited","ExitCode":1}'
        ok, bad = health._parse_compose_ps(nd)
        self.assertEqual(ok, {})
        self.assertEqual(bad.get("broken"), "exited(1)")

    def test_starting_is_bad(self):
        nd = '{"Name":"web","State":"starting","Health":""}'
        _, bad = health._parse_compose_ps(nd)
        self.assertIn("web", bad)


class WaitHealthyTests(unittest.TestCase):
    """_wait_healthy with `docker compose ps` faked; no docker needed."""

    def _run(self, stdout, timeout=0.5):
        from unittest import mock
        proc = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(health, "HEALTH_POLL_INTERVAL", 0), \
                mock.patch.object(health.subprocess, "run", return_value=proc):
            return health._wait_healthy("/tmp", timeout)

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


if __name__ == "__main__":
    unittest.main()