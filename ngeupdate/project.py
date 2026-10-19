"""Project path resolution + migrate/seed header validation.

`resolve_project_path` is the single chokepoint that lets a deploy tool
call reach a host path; it rejects traversal, symlink escapes, and bare
directories without a recognised compose file.

The header validators (`_parse_optional_shlex`, `_validate_service`,
`_validate_migrate_seed`) all run synchronously BEFORE any side-effecting
step (git pull) runs. A bad header fails the call, not the build.
"""
import re
import shlex
import os


COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yml",
    "docker-compose.yaml",
)


def resolve_project_path(raw, allowed_root):
    """Validate and normalize a project path.

    - Empty / None raises ValueError ("path wajib").
    - Relative inputs are joined to allowed_root.
    - Realpath must live under allowed_root (rejects ../ and symlink escapes).
    - Directory must contain a recognised compose file.
    """
    if raw is None or raw == "":
        raise ValueError("path wajib (argumen 'path' atau header 'X-Project-Path')")

    candidate = raw if os.path.isabs(raw) else os.path.join(allowed_root, raw)
    real = os.path.realpath(candidate)
    try:
        common = os.path.commonpath([allowed_root, real])
    except ValueError:
        raise ValueError("path di luar ALLOWED_ROOT: %s" % raw)
    if common != allowed_root:
        raise ValueError("path di luar ALLOWED_ROOT: %s" % raw)

    if not os.path.isdir(real):
        raise ValueError("bukan direktori: %s" % real)

    if not any(os.path.isfile(os.path.join(real, name)) for name in COMPOSE_FILENAMES):
        raise ValueError(
            "tidak ada compose file (compose.yaml/compose.yml/docker-compose.{yml,yaml}) di %s"
            % real
        )

    return real


# Service names: [A-Za-z0-9_.-], first char alphanumeric. The leading-char
# rule matters: "--privileged" would otherwise be parsed by `docker compose run`
# as an option, not a service name.
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _parse_optional_shlex(raw, header_name):
    """Parse a header value as a shell-like argv list.

    Empty / None / whitespace only → [].
    Unparseable (unclosed quote, trailing backslash, …) raises ValueError
    naming the offending header. The caller MUST treat this as a hard
    validation failure that aborts the deploy BEFORE git pull.
    """
    if raw is None or not str(raw).strip():
        return []
    try:
        argv = shlex.split(raw)
    except ValueError as exc:
        raise ValueError(
            "%s tidak valid (shlex: %s)" % (header_name, exc)
        ) from exc
    return argv


def _parse_optional_paths(raw):
    """Split a comma-separated header into a clean list of directory prefixes.

    Empty / None → []. Empty items (",,") are dropped. A literal leading "./"
    and trailing slashes are stripped (".drizzle" stays ".drizzle"). Pure
    helper, never raises.
    """
    if raw is None:
        return []
    out = []
    for chunk in str(raw).split(","):
        p = chunk.strip()
        while p.startswith("./"):
            p = p[2:]
        p = p.rstrip("/")
        if p:
            out.append(p)
    return out


def _validate_service(raw):
    """Service name must be set and match [A-Za-z0-9_.-]+. Anything else → ValueError."""
    if raw is None or not str(raw).strip():
        raise ValueError("X-Compose-Service wajib diisi jika migrate / seed diminta")
    name = raw.strip()
    if not _SERVICE_NAME_RE.match(name):
        raise ValueError(
            "X-Compose-Service tidak valid: %r (A-Za-z0-9_.-, diawali huruf/angka)" % raw
        )
    return name


def _validate_migrate_seed(migrate, seed, headers):
    """Resolve + validate the migrate/seed config for this call.

    Returns (service, migrate_argv, seed_argv, migrate_paths, seed_paths).

    Service + argv validation only kicks in when the corresponding flag is
    set — a deploy without migrate / seed must NOT require headers. But
    ``X-Migrate-Paths`` / ``X-Seed-Paths`` are ALWAYS parsed, so the
    deploy output can attach an informational / warning notice whenever the
    project has opted into one.

    Raises ValueError when an inconsistency is detected; callers MUST raise
    before any side-effecting step (git pull) runs.
    """
    # Paths are always parsed — they're informational and feed the notice.
    migrate_paths = _parse_optional_paths(headers.get("X-Migrate-Paths"))
    seed_paths = _parse_optional_paths(headers.get("X-Seed-Paths"))

    if not migrate and not seed:
        # No flags → no service / argv needed. Paths still flow through so
        # the notice can fire if they were.
        return None, None, None, migrate_paths, seed_paths

    service = _validate_service(headers.get("X-Compose-Service"))

    migrate_argv = _parse_optional_shlex(
        headers.get("X-Migrate-Cmd"), "X-Migrate-Cmd"
    )
    seed_argv = _parse_optional_shlex(
        headers.get("X-Seed-Cmd"), "X-Seed-Cmd"
    )

    if migrate and not migrate_argv:
        raise ValueError(
            "migrate=true tapi X-Migrate-Cmd kosong atau tidak valid"
        )
    if seed and not seed_argv:
        raise ValueError(
            "seed=true tapi X-Seed-Cmd kosong atau tidak valid"
        )

    return service, migrate_argv, seed_argv, migrate_paths, seed_paths