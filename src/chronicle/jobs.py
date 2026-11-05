"""Bounded in-process jobs with cooperative CPU and real PostgreSQL cancellation.

Report identity is durable; this scheduling registry is not a durable queue. A
service restart loses job handles: recover via the retained report identity, never
claim an abandoned handle succeeded. Shutdown waits for execution termination.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from uuid import uuid4

from chronicle.errors import ContractError, public_error

TERMINAL = {"completed", "failed", "cancelled"}


@dataclass
class Job:
    job_id: str
    dataset_id: str
    owner: str
    report_id: str
    state: str = "queued"
    result: dict | None = None
    error: dict | None = None
    connection: object = None
    cancel_requested: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self):
        with self.lock:
            return {
                "job_id": self.job_id,
                "report_id": self.report_id,
                "state": self.state,
                "result": self.result,
                "error": self.error,
                "execution_stopped": self.done.is_set(),
            }


class JobManager:
    def __init__(self, connect, *, workers=4):
        self.connect = connect
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="chronicle-job")
        self.capacity = threading.BoundedSemaphore(workers)
        self.lock = threading.Lock()
        self.jobs = {}
        self.closed = False

    def submit(self, dataset_id, owner, report_id, action, *, guard=None):
        with self.lock:
            if self.closed or not self.capacity.acquire(blocking=False):
                raise ContractError("SERVICE_BUSY", "Executor is at capacity")
            for key in list(self.jobs):
                if len(self.jobs) < 256:
                    break
                if self.jobs[key].done.is_set():
                    del self.jobs[key]
            job = Job(str(uuid4()), dataset_id, owner, str(report_id))
            self.jobs[job.job_id] = job
            self.pool.submit(self._run, job, action, guard)
            return job

    def get(self, job_id, dataset_id, owner):
        with self.lock:
            job = self.jobs.get(job_id)
        if not job or job.dataset_id != dataset_id or job.owner != owner:
            raise ContractError("JOB_NOT_FOUND", "Job not found")
        return job

    def cancel(self, job):
        with job.lock:
            if job.state in TERMINAL or job.state == "committing":
                return None
            job.cancel_requested.set()
            job.state = "cancel_requested"
            conn = job.connection
        if conn is not None:
            try:
                conn.cancel_safe(timeout=2.0)
            except Exception:
                # A failed cancel request is not completed cancellation. The query
                # deadline and done flag remain authoritative, and pins stay live.
                pass

    def _run(self, job, action, guard):
        try:
            with guard() if guard else nullcontext(), self.connect("reader") as conn:
                with job.lock:
                    job.connection = conn
                    if job.cancel_requested.is_set():
                        raise ContractError("JOB_CANCELLED", "Job was cancelled")
                    job.state = "running"
                with conn.transaction():
                    action(conn)
                    with job.lock:
                        if job.cancel_requested.is_set():
                            raise ContractError("JOB_CANCELLED", "Job was cancelled")
                        # Cancellation no longer wins after this commit boundary.
                        # It returns 'committing', never a false cancelled success.
                        job.state = "committing"
                with job.lock:
                    # Retain only a durable identifier, never a many-megabyte report.
                    job.result = {"report_id": job.report_id}
                    job.state = "completed"
        except Exception as exc:
            with job.lock:
                if job.state == "completed":
                    job.error = {
                        "code": "CLEANUP_INCOMPLETE",
                        "message": "Report committed; inspect unreleased reader pins.",
                        "retryable": False,
                    }
                else:
                    job.state = "cancelled" if job.cancel_requested.is_set() else "failed"
                    job.error = public_error(exc) if job.state == "failed" else None
        finally:
            with job.lock:
                job.connection = None
                job.done.set()
            self.capacity.release()

    def close(self):
        with self.lock:
            self.closed = True
            jobs = list(self.jobs.values())
        for job in jobs:
            self.cancel(job)
        self.pool.shutdown(wait=True, cancel_futures=False)
