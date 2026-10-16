#!/usr/bin/env python3
"""ngeupdate: stateless MCP Streamable HTTP server for production deploys.

Stdlib-only. Targets Python >= 3.8.
"""
import hmac
import http.server
import json
import os
import secrets
import re
import shlex
import subprocess
import sys
import threading
import time
import types


MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yml",
    "docker-compose.yaml",
)
TAIL_CHARS = 4000  # per-step stdout+stderr tail
MIN_KEY_LEN = 32  # `openssl rand -hex 32` gives 64
SOCKET_TIMEOUT = 30  # seconds a client may stall on any single read/write (slowloris)
JOB_RETENTION_SECONDS = 60 * 60  # drop done jobs after 1h
DEPLOY_STATUS_POLL = 25  # long-poll window; well below Cloudflare's 100s hard cap
HEALTH_POLL_INTERVAL = 5  # seconds between docker compose ps checks


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def load_config(env=None):
    """Read config from env only (no CLI flags: a key in argv leaks via `ps`).
    Server refuses to start without a key."""
    env = os.environ if env is None else env
    cfg = types.SimpleNamespace(
        host=env.get("HOST", "127.0.0.1"),
        port=int(env.get("PORT", "8787")),
        key=env.get("NGEUPDATE_KEY", ""),
        allowed_root=env.get("ALLOWED_ROOT", ""),
        deploy_timeout=int(env.get("DEPLOY_TIMEOUT", "1800")),
        max_deploys=int(env.get("MAX_CONCURRENT_DEPLOYS", "2")),
        health_timeout=int(env.get("HEALTH_TIMEOUT", "90")),
    )

    if not cfg.key:
        sys.stderr.write("NGEUPDATE_KEY is required; refusing to start\n")
        sys.exit(2)
    if len(cfg.key) < MIN_KEY_LEN:
        sys.stderr.write(
            "NGEUPDATE_KEY too short (< %d chars); use `openssl rand -hex 32`\n" % MIN_KEY_LEN
        )
        sys.exit(2)
    if not cfg.allowed_root:
        sys.stderr.write("ALLOWED_ROOT is required; refusing to start\n")
        sys.exit(2)

    cfg.allowed_root = os.path.realpath(cfg.allowed_root)
    if not os.path.isdir(cfg.allowed_root):
        sys.stderr.write("ALLOWED_ROOT is not a directory: %s\n" % cfg.allowed_root)
        sys.exit(2)

    return cfg


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Migrate / seed header parsing
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Pipeline construction
# ---------------------------------------------------------------------------

def build_pipeline(mode, service, migrate_argv, seed_argv):
    """Compose the ordered argv list for a deploy.

    Pure function — no I/O, no globals, no logging — so the test suite can
    assert on the exact sequence without spinning up docker. Returns a list
    of argv lists. ``service`` may be None when neither migrate nor seed
    runs; otherwise it must be a validated service name.

    Order:
      1. git pull --ff-only
      2. docker compose build [--no-cache]   (clean only adds --no-cache)
      3. docker compose run --rm -T <service> <migrate argv>   (if requested)
      4. docker compose run --rm -T <service> <seed argv>      (if requested)
      5. docker compose up -d [--force-recreate]                (clean only adds --force-recreate)

    `run --rm -T` overrides CMD but NOT ENTRYPOINT — the project's image
    must use an exec-form entrypoint so the migrate argv lands at PID 1.
    See README "Migrate & seed" for the data-modeling tradeoffs.
    """
    if mode not in ("fast", "clean"):
        raise ValueError("mode tidak dikenal: %s" % mode)

    pipeline = [
        ["git", "pull", "--ff-only"],
        ["docker", "compose", "build"] + (["--no-cache"] if mode == "clean" else []),
    ]
    if migrate_argv:
        if not service:
            raise ValueError("service wajib jika migrate_argv diisi")
        pipeline.append(
            ["docker", "compose", "run", "--rm", "-T", service] + list(migrate_argv)
        )
    if seed_argv:
        if not service:
            raise ValueError("service wajib jika seed_argv diisi")
        pipeline.append(
            ["docker", "compose", "run", "--rm", "-T", service] + list(seed_argv)
        )
    pipeline.append(
        ["docker", "compose", "up", "-d"] + (["--force-recreate"] if mode == "clean" else [])
    )
    return pipeline


# ---------------------------------------------------------------------------
# Command execution (sequential, stop-on-first-failure)
# ---------------------------------------------------------------------------

# No stdin + no git prompts: a credential/host-key prompt fails fast instead of
# hanging until DEPLOY_TIMEOUT. The bearer key is dropped so git / compose
# (and compose's ${VAR} interpolation) never see it.
_STEP_ENV = {k: v for k, v in os.environ.items() if k != "NGEUPDATE_KEY"}
_STEP_ENV["GIT_TERMINAL_PROMPT"] = "0"


