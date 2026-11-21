"""What a Claude Code builder's model call cost, priced from Anthropic's published rates.

Claude Code's stream states each call's token usage and **no price**; it states a price only per
model, in its final ``result`` (``modelUsage.costUSD``). A wake that dies before ``result`` would
therefore leave its ``llm`` lines unpriced, so the translator
(:mod:`basecradle_router.claude_stream`, this table's one reader) prices each call the moment it
ends, from this table, and tags the figure ``cost_basis=computed`` (basecradle-router#481). At
``result`` it checks its own sum per model against the figure Claude Code states, so a stale row is
loud rather than silently wrong.

The rows are transcribed from Anthropic's pricing page (:data:`SOURCE`), the page and never the
basecradle-harness ``_anthropic_rates`` table, which keeps a narrower set of models. On the
captures behind #481 they reproduce Claude Code's own ``costUSD`` to the last digit, 5-minute and
1-hour cache writes, Haiku and the per-search fee included, and each rate is pinned on its own
against the page, typed by hand (``tests/test_anthropic_rates.py``). Where the fleet's vendor
price tables live is being decided by @origin (basecradle/basecradle#864, #865): this file is
deliberately one small table with one reader, so it can move without touching anything else.

Three rules keep it honest, the harness table's own:

- **A figure the table cannot state is omitted, never estimated.** An unknown model, a tier or speed
  other than standard, an inference geography the page prices nowhere, a host whose price is not
  Anthropic's: each returns no cost and names its gap, and the line carries ``cost_gap=`` instead of
  ``cost=``. A wrong number is worse than a missing one, because the dashboard sums it.
- **Every number is transcribed, never derived**, row by row from :data:`SOURCE`. When the page
  moves, the table is edited from the page in a reviewed pull request, with the new date.
- **Which row applies is decided by the call's own facts**: the model id, the prompt length (on
  Claude Haiku 5.5, a prompt over :data:`HAIKU_LONG_PROMPT_THRESHOLD` prices every token of the call
  on the higher row), the cache split the usage block reports, and the inference geography.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

#: Where every number in this module was read, and when.
SOURCE = "https://platform.claude.com/docs/en/about-claude/pricing (read 2026-11-21)"

#: On Claude Haiku 5.5, a request whose prompt is over this many tokens pays the higher row for all
#: of them. Claude 4.6 and later are otherwise priced flat across their whole 1M window.
HAIKU_LONG_PROMPT_THRESHOLD = 100_000

#: The context window of the models before Claude 4.6, "200K tokens" on Anthropic's models overview
#: (https://platform.claude.com/docs/en/about-claude/models/overview, read 2026-11-21). The pricing
#: page states a flat price across the whole 1M window only for Claude 4.6 and later and prices no
#: longer prompt on an earlier model, so a prompt over this on one is not priced.
PRE_4_6_CONTEXT_WINDOW = 200_000

#: "$10 USD per 1,000 searches" (Web search tool).
WEB_SEARCH_PER_SEARCH = 10.00 / 1_000

#: The ``inference_geo`` values a call may report and the multiplier the page states for each. Unset
#: and ``global`` are global routing, the list price; a value the page prices nowhere is not priced.
_GEO_MULTIPLIERS: Mapping[str, float] = MappingProxyType({"": 1.0, "global": 1.0, "us": 1.1})

#: The one host these rates are the list price on.
PRICED_HOST = "api.anthropic.com"

#: The contract's ``cost_gap`` words this table can name (basecradle-harness ``docs/telemetry.md``).
UNKNOWN_MODEL = "unknown_model"
UNPRICED_TIER = "unpriced_tier"
UNPRICED_HOST = "unpriced_host"
NOT_REPORTED = "not_reported"


@dataclass(frozen=True, slots=True)
class Tier:
    """One row of the page's model table, in USD per million tokens."""

    input: float
    write_5m: float
    write_1h: float
    read: float
    output: float


@dataclass(frozen=True, slots=True)
class Rates:
    """A model's rates: one row for every prompt, or (Claude Haiku 5.5) two prompt-length rows.

    ``priced_up_to`` is the longest prompt the page prices on the model, where it prices fewer
    than the model may be sent; a longer one names :data:`UNPRICED_TIER`.
    """

    short: Tier
    long: Tier | None = None
    priced_up_to: int | None = None


