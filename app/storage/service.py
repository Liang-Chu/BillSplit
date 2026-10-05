"""R2 primary, local fallback. Files that fell back are re-synced to R2 later and the local copy removed."""

import logging
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Attachment
from app.storage.base import StorageBackend
from app.storage.local import LocalStorage

log = logging.getLogger(__name__)


class StorageService:
    def __init__(self, local: LocalStorage, r2: StorageBackend | None):
        self.local = local
        self.r2 = r2

    def backend(self, name: str) -> StorageBackend:
        if name == "r2":
            if self.r2 is None:
                raise RuntimeError("R2 is not configured")
            return self.r2
        return self.local

    def put(self, key: str, data: bytes, content_type: str) -> tuple[str, str]:
        """Returns (backend_name, sync_status)."""
        if self.r2 is not None:
            try:
                self.r2.put(key, data, content_type)
                return "r2", "ok"
            except Exception:
                log.exception("R2 upload failed for %s; storing locally", key)
                self.local.put(key, data, content_type)
                return "local", "pending"
        self.local.put(key, data, content_type)
        return "local", "ok"

    def get(self, backend: str, key: str) -> bytes:
        return self.backend(backend).get(key)

    def sync_pending(self, db: Session) -> int:
        """Push locally stored fallback files to R2. Returns the number synced."""
        if self.r2 is None:
            return 0
        done = 0
        pending = db.scalars(select(Attachment).where(Attachment.sync_status == "pending")).all()
        for a in pending:
            try:
                for key, ct in ((a.storage_key, a.mime), (a.thumb_key, "image/jpeg")):
                    if key:
                        self.r2.put(key, self.local.get(key), ct)
            except Exception:
                log.warning("R2 re-sync failed for attachment %s", a.id)
                continue
            a.storage_backend = "r2"
            a.sync_status = "ok"
            db.commit()
            for key in (a.storage_key, a.thumb_key):
                if key:
                    self.local.delete(key)
            done += 1
        return done


@lru_cache
def get_storage() -> StorageService:
    s = get_settings()
    r2 = None
    if s.r2_configured:
        from app.storage.r2 import R2Storage

        r2 = R2Storage(s.r2_endpoint_url, s.r2_access_key_id, s.r2_secret_access_key, s.r2_bucket, s.r2_presign_seconds)
    return StorageService(LocalStorage(s.data_dir / "files"), r2)
