"""In-process scheduler: period rollover/freeze and R2 re-sync. Idempotent; safe to run at any time."""

import asyncio
import logging

from fastapi.concurrency import run_in_threadpool

from app.config import get_settings
from app.db import SessionLocal
from app.services.periods import rollover_all
from app.storage import get_storage

log = logging.getLogger(__name__)


def run_jobs_once() -> None:
    db = SessionLocal()
    try:
        rollover_all(db)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("period rollover failed")
    try:
        n = get_storage().sync_pending(db)
        if n:
            log.info("re-synced %d attachments to R2", n)
    except Exception:
        log.exception("R2 re-sync failed")
    finally:
        db.close()


async def scheduler_loop() -> None:
    interval = get_settings().scheduler_interval_seconds
    while True:
        await run_in_threadpool(run_jobs_once)
        await asyncio.sleep(interval)
