from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

    return engine


_engine: Engine | None = None
SessionLocal = sessionmaker(autoflush=False, expire_on_commit=False)


def init_engine(url: str | None = None) -> Engine:
    global _engine
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    _engine = make_engine(url or settings.db_url)
    SessionLocal.configure(bind=_engine)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_engine()
    return _engine


def get_db() -> Iterator[Session]:
    """Request-scoped session. Use as Depends(get_db, scope="function") so the commit happens before the response."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