#: Model id → its rates, transcribed from :data:`SOURCE`, "Model pricing". Every model the page
#: lists that a fleet agent can run on Anthropic's API has a row. Left out: Claude Mythos, limited
#: to organizations verified through Anthropic's own programs, and the models the page marks retired
#: (Opus 4.1, Opus 4, Sonnet 4). A dated snapshot of an id (``claude-haiku-4-5-20251001``) is
#: priced as that id.
#:
#: The models before Claude 4.6 reject ``inference_geo`` (the page: "a request that includes the
#: parameter returns a 400 error"), and are priced only up to :data:`PRE_4_6_CONTEXT_WINDOW`.
_PRE_4_6 = PRE_4_6_CONTEXT_WINDOW

RATES: Mapping[str, Rates] = MappingProxyType(
    {
        "claude-fable-5-2": Rates(Tier(8.0, 10.0, 16.0, 0.20, 40.0)),
        "claude-fable-5-1": Rates(Tier(10.0, 12.50, 20.0, 0.25, 50.0)),
        "claude-fable-5": Rates(Tier(10.0, 12.50, 20.0, 1.0, 50.0)),
        "claude-opus-5-5": Rates(Tier(4.0, 5.0, 8.0, 0.20, 20.0)),
        "claude-opus-5": Rates(Tier(5.0, 6.25, 10.0, 0.50, 25.0)),
        "claude-opus-4-8": Rates(Tier(5.0, 6.25, 10.0, 0.50, 25.0)),
        "claude-opus-4-7": Rates(Tier(5.0, 6.25, 10.0, 0.50, 25.0)),
        "claude-opus-4-6": Rates(Tier(5.0, 6.25, 10.0, 0.50, 25.0)),
        "claude-opus-4-5": Rates(Tier(5.0, 6.25, 10.0, 0.50, 25.0), priced_up_to=_PRE_4_6),
        "claude-sonnet-5-5": Rates(Tier(2.0, 2.50, 4.0, 0.10, 10.0)),
        "claude-sonnet-5": Rates(Tier(2.0, 2.50, 4.0, 0.20, 10.0)),
        "claude-sonnet-4-6": Rates(Tier(3.0, 3.75, 6.0, 0.30, 15.0)),
        "claude-sonnet-4-5": Rates(Tier(3.0, 3.75, 6.0, 0.30, 15.0), priced_up_to=_PRE_4_6),
        "claude-haiku-5-5": Rates(
            short=Tier(0.10, 0.125, 0.20, 0.01, 0.50),
            long=Tier(0.50, 0.625, 1.0, 0.05, 2.50),
        ),
        "claude-haiku-4-5": Rates(Tier(1.0, 1.25, 2.0, 0.10, 5.0), priced_up_to=_PRE_4_6),
    }
)


@dataclass(frozen=True, slots=True)
class Usage:
    """One call's tokens as Anthropic bills them: the prompt in four disjoint parts, and the output.

    ``output`` includes the thinking, which Anthropic bills as output.
    """

    uncached: int
    write_5m: int
    write_1h: int
    read: int
    output: int
    #: Whether the written tokens' 5-minute and 1-hour split is the vendor's own. A write reported
    #: with no breakdown is priced nowhere, because the two rates differ.
    split_known: bool = True

    @property
    def prompt(self) -> int:
        """Every input token of the call: the contract's ``tokens_in``."""
        return self.uncached + self.write_5m + self.write_1h + self.read

    @property
    def written(self) -> int:
        return self.write_5m + self.write_1h


@dataclass(frozen=True, slots=True)
class Price:
    """A call's price: ``cost`` in USD, or the contract's word for why there is none."""

    cost: float | None
    gap: str | None = None


