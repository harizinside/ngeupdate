"""End-to-end OAuth flow tests against a real running server.

Uses `_post_form` / `_post_json_to` which return the headers dict so each
test only sends its request once.
"""
import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.parse
from http.client import HTTPConnection

import ngeupdate

from tests.helpers import (
    KEY,
    _free_port,
    _make_fake_compose,
    _post_form,
    _post_json_to,
    _start_server,
)


class OAuthFlowTests(unittest.TestCase):
    """End-to-end OAuth flow plus the negative paths the plan calls out."""

    # Loopback redirect_uri — the only kind the server accepts.
    LOOPBACK = "http://127.0.0.1:12345/cb"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.realpath(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.proj = os.path.join(self.root, "myapp")
        os.makedirs(self.proj)
        _make_fake_compose(self.proj)
        self.port = _free_port()
        self.httpd = _start_server(self.root, self.port)
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        # Use a private verifier + challenge so each test is independent.
        self.verifier = "abcdefghijklmnopqrstuvwxyz0123456789-._~abcdefghij"
        self.challenge = ngeupdate.tokens._b64url(
            hashlib.sha256(self.verifier.encode("ascii")).digest()
        )

    # ---- helpers ----

    def _register(self):
        status, _, body = _post_json_to(
            self.port, "/register",
            {
                "client_name": "smoke-test",
                "redirect_uris": [self.LOOPBACK],
            },
        )
        self.assertEqual(status, 201, body)
        return json.loads(body)

    def _authorize_get(self, client_id, **overrides):
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": self.LOOPBACK,
            "state": "statexyz",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
        }
        params.update(overrides)
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/authorize?" + urllib.parse.urlencode(params))
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        return resp.status, {k: v for k, v in resp.getheaders()}, body

    def _authorize_post(self, client_id, key, **overrides):
        fields = {
            "client_id": client_id,
            "redirect_uri": self.LOOPBACK,
            "state": "statexyz",
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "key": key,
        }
        fields.update(overrides)
        return _post_form(self.port, "/authorize", fields)

    def _fetch_code(self, client_id, key=KEY):
        """POST /authorize, return the code from the redirect Location."""
        status, headers, _ = self._authorize_post(client_id, key)
        self.assertEqual(status, 302)
        location = headers["Location"]
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(location).query)
        return qs["code"][0]

    # ---- happy path ----

    def test_full_flow(self):
        # 1. Both metadata docs reachable.
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/.well-known/oauth-protected-resource/mcp")
        resp = conn.getresponse()
        meta_pr = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(meta_pr["resource"], "http://127.0.0.1:%d/mcp" % self.port)
        self.assertEqual(
            meta_pr["authorization_servers"],
            ["http://127.0.0.1:%d" % self.port],
        )

        conn.request("GET", "/.well-known/oauth-authorization-server")
        resp = conn.getresponse()
        meta_as = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(
            meta_as["code_challenge_methods_supported"], ["S256"],
        )
        self.assertEqual(
            meta_as["token_endpoint_auth_methods_supported"], ["none"],
        )

        # 2. Register.
        client = self._register()
        self.assertTrue(client["client_id"])
        self.assertEqual(client["redirect_uris"], [self.LOOPBACK])

        # 3. GET /authorize renders the form.
        status, _, body = self._authorize_get(client["client_id"])
        self.assertEqual(status, 200)
        self.assertIn("ngeupdate login", body)
        self.assertIn("password", body.lower())

        # 4. POST /authorize with the key → 302 + Location with ?code=...
        status, headers, _ = self._authorize_post(client["client_id"], KEY)
        self.assertEqual(status, 302)
        location = headers["Location"]
        self.assertTrue(location.startswith(self.LOOPBACK + "?"), location)
        parsed = urllib.parse.urlparse(location)
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertIn("code", qs)
        self.assertEqual(qs["state"], ["statexyz"])
        code = qs["code"][0]

        # 5. POST /token with the verifier → access_token.
        status, _, body = _post_form(
            self.port, "/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.LOOPBACK,
                "code_verifier": self.verifier,
                "client_id": client["client_id"],
            },
        )
        self.assertEqual(status, 200, body)
        tok = json.loads(body)
        self.assertEqual(tok["token_type"], "Bearer")
        self.assertTrue(tok["access_token"])
        # 90 days, give or take a second.
        self.assertGreater(tok["expires_in"], 90 * 24 * 60 * 60 - 5)

        # 6. /mcp tools/list with that token → 200.
        status, _, body = _post_json_to(
            self.port, "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": "Bearer " + tok["access_token"]},
        )
        self.assertEqual(status, 200, body)
        body_json = json.loads(body)
        names = [t["name"] for t in body_json["result"]["tools"]]
        self.assertIn("deploy", names)

    # ---- negatives ----

    def test_wrong_key_returns_401_no_redirect(self):
        client = self._register()
        status, headers, body = self._authorize_post(client["client_id"], "wrong-key")
        # Wrong key re-renders the form at 401; no Location, no code.
        self.assertEqual(status, 401)
        self.assertNotIn("Location", headers)
        self.assertIn("Key salah", body)
        # The body still carries the hidden fields so the user can retry.
        self.assertIn(client["client_id"], body)

    def test_non_loopback_redirect_is_rejected(self):
        client = self._register()
        # GET /authorize with a non-loopback redirect_uri.
        status, _, body = self._authorize_get(
            client["client_id"],
            redirect_uri="https://attacker.example.com/cb",
        )
        self.assertEqual(status, 400)
        err = json.loads(body)
        self.assertEqual(err["error"], "invalid_request")
        self.assertIn("loopback", err["error_description"])

    def test_wrong_verifier_is_invalid_grant(self):
        client = self._register()
        code = self._fetch_code(client["client_id"])

        # Wrong verifier.
        status, _, body = _post_form(
            self.port, "/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.LOOPBACK,
                "code_verifier": "totally-wrong-verifier-1234567890",
            },
        )
        self.assertEqual(status, 400)
        err = json.loads(body)
        self.assertEqual(err["error"], "invalid_grant")

    def test_tampered_token_is_401_on_mcp(self):
        # Forge a token by flipping a character in the middle of the payload
        # part. The last base64 character of the signature carries padding
        # bits, so swapping A↔B at the end can still decode to the same
        # bytes — flipping a middle payload byte always produces a different
        # signature and reliably fails HMAC.
        good = ngeupdate.tokens._sign(
            {"kind": "at", "exp": time.time() + 3600}, KEY,
        )
        body_b64, _, sig_b64 = good.partition(".")
        # Flip a character in the middle of the payload.
        mid = len(body_b64) // 2
        flipped = ("A" if body_b64[mid] != "A" else "B")
        bad = body_b64[:mid] + flipped + body_b64[mid + 1:] + "." + sig_b64

        status, headers, _ = _post_json_to(
            self.port, "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            headers={"Authorization": "Bearer " + bad},
        )
        self.assertEqual(status, 401)
        wa = headers.get("Www-Authenticate") or headers.get("WWW-Authenticate") or ""
        self.assertIn("resource_metadata=", wa)
        self.assertIn("/.well-known/oauth-protected-resource", wa)

    def test_expired_token_is_401_on_mcp(self):
        expired = ngeupdate.tokens._sign(
            {"kind": "at", "exp": time.time() - 60}, KEY,
        )
        status, _, _ = _post_json_to(
            self.port, "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            headers={"Authorization": "Bearer " + expired},
        )
        self.assertEqual(status, 401)

    def test_raw_key_as_bearer_is_401(self):
        # The raw NGEUPDATE_KEY must NOT work on /mcp anymore. The plan
        # calls this out specifically: old scripts that paste the key
        # straight into the header have to break.
        status, _, _ = _post_json_to(
            self.port, "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            headers={"Authorization": "Bearer " + KEY},
        )
        self.assertEqual(status, 401)

    def test_missing_auth_carries_www_authenticate(self):
        status, headers, _ = _post_json_to(
            self.port, "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
        )
        self.assertEqual(status, 401)
        wa = headers.get("Www-Authenticate") or headers.get("WWW-Authenticate") or ""
        self.assertIn("Bearer", wa)
        self.assertIn("resource_metadata=", wa)

    def test_token_response_carries_cache_control_no_store(self):
        # RFC 6749 §5.1: token responses must not be cached. The header must
        # be on both successful and error responses (we test one of each).
        client = self._register()
        code = self._fetch_code(client["client_id"])

        # Success.
        status, headers, _ = _post_form(
            self.port, "/token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.LOOPBACK,
                "code_verifier": self.verifier,
            },
        )
        self.assertEqual(status, 200)
        cc = headers.get("Cache-Control") or headers.get("cache-control") or ""
        self.assertEqual(cc, "no-store")

        # Error.
        status, headers, _ = _post_form(
            self.port, "/token",
            {
                "grant_type": "authorization_code",
                "code": code,  # already used above; PKCE still required
                "redirect_uri": self.LOOPBACK,
                "code_verifier": "wrong",
            },
        )
        self.assertEqual(status, 400)
        cc = headers.get("Cache-Control") or headers.get("cache-control") or ""
        self.assertEqual(cc, "no-store")

    def test_authorize_form_has_clickjacking_headers(self):
        # The password form must refuse to load inside an iframe (CSP
        # frame-ancestors + legacy X-Frame-Options).
        client = self._register()
        # 200 path.
        status, headers, _ = self._authorize_get(client["client_id"])
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        csp = headers.get("Content-Security-Policy") or headers.get("content-security-policy") or ""
        self.assertIn("frame-ancestors 'none'", csp)

        # 401 path (wrong key, form re-rendered).
        status, headers, _ = self._authorize_post(client["client_id"], "wrong-key")
        self.assertEqual(status, 401)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        csp = headers.get("Content-Security-Policy") or headers.get("content-security-policy") or ""
        self.assertIn("frame-ancestors 'none'", csp)

    def test_redirect_with_no_path_is_accepted(self):
        # RFC 8252 doesn't require a path. A client using `http://127.0.0.1:PORT`
        # (no path component) must still pass the loopback check.
        client = self._register()
        status, _, _ = self._authorize_get(
            client["client_id"],
            redirect_uri="http://127.0.0.1:54321",
        )
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()