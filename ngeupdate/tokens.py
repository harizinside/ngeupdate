"""OAuth: stateless signed tokens, no DB, survives restart. Rotating
NGEUPDATE_KEY revokes every outstanding token because the HMAC key
changes.

Tokens are `b64url(json(payload)).b64url(hmac_sha256(sig_key, json))`,
where ``sig_key = hmac_sha256(NGEUPDATE_KEY, b"ngeupdate-oauth-v1")``.

Provides `_auth_ok`, the bearer check used by `/mcp` — accepts only a
signed `at` (access token), never the raw `NGEUPDATE_KEY`.
"""
import base64
import binascii
import hashlib
import hmac
import json
import time


OAUTH_SIGN_DOMAIN = b"ngeupdate-oauth-v1"
ACCESS_TOKEN_LIFETIME = 90 * 24 * 60 * 60  # 90 days
AUTH_CODE_LIFETIME = 5 * 60  # 5 minutes


def _oauth_sign_key(cfg_key):
    """Derive the per-deployment signing key.

    Done via HMAC so a server restart with the same ``cfg_key`` produces
    identical signatures (and so rotating ``cfg_key`` immediately invalidates
    every outstanding code / access token). The domain tag stops the same
    key from signing something with a different shape we add later.
    """
    return hmac.new(cfg_key.encode("utf-8"), OAUTH_SIGN_DOMAIN, hashlib.sha256).digest()


def _b64url(data):
    """Stdlib base64url without padding (RFC 7515 / 7638 friendly)."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text):
    """Inverse of ``_b64url``. Accepts str or bytes; tolerates missing padding."""
    if isinstance(text, str):
        text = text.encode("ascii")
    pad = b"=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(obj, cfg_key):
    """Sign a JSON-serialisable object with the deployment key.

    Returns the string ``"<body>.<sig>"`` where both parts are base64url
    (no padding). The body is canonical JSON (``sort_keys=True``,
    ``separators=(",", ":")``) so the same payload always produces the
    same signature — the server can round-trip it without ambiguity.
    """
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_oauth_sign_key(cfg_key), payload, hashlib.sha256).digest()
    return _b64url(payload) + "." + _b64url(sig)


def _unsign(token, cfg_key, kind):
    """Verify a token and return its payload, or ``None`` on any failure.

    Validates HMAC with ``hmac.compare_digest`` (constant time), then the
    ``kind`` tag, then ``exp``. Bad signatures, wrong kind, expired, or
    malformed tokens all collapse to ``None``; callers don't need to know
    which guard fired.
    """
    if not isinstance(token, str) or "." not in token:
        return None
    body_b64, _, sig_b64 = token.partition(".")
    try:
        body = _b64url_decode(body_b64)
        sig = _b64url_decode(sig_b64)
    except (ValueError, TypeError, binascii.Error):
        return None
    expected = hmac.new(_oauth_sign_key(cfg_key), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("kind") != kind:
        return None
    exp = obj.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        return None
    return obj


def _verify_pkce(verifier, stored_challenge):
    """RFC 7636 §4.6: S256 challenge is base64url(sha256(verifier)).

    Verifier must be 43-128 chars of [A-Za-z0-9-._~] per the spec. We do
    NOT enforce the exact length range — a malformed verifier just won't
    match the stored challenge, and the caller already raises invalid_grant.
    """
    if not isinstance(verifier, str) or not isinstance(stored_challenge, str):
        return False
    try:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
    except (UnicodeEncodeError, ValueError):
        return False
    return hmac.compare_digest(_b64url(digest), stored_challenge)


def _auth_ok(headers, key):
    """Bearer check: only a signed ``at`` (access token) is accepted.

    The raw ``NGEUPDATE_KEY`` is NOT accepted here — it must be typed into
    the ``/authorize`` page to mint an access token. A leaked raw key on a
    header is still useful for ``/authorize`` (the page validates it with
    ``hmac.compare_digest``), but it stops working on ``/mcp`` itself, so
    a key dumped in a chat that scrolls out of view becomes useless for
    any client that hasn't already exchanged it for a token.
    """
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    presented = auth[len("Bearer "):].strip()
    if not presented:
        return False
    obj = _unsign(presented, key, "at")
    return obj is not None