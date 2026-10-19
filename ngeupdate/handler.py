"""Request handler + threading HTTP server.

Composes `OAuthRoutes` and `McpRoutes` mixins into a single handler so
each request gets all the methods (well-known, /authorize, /token,
/register, /mcp). Wiring is in `main()` (entry point).

The handler holds shared state via class attributes (`config`,
`deploy_lock`, `job_store`) that `main()` sets before the server starts
serving. This avoids module-level globals while still letting the test
suite spin up isolated instances.
"""
import http.server
import json
import sys
import urllib.parse

from .config import MAX_BODY_BYTES, SOCKET_TIMEOUT
from .mcp import McpRoutes
from .oauth import OAuthRoutes


class NgeupdateHandler(OAuthRoutes, McpRoutes, http.server.BaseHTTPRequestHandler):
    """Single request handler for /mcp + the OAuth routes.

    The mixin order matters: `OAuthRoutes` first defines the loopback-
    redirect helper and `_base_url` / `_www_authenticate` that `McpRoutes`
    references in `_handle_mcp`. Python's MRO resolves the diamond
    cleanly — `OAuthRoutes` then `McpRoutes` then `BaseHTTPRequestHandler`.
    """

    server_version = "ngeupdate/1.0"
    # Socket timeout per read/write: a client trickling headers/body can't pin
    # a thread forever. Long deploys are fine: nothing is read while they run.
    timeout = SOCKET_TIMEOUT

    # Set by main() / _start_server before serve_forever. Class attributes so
    # every request (across the ThreadingHTTPServer's worker threads) sees
    # the same instance.
    config = None
    deploy_lock = None
    job_store = None

    # ------------------------------------------------------------------
    # Logging: one-line access log to stderr (lands in journald / compose logs)
    # ------------------------------------------------------------------
    def log_message(self, format, *args):  # noqa: A002 - signature mandated by stdlib
        sys.stderr.write(
            "%s - %s\n" % (self.address_string(), format % args)
        )

    # ------------------------------------------------------------------
    # Low-level response helpers
    # ------------------------------------------------------------------
    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self._wfile_write(body)

    def _send_text(self, status, text, content_type="text/plain; charset=utf-8",
                    extra_headers=None):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self._wfile_write(body)

    def _wfile_write(self, body):
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_redirect(self, status, location):
        """302/303 with a Location header. Falls back to a tiny text body
        so curl and other clients that don't auto-follow show something
        instead of "no body"."""
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = ("redirecting to %s\n" % location).encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._wfile_write(body)

    def _read_body(self):
        """Read + size-check the request body. Returns the raw bytes.

        The OAuth endpoints (``/register``, ``/authorize``, ``/token``) and
        the MCP endpoint share the same Content-Length contract. Returns
        ``None`` after sending the right error response, so callers bail.
        """
        length_hdr = self.headers.get("Content-Length")
        if length_hdr is None:
            self._send_json(411, {"error": "length required"})
            return None
        try:
            length = int(length_hdr)
        except ValueError:
            self._send_json(400, {"error": "bad content-length"})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "body too large"})
            return None
        try:
            return self.rfile.read(length) if length > 0 else b""
        except OSError:
            self._send_json(400, {"error": "read failed"})
            return None

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802 - stdlib name
        path = urllib.parse.urlsplit(self.path).path
        if path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
        ):
            self._oauth_protected_resource_metadata()
            return
        if path == "/.well-known/oauth-authorization-server":
            self._oauth_authorization_server_metadata()
            return
        if path == "/authorize":
            self._oauth_authorize_get()
            return
        # Everything else, including /mcp GET, is not allowed.
        self._send_json(405, {"error": "method not allowed"})

    def do_DELETE(self):  # noqa: N802
        self._send_json(405, {"error": "method not allowed"})

    def do_POST(self):  # noqa: N802
        # POST has no query string in our routes — paths are exact — but
        # use the same split so a trailing ? doesn't sneak past us.
        path = urllib.parse.urlsplit(self.path).path
        if path == "/mcp":
            self._handle_mcp()
            return
        if path == "/register":
            self._oauth_register()
            return
        if path == "/authorize":
            self._oauth_authorize_post()
            return
        if path == "/token":
            self._oauth_token()
            return
        self._send_json(404, {"error": "not found"})


class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True