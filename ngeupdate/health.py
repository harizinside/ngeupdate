"""Container healthcheck: parse ``docker compose ps`` output, poll until
every service is healthy twice in a row.

`_wait_healthy` is invoked as the final pipeline step by `run_deploy`
(when the build + up succeed). Anything else (build failure, exited
containers) leaves the deploy marked failed at the failing step.
"""
import json
import subprocess
import time

from .config import STEP_ENV, TAIL_CHARS


HEALTH_POLL_INTERVAL = 5  # seconds between docker compose ps checks


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
                env=STEP_ENV,
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