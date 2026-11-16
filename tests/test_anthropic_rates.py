"""The builder translator's price table, held to Claude Code's own arithmetic.

Claude Code states each model's list-price total (``modelUsage.costUSD``) at the end of a run.
The fixtures carry the captured figures (``tests/fixtures/claude_stream/``), so the table is
checked against the vendor-side figure it must reproduce, not against itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from basecradle_router import anthropic_rates as rates

FIXTURES = Path(__file__).parent / "fixtures" / "claude_stream"


def _billed() -> list[tuple[str, str, dict]]:
    rows = []
    for path in sorted(FIXTURES.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            event = json.loads(line)
            if event.get("type") == "result":
                for model, figures in event["modelUsage"].items():
                    rows.append((path.stem, model, figures))
    return rows


BILLED = _billed()


def test_the_captures_bill_more_than_one_model_and_both_cache_lengths() -> None:
    models = {model for _, model, _ in BILLED}
    assert {"claude-opus-5-5", "claude-haiku-5-5"} <= models
    assert any(f["cacheCreationEphemeral5mInputTokens"] for _, _, f in BILLED)
    assert any(f["cacheCreationEphemeral1hInputTokens"] for _, _, f in BILLED)
    assert any(f["webSearchRequests"] for _, _, f in BILLED)


@pytest.mark.parametrize(
    ("case", "model", "figures"), BILLED, ids=[f"{c}-{m}" for c, m, _ in BILLED]
)
def test_the_table_reproduces_claude_codes_own_figure(case: str, model: str, figures: dict) -> None:
    usage = rates.Usage(
        uncached=figures["inputTokens"],
        write_5m=figures["cacheCreationEphemeral5mInputTokens"],
        write_1h=figures["cacheCreationEphemeral1hInputTokens"],
        read=figures["cacheReadInputTokens"],
        output=figures["outputTokens"],
    )
    price = rates.call_price(model, usage, geo="global", tier="standard", speed="standard")
    assert price.gap is None
    searches = rates.web_search_price(figures["webSearchRequests"]).cost
    assert price.cost + searches == pytest.approx(figures["costUSD"], abs=1e-9)


USAGE = rates.Usage(uncached=10, write_5m=20, write_1h=30, read=40, output=50)


@pytest.mark.parametrize(
    ("kwargs", "gap"),
    [
        ({"model": "claude-unknown-9"}, rates.UNKNOWN_MODEL),
        ({"tier": "priority"}, rates.UNPRICED_TIER),
        ({"speed": "fast"}, rates.UNPRICED_TIER),
        ({"geo": "europe-west4"}, rates.UNPRICED_TIER),
        ({"base_url": "https://gateway.example.com"}, rates.UNPRICED_HOST),
    ],
)
def test_a_figure_the_table_cannot_state_is_a_named_gap(kwargs: dict, gap: str) -> None:
    model = kwargs.pop("model", "claude-opus-5-5")
    assert rates.call_price(model, USAGE, **kwargs) == rates.Price(None, gap)


def test_a_write_with_no_ttl_breakdown_is_not_priced() -> None:
    usage = rates.usage_of(
        {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 9}
    )
    assert usage is not None and usage.prompt == 10 and not usage.split_known
    assert rates.call_price("claude-opus-5-5", usage).gap == rates.NOT_REPORTED


def test_a_breakdown_that_does_not_add_up_is_not_priced() -> None:
    usage = rates.usage_of(
        {
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_creation_input_tokens": 9,
            "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 0},
        }
    )
    assert rates.call_price("claude-opus-5-5", usage).gap == rates.NOT_REPORTED


def test_us_inference_is_the_pages_multiplier() -> None:
    base = rates.call_price("claude-opus-5-5", USAGE).cost
    assert rates.call_price("claude-opus-5-5", USAGE, geo="us").cost == pytest.approx(base * 1.1)


def test_haiku_5_5_prices_a_long_prompt_entirely_on_its_higher_row() -> None:
    short = rates.Usage(
        uncached=rates.HAIKU_LONG_PROMPT_THRESHOLD, write_5m=0, write_1h=0, read=0, output=0
    )
    long = rates.Usage(
        uncached=rates.HAIKU_LONG_PROMPT_THRESHOLD + 1, write_5m=0, write_1h=0, read=0, output=0
    )
    assert rates.call_price("claude-haiku-5-5", short).cost == pytest.approx(0.01)
    assert rates.call_price("claude-haiku-5-5", long).cost == pytest.approx(100_001 * 0.5 / 1e6)


def test_a_dated_snapshot_is_priced_as_the_id_it_pins() -> None:
    assert rates.known("claude-haiku-4-5-20251001")
    assert rates.call_price("claude-haiku-4-5-20251001", USAGE) == rates.call_price(
        "claude-haiku-4-5", USAGE
    )


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": -1}, {"output_tokens": True}, []])
def test_usage_that_states_no_counts_is_none(usage) -> None:
    assert rates.usage_of(usage) is None
