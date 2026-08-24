"""
src/job_queue.py
----------------
SQLite/Postgres polling job queue (Phase D) — no Redis.

- enqueue_job(user_id, youtube_url, filename=None, db) -> job_id (non-blocking)
- run_worker() polling every 3s, single worker, SKIP LOCKED for Postgres, lock for SQLite
- GET /jobs/{job_id}/status handled in server.py
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from src.logger import get_logger
from src.models import Job, SessionLocal, engine

log = get_logger("job_queue")

# Single worker lock to avoid race on SQLite
_worker_lock = threading.Lock()
_worker_running = False


def enqueue_job(user_id: int, youtube_url: Optional[str] = None, filename: Optional[str] = None, prompt_version: Optional[str] = None, db: Optional[Session] = None) -> str:
    """Create Job row with status=queued, return job_id immediately."""
    own_db = False
    if db is None:
        db = SessionLocal()
        own_db = True
    try:
        job_id = str(uuid.uuid4())
        job = Job(
            id=job_id,
            user_id=user_id,
            youtube_url=youtube_url or "",
            filename=filename or "",
            status="queued",
            progress_percent=0,
            prompt_version=prompt_version or "v1",
        )
        db.add(job)
        db.commit()
        log.info("Enqueued job %s for user %s (url=%s)", job_id[:8], user_id, (youtube_url or filename or "")[:60])
        return job_id
    finally:
        if own_db:
            db.close()


def _process_one_job(job: Job, db: Session):
    """Process single job - called by worker thread."""
    job_id = job.id
    # Import pipeline lazily to avoid circular
    from pathlib import Path

    # Reuse existing pipeline runner but per-job isolation
    # For now call server's _run_full_pipeline_task equivalent via DB job
    # We'll delegate to job-specific runner that updates DB progress
    try:
        job.status = "processing"
        job.progress_percent = 5
        db.commit()
        log.info("Worker processing job %s", job_id[:8])

        # Use same logic as server.py _run_full_pipeline_task but with DB progress callback
        # For simplicity, call a helper that runs pipeline and updates job
        from src.job_runner import run_job_pipeline

        run_job_pipeline(job, db)
        job.status = "done"
        job.progress_percent = 100
        db.commit()
        log.info("Job %s done", job_id[:8])
    except Exception as e:
        log.error("Job %s failed: %s", job_id[:8], e)
        try:
            job.status = "failed"
            job.error_message = str(e)[:2000]
            db.commit()
        except Exception:
            pass
        # Quota refund handled in quota.py if needed


def run_worker():
    """Background worker polling every 3s — single worker, SKIP LOCKED for Postgres."""
    global _worker_running
    # Prevent multiple workers
    with _worker_lock:
        if _worker_running:
            return
        _worker_running = True

    log.info("Job worker started (poll 3s, single worker)")

    # Detect Postgres vs SQLite for SKIP LOCKED
    is_postgres = str(engine.url).startswith("postgresql")

    while True:
        try:
            db = SessionLocal()
            try:
                # Poll oldest queued job
                if is_postgres:
                    # Postgres SKIP LOCKED
                    job = db.execute(
                        # Use raw SQL for SKIP LOCKED
                        __import__("sqlalchemy").text(
                            "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
                        )
                    ).fetchone()
                    if job:
                        job_id = job[0]
                        job_obj = db.query(Job).filter(Job.id == job_id).first()
                    else:
                        job_obj = None
                else:
                    # SQLite: simple lock + select (single worker so safe)
                    with _worker_lock:
                        job_obj = db.query(Job).filter(Job.status == "queued").order_by(Job.created_at).first()
                        if job_obj:
                            # Mark processing inside lock to avoid double pick
                            job_obj.status = "processing"
                            db.commit()

                if job_obj and is_postgres:
                    # For postgres we already locked, now mark processing
                    job_obj.status = "processing"
                    db.commit()
                    _process_one_job(job_obj, db)
                elif job_obj and not is_postgres:
                    # SQLite already marked processing, now process
                    # Need to re-fetch to ensure we have the object attached
                    _process_one_job(job_obj, db)
                else:
                    # No job, sleep
                    pass
            finally:
                db.close()
        except Exception as e:
            log.warning("Worker poll error: %s", e)
        time.sleep(3)


def start_worker_thread():
    """Start worker on FastAPI startup — daemon thread."""
    t = threading.Thread(target=run_worker, daemon=True, name="job-worker")
    t.start()
    log.info("Job worker thread started")
    return t