def _run_step(cmd, cwd, timeout):
    """Run one command. Returns dict with command, returncode, timed_out, tail.

    Never raises. Missing executable (FileNotFoundError on POSIX) or any
    OS-level spawn failure is reported as a normal failing step so the
    pipeline's stop-on-first-failure still kicks in and the deploy output
    gets the usual `❌ FAILED at "..."` line.
    """
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            shell=False,
            env=_STEP_ENV,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        rc = proc.returncode
        timed_out = False
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        rc = -1
        timed_out = True
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        if isinstance(exc.stdout, bytes):
            out = exc.stdout.decode("utf-8", "replace")
        err = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
        if isinstance(exc.stderr, bytes):
            err = exc.stderr.decode("utf-8", "replace")
        out += err
    except OSError as exc:
        # Most commonly: executable not found (e.g. `docker` missing on a
        # non-docker host). Treat as a failed step, not an exception.
        rc = -1
        timed_out = False
        out = "%s: %s" % (type(exc).__name__, exc)

    elapsed = time.time() - started
    tail = out[-TAIL_CHARS:] if len(out) > TAIL_CHARS else out
    return {
        "command": cmd,
        "returncode": rc,
        "timed_out": timed_out,
        "elapsed_seconds": round(elapsed, 2),
        "output_tail": tail,
    }


def _capture_head(project_path, per_step_timeout):
    """Best-effort `git rev-parse HEAD`. Returns the hash or None on any failure.

    Used only to detect whether the pull brought migration/seed changes.
    Never raises — the pipeline keeps running even if the repo has no head.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_path,
            shell=False,
            env=_STEP_ENV,
            stdin=subprocess.DEVNULL,
            timeout=min(per_step_timeout, 10),
            capture_output=True,
            text=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _diff_files(project_path, old_head, new_head, per_step_timeout):
    """Return changed file paths between two commits, or [] on any failure.

    Used only for the migrate/seed notice. Never raises; returns [] if git
    can't compute the diff (e.g. shallow clone, missing commits).
    """
    if not old_head or not new_head or old_head == new_head:
        return []
    try:
        proc = subprocess.run(
            ["git", "diff", "--name-only", old_head, new_head],
            cwd=project_path,
            shell=False,
            env=_STEP_ENV,
            stdin=subprocess.DEVNULL,
            timeout=min(per_step_timeout, 10),
            capture_output=True,
            text=True,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if proc.returncode != 0:
        return []
    return [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]


def _is_under(file_path, dir_prefix):
    """True if ``file_path`` equals ``dir_prefix`` or lives under it.

    Refuses any directory whose name is a strict prefix of the path's first
    segment (e.g. ``migrations`` won't match ``migrations_old/x``).
    """
    return file_path == dir_prefix or file_path.startswith(dir_prefix + "/")


def _migrate_seed_notice_lines(
    diff_files, migrate_paths, seed_paths, migrate_requested, seed_requested,
):
    """Build the human-readable notice appended to the deploy output.

    Rules (from plan §"Biar agent mana pun notice"):
      - Path config present + pull brought files under it + flag NOT set → ⚠️
      - Path config present + no relevant changes + flag NOT set → ℹ️
      - Flag requested → no notice for that channel, whatever the outcome.
        The step list already shows whether it ran; a "TIDAK dijalankan"
        line after a seed that DID run would invite a duplicate seed.

    Returns a list of strings (each one ≤ 1 line) suitable for direct append.
    Empty list = nothing to say.
    """
    lines = []

    def _for(prefixes, label, requested):
        if not prefixes or requested:
            return
        primary = prefixes[0]
        rest = " (paths: %s)" % ", ".join(prefixes) if len(prefixes) > 1 else ""
        changed = [
            f for f in diff_files
            if any(_is_under(f, p) for p in prefixes)
        ]
        if changed:
            lines.append(
                "⚠️ Pull ini membawa perubahan di %s (%d file) tapi %s TIDAK "
                "dijalankan. Kalau perlu: deploy ulang dengan %s=true.%s"
                % (primary, len(changed), label, label, rest)
            )
        else:
            lines.append(
                "ℹ️ Project ini support %s%s; tidak dijalankan."
                % (label, rest)
            )

    _for(migrate_paths, "migrate", migrate_requested)
    _for(seed_paths, "seed", seed_requested)
    return lines


def _set_job_current(job, label, idx, total):
    """Mark the in-flight step on a job, if a job dict was provided."""
    if job is None:
        return
    with job["lock"]:
        job["current"] = {
            "label": label,
            "started": time.time(),
            "idx": idx,
            "total": total,
        }


def _append_job_step(job, step):
    """Append a finished step to a job's results list, if a job was provided."""
    if job is None:
        return
    with job["lock"]:
        job["steps"].append(step)
        job["current"] = None


def _log_event(payload):
    """One-line JSON event to stderr (lands in `docker compose logs` / journald).

    Best-effort: failures here must not affect the deploy. Always flushed so
    container death doesn't lose the closing entry.
    """
    try:
        out = {**payload, "ts": round(time.time(), 3)}
        sys.stderr.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stderr.flush()
    except Exception:
        return


def _parse_compose_ps(raw_output):
    """Parse ``docker compose ps -a --format json`` output.

    Two output shapes are supported:
      - NDJSON (docker compose >= 2.21) — one JSON object per line.
      - JSON array (older compose) — single JSON list.

    Returns ``(ok_services, bad_services)``: each a ``{name: display}`` dict.
    A service is "ok" when its State is ``running`` with Health empty or
    ``healthy``, OR ``exited`` with ExitCode 0 (one-shot). Anything else
    is "bad" (restarting, unhealthy, exited !=0, starting, paused, …).
    Empty / unparseable input yields two empty dicts.
    """
    text = (raw_output or "").strip()
    if not text:
        return {}, {}

    services = []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list):
        services = [s for s in parsed if isinstance(s, dict)]
    elif isinstance(parsed, dict):
        services = [parsed]
    else:
        # Fall back to NDJSON line-by-line.
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                services.append(obj)

    ok_services = {}
    bad_services = {}
    for svc in services:
        name = (
            svc.get("Name")
            or svc.get("Service")
            or svc.get("Names")
            or svc.get("ID")
            or ""
        )
        if not name:
            continue
        if isinstance(name, list):
            name = name[0] if name else ""
            if not name:
                continue
        state = str(svc.get("State", "") or "")
        health = str(svc.get("Health", "") or "")
        try:
            exit_code = svc.get("ExitCode")
            exit_code = int(exit_code) if exit_code not in (None, "") else None
        except (TypeError, ValueError):
            exit_code = None

        if state == "running" and health in ("", "healthy"):
            ok_services[name] = "running"
        elif state == "exited" and exit_code == 0:
            ok_services[name] = "exited(0)"
        else:
            display = state or "unknown"
            if health:
                display = "%s/%s" % (state or "running", health)
            if state == "exited" and exit_code is not None:
                display = "exited(%d)" % exit_code
            bad_services[name] = display
    return ok_services, bad_services


