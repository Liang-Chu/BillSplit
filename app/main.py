import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import attachments, auth, expenses, exports, groups, me, routing, settlements
from app.config import get_settings
from app.db import SessionLocal, init_engine
from app.errors import install_error_handlers

API_PREFIX = "/api/v1"
log = logging.getLogger("billsplit")


def run_migrations() -> None:
    from alembic import command
    from alembic.config import Config
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", get_settings().db_url)
    command.upgrade(cfg, "head")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.bootstrap import bootstrap_admin
    from app.jobs import scheduler_loop

    settings = get_settings()
    init_engine()
    if getattr(app.state, "run_migrations", True):
        run_migrations()
    with SessionLocal() as db:
        if bootstrap_admin(db):
            log.warning("Created super admin '%s'", settings.admin_username)
    task = asyncio.create_task(scheduler_loop()) if settings.scheduler_enabled else None
    yield
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def create_app(*, run_migrations_on_start: bool = True) -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="BillSplit API",
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.run_migrations = run_migrations_on_start
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )
    install_error_handlers(app)
    for r in (
        auth.router, me.router, me.admin, groups.router, routing.router, expenses.router,
        settlements.router, attachments.router, exports.router,
    ):
        app.include_router(r, prefix=API_PREFIX)

    @app.get(f"{API_PREFIX}/health", tags=["meta"])
    def health():
        return {
            "status": "ok",
            "version": __version__,
            "ocr_enabled": settings.ocr_enabled,
            "storage": "r2" if settings.r2_configured else "local",
        }

    return app


app = create_app()
