"""OAuth endpoints as a mixin: the well-known docs, dynamic client
registration, the password-form ``/authorize`` page, and the
code→token ``/token`` exchange.

The mixin is composed into the request handler in `handler.py`. It
relies on `self._send_json`, `self._send_text`, `self._send_redirect`,
`self._read_body` (defined on the handler) and `self.config.key`.
"""
import hmac
import html
import json
import secrets
import time
import urllib.parse

from .tokens import (
    ACCESS_TOKEN_LIFETIME,
    AUTH_CODE_LIFETIME,
    _b64url,
    _sign,
    _unsign,
    _verify_pkce,
)


class OAuthRoutes:
    """Mixin: OAuth well-known docs + DCR + authorize page + token exchange.

    The request handler composes this in so each request gets all of these
    methods on `self`. Methods that don't need request state live on the
    class (not the instance) so they're cheap to call from tests too.
    """

    # RFC 8252 §7.3 / §7.1: native OAuth clients use loopback redirects.
    # Anything else is either a web app (which we don't want to be) or an
    # attempt to phish the auth code into someone else's listener.
    _LOOPBACK_REDIRECT_HOSTS = frozenset(("127.0.0.1", "localhost", "::1"))

    # ------------------------------------------------------------------
    # Helpers shared with the handler / MCP
    # ------------------------------------------------------------------
    def _base_url(self):
        """Origin used in OAuth metadata URLs.

        Trust ``X-Forwarded-Proto`` when present (Traefik terminates TLS) so
        advertised endpoints match what the client connected with. The Host
        header is also forwarded verbatim — the alternative would be the
        bind address, which lies to the agent about where it actually is.
        """
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("Host") or "%s:%s" % (
            self.config.host, self.config.port,
        )
        return "%s://%s" % (proto, host)

    def _resource_metadata_url(self):
        """RFC 9728 discovery URL for the protected resource."""
        return self._base_url() + "/.well-known/oauth-protected-resource"

    def _www_authenticate(self):
        """``WWW-Authenticate`` value sent on 401 from /mcp.

        Per RFC 9728 §5.1: ``Bearer resource_metadata="..."`` is how the
        client learns that this server uses OAuth and where to find the
        authorisation server metadata.
        """
        return 'Bearer resource_metadata="%s"' % self._resource_metadata_url()

    # ------------------------------------------------------------------
    # Loopback redirect helper (class-level — no request state needed)
    # ------------------------------------------------------------------
    @classmethod
    def _is_loopback_redirect(cls, redirect_uri):
        try:
            parsed = urllib.parse.urlparse(redirect_uri)
        except ValueError:
            return False
        if parsed.scheme != "http":
            return False
        host = parsed.hostname or ""
        if host not in cls._LOOPBACK_REDIRECT_HOSTS:
            return False
        # RFC 8252 doesn't require a path; a client using `http://127.0.0.1:PORT`
        # (no path) is valid. Query/fragment are still rejected because the
        # auth code is appended via ?code=...&state=... — a pre-existing query
        # or fragment would break the redirect target.
        if parsed.query or parsed.fragment:
            return False
        return True

    # ------------------------------------------------------------------
    # GET routes
    # ------------------------------------------------------------------
    def _oauth_protected_resource_metadata(self):
        """RFC 9728: where clients learn the resource and its AS."""
        base = self._base_url()
        self._send_json(200, {
            "resource": base + "/mcp",
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        })

    def _oauth_authorization_server_metadata(self):
        """RFC 8414: the well-known doc clients hit after discovery."""
        base = self._base_url()
        self._send_json(200, {
            "issuer": base,
            "authorization_endpoint": base + "/authorize",
            "token_endpoint": base + "/token",
            "registration_endpoint": base + "/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "code_challenge_methods_supported": ["S256"],
            # Public client: no client_secret. The dynamic client id is the
            # only credential; PKCE carries the real proof.
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": ["deploy"],
        })

    def _oauth_authorize_get(self):
        """Render the password page that collects the key.

        Strict validation runs BEFORE we render anything: a request with
        the wrong response_type, missing PKCE, or a non-loopback redirect
        is a 400, never a half-rendered form.
        """
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        params = {k: v[0] for k, v in q.items() if v}

        response_type = params.get("response_type", "")
        if response_type != "code":
            self._oauth_error(400, "unsupported_response_type",
                              "response_type must be 'code'")
            return

        client_id = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state = params.get("state", "")
        code_challenge = params.get("code_challenge", "")
        code_challenge_method = params.get("code_challenge_method", "")

        if code_challenge_method != "S256":
            self._oauth_error(400, "invalid_request",
                              "code_challenge_method must be 'S256'")
            return
        if not code_challenge:
            self._oauth_error(400, "invalid_request", "code_challenge required")
            return
        if not redirect_uri or not self._is_loopback_redirect(redirect_uri):
            self._oauth_error(400, "invalid_request",
                              "redirect_uri must be a loopback http URL")
            return
        if not client_id:
            self._oauth_error(400, "invalid_request", "client_id required")
            return

        # Safe to render the form. All quoted fields are user-controlled
        # and go through html.escape inside _render_authorize_form — a weird
        # state value can't smuggle HTML into the page.
        html_doc = self._render_authorize_form(
            client_id, redirect_uri, state, code_challenge,
        )
        self._send_text(200, html_doc, content_type="text/html; charset=utf-8",
                        extra_headers=self._AUTHORIZE_FORM_HEADERS)

    # ------------------------------------------------------------------
    # POST routes
    # ------------------------------------------------------------------
    def _oauth_register(self):
        """Dynamic Client Registration (RFC 7591).

        We mint a random ``client_id`` and echo the metadata back. Clients
        are public (PKCE-only); the redirect URI list is recorded verbatim
        so ``/authorize`` can validate it on the server side.
        """
        body = self._read_body()
        if body is None:
            return
        try:
            req = json.loads(body.decode("utf-8")) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._oauth_error(400, "invalid_request", "json body required")
            return
        if not isinstance(req, dict):
            self._oauth_error(400, "invalid_request", "json body must be object")
            return

        # 24 chars of url-safe base64 — plenty of entropy.
        client_id = _b64url(secrets.token_bytes(18))
        raw_uris = req.get("redirect_uris") or []
        if not isinstance(raw_uris, list) or not raw_uris:
            self._oauth_error(400, "invalid_redirect_uri", "redirect_uris required")
            return
        redirect_uris = [u for u in raw_uris if isinstance(u, str)]
        if not redirect_uris:
            self._oauth_error(400, "invalid_redirect_uri", "no valid redirect_uris")
            return

        client_name = req.get("client_name") if isinstance(req.get("client_name"), str) else None

        resp = {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        }
        if client_name:
            resp["client_name"] = client_name
        self._send_json(201, resp)

    def _oauth_authorize_post(self):
        """Validate the submitted key, mint an auth code, redirect back."""
        body = self._read_body()
        if body is None:
            return
        try:
            form = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except (UnicodeDecodeError, ValueError):
            self._oauth_error(400, "invalid_request", "form body required")
            return

        def _one(name):
            values = form.get(name) or [""]
            return values[0]

        client_id = _one("client_id")
        redirect_uri = _one("redirect_uri")
        state = _one("state")
        code_challenge = _one("code_challenge")
        code_challenge_method = _one("code_challenge_method")
        key = _one("key")

        if not client_id or not redirect_uri or not code_challenge:
            self._oauth_error(400, "invalid_request", "missing required field")
            return
        if code_challenge_method != "S256":
            self._oauth_error(400, "invalid_request",
                              "code_challenge_method must be 'S256'")
            return
        if not self._is_loopback_redirect(redirect_uri):
            self._oauth_error(400, "invalid_request",
                              "redirect_uri must be a loopback http URL")
            return

        if not key or not hmac.compare_digest(
            key.encode("utf-8"), self.config.key.encode("utf-8")
        ):
            # Wrong / missing key: re-render the page with the same params
            # plus an inline error. Re-render at 401 so monitoring can see
            # a key rejection without parsing the body.
            html_doc = self._render_authorize_form(
                client_id, redirect_uri, state, code_challenge,
                error="Key salah. Coba lagi.",
            )
            self._send_text(401, html_doc, content_type="text/html; charset=utf-8",
                            extra_headers=self._AUTHORIZE_FORM_HEADERS)
            return

        code = _sign(
            {"kind": "code", "ch": code_challenge, "ru": redirect_uri,
             "exp": time.time() + AUTH_CODE_LIFETIME},
            self.config.key,
        )
        sep_char = "?" if "?" not in redirect_uri else "&"
        location = "%s%scode=%s&state=%s" % (
            redirect_uri, sep_char,
            urllib.parse.quote(code, safe=""),
            urllib.parse.quote(state, safe=""),
        )
        self._send_redirect(302, location)

    # RFC 6749 §5.1: token responses carry credentials (or error
    # descriptions that shouldn't be cached by intermediaries). We set
    # `Cache-Control: no-store` on every response from /token (success and
    # errors).
    _TOKEN_HEADERS = {"Cache-Control": "no-store"}

    # Clickjacking protection for the password form. Both `X-Frame-Options`
    # (legacy browsers / proxies) and `Content-Security-Policy`'s
    # `frame-ancestors` directive (modern) refuse to embed the form inside
    # an `<iframe>` from any origin.
    _AUTHORIZE_FORM_HEADERS = {
        "X-Frame-Options": "DENY",
        "Content-Security-Policy": "frame-ancestors 'none'",
    }

    def _oauth_token(self):
        """Exchange an authorisation_code + PKCE verifier for an access token."""
        body = self._read_body()
        if body is None:
            return
        try:
            form = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
        except (UnicodeDecodeError, ValueError):
            self._oauth_error(400, "invalid_request", "form body required",
                              extra_headers=self._TOKEN_HEADERS)
            return

        def _one(name):
            values = form.get(name) or [""]
            return values[0]

        grant_type = _one("grant_type")
        code = _one("code")
        redirect_uri = _one("redirect_uri")
        code_verifier = _one("code_verifier")
        # client_id is optional for public clients (RFC 6749 §4.1.3); we
        # accept it when present and ignore it otherwise.

        if grant_type != "authorization_code":
            self._oauth_error(400, "unsupported_grant_type",
                              "grant_type must be 'authorization_code'",
                              extra_headers=self._TOKEN_HEADERS)
            return
        if not code or not redirect_uri or not code_verifier:
            self._oauth_error(400, "invalid_request",
                              "code, redirect_uri, code_verifier required",
                              extra_headers=self._TOKEN_HEADERS)
            return

        payload = _unsign(code, self.config.key, "code")
        if payload is None:
            self._oauth_error(400, "invalid_grant",
                              "code expired or tampered",
                              extra_headers=self._TOKEN_HEADERS)
            return
        # Code is bound to the exact redirect_uri it was minted for, so an
        # attacker who only has the code can't redirect it elsewhere.
        # ponytail: code not single-use (stateless, PKCE still required);
        # add a used-code set if replay matters.
        if payload.get("ru") != redirect_uri:
            self._oauth_error(400, "invalid_grant", "redirect_uri mismatch",
                              extra_headers=self._TOKEN_HEADERS)
            return
        if not _verify_pkce(code_verifier, payload.get("ch", "")):
            self._oauth_error(400, "invalid_grant", "PKCE verification failed",
                              extra_headers=self._TOKEN_HEADERS)
            return

        access_token = _sign(
            {"kind": "at", "exp": time.time() + ACCESS_TOKEN_LIFETIME},
            self.config.key,
        )
        self._send_json(200, {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": ACCESS_TOKEN_LIFETIME,
        }, extra_headers=self._TOKEN_HEADERS)

    def _oauth_error(self, status, code, description, extra_headers=None):
        """RFC 6749 §5.2 error response.

        All OAuth errors go through this — keeps the JSON shape uniform
        so clients see the same keys whether they hit /authorize or /token.
        `extra_headers` lets the caller attach response headers (e.g.
        `Cache-Control: no-store` from /token).
        """
        self._send_json(status, {
            "error": code,
            "error_description": description,
        }, extra_headers=extra_headers)

    # ------------------------------------------------------------------
    # Authorize-page renderer (pure)
    # ------------------------------------------------------------------
    def _render_authorize_form(self, client_id, redirect_uri, state,
                               code_challenge, error=""):
        """Return the password-form HTML for the given OAuth params.

        Pure renderer: status code is the caller's choice. The GET path
        sends this at 200; the POST path sends it at 401 after a wrong
        key. All user-controlled fields go through ``html.escape`` so a
        weird ``state`` value can't write HTML into the page.

        NOTE: we use ``str.format`` rather than ``%`` because the inline
        CSS contains literal ``%`` (e.g. ``width:100%``) which the ``%``
        formatter would mistake for a placeholder.
        """
        err_html = (
            "<p class=\"err\">{}</p>\n".format(html.escape(error))
            if error else ""
        )
        return (
            "<!doctype html>\n"
            "<meta charset=\"utf-8\">\n"
            "<title>ngeupdate login</title>\n"
            "<style>body{{font-family:system-ui;margin:2rem auto;max-width:28rem;padding:0 1rem}}"
            "label{{display:block;margin-top:1rem}}input{{width:100%;padding:.5rem}}"
            ".err{{color:#b00020;margin-top:1rem}}</style>\n"
            "<h1>ngeupdate login</h1>\n"
            "<p>Paste your ngeupdate key. The agent stores a token and "
            "won't ask again for 90 days.</p>\n"
            "{err}"
            "<form method=\"post\" action=\"/authorize\">\n"
            "<input type=\"hidden\" name=\"client_id\" value=\"{cid}\">\n"
            "<input type=\"hidden\" name=\"redirect_uri\" value=\"{ru}\">\n"
            "<input type=\"hidden\" name=\"state\" value=\"{state}\">\n"
            "<input type=\"hidden\" name=\"code_challenge\" value=\"{ch}\">\n"
            "<input type=\"hidden\" name=\"code_challenge_method\" value=\"S256\">\n"
            "<label>ngeupdate key<input type=\"password\" name=\"key\" "
            "autocomplete=\"off\" autofocus required></label>\n"
            "<p><button type=\"submit\">Sign in</button></p>\n"
            "</form>\n"
        ).format(
            err=err_html,
            cid=html.escape(client_id),
            ru=html.escape(redirect_uri),
            state=html.escape(state),
            ch=html.escape(code_challenge),
        )