def _wait_healthy(project_path, timeout):
    """Poll ``docker compose ps -a`` until all services are healthy.

    Returns a step dict formatted like ``_run_step`` so the regular output
    formatter handles it. Done (returncode 0) when two consecutive polls
    find every service in ``ok_services`` (running healthy / exited 0).
    Failed (returncode 1) when ``timeout`` elapses while something is still
    restarting / unhealthy / starting / exited !=0.

    The step's ``command`` is ``["healthcheck"]`` so the existing format
    renders ``at 'healthcheck' (exit N) — ...``.
    """
    started = time.time()
    deadline = started + max(0, timeout)
    consecutive_good = 0
    last_bad = {}
    last_tail = ""

    while time.time() < deadline:
        try:
            proc = subprocess.run(
                ["docker", "compose", "ps", "-a", "--format", "json"],
                cwd=project_path,
                shell=False,
                env=_STEP_ENV,
                stdin=subprocess.DEVNULL,
                timeout=min(10, max(1, HEALTH_POLL_INTERVAL)),
                capture_output=True,
                text=True,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            last_tail = "%s: %s" % (type(exc).__name__, exc)
            consecutive_good = 0
            time.sleep(HEALTH_POLL_INTERVAL)
            continue

        out = (proc.stdout or "") + (
            ("\n" + proc.stderr) if proc.returncode != 0 else ""
        )
        last_tail = out[-TAIL_CHARS:] if out else ""

        if proc.returncode != 0:
            consecutive_good = 0
            time.sleep(HEALTH_POLL_INTERVAL)
            continue

        good, bad = _parse_compose_ps(proc.stdout or "")
        last_bad = bad if (good or bad) else {"(none)": "no services found by docker compose ps"}

        if good and not bad:
            consecutive_good += 1
            if consecutive_good >= 2:
                elapsed = round(time.time() - started, 2)
                return {
                    "command": ["healthcheck"],
                    "returncode": 0,
                    "timed_out": False,
                    "elapsed_seconds": elapsed,
                    "output_tail": "ok: 2x consecutive all-services-healthy polls",
                }
        else:
            consecutive_good = 0

        time.sleep(HEALTH_POLL_INTERVAL)

    elapsed = round(time.time() - started, 2)
    bad_str = ", ".join("%s=%s" % (k, v) for k, v in last_bad.items())
    return {
        "command": ["healthcheck"],
        "returncode": 1,
        "timed_out": False,
        "elapsed_seconds": elapsed,
        "output_tail": (
            "container baru SUDAH jalan tapi tidak sehat: %s\n%s"
            % (bad_str, last_tail)
        ).rstrip(),
    }


def run_deploy(
    project_path, mode, per_step_timeout,
    service=None, migrate_argv=None, seed_argv=None,
    health_timeout=90, job=None,
):
    """Execute the deploy pipeline.

    Returns (steps, ok, total_seconds, diff_files).

    `diff_files` lists paths changed between the pre-pull and post-pull
    HEADs; used by format_deploy_result to print the migrate/seed notice.
    Heads + diff are best-effort — any git failure yields None / [] but the
    pipeline itself still runs to completion.

    If ``job`` is provided, each completed step is appended to
    ``job["steps"]`` and the in-flight step is mirrored on ``job["current"]``
    so ``deploy_status`` can show progress without blocking on the worker
    thread. The caller holds the per-job lock when mutating these fields.

    The last pipeline step (on success) is a healthcheck over
    ``docker compose ps -a``: it polls until every service is "healthy"
    twice in a row, or until ``health_timeout`` elapses. Skip happens
    automatically when an earlier step fails.
    """
    started = time.time()
    pipeline = build_pipeline(mode, service, migrate_argv, seed_argv)
    total_steps = len(pipeline) + 1  # +1 for the healthcheck that follows

    if job is not None:
        with job["lock"]:
            job["total_steps"] = total_steps

    old_head = _capture_head(project_path, per_step_timeout)
    steps = []
    ok = True
    for idx, cmd in enumerate(pipeline, start=1):
        _set_job_current(job, " ".join(cmd), idx, total_steps)
        step = _run_step(cmd, cwd=project_path, timeout=per_step_timeout)
        _append_job_step(job, step)
        steps.append(step)
        if step["returncode"] != 0 or step["timed_out"]:
            ok = False
            break

    # Healthcheck: only when the full pipeline succeeded. A failing build or
    # `up -d` is itself a deploy failure and the running container state is
    # irrelevant at that point.
    if ok:
        _set_job_current(job, "healthcheck", total_steps, total_steps)
        hc_step = _wait_healthy(project_path, health_timeout)
        _append_job_step(job, hc_step)
        steps.append(hc_step)
        if hc_step["returncode"] != 0 or hc_step["timed_out"]:
            ok = False

    new_head = _capture_head(project_path, per_step_timeout)
    diff_files = _diff_files(project_path, old_head, new_head, per_step_timeout)
    return steps, ok, round(time.time() - started, 2), diff_files


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def _fmt_duration(seconds):
    s = int(round(seconds))
    if s < 60:
        return "%ds" % s
    m, s = divmod(s, 60)
    if m < 60:
        return "%dm%02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh%02dm%02ds" % (h, m, s)


def format_deploy_result(
    project_path, mode, steps, ok, total_seconds,
    migrate_paths=None, seed_paths=None,
    migrate_requested=False, seed_requested=False,
    diff_files=None,
):
    """First line = clear status; rest = per-step details + notices. Always UTF-8."""
    if ok:
        status = "✅ DONE — %s deployed (%s) in %s" % (
            project_path,
            mode,
            _fmt_duration(total_seconds),
        )
    else:
        failed = steps[-1]
        label = " ".join(failed["command"])
        reason = "timeout" if failed["timed_out"] else "exit %d" % failed["returncode"]
        status = "❌ FAILED at %r (%s) — %s after %s" % (
            label,
            reason,
            project_path,
            _fmt_duration(total_seconds),
        )

    lines = [status, ""]
    for idx, step in enumerate(steps, start=1):
        cmd = " ".join(step["command"])
        rc = step["returncode"]
        if step["timed_out"]:
            outcome = "TIMEOUT"
        elif rc == 0:
            outcome = "ok"
        else:
            outcome = "exit %d" % rc
        lines.append("step %d: %s  [%s, %ss]" % (idx, cmd, outcome, step["elapsed_seconds"]))
        tail = step["output_tail"].rstrip()
        if tail:
            for ln in tail.splitlines()[-20:]:
                lines.append("    " + ln)
        lines.append("")

    # Migrate/seed notice. The caller (run_deploy) pre-computes diff_files
    # between the pre-pull and post-pull HEADs so this function stays pure.
    if migrate_paths or seed_paths:
        notes = _migrate_seed_notice_lines(
            diff_files or [], migrate_paths or [], seed_paths or [],
            migrate_requested, seed_requested,
        )
        for note in notes:
            lines.append(note)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# JSON-RPC / MCP handlers
# ---------------------------------------------------------------------------

SERVER_INFO = {
    "name": "ngeupdate",
    "version": "1.0.0",
    "vendor": "ngeupdate",
}

TOOL_SCHEMAS = [
    {
        "name": "deploy",
        "description": (
            "Start a deploy on the production VPS. The call validates "
            "synchronously (path, mode, migrate/seed headers, path lock) and "
            "returns IMMEDIATELY with a job id (e.g. `🚀 STARTED job a1b2c3 "
            "— /srv/apps/myapp (fast). Panggil deploy_status(...) sampai "
            "DONE/FAILED.`) — long `--no-cache` builds will not hit "
            "Cloudflare's 100s 524 cap. Use deploy again whenever the user "
            "asks to \"pull build production\", \"deploy production\", "
            "\"rebuild production\", \"restart production\", or asks for a "
            "no-cache / force-recreate production deploy. If the user says "
            "'no cache' / 'force recreate' / '--no-cache', pass mode='clean'. "
            "If the user asks to also run database migrations, pass "
            "migrate=true (\"deploy + migrate\" / \"pull + migrate\" / "
            "\"apply migration\" / \"db migrate\"). For seeders pass "
            "seed=true (\"deploy + seed\", \"pull + seed\", \"db seed\"). "
            "Both flags need the matching header (X-Migrate-Cmd / X-Seed-Cmd) "
            "plus X-Compose-Service to be set in .mcp.json — without them "
            "the call errors out before git pull runs. Never set "
            "migrate=true or seed=true just because the project has the "
            "config; only when the user asked. "
            "After calling deploy, IMMEDIATELY call deploy_status(job_id=...) "
            "in a loop until the response is the final ✅ DONE / ❌ FAILED "
            "output. Show that final output's first status line to the user, "
            "plus any ⚠️ / ℹ️ notice lines that follow."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "Project path on the VPS. Absolute (/srv/apps/myapp) or "
                        "relative to ALLOWED_ROOT (e.g. 'myapp'). Omit to use "
                        "the X-Project-Path header from .mcp.json."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "clean"],
                    "default": "fast",
                    "description": (
                        "fast = incremental build; clean = --no-cache build + "
                        "--force-recreate."
                    ),
                },
                "migrate": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Run X-Migrate-Cmd via 'docker compose run --rm -T' "
                        "between build and up. Only set when the user "
                        "explicitly asked for a migration (e.g. 'deploy + "
                        "migrate', 'pull production and migrate', 'apply "
                        "migrations'). Requires X-Migrate-Cmd and "
                        "X-Compose-Service headers; errors out otherwise."
                    ),
                },
                "seed": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Run X-Seed-Cmd via 'docker compose run --rm -T' "
                        "between migrate and up. Only set when the user "
                        "explicitly asked for a seed (e.g. 'deploy + seed', "
                        "'db seed'). Requires X-Seed-Cmd and X-Compose-Service "
                        "headers; errors out otherwise."
                    ),
                },
            },
        },
    },
    {
        "name": "deploy_status",
        "description": (
            "Poll the status of a deploy job that was started by the deploy "
            "tool. The deploy tool returned a job id; pass it as job_id. "
            "This call LONG-POLLS up to 25 seconds (well below Cloudflare's "
            "100s 524 hard cap). If the deploy finishes within that window, "
            "the response is the SAME final output you would have seen from "
            "a synchronous deploy — first line '✅ DONE ...' on success or "
            "'❌ FAILED ...' on failure, plus any ⚠️ / ℹ️ migrate-seed notice "
            "lines. If the deploy is still running, the response is a "
            "single '⏳ RUNNING job <id> — step N/M: <command> (<elapsed>)' "
            "line; call this tool again immediately to keep waiting. An "
            "unknown job_id (server restart, retention expiry after 1 hour) "
            "returns 'job tidak ditemukan (expired / server restart)'. "
            "Always keep calling deploy_status in a loop until the final "
            "DONE / FAILED / not-found response, then show that final text "
            "to the user."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": (
                        "The job id returned by the deploy tool, e.g. "
                        "'a1b2c3'."
                    ),
                },
            },
            "required": ["job_id"],
        },
    },
]


