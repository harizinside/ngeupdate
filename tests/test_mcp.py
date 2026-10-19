"""Tests for the MCP Streamable HTTP surface: HTTP smoke + tool schemas."""
import json
import os
import shutil
import tempfile
import unittest

from ngeupdate import mcp
from ngeupdate.config import MAX_BODY_BYTES

from tests.helpers import (
    AUTH,
    BAD_AUTH,
    _free_port,
    _make_fake_compose,
    _post_json,
    _post_with_declared_length,
    _start_server,
)


# ---------------------------------------------------------------------------
# Tool description mentions "pull/build/deploy production"
# ---------------------------------------------------------------------------

class ToolSchemaTests(unittest.TestCase):
    def test_description_mentions_trigger_phrases(self):
        desc = mcp.TOOL_SCHEMA["description"].lower()
        for needle in ("pull", "build", "deploy"):
            self.assertIn(needle, desc)

    def test_mode_enum_and_default(self):
        schema = mcp.TOOL_SCHEMA["inputSchema"]
        self.assertEqual(schema["properties"]["mode"]["enum"], ["fast", "clean"])
        self.assertEqual(schema["properties"]["mode"]["default"], "fast")


class ToolSchemaMigrateSeedTests(unittest.TestCase):
    def test_migrate_and_seed_params_present(self):
        props = mcp.TOOL_SCHEMA["inputSchema"]["properties"]
        self.assertIn("migrate", props)
        self.assertIn("seed", props)
        self.assertEqual(props["migrate"]["type"], "boolean")
        self.assertEqual(props["seed"]["type"], "boolean")
        self.assertEqual(props["migrate"]["default"], False)
        self.assertEqual(props["seed"]["default"], False)

    def test_description_mentions_migrate_and_seed_triggers(self):
        desc = mcp.TOOL_SCHEMA["description"].lower()
        self.assertIn("migrate", desc)
        self.assertIn("seed", desc)
        for phrase in ("deploy + migrate", "db seed"):
            self.assertIn(phrase.lower(), desc)


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
        from http.client import HTTPConnection
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
        from http.client import HTTPConnection
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
            MAX_BODY_BYTES + 100,
            # the helper has its own ngeupdate.MAX_BODY_BYTES via the shim
            headers={"Authorization": AUTH},
        )
        self.assertEqual(status, 413)


if __name__ == "__main__":
    unittest.main()