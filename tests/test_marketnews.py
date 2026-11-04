import httpx
import pytest

from app.services.marketnews import MarketNewsClient, MarketNewsError

URL = "https://marketnewsfeed.test/api/feed?limit=50"


def client_returning(handler) -> MarketNewsClient:  # type: ignore[no-untyped-def]
    return MarketNewsClient(URL, transport=httpx.MockTransport(handler))


async def test_parses_items_oldest_first() -> None:
    payload = {
        "items": [
            {"hid": 2, "ts": 200.0, "text": "Second", "link": "https://a", "cat": "MACRO",
             "dir": "bull", "extra_field": "ignored"},
            {"hid": 1, "ts": 100.0, "text": "First"},
        ]
    }
    client = client_returning(lambda request: httpx.Response(200, json=payload))
    items = await client.fetch_latest()
    await client.close()

    assert [item.hid for item in items] == [1, 2]
    assert items[1].cat == "MACRO"
    assert items[0].link is None


async def test_accepts_bare_list() -> None:
    client = client_returning(
        lambda request: httpx.Response(200, json=[{"hid": 5, "ts": 1, "text": "x"}])
    )
    assert [i.hid for i in await client.fetch_latest()] == [5]
    await client.close()


async def test_skips_invalid_items_and_duplicates() -> None:
    payload = {
        "items": [
            {"hid": 1, "ts": 1, "text": "ok"},
            {"hid": 1, "ts": 1, "text": "ok again"},  # duplicate hid
            {"ts": 2, "text": "no hid"},
            {"hid": 3, "ts": 3, "text": "   "},  # empty text
            "not an object",
        ]
    }
    client = client_returning(lambda request: httpx.Response(200, json=payload))
    items = await client.fetch_latest()
    await client.close()
    assert [i.hid for i in items] == [1]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(200, json={"unexpected": True}),
    ],
)
async def test_bad_responses_raise(response: httpx.Response) -> None:
    client = client_returning(lambda request: response)
    with pytest.raises(MarketNewsError):
        await client.fetch_latest()
    await client.close()


async def test_network_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    client = client_returning(handler)
    with pytest.raises(MarketNewsError, match="unavailable"):
        await client.fetch_latest()
    await client.close()