# Back-compat alias — some existing tests reach TOOL_SCHEMA["description"]
# / TOOL_SCHEMA["inputSchema"] assuming the dict shape. Keep the FIRST tool's
# schema on the old name so that surface stays a single deploy-tool dict.
TOOL_SCHEMA = TOOL_SCHEMAS[0]


def _jsonrpc_result(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _tool_call_text(text, is_error=False):
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _auth_ok(headers, key):
    """Constant-time bearer check. Returns True on match."""
    auth = headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    presented = auth[len("Bearer "):].strip()
    if not presented:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), key.encode("utf-8"))


# ---------------------------------------------------------------------------
# Job store + per-path deploy lock
# ---------------------------------------------------------------------------

def _gen_job_id():
    """Random 8-hex job id (CSPRNG; ids are handed to clients)."""
    return secrets.token_hex(4)


def _snapshot_job(job):
    """Deep-ish copy of a job's reader-facing fields.

    Caller MUST hold ``job['lock']`` so partial writes from the worker thread
    never land in the snapshot. The two list fields (``steps``,
    ``diff_files``) are shallow-copied so the worker can keep appending.
    """
    return {
        "id": job["id"],
        "path": job["path"],
        "mode": job["mode"],
        "started_at": job["started_at"],
        "completed_at": job.get("completed_at"),
        "status": job["status"],
        "ok": job.get("ok"),
        "total_seconds": job.get("total_seconds"),
        "total_steps": job.get("total_steps", len(job.get("steps") or [])),
        "steps": list(job.get("steps") or []),
        "current": dict(job["current"]) if job.get("current") else None,
        "diff_files": list(job.get("diff_files") or []),
        "migrate_paths": list(job.get("migrate_paths") or []),
        "seed_paths": list(job.get("seed_paths") or []),
        "migrate_requested": bool(job.get("migrate_requested", False)),
        "seed_requested": bool(job.get("seed_requested", False)),
        "service": job.get("service"),
        "ip": job.get("ip", ""),
    }


