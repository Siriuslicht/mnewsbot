"""ImportanceScorer tests with a fake Anthropic client: no real API calls."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx2
import pytest

from app.services.importance import (
    BATCH_SIZE,
    ImportanceScore,
    ImportanceScorer,
    ImportanceScorerError,
    _ScoredBatch,
    _ScoredHeadline,
)


def response(entries: list[tuple[int, int, str]], stop_reason: str = "end_turn") -> SimpleNamespace:
    parsed = _ScoredBatch(items=[_ScoredHeadline(id=i, score=s, category=c) for i, s, c in entries])
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=20),
    )


def make_scorer(*responses: object) -> tuple[ImportanceScorer, AsyncMock]:
    client = AsyncMock()
    client.messages.parse.side_effect = list(responses)
    return ImportanceScorer("unused", "claude-haiku-5-5", client=client), client


async def test_scores_are_parsed_and_clamped(make_item) -> None:  # type: ignore[no-untyped-def]
    scorer, client = make_scorer(response([(1, 5, "Rates"), (2, 9, "oil price"), (3, 0, "")]))
    result = await scorer.score([make_item(1), make_item(2), make_item(3)])
    assert result == {
        1: ImportanceScore(5, "rates"),
        2: ImportanceScore(5, "oil"),
        3: ImportanceScore(1, "other"),
    }
    kwargs = client.messages.parse.call_args.kwargs
    assert kwargs["model"] == "claude-haiku-5-5"
    assert '"id": 1' in kwargs["messages"][0]["content"]


async def test_batches_of_twenty(make_item) -> None:  # type: ignore[no-untyped-def]
    items = [make_item(i) for i in range(1, BATCH_SIZE + 6)]
    first = response([(i.hid, 4, "stocks") for i in items[:BATCH_SIZE]])
    second = response([(i.hid, 2, "other") for i in items[BATCH_SIZE:]])
    scorer, client = make_scorer(first, second)
    result = await scorer.score(items)
    assert client.messages.parse.await_count == 2
    assert len(result) == len(items)


async def test_missing_and_unknown_ids_are_ignored(make_item) -> None:  # type: ignore[no-untyped-def]
    scorer, _ = make_scorer(response([(1, 4, "gold"), (999, 5, "macro")]))
    result = await scorer.score([make_item(1), make_item(2)])
    assert set(result) == {1}  # 2 is retried next cycle, 999 was never asked


async def test_api_errors_become_scorer_errors(make_item) -> None:  # type: ignore[no-untyped-def]
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    error = anthropic.APIConnectionError(request=request)
    scorer, _ = make_scorer(error)
    with pytest.raises(ImportanceScorerError):
        await scorer.score([make_item(1)])


async def test_refusal_or_truncation_is_an_error(make_item) -> None:  # type: ignore[no-untyped-def]
    scorer, _ = make_scorer(response([(1, 4, "oil")], stop_reason="max_tokens"))
    with pytest.raises(ImportanceScorerError):
        await scorer.score([make_item(1)])
