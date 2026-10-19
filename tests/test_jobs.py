"""Tests for the in-memory job store + per-path deploy lock."""
import threading
import time
import unittest

from ngeupdate import jobs


# ---------------------------------------------------------------------------
# DeployLock: at most one per path, at most max_total overall
# ---------------------------------------------------------------------------

class DeployLockTests(unittest.TestCase):
    def test_same_path_refused(self):
        lock = jobs.DeployLock(max_total=5)
        self.assertIsNone(lock.acquire("/a"))
        self.assertIn("sedang berjalan", lock.acquire("/a"))

    def test_global_cap(self):
        lock = jobs.DeployLock(max_total=2)
        self.assertIsNone(lock.acquire("/a"))
        self.assertIsNone(lock.acquire("/b"))
        self.assertIn("server sibuk", lock.acquire("/c"))
        lock.release("/a")
        self.assertIsNone(lock.acquire("/c"))


# ---------------------------------------------------------------------------
# JobStore: wait_for_terminal + mark_complete + GC + collision handling
# ---------------------------------------------------------------------------

class JobStoreTests(unittest.TestCase):
    def _make_job(self, jid, status="running"):
        return {
            "id": jid,
            "path": "/p",
            "mode": "fast",
            "started_at": time.time(),
            "completed_at": None,
            "status": status,
            "steps": [],
            "current": None,
            "diff_files": [],
            "ok": None,
            "total_seconds": None,
            "total_steps": 0,
            "lock": threading.Lock(),
            "ip": "127.0.0.1",
            "migrate_paths": [],
            "seed_paths": [],
            "migrate_requested": False,
            "seed_requested": False,
            "service": None,
        }

    def test_create_and_wait_completes_after_mark(self):
        store = jobs.JobStore()
        j = self._make_job("aaaa11")
        store.create(j)
        # While running, wait_for_terminal blocks until the deadline
        # OR mark_complete fires.
        completed = threading.Event()
        def waiter():
            r = store.wait_for_terminal("aaaa11", time.time() + 1.0)
            completed.set()
            return r
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        store.mark_complete("aaaa11")
        t.join(timeout=2)
        self.assertTrue(completed.is_set())
        snap = store.wait_for_terminal("aaaa11", time.time() + 1.0)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["status"], "done")
        self.assertEqual(snap["id"], "aaaa11")

    def test_unknown_job_id_returns_none(self):
        store = jobs.JobStore()
        r = store.wait_for_terminal("missing", time.time() + 0.1)
        self.assertIsNone(r)

    def test_gc_drops_old_done_jobs(self):
        store = jobs.JobStore(retention_seconds=0)
        store.create(self._make_job("oldjob", status="done"))
        store._jobs["oldjob"]["completed_at"] = time.time() - 5
        store._gc_locked()
        self.assertEqual(store.count(), 0)

    def test_mark_complete_idempotent(self):
        store = jobs.JobStore()
        j = self._make_job("twice")
        store.create(j)
        store.mark_complete("twice")
        # Second call must not flip status back, and must not crash.
        store.mark_complete("twice")
        snap = store.wait_for_terminal("twice", time.time() + 0.5)
        self.assertEqual(snap["status"], "done")
        self.assertIsNotNone(snap["completed_at"])

    def test_mark_complete_on_unknown_job_is_no_op(self):
        # The worker thread and the polling call can be racing; mark_complete
        # on a job id we don't know must not crash (e.g. GC swept it).
        store = jobs.JobStore()
        store.mark_complete("ghost")  # no raise


class JobIdTests(unittest.TestCase):
    def test_collision_never_overwrites_live_job(self):
        store = jobs.JobStore()
        a = {"id": "dup", "status": "running", "lock": threading.Lock()}
        b = {"id": "dup", "status": "running", "lock": threading.Lock()}
        self.assertEqual(store.create(a), "dup")
        new_id = store.create(b)
        self.assertNotEqual(new_id, "dup")
        self.assertEqual(store.count(), 2)


if __name__ == "__main__":
    unittest.main()