def count(value: Any) -> int | None:
    """A non-negative integer, or ``None`` — a count the vendor did not state is never a zero."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def usage_of(usage: Any) -> Usage | None:
    """A Messages ``usage`` mapping as a :class:`Usage`, or ``None`` if it states no token counts.

    The written tokens are split by the ``cache_creation`` breakdown; a write with no breakdown is
    marked unsplit, and :func:`call_price` names the gap rather than guess which rate it took.
    """
    if not isinstance(usage, Mapping):
        return None
    uncached = count(usage.get("input_tokens"))
    output = count(usage.get("output_tokens"))
    if uncached is None and output is None:
        return None
    written = count(usage.get("cache_creation_input_tokens")) or 0
    breakdown = usage.get("cache_creation")
    five = hour = None
    if isinstance(breakdown, Mapping):
        five = count(breakdown.get("ephemeral_5m_input_tokens"))
        hour = count(breakdown.get("ephemeral_1h_input_tokens"))
    if five is None and hour is None:
        # No breakdown: carried as 5-minute so the prompt counts stay right, and marked unsplit so
        # it is never priced.
        five, hour = written, 0
        split_known = written == 0
    else:
        split_known = (five or 0) + (hour or 0) == written
    return Usage(
        uncached=uncached or 0,
        write_5m=five or 0,
        write_1h=hour or 0,
        read=count(usage.get("cache_read_input_tokens")) or 0,
        output=output or 0,
        split_known=split_known,
    )


def canonical(model: str) -> str:
    """The id a model is priced and counted under.

    Claude Code names a model by its configured alias, which may carry a context-window marker
    (``claude-opus-5-5[1m]``, the box's builders' since 2026-11), while the API's own message
    carries the plain id; and a dated snapshot (``claude-haiku-4-5-20251001``) is the id it pins.
    A rate turns on the prompt's length, never on the marker, so the marker changes no rate.
    """
    return re.sub(r"(?:\[[^\]]*\]|-\d{8})\Z", "", model.strip().lower())


def known(model: str) -> bool:
    """Whether the table carries ``model`` at all."""
    return canonical(model) in RATES


def priced_host(base_url: str | None) -> bool:
    """Whether ``base_url`` is Anthropic's own API host, the one these rates are the price on."""
    if not base_url:
        return True
    return (urlsplit(base_url).hostname or "").lower() == PRICED_HOST


def call_price(
    model: str,
    usage: Usage,
    *,
    geo: Any = None,
    tier: Any = None,
    speed: Any = None,
    base_url: str | None = None,
) -> Price:
    """What one call cost at Anthropic's published rates, or the gap that leaves it unpriced.

    ``geo``, ``tier`` and ``speed`` are what the call's usage reports (``inference_geo``,
    ``service_tier``, ``speed``); ``base_url`` is the host Claude Code was aimed at
    (``ANTHROPIC_BASE_URL``). Anything but standard tier and speed is another row of the page, or
    none, so it is not priced.
    """
    if not priced_host(base_url):
        return Price(None, UNPRICED_HOST)
    rates = RATES.get(canonical(model))
    if rates is None:
        return Price(None, UNKNOWN_MODEL)
    multiplier = _GEO_MULTIPLIERS.get(_name(geo))
    if (
        multiplier is None
        or _name(tier) not in ("", "standard")
        or _name(speed)
        not in (
            "",
            "standard",
        )
    ):
        return Price(None, UNPRICED_TIER)
    if not usage.split_known:
        return Price(None, NOT_REPORTED)
    if rates.priced_up_to is not None and usage.prompt > rates.priced_up_to:
        return Price(None, UNPRICED_TIER)
    row = (
        rates.long
        if rates.long is not None and usage.prompt > HAIKU_LONG_PROMPT_THRESHOLD
        else (rates.short)
    )
    total = (
        usage.uncached * row.input
        + usage.write_5m * row.write_5m
        + usage.write_1h * row.write_1h
        + usage.read * row.read
        + usage.output * row.output
    )
    return Price(total * multiplier / 1_000_000)


def web_search_price(searches: int, *, base_url: str | None = None) -> Price:
    """The per-search fee for ``searches`` server-side web searches."""
    if not priced_host(base_url):
        return Price(None, UNPRICED_HOST)
    return Price(searches * WEB_SEARCH_PER_SEARCH)


def _name(value: Any) -> str:
    return str(value or "").strip().lower()
