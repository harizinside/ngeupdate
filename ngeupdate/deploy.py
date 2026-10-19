"""Pipeline construction + command execution + deploy worker.

`build_pipeline` is a pure function (so tests can assert on the exact
sequence without spinning up docker). `_run_step` / `_capture_head` /
`_diff_files` are subprocess wrappers. `run_deploy` is the worker that
orchestrates the pipeline under a job and reports back to the caller.
"""
import json
import subprocess
import sys
import time

from .config import STEP_ENV, TAIL_CHARS
from .health import _wait_healthy


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
            env=STEP_ENV,
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
            env=STEP_ENV,
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
            env=STEP_ENV,
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