class JobStore:
    """In-memory store of running/done deploy jobs.

    Each job is a plain dict created by ``_start_deploy_job`` below. The
    worker thread mutates ``steps`` / ``current`` under the job's own lock;
    readers must hold that lock to snapshot safely.

    Top-level state (the dict mapping id → job) lives behind a condition
    variable used both as a guard and as a wakeup channel for
    ``wait_for_terminal``. Retention is best-effort GC: jobs older than
    ``JOB_RETENTION_SECONDS`` after completion are evicted on the next
    ``create`` / ``wait_for_terminal`` call.

    No persistence: a server restart loses all in-flight jobs AND any
    running child process. The plan accepts this — file-based status would
    be the upgrade path if it ever becomes painful.
    """

    def __init__(self, retention_seconds=JOB_RETENTION_SECONDS):
        self._retention = retention_seconds
        self._jobs = {}
        self._cv = threading.Condition()

    def create(self, job):
        """Insert a brand-new running job. Re-rolls the id on collision so a
        live job is never overwritten. Returns the id actually used."""
        with self._cv:
            self._gc_locked()
            while job["id"] in self._jobs:
                job["id"] = _gen_job_id()
            self._jobs[job["id"]] = job
            return job["id"]

    def wait_for_terminal(self, job_id, deadline):
        """Block until ``job_id`` is "done" or ``deadline`` is reached.

        Returns a snapshot dict (see ``_snapshot_job``), or ``None`` if the
        id is unknown to us — handled by the caller as "job tidak ditemukan".
        """
        with self._cv:
            self._gc_locked()
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] != "running":
                with job["lock"]:
                    return _snapshot_job(job)

            remaining = max(0.001, deadline - time.time())
            self._cv.wait_for(
                lambda: job_id not in self._jobs
                or self._jobs.get(job_id, {}).get("status") != "running",
                timeout=remaining,
            )
            self._gc_locked()
            job = self._jobs.get(job_id)
            if job is None:
                return None
            with job["lock"]:
                return _snapshot_job(job)

    def mark_complete(self, job_id):
        """Mark ``job_id`` as done and wake any ``wait_for_terminal`` callers.

        Idempotent: a second call after the first is a no-op (the job is
        already "done"). The ``completed_at`` field might already be present
        (initialised to ``None`` when the job was created), so we check the
        concrete value, not key presence.
        """
        with self._cv:
            job = self._jobs.get(job_id)
            if job is not None and job.get("completed_at") is None:
                job["completed_at"] = time.time()
                job["status"] = "done"
            self._cv.notify_all()

    def count(self):
        """Test hook: number of jobs currently retained."""
        with self._cv:
            self._gc_locked()
            return len(self._jobs)

    def _gc_locked(self):
        """Drop done jobs older than retention. Must be called with ``self._cv`` held."""
        cutoff = time.time() - self._retention
        dead = [
            jid
            for jid, j in self._jobs.items()
            if j["status"] == "done"
            and j.get("completed_at", 0) < cutoff
        ]
        for jid in dead:
            self._jobs.pop(jid, None)


# ---------------------------------------------------------------------------
# Per-path deploy lock
# ---------------------------------------------------------------------------

class DeployLock:
    """In-flight deploys: at most one per path, and at most ``max_total``
    overall (parallel builds can starve the production box)."""

    def __init__(self, max_total=2):
        self._lock = threading.Lock()
        self._busy = set()
        self._max = max_total

    def acquire(self, path):
        """Returns None on success, else the reason it was refused."""
        with self._lock:
            if path in self._busy:
                return "deploy lain sedang berjalan untuk %s; tolak permintaan ini." % path
            if len(self._busy) >= self._max:
                return "server sibuk: sudah %d deploy berjalan; coba lagi nanti." % len(self._busy)
            self._busy.add(path)
            return None

    def release(self, path):
        with self._lock:
            self._busy.discard(path)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class NgeupdateHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ngeupdate/1.0"
    # Socket timeout per read/write: a client trickling headers/body can't pin
    # a thread forever. Long deploys are fine: nothing is read while they run.
    timeout = SOCKET_TIMEOUT

    # Subclasses / factory set these.
    config = None
    deploy_lock = None
    job_store = None

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def log_message(self, format, *args):  # noqa: A002 - signature mandated by stdlib
        sys.stderr.write(
            "%s - %s\n" % (self.address_string(), format % args)
        )

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

    def _send_text(self, status, text, content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self._wfile_write(body)

    def _wfile_write(self, body):
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------
    def do_GET(self):  # noqa: N802 - stdlib name
        self._send_json(405, {"error": "method not allowed"})

    def do_DELETE(self):  # noqa: N802
        self._send_json(405, {"error": "method not allowed"})

    def do_POST(self):  # noqa: N802
        if self.path != "/mcp":
            self._send_json(404, {"error": "not found"})
            return

        # MCP spec: validate Origin against DNS rebinding. CLI/IDE clients send
        # none; a browser always does, and no browser should talk to this.
        if self.headers.get("Origin"):
            self._send_json(403, {"error": "origin not allowed"})
            return

        if not _auth_ok(self.headers, self.config.key):
            self._send_json(401, {"error": "unauthorized"})
            return

        length_hdr = self.headers.get("Content-Length")
        if length_hdr is None:
            self._send_json(411, {"error": "length required"})
            return
        try:
            length = int(length_hdr)
        except ValueError:
            self._send_json(400, {"error": "bad content-length"})
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._send_json(413, {"error": "body too large"})
            return

        try:
            raw = self.rfile.read(length) if length > 0 else b""
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._send_json(400, {"error": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._send_json(400, {"error": "invalid json-rpc envelope"})
            return

        method = payload.get("method")
        req_id = payload.get("id")
        params = payload.get("params") or {}

        # Notifications: acknowledge with 202 + empty body, no JSON-RPC envelope.
        if isinstance(method, str) and method.startswith("notifications/"):
            self._send_text(202, "")
            return

        if req_id is None:
            # Spec says notifications omit id; we already handled them above.
            self._send_json(400, {"error": "missing id"})
            return

        result, error = self._dispatch(method, params, self.headers)
        if error is not None:
            self._send_json(200, _jsonrpc_error(req_id, error["code"], error["message"]))
        else:
            self._send_json(200, _jsonrpc_result(req_id, result))

    # ------------------------------------------------------------------
    # MCP method dispatch
    # ------------------------------------------------------------------
    def _dispatch(self, method, params, headers):
        if method == "initialize":
            client_version = params.get("protocolVersion") if isinstance(params, dict) else None
            version = client_version if isinstance(client_version, str) else "2025-03-26"
            return (
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                    "instructions": (
                        "The deploy tool runs asynchronously: it validates "
                        "the request synchronously then returns "
                        "immediately with a job id like "
                        "'🚀 STARTED job a1b2c3 — /srv/apps/myapp (fast). "
                        "Panggil deploy_status(job_id=\"a1b2c3\") sampai "
                        "DONE/FAILED.' Call deploy_status(job_id=...) "
                        "in a tight loop until the response is the final "
                        "'✅ DONE ...' / '❌ FAILED ...' (or the unknown-job "
                        "error). Always show that final output's first "
                        "status line to the user, plus any ⚠️ warning or "
                        "ℹ️ info line that follows. The deploy tool also "
                        "supports optional migrate=true and seed=true "
                        "flags. Set them ONLY when the user explicitly "
                        "asked to run migrations or seeders (e.g. "
                        "\"deploy + migrate\", \"db seed\", \"apply "
                        "migration\"). Never set them by default — the "
                        "project has to opt in via X-Migrate-Cmd / "
                        "X-Seed-Cmd / X-Compose-Service headers in "
                        ".mcp.json."
                    ),
                },
                None,
            )

        if method == "ping":
            return ({}, None)

        if method == "tools/list":
            return ({"tools": list(TOOL_SCHEMAS)}, None)

        if method == "tools/call":
            return self._call_tool(params, headers)

        if isinstance(method, str) and method.startswith("notifications/"):
            # Already handled in do_POST, but keep the door closed here too.
            return (None, {"code": -32601, "message": "notification has no response"})

        return (None, {"code": -32601, "message": "method not found: %s" % method})

    # ------------------------------------------------------------------
    # tools/call
    # ------------------------------------------------------------------
    def _call_tool(self, params, headers):
        if not isinstance(params, dict):
            return (None, {"code": -32602, "message": "params must be object"})
        name = params.get("name")
        if name == "deploy":
            return self._call_deploy(params, headers)
        if name == "deploy_status":
            return self._call_deploy_status(params, headers)
        return (
            None,
            {"code": -32602, "message": "unknown tool: %s" % name},
        )

    def _call_deploy(self, params, headers):
        """Validate synchronously, then kick the deploy into a background
        thread and return immediately with a job id. The result is read
        back via ``_call_deploy_status``.

        Pre-flight errors (bad path, bad mode, missing migrate headers,
        busy path, …) are returned here, INSIDE the JSON-RPC response — no
        job is created for those, so ``deploy_status`` wouldn't have
        anything to look up.
        """
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return (None, {"code": -32602, "message": "arguments must be object"})

        arg_path = args.get("path")
        if arg_path in (None, ""):
            arg_path = headers.get("X-Project-Path", "")
        mode = args.get("mode", "fast")
        if mode not in ("fast", "clean"):
            return (
                _tool_call_text(
                    "mode tidak valid: %r (pilih 'fast' atau 'clean')" % mode,
                    is_error=True,
                ),
                None,
            )

        # Strict: "false" (a string) must not turn into True and run a seed.
        migrate = args.get("migrate", False)
        seed = args.get("seed", False)
        if not isinstance(migrate, bool) or not isinstance(seed, bool):
            return (
                _tool_call_text("migrate / seed harus boolean true/false", is_error=True),
                None,
            )

        try:
            project_path = resolve_project_path(arg_path, self.config.allowed_root)
        except ValueError as exc:
            return (_tool_call_text("path tidak valid: %s" % exc, is_error=True), None)

        # Validate migrate / seed config BEFORE any side-effecting step
        # (git pull). All errors here mean nothing ran.
        try:
            service, migrate_argv, seed_argv, migrate_paths, seed_paths = (
                _validate_migrate_seed(migrate, seed, headers)
            )
        except ValueError as exc:
            return (_tool_call_text(exc.args[0], is_error=True), None)

        refused = self.deploy_lock.acquire(project_path)
        if refused:
            return (_tool_call_text(refused, is_error=True), None)

        job_id = _gen_job_id()
        job = {
            "id": job_id,
            "path": project_path,
            "mode": mode,
            "migrate_requested": migrate,
            "seed_requested": seed,
            "service": service,
            "migrate_paths": list(migrate_paths or []),
            "seed_paths": list(seed_paths or []),
            "started_at": time.time(),
            "completed_at": None,
            "status": "running",
            "steps": [],
            "current": None,
            "diff_files": [],
            "ok": None,
            "total_seconds": None,
            "total_steps": 0,
            "lock": threading.Lock(),
            "ip": headers.get("CF-Connecting-IP") or self.address_string() or "",
        }

        job_id = self.job_store.create(job)
        started_at = job["started_at"]

        def _run_in_thread():
            try:
                try:
                    steps, ok, total, diff_files = run_deploy(
                        project_path,
                        mode,
                        per_step_timeout=self.config.deploy_timeout,
                        service=service,
                        migrate_argv=migrate_argv,
                        seed_argv=seed_argv,
                        health_timeout=self.config.health_timeout,
                        job=job,
                    )
                    with job["lock"]:
                        job["steps"] = steps
                        job["ok"] = ok
                        job["total_seconds"] = total
                        job["diff_files"] = diff_files
                except Exception as exc:  # pragma: no cover - defensive
                    err_step = {
                        "command": ["internal-error"],
                        "returncode": -1,
                        "timed_out": False,
                        "elapsed_seconds": 0,
                        "output_tail": "internal error saat deploy %s: %r"
                        % (project_path, exc),
                    }
                    with job["lock"]:
                        job["steps"] = [err_step]
                        job["ok"] = False
                        job["total_seconds"] = round(time.time() - started_at, 2)
            finally:
                # Lock release happens here (NOT in the handler), so the
                # path is busy until the deploy actually finishes — that's
                # what protects the production box from a stampede.
                try:
                    self.deploy_lock.release(project_path)
                except Exception:
                    pass
                self.job_store.mark_complete(job_id)
                _log_event({
                    "event": "deploy_end",
                    "job": job_id,
                    "path": project_path,
                    "mode": mode,
                    "migrate": bool(migrate),
                    "seed": bool(seed),
                    "ip": job.get("ip", ""),
                    "ok": job.get("ok"),
                    "failed_step": (
                        " ".join(job["steps"][-1]["command"])
                        if job.get("steps") and not job.get("ok")
                        else None
                    ),
                    "seconds": round(time.time() - started_at, 2),
                })

        t = threading.Thread(
            target=_run_in_thread,
            daemon=True,
            name="deploy-%s" % job_id,
        )
        t.start()

        msg = (
            "🚀 STARTED job %s — %s (%s). Panggil "
            'deploy_status(job_id="%s") sampai DONE/FAILED.'
            % (job_id, project_path, mode, job_id)
        )
        _log_event({
            "event": "deploy_start",
            "job": job_id,
            "path": project_path,
            "mode": mode,
            "migrate": bool(migrate),
            "seed": bool(seed),
            "ip": job["ip"],
        })
        return (_tool_call_text(msg, is_error=False), None)

    def _call_deploy_status(self, params, headers):
        """Long-poll the job store. Returns the formatted final output if
        the job finished during the wait, or a "⏳ RUNNING ..." line if the
        25-second window expired first."""
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return (None, {"code": -32602, "message": "arguments must be object"})
        job_id = args.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            return (
                _tool_call_text(
                    "job_id wajib diisi (string)",
                    is_error=True,
                ),
                None,
            )

        snapshot = self.job_store.wait_for_terminal(
            job_id, time.time() + DEPLOY_STATUS_POLL
        )
        if snapshot is None:
            return (
                _tool_call_text(
                    "job tidak ditemukan (expired / server restart): %s" % job_id,
                    is_error=True,
                ),
                None,
            )

        if snapshot["status"] != "running":
            text = format_deploy_result(
                snapshot["path"],
                snapshot["mode"],
                snapshot["steps"],
                bool(snapshot["ok"]),
                snapshot["total_seconds"] or 0.0,
                migrate_paths=snapshot.get("migrate_paths") or [],
                seed_paths=snapshot.get("seed_paths") or [],
                migrate_requested=snapshot.get("migrate_requested", False),
                seed_requested=snapshot.get("seed_requested", False),
                diff_files=snapshot.get("diff_files") or [],
            )
            return (_tool_call_text(text, is_error=not snapshot["ok"]), None)

        # Still running after the wait window — show current step.
        current = snapshot.get("current")
        if current:
            elapsed = max(0, time.time() - current["started"])
            line = "⏳ RUNNING job %s — step %d/%d: %s (%s)" % (
                job_id,
                current["idx"],
                current["total"],
                current["label"],
                _fmt_duration(elapsed),
            )
        else:
            steps_done = len(snapshot.get("steps") or [])
            total = snapshot.get("total_steps") or steps_done
            line = "⏳ RUNNING job %s — %d/%d step selesai" % (
                job_id, steps_done, total
            )
        return (_tool_call_text(line, is_error=False), None)


# ---------------------------------------------------------------------------
# Threading server
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    config = load_config()
    handler = NgeupdateHandler
    handler.config = config
    handler.deploy_lock = DeployLock(config.max_deploys)
    handler.job_store = JobStore()
    server = ThreadingHTTPServer((config.host, config.port), handler)
    sys.stderr.write(
        "ngeupdate listening on http://%s:%d (root=%s, timeout=%ds, health=%ds)\n"
        % (
            config.host, config.port, config.allowed_root,
            config.deploy_timeout, config.health_timeout,
        )
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("shutting down\n")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
