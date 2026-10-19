"""In-memory job store + per-path deploy lock.

No persistence. A server restart loses all in-flight jobs AND any running
child process. That's the explicit tradeoff — file-based status would be
the upgrade path if it ever becomes painful.
"""
import secrets
import threading
import time


JOB_RETENTION_SECONDS = 60 * 60  # drop done jobs after 1h


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