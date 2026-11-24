"""FastAPI entry point.

The lifespan hook builds the services, starts the scheduler, and shuts
everything down in the right order. HTTP endpoints exist for monitoring only.
"""

from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.database.database import create_db_engine, create_session_factory, init_db, ping_db
from app.scheduler import NewsPoller, create_scheduler
from app.services.importance import ImportanceScorer
from app.services.marketnews import MarketNewsClient
from app.services.storage import NewsStorage
from app.services.telegram import TelegramPublisher
from app.services.translator import Translator
from app.utils.lock import SingleInstanceLock
from app.utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    token = settings.bot_token.get_secret_value()
    claude_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
    setup_logging(
        settings.log_level, settings.log_format, settings.log_file, secrets=[token, claude_key]
    )
    logger.info("Starting in %s mode", settings.environment)

    # AsyncExitStack runs every registered cleanup in reverse order, even if
    # startup fails half-way (e.g. bad Telegram permissions).
    async with AsyncExitStack() as stack:
        engine = create_db_engine(settings.database_url)
        stack.callback(engine.dispose)
        init_db(engine)
        storage = NewsStorage(create_session_factory(engine))

        app.state.settings = settings
        app.state.engine = engine
        app.state.started_at = datetime.now(UTC)
        app.state.poller = None
        app.state.publisher = None

        lock = SingleInstanceLock(settings.lock_file)
        if not lock.acquire():
            # Another process already runs the scheduler. Serving HTTP is
            # harmless, but posting would create duplicates, so stay passive.
            logger.warning("Another instance holds %s; running in standby mode", settings.lock_file)
            yield
            logger.info("Standby instance stopped")
            return
        stack.callback(lock.release)

        news_client = MarketNewsClient(settings.marketnews_url, settings.http_timeout)
        stack.push_async_callback(news_client.close)

        publisher = TelegramPublisher(
            token, settings.channel_id, max_attempts=settings.telegram_max_attempts
        )
        stack.push_async_callback(publisher.stop)
        # Fail fast on a bad token or missing channel permissions.
        await publisher.start()

        scorer = None
        if settings.importance_filter and settings.anthropic_api_key:
            scorer = ImportanceScorer(
                settings.anthropic_api_key.get_secret_value(),
                settings.claude_model,
                settings.claude_timeout,
            )
            stack.push_async_callback(scorer.close)
            logger.info(
                "Importance filter on: %s, min score %d, cap %d/day",
                settings.claude_model, settings.min_importance, settings.daily_post_cap,
            )
        else:
            logger.warning("Importance filter OFF: every non-excluded item is posted (still capped)")

        translator = None
        if settings.translate_uz and settings.anthropic_api_key:
            translator = Translator(
                settings.anthropic_api_key.get_secret_value(),
                settings.claude_model,
                settings.claude_timeout,
            )
            stack.push_async_callback(translator.close)
            logger.info("Uzbek translation on: %s", settings.claude_model)
        else:
            logger.warning("Uzbek translation OFF: posts stay in English")

        poller = NewsPoller(
            news_client,
            publisher,
            storage,
            settings.post_delay,
            settings.initial_sync_mode,
            excluded_accounts=settings.excluded_accounts,
            scorer=scorer,
            translator=translator,
            min_importance=settings.min_importance,
            daily_cap=settings.daily_post_cap,
            burst=settings.post_burst,
            timezone=ZoneInfo(settings.bot_timezone),
        )
        scheduler = create_scheduler(poller, settings.poll_interval)
        scheduler.start()
        app.state.poller = poller
        app.state.publisher = publisher
        logger.info("Scheduler started, polling every %ds", settings.poll_interval)

        try:
            yield
        finally:
            logger.info("Shutting down")
            scheduler.shutdown(wait=False)  # no new cycles
            await poller.stop(settings.shutdown_timeout)  # let the current one finish
    logger.info("Shutdown complete")


app = FastAPI(
    title="MarketNewsFeed Telegram Bot",
    lifespan=lifespan,
    # Interactive API docs are unnecessary attack surface in production.
    docs_url=None if get_settings().environment == "production" else "/docs",
    redoc_url=None,
    openapi_url=None if get_settings().environment == "production" else "/openapi.json",
)


@app.get("/")
async def root() -> dict[str, str]:
    return {"status": "running"}


@app.get("/health")
async def health(request: Request) -> JSONResponse:
    """Liveness: the process answers and the database is reachable.

    Used by the Docker healthcheck. It deliberately does NOT depend on
    Telegram or MarketNewsFeed: an outage there is not fixed by restarting us.
    """
    if not ping_db(request.app.state.engine):
        return JSONResponse({"status": "unhealthy", "database": "error"}, status_code=503)
    return JSONResponse({"status": "healthy", "database": "ok"})


@app.get("/ready")
async def ready(request: Request) -> JSONResponse:
    """Readiness: is the bot actually doing its job? For monitoring/uptime checks."""
    state = request.app.state
    checks: dict[str, str] = {"database": "ok" if ping_db(state.engine) else "error"}

    poller: NewsPoller | None = state.poller
    if poller is None:
        checks["scheduler"] = "standby"
    else:
        checks["telegram"] = "ok" if await state.publisher.check_connection() else "error"
        checks["scheduler"] = _scheduler_check(poller, state.started_at, state.settings.poll_interval)

    body: dict[str, object] = {"status": "ready", "checks": checks}
    if poller is not None:
        status = poller.status
        body["last_cycle_at"] = status.last_cycle_at.isoformat() if status.last_cycle_at else None
        body["last_error"] = status.last_error
        body["posted_since_start"] = status.total_posted

    if "error" in checks.values() or checks["scheduler"] == "stale":
        body["status"] = "not_ready"
        return JSONResponse(body, status_code=503)
    return JSONResponse(body)


def _scheduler_check(poller: NewsPoller, started_at: datetime, interval: int) -> str:
    """'stale' if no cycle has succeeded for several intervals (API/Telegram down for long)."""
    allowed = timedelta(seconds=max(interval * 5, 180))
    reference = poller.status.last_cycle_at or started_at
    return "stale" if datetime.now(UTC) - reference > allowed else "ok"
