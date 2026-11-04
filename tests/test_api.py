"""Endpoint tests. The lifespan (real Telegram, scheduler) is NOT started:
TestClient only runs it inside a `with` block, so we fill app.state ourselves."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.scheduler import NewsPoller


class FakePublisher:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    async def check_connection(self) -> bool:
        return self.ok


@pytest.fixture
def client(engine, storage):  # type: ignore[no-untyped-def]
    app.state.settings = get_settings()
    app.state.engine = engine
    app.state.started_at = datetime.now(UTC)
    app.state.publisher = FakePublisher()
    app.state.poller = NewsPoller(None, None, storage)  # type: ignore[arg-type]
    return TestClient(app)


def test_root(client: TestClient) -> None:
    assert client.get("/").json() == {"status": "running"}


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


def test_ready(client: TestClient) -> None:
    app.state.poller.status.last_cycle_at = datetime.now(UTC)
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "telegram": "ok", "scheduler": "ok"}


def test_ready_fails_when_telegram_down(client: TestClient) -> None:
    app.state.publisher = FakePublisher(ok=False)
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["telegram"] == "error"


def test_ready_fails_when_no_cycle_succeeds(client: TestClient) -> None:
    app.state.started_at = datetime.now(UTC) - timedelta(hours=1)
    app.state.poller.status.last_cycle_at = None
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["scheduler"] == "stale"


def test_standby_instance_is_ready(client: TestClient) -> None:
    app.state.poller = None
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["checks"]["scheduler"] == "standby"
