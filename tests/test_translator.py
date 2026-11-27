"""Translator tests with a fake Anthropic client: no real API calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest

from app.services.translator import (
    BATCH_SIZE,
    Translation,
    Translator,
    TranslatorError,
    _TranslatedBatch,
    _TranslatedItem,
)


def response(entries: list[tuple[int, str, str]], stop_reason: str = "end_turn") -> SimpleNamespace:
    parsed = _TranslatedBatch(
        items=[_TranslatedItem(id=i, title=t, description=d) for i, t, d in entries]
    )
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=150, output_tokens=60),
    )


def make_translator(*responses: object) -> tuple[Translator, AsyncMock]:
    client = AsyncMock()
    client.messages.parse.side_effect = list(responses)
    return Translator("unused", "claude-haiku-5-5", client=client), client


async def test_translation_is_returned(make_item) -> None:  # type: ignore[no-untyped-def]
    translator, client = make_translator(
        response([(1, " Bitcoin 60 000 dollardan pastga tushdi ", "")])
    )
    result = await translator.translate([make_item(1, text="BITCOIN FALLS BELOW $60,000")])
    assert result == {1: Translation("Bitcoin 60 000 dollardan pastga tushdi", "")}
    kwargs = client.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    assert "BITCOIN FALLS BELOW $60,000" in kwargs["messages"][0]["content"]
    assert kwargs["max_tokens"] <= 1000  # kept small


async def test_large_input_is_batched(make_item) -> None:  # type: ignore[no-untyped-def]
    items = [make_item(i) for i in range(1, BATCH_SIZE + 3)]
    first = response([(i.hid, f"Sarlavha {i.hid}", "") for i in items[:BATCH_SIZE]])
    second = response([(i.hid, f"Sarlavha {i.hid}", "") for i in items[BATCH_SIZE:]])
    translator, client = make_translator(first, second)
    assert len(await translator.translate(items)) == len(items)
    assert client.messages.parse.await_count == 2


async def test_missing_or_empty_translations_are_left_out(make_item) -> None:  # type: ignore[no-untyped-def]
    translator, _ = make_translator(response([(1, "Yangilik", "Tavsif"), (2, "  ", "")]))
    result = await translator.translate([make_item(1), make_item(2), make_item(3)])
    assert set(result) == {1}


@pytest.mark.parametrize("kind", ["timeout", "rate_limit"])
async def test_api_errors_become_translator_errors(make_item, kind: str) -> None:  # type: ignore[no-untyped-def]
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    if kind == "timeout":
        error: Exception = anthropic.APITimeoutError(request=request)
    else:
        error = anthropic.RateLimitError(
            "rate limited", response=httpx2.Response(429, request=request), body=None
        )
    translator, _ = make_translator(error)
    with pytest.raises(TranslatorError):
        await translator.translate([make_item(1)])


async def test_truncated_output_is_an_error(make_item) -> None:  # type: ignore[no-untyped-def]
    translator, _ = make_translator(response([(1, "Yangilik", "")], stop_reason="max_tokens"))
    with pytest.raises(TranslatorError):
        await translator.translate([make_item(1)])


def test_tags_are_cleaned() -> None:
    from app.services.marketnews import AssetTag
    from app.services.translator import _clean_tags

    tags = _clean_tags([
        AssetTag(symbol="$nvda", country="us", change_pct=4.6),
        AssetTag(symbol="NVDA", country="US"),  # repeat
        AssetTag(symbol="S&P 500", country="US"),  # invalid symbol
        AssetTag(symbol="GOLD", country="world"),  # invalid country
    ])
    assert tags == (AssetTag(symbol="NVDA", country="US", change_pct=4.6), AssetTag(symbol="GOLD"))
