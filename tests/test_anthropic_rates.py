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


#: Anthropic's pricing page, "Model pricing", typed by hand from the page (read 2026-11-21), never
#: copied from the table: (input, 5-minute write, 1-hour write, cache hit, output) in USD per
#: million tokens. Claude Haiku 5.5's second row is the page's "for prompts over 100,000 tokens".
LONG = " over 100k"
PAGE = {
    "claude-fable-5-2": (8.0, 10.0, 16.0, 0.20, 40.0),
    "claude-fable-5-1": (10.0, 12.50, 20.0, 0.25, 50.0),
    "claude-fable-5": (10.0, 12.50, 20.0, 1.0, 50.0),
    "claude-opus-5-5": (4.0, 5.0, 8.0, 0.20, 20.0),
    "claude-opus-5": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-8": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-7": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-6": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-5": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-sonnet-5-5": (2.0, 2.50, 4.0, 0.10, 10.0),
    "claude-sonnet-5": (2.0, 2.50, 4.0, 0.20, 10.0),
    "claude-sonnet-4-6": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4-5": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-haiku-5-5": (0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-5-5" + LONG: (0.50, 0.625, 1.0, 0.05, 2.50),
    "claude-haiku-4-5": (1.0, 1.25, 2.0, 0.10, 5.0),
}

CATEGORIES = ("input", "write_5m", "write_1h", "read", "output")
RATE_CASES = [
    (row, category, page)
    for row, figures in PAGE.items()
    for category, page in zip(CATEGORIES, figures, strict=True)
]


def _usage(uncached: int = 0, **counts: int) -> rates.Usage:
    full = dict.fromkeys(("write_5m", "write_1h", "read", "output"), 0) | counts
    return rates.Usage(uncached=uncached, **full)


def test_every_row_of_the_table_is_pinned_against_the_page() -> None:
    assert set(rates.RATES) == {row.removesuffix(LONG) for row in PAGE}
    assert {row for row in PAGE if row.endswith(LONG)} == {
        f"{model}{LONG}" for model, r in rates.RATES.items() if r.long is not None
    }


@pytest.mark.parametrize(
    ("row", "category", "page"), RATE_CASES, ids=[f"{r}-{c}" for r, c, _ in RATE_CASES]
)
def test_each_rate_is_the_pages_own_number(row: str, category: str, page: float) -> None:
    """One category at a time, so a wrong rate fails by name rather than hiding in a sum.

    The rate is read as the price of adding a threshold's worth of that one category to a base
    call: an empty one for a short row, so the prompt never passes the threshold, and for the long
    row an uncached prompt one token over it, so the call is on that row whatever is added.
    """
    model = row.removesuffix(LONG)
    tokens = rates.HAIKU_LONG_PROMPT_THRESHOLD
    base = {"uncached": tokens + 1 if row.endswith(LONG) else 0}
    added = dict(base)
    field = "uncached" if category == "input" else category
    added[field] = added.get(field, 0) + tokens
    before = rates.call_price(model, _usage(**base))
    after = rates.call_price(model, _usage(**added))
    assert before.gap is None and after.gap is None
    assert (after.cost - before.cost) * 1_000_000 / tokens == pytest.approx(page, rel=1e-9)


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


@pytest.mark.parametrize("model", ["claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5"])
def test_a_model_before_4_6_is_priced_only_within_its_200k_window(model: str) -> None:
    # The page states the flat price across the whole 1M window only for Claude 4.6 and later, so
    # a longer prompt on an earlier model (a 1M alias, say) is a named gap, never the short row.
    window = rates.PRE_4_6_CONTEXT_WINDOW
    assert rates.call_price(model, _usage(read=window)).gap is None
    assert rates.call_price(f"{model}[1m]", _usage(read=window, uncached=1)) == rates.Price(
        None, rates.UNPRICED_TIER
    )


def test_claude_4_6_and_later_are_priced_flat_past_200k() -> None:
    big = _usage(uncached=rates.PRE_4_6_CONTEXT_WINDOW * 4)
    assert rates.call_price("claude-opus-4-6", big).cost == pytest.approx(800_000 * 5.0 / 1e6)


def test_a_dated_snapshot_is_priced_as_the_id_it_pins() -> None:
    assert rates.known("claude-haiku-4-5-20251001")
    assert rates.call_price("claude-haiku-4-5-20251001", USAGE) == rates.call_price(
        "claude-haiku-4-5", USAGE
    )


@pytest.mark.parametrize("usage", [None, {}, {"input_tokens": -1}, {"output_tokens": True}, []])
def test_usage_that_states_no_counts_is_none(usage) -> None:
    assert rates.usage_of(usage) is None


@pytest.mark.parametrize(
    ("model", "id"),
    [
        ("claude-opus-5-5[1m]", "claude-opus-5-5"),
        ("Claude-Opus-5-5", "claude-opus-5-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("claude-opus-5-5", "claude-opus-5-5"),
    ],
)
def test_a_model_is_priced_under_its_plain_id(model: str, id: str) -> None:
    # Claude Code's alias carries a context-window marker the API's message does not, and every
    # current model is priced flat across its whole window, so the marker changes no rate.
    assert rates.canonical(model) == id
    assert rates.call_price(model, USAGE) == rates.call_price(id, USAGE)
