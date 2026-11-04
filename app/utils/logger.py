"""Central logging setup: text or JSON to stdout, optional rotating file, secret redaction."""

import json
import logging
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config import BOT_TOKEN_PATTERN

TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


class Redactor:
    """Removes secrets from final log lines, including exception tracebacks.

    Some libraries put the bot token into error messages (python-telegram-bot's
    InvalidToken does, and Bot API URLs contain it), so we scrub the formatted
    output instead of trusting every call site.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = [s for s in secrets if s]

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return BOT_TOKEN_PATTERN.sub("***", text)


class TextFormatter(logging.Formatter):
    def __init__(self, redact: Redactor) -> None:
        super().__init__(TEXT_FORMAT)
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:
        return self._redact(super().format(record))


class JsonFormatter(logging.Formatter):
    """One JSON object per line, ready for Loki, ELK, CloudWatch, etc."""

    def __init__(self, redact: Redactor) -> None:
        super().__init__()
        self._redact = redact

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return self._redact(json.dumps(entry, ensure_ascii=False))


def setup_logging(
    level: str = "INFO",
    fmt: str = "text",
    log_file: str = "",
    secrets: Iterable[str] = (),
) -> None:
    """Configure the root logger.

    stdout is the default because Docker collects it (and rotates it via the
    logging options in docker-compose.yml). `log_file` is for VPS runs without
    Docker: 10 MB per file, 5 files kept.
    """
    redact = Redactor(secrets)
    formatter: logging.Formatter = (
        JsonFormatter(redact) if fmt == "json" else TextFormatter(redact)
    )

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(level=level.upper(), handlers=handlers, force=True)

    # Third-party libraries are noisy at INFO. httpx in particular logs every
    # request URL, and Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Send uvicorn's logs through our handlers so every line has one format.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers = []
        uv_logger.propagate = True
    # Docker health checks every 30s would drown everything else.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
