"""Claude Code's own structured output in, the telemetry contract's lines out (router#481).

A Claude Code builder's wake runs ``claude -p <trigger> --output-format stream-json --verbose
--include-partial-messages``, and ``wake-runner`` pipes that stream through this translator into
the journal stream it already opened for the builder (``basecradle-wake-<os_user>``). So a
builder's model calls and tool uses reach the fleet's record the way every harness agent's do:
written by the builder's own wake, into a journald stream root opened and named for it, never
through a port any local account can write to under any name (basecradle/basecradle#789).

**The interface is "a runtime's own structured output in, contract lines out."** Claude Code is
the first runtime; a second runtime is a second module like this one, never a second receiver.
The lines are the contract's (basecradle-harness ``docs/telemetry.md``), rendered by the router's
one renderer, :func:`basecradle_router.logfmt.log_fields`, and the final reply is written as prose
exactly where the reply went before.

What the stream carries, and what the translator writes from it (the reading behind each rule is
the stream captured for #481, Claude Code 2.1.325):

- **``llm``, one line per model call.** A main-thread call opens with ``stream_event``
  ``message_start`` and closes with ``message_stop``; its final usage is on ``message_delta`` only
  (each ``assistant`` line carries the ``message_start`` snapshot, whose ``output_tokens`` is a
  placeholder), which is why the third flag is required. The line is written at ``message_stop``,
  priced by :mod:`basecradle_router.anthropic_rates` (``cost_basis=computed``), because the stream
  states a price only per model at ``result``, and a wake that dies first must still leave its
  calls. Its ``duration`` runs from the ``system/status requesting`` line that began the call (so a
  retried call's whole wait) to its ``message_stop``, by this translator's clock.
- **``llm … outcome=retrying``** for each ``system/api_retry``, and **``outcome=failed``** for the
  synthetic ``assistant`` line Claude Code writes when a call ends with no answer. Both name the
  fault by the contract's Fault Slugs table.
- **A subagent's calls** arrive as ``assistant`` lines with ``parent_tool_use_id`` set and no
  ``stream_event``\\ s, so the stream never states their final usage: each gets an ``llm`` line with
  no token fields and ``cost_gap=not_reported``. **Claude Code's helper calls** (the model a
  WebSearch runs on, say) never appear one by one at all. What neither itemizes is stated, per
  model, on a prose line at ``result``, against Claude Code's own ``modelUsage``.
- **``tool``, one line per tool use**, joining a ``tool_use`` block to its ``tool_result``. The
  stream's only verdict is ``is_error``, so a non-zero shell exit is ``outcome=error`` (the contract
  calls one ``ok``; the stream states no exit code but in text, and a verdict is never read off
  text). A refusal is structural (``permission_decision.decision=reject``): ``reason=withheld``.
- **A call cut short** (a stop or a crash before ``message_stop``) has no word in the contract, so
  it gets no ``llm`` line.

**The comparison mark.** While the ``ai_otel`` receiver still renders the same calls, every ``llm``
line here is headed ``llm shadow provider=``: every NOC column that counts, times or prices a call
is gated on ``llm provider=``, so no chart counts a call twice, and the two records differ by one
word. Dropping it is the receiver's removal, and nothing else changes then.

**No wake lines.** A builder has no wake in the contract's sense, and the router's own
``event=wake_start``, ``event=wake_end`` and ``stage=wake outcome=`` lines, observed by root from
the exit status, are already its record (the capital's ruling on #481). The turn count and the
reason the run ended go on the prose line at ``result``.

**Nothing here may cost the wake or its reply.** The translator never raises out of :meth:`feed`
(a line it cannot read is counted and passed over), and it writes the reply before anything else
at ``result``. The launcher (``deploy/bin/claude-stream-translate``) keeps reading through a stop's
SIGTERM until the stream ends, and ``wake-runner`` drains whatever is left should the translator
die anyway.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from basecradle_router import anthropic_rates as rates
from basecradle_router.logfmt import bounded, log_fields

#: The vendor that bills every call a Claude Code builder makes.
PROVIDER = "anthropic"

#: The head of every model-call line while the receiver still writes the same calls (see the
#: module docstring). Dropping ``shadow`` is the whole cutover.
LLM_HEAD = "llm shadow"

#: The model id Claude Code gives the ``assistant`` line it writes when a call failed.
SYNTHETIC_MODEL = "<synthetic>"

#: An id the line may carry as ``generation_id``: the inert charset the ``ai_otel_llm`` remap pins.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

Write = Callable[[str], None]
Clock = Callable[[], float]


def fault_slug(status: object, *, no_response: bool = False) -> str:
    """The contract's ``reason`` for a failed attempt, from what the transport said.

    basecradle-harness ``docs/telemetry.md`` → Fault Slugs, row for row: no HTTP answer is
    ``transport``, and a wait that ran out with no answer (Claude Code's ``no_response``) is
    ``timeout``.
    """
    if isinstance(status, bool) or not isinstance(status, int):
        return "timeout" if no_response else "transport"
    if status in (401, 403):
        return "config:auth"
    if status == 402:
        return "config:billing"
    if status == 404:
        return "config:model_not_found"
    if status == 413:
        return "payload_too_large"
    if status == 429:
        return "rate_limited"
    if status == 504:
        return "timeout"
    if 500 <= status <= 599:
        return "server_error"
    return "api_error"


def money(value: float) -> str:
    """Fixed-point US dollars, at most eight decimals, as the ``ai_otel_llm`` remap writes them."""
    text = f"{round(value, 8):.8f}".rstrip("0").rstrip(".")
    return text or "0"


def seconds(value: float) -> str:
    """A length of time in the contract's unit: seconds, two decimals, a trailing ``s``."""
    return f"{max(value, 0.0):.2f}s"


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _when(value: Any) -> datetime | None:
    """An ISO 8601 moment from Claude Code's ``timestamp``, or ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) else None


def _text(content: Any) -> str:
    """The text of a message or tool-result ``content``: a string, or its ``text`` blocks joined."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            block["text"]
            for block in content
            if isinstance(block, Mapping) and isinstance(block.get("text"), str)
        )
    return ""


def _final_usage(start: Mapping[str, Any], delta: Mapping[str, Any] | None) -> dict[str, Any]:
    """One call's final usage: ``message_delta``'s counts over ``message_start``'s facts.

    ``message_start`` states the serving facts (``inference_geo``, ``service_tier``, ``speed``) and
    the cache-write breakdown; ``message_delta`` states the final counts. When the call ran more
    than one iteration (a server-side tool loop), the breakdown is the iterations' sum, so the
    written tokens stay split the way they were billed.
    """
    usage = dict(start)
    if delta is None:
        return usage
    usage.update(delta)
    iterations = delta.get("iterations")
    if isinstance(iterations, list) and iterations:
        five = hour = 0
        for item in iterations:
            breakdown = item.get("cache_creation") if isinstance(item, Mapping) else None
            if not isinstance(breakdown, Mapping):
                return usage
            five += _count(breakdown.get("ephemeral_5m_input_tokens")) or 0
            hour += _count(breakdown.get("ephemeral_1h_input_tokens")) or 0
        usage["cache_creation"] = {
            "ephemeral_5m_input_tokens": five,
            "ephemeral_1h_input_tokens": hour,
        }
    else:
        usage["cache_creation"] = start.get("cache_creation")
    return usage


@dataclass(slots=True)
class _Call:
    """A main-thread model call between its ``message_start`` and its ``message_stop``."""

    model: str
    started: float
    start_usage: Mapping[str, Any]
    delta_usage: Mapping[str, Any] | None = None
    stop_reason: str | None = None
    request_id: str | None = None


@dataclass(slots=True)
class _ToolUse:
    name: str
    at: datetime | None
    arrived: float


@dataclass(slots=True)
class _Ledger:
    """What the stream itemized for one model: its calls' tokens and the dollars priced on them."""

    prompt: int = 0
    output: int = 0
    cost: float = 0.0
    #: Itemized calls this table could not price, so ``cost`` is short by theirs.
    unpriced: int = 0
    #: Calls seen with no final usage (a subagent's), so the tokens are short by theirs.
    unitemized: int = 0


@dataclass(slots=True)
class Translator:
    """One builder wake's stream, translated line by line. Feed it each line; finish it at EOF.

    ``write`` receives each output line without its newline. ``clock`` is a monotonic clock in
    seconds, read once per line as it arrives. ``base_url`` is the host Claude Code was aimed at
    (``ANTHROPIC_BASE_URL``), which decides whether the published rates are its price.
    """

    write: Write
    clock: Clock
    base_url: str | None = None

    model: str | None = None
    unreadable: int = 0
    result_seen: bool = False
    _calls: dict[str, _Call] = field(default_factory=dict)
    _request_ids: dict[str, str] = field(default_factory=dict)
    _requested_at: float | None = None
    _retries: int = 0
    _no_response: bool = False
    _max_retries: int | None = None
    _subagent_calls: set[str] = field(default_factory=set)
    _tools: dict[str, _ToolUse] = field(default_factory=dict)
    _ledger: dict[str, _Ledger] = field(default_factory=dict)
    _gaps: set[tuple[str, str, str]] = field(default_factory=set)

    # --- input ---------------------------------------------------------------------------------

    def feed(self, line: str) -> None:
        """Translate one line of the stream. Never raises: a line it cannot read is passed over.

        A line that is not JSON is Claude Code (or something it ran) writing plain text to stdout,
        which reached the journal as prose before the translator existed, so it still does.
        """
        now = self.clock()
        text = line.rstrip("\r\n")
        if not text.strip():
            return
        try:
            event = json.loads(text)
        except ValueError:
            self.write(text)
            return
        if not isinstance(event, dict):
            return
        try:
            self._dispatch(event, now)
        except Exception:  # noqa: BLE001 — a translator fault must never cost the wake
            self.unreadable += 1

    def finish(self, *, stopped_by: str | None = None) -> None:
        """The stream has ended. Say so in prose when it ended without a ``result``.

        ``stopped_by`` is the signal the translator received while reading (``TERM`` from a stop),
        or ``None``. Whatever was in flight writes no contract line: a call cut short has no word
        in the contract, and the wake's own record is the router's ``stage=wake`` line.
        """
        if self.unreadable:
            self.write(
                f"WARNING The stream translator could not read {self.unreadable} line(s) of "
                "Claude Code's stream; their model calls or tool uses have no line."
            )
        if self.result_seen:
            return
        how = f"after a SIG{stopped_by}" if stopped_by else "without a result"
        self.write(
            f"INFO Claude Code's stream ended {how}: {len(self._calls)} model call(s) and "
            f"{len(self._tools)} tool use(s) in flight have no line."
        )

    # --- dispatch ------------------------------------------------------------------------------

    def _dispatch(self, event: dict[str, Any], now: float) -> None:
        kind = event.get("type")
        if kind == "system":
            self._system(event, now)
        elif kind == "stream_event":
            if event.get("parent_tool_use_id") is None:
                self._stream_event(event, now)
        elif kind == "assistant":
            self._assistant(event, now)
        elif kind == "user":
            self._user(event, now)
        elif kind == "result":
            self._result(event)

    def _system(self, event: Mapping[str, Any], now: float) -> None:
        subtype = event.get("subtype")
        if subtype == "init":
            model = event.get("model")
            if isinstance(model, str) and model:
                self.model = model
        elif subtype == "status" and event.get("status") == "requesting":
            self._requested_at = now
            self._retries = 0
            self._no_response = False
        elif subtype == "api_retry":
            self._retry(event)

    def _stream_event(self, event: Mapping[str, Any], now: float) -> None:
        inner = event.get("event")
        if not isinstance(inner, Mapping):
            return
        kind = inner.get("type")
        message_id = event.get("api_message_id")
        if kind == "message_start":
            message = inner.get("message")
            if not isinstance(message, Mapping):
                return
            message_id = message.get("id") or message_id
            if not isinstance(message_id, str):
                return
            started = self._requested_at
            if started is None:
                ttft = _count(event.get("ttft_ms"))
                started = now - (ttft or 0) / 1000
            usage = message.get("usage")
            model = message.get("model")
            self._calls[message_id] = _Call(
                model=model if isinstance(model, str) and model else (self.model or ""),
                started=started,
                start_usage=usage if isinstance(usage, Mapping) else {},
            )
            self._requested_at = None
        elif isinstance(message_id, str) and message_id in self._calls:
            call = self._calls[message_id]
            if kind == "message_delta":
                usage = inner.get("usage")
                if isinstance(usage, Mapping):
                    call.delta_usage = usage
                delta = inner.get("delta")
                if isinstance(delta, Mapping) and isinstance(delta.get("stop_reason"), str):
                    call.stop_reason = delta["stop_reason"]
            elif kind == "message_stop":
                del self._calls[message_id]
                call.request_id = call.request_id or self._request_ids.pop(message_id, None)
                self._call_ended(call, now)

    def _assistant(self, event: Mapping[str, Any], now: float) -> None:
        message = event.get("message")
        if not isinstance(message, Mapping):
            return
        if message.get("model") == SYNTHETIC_MODEL:
            if event.get("is_api_error_message") or event.get("error"):
                self._call_failed(event, message, now)
            return
        message_id = message.get("id")
        request_id = _safe_id(event.get("request_id"))
        parent = event.get("parent_tool_use_id")
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, Mapping)
                    and block.get("type") == "tool_use"
                    and isinstance(block.get("id"), str)
                    and isinstance(block.get("name"), str)
                ):
                    self._tools[block["id"]] = _ToolUse(
                        name=block["name"], at=_when(event.get("timestamp")), arrived=now
                    )
        if not isinstance(message_id, str):
            return
        if parent is None:
            if request_id is not None:
                if message_id in self._calls:
                    self._calls[message_id].request_id = request_id
                else:
                    self._request_ids[message_id] = request_id
        elif message_id not in self._subagent_calls:
            self._subagent_calls.add(message_id)
            self._subagent_call(message, request_id)

    def _user(self, event: Mapping[str, Any], now: float) -> None:
        message = event.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            return
        decisions: dict[str, str] = {}
        meta = event.get("tool_result_meta")
        if isinstance(meta, list):
            for item in meta:
                if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                    continue
                decision = item.get("permission_decision")
                if isinstance(decision, Mapping) and isinstance(decision.get("decision"), str):
                    decisions[item["id"]] = decision["decision"]
        at = _when(event.get("timestamp"))
        for block in content:
            if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                continue
            use = (
                self._tools.pop(block.get("tool_use_id"), None)
                if isinstance(block.get("tool_use_id"), str)
                else None
            )
            if use is None:
                continue
            self._tool_ended(use, block, decisions.get(block["tool_use_id"]), at, now)

    # --- model calls ---------------------------------------------------------------------------

    def _ledger_for(self, model: str) -> _Ledger:
        return self._ledger.setdefault(model, _Ledger())

    def _unpriced(self, model: str, charge: str, reason: str) -> None:
        """The contract's ``cost unpriced`` WARNING, once per wake per gap."""
        key = (model, charge, reason)
        if key in self._gaps:
            return
        self._gaps.add(key)
        self.write(
            "WARNING cost unpriced "
            + log_fields(provider=PROVIDER, model=model, charge=charge, reason=reason)
        )

    def _llm(self, level: str, **fields: object) -> None:
        self.write(f"{level} {LLM_HEAD} " + log_fields(provider=PROVIDER, **fields))

    def _call_ended(self, call: _Call, now: float) -> None:
        if call.delta_usage is None:
            # No final usage: the call is counted, and its tokens are not invented.
            self._ledger_for(call.model).unitemized += 1
            self._unpriced(call.model, "tokens", rates.NOT_REPORTED)
            self._llm(
                "INFO",
                purpose="main",
                model=call.model,
                duration=seconds(now - call.started),
                cost_gap=rates.NOT_REPORTED,
                outcome="ok",
                generation_id=call.request_id,
            )
            return
        raw = _final_usage(call.start_usage, call.delta_usage)
        usage = rates.usage_of(raw)
        tokens: dict[str, object] = {}
        price: rates.Price | None = None
        if usage is not None and (usage.prompt or usage.output):
            tokens = {
                "tokens_in": usage.prompt,
                "tokens_out": usage.output,
                "tokens_total": usage.prompt + usage.output,
                "cached_tokens": usage.read if "cache_read_input_tokens" in raw else None,
                "cache_write_tokens": usage.written or None,
                "tokens_reasoning": _count(
                    (raw.get("output_tokens_details") or {}).get("thinking_tokens")
                ),
            }
            price = rates.call_price(
                call.model,
                usage,
                geo=raw.get("inference_geo"),
                tier=raw.get("service_tier"),
                speed=raw.get("speed"),
                base_url=self.base_url,
            )
            ledger = self._ledger_for(call.model)
            ledger.prompt += usage.prompt
            ledger.output += usage.output
            if price.cost is not None:
                ledger.cost += price.cost
            else:
                ledger.unpriced += 1
                self._unpriced(call.model, "tokens", price.gap or rates.NOT_REPORTED)
        geo = raw.get("inference_geo")
        refused = call.stop_reason == "refusal"
        self._llm(
            "WARNING" if refused else "INFO",
            purpose="main",
            residency=geo if isinstance(geo, str) and geo else None,
            model=call.model,
            duration=seconds(now - call.started),
            **tokens,
            cost=money(price.cost) if price and price.cost is not None else None,
            cost_basis="computed" if price and price.cost is not None else None,
            cost_gap=price.gap if price else None,
            outcome="refused" if refused else "ok",
            generation_id=call.request_id,
        )
        self._server_tools(raw, call.request_id)

    def _server_tools(self, usage: Mapping[str, Any], request_id: str | None) -> None:
        """One ``tool executor=server`` line per server-side tool the call used, after its call."""
        block = usage.get("server_tool_use")
        if not isinstance(block, Mapping):
            return
        searches = _count(block.get("web_search_requests")) or 0
        if searches:
            price = rates.web_search_price(searches, base_url=self.base_url)
            if price.cost is None:
                self._unpriced(self.model or "", "search", price.gap or rates.NOT_REPORTED)
            self.write(
                "INFO tool "
                + log_fields(
                    name="web_search",
                    executor="server",
                    provider=PROVIDER,
                    kind="search.web",
                    count=searches,
                    outcome="ok",
                    cost=money(price.cost) if price.cost is not None else None,
                    cost_basis="computed" if price.cost is not None else None,
                    cost_gap=price.gap,
                    generation_id=request_id,
                )
            )
        fetches = _count(block.get("web_fetch_requests")) or 0
        if fetches:
            # Web fetch carries "no additional charges beyond standard token costs".
            self.write(
                "INFO tool "
                + log_fields(
                    name="web_fetch",
                    executor="server",
                    provider=PROVIDER,
                    count=fetches,
                    outcome="ok",
                    generation_id=request_id,
                )
            )

    def _retry(self, event: Mapping[str, Any]) -> None:
        attempt = _count(event.get("attempt"))
        max_retries = _count(event.get("max_retries"))
        self._retries += 1
        self._max_retries = max_retries
        self._no_response = isinstance(event.get("no_response"), Mapping)
        delay = event.get("retry_delay_ms")
        self._llm(
            "WARNING",
            purpose="main",
            model=self.model,
            outcome="retrying",
            attempt=f"{attempt}/{max_retries + 1}"
            if attempt is not None and max_retries is not None
            else None,
            reason=fault_slug(event.get("error_status"), no_response=self._no_response),
            next_in=seconds(delay / 1000)
            if isinstance(delay, (int, float))
            and not isinstance(delay, bool)
            and math.isfinite(delay)
            else None,
        )

    def _call_failed(
        self, event: Mapping[str, Any], message: Mapping[str, Any], now: float
    ) -> None:
        attempts = self._retries + 1
        self._llm(
            "WARNING",
            purpose="main",
            model=self.model,
            duration=seconds(now - self._requested_at) if self._requested_at is not None else None,
            outcome="failed",
            attempt=f"{attempts}/{self._max_retries + 1}"
            if self._retries and self._max_retries is not None
            else None,
            reason=fault_slug(event.get("api_error_status"), no_response=self._no_response),
            detail=bounded(_text(message.get("content"))) or None,
            generation_id=_safe_id(event.get("request_id")),
        )
        self._requested_at = None
        self._retries = 0
        self._no_response = False

    def _subagent_call(self, message: Mapping[str, Any], request_id: str | None) -> None:
        """A subagent's call: counted, with the usage the stream never states left out."""
        model = message.get("model")
        model = model if isinstance(model, str) and model else (self.model or "")
        self._ledger_for(model).unitemized += 1
        self._unpriced(model, "tokens", rates.NOT_REPORTED)
        self._llm(
            "INFO",
            purpose="main",
            model=model,
            cost_gap=rates.NOT_REPORTED,
            outcome="ok",
            generation_id=request_id,
        )

    # --- tools ---------------------------------------------------------------------------------

    def _tool_ended(
        self,
        use: _ToolUse,
        block: Mapping[str, Any],
        decision: str | None,
        at: datetime | None,
        now: float,
    ) -> None:
        if use.at is not None and at is not None:
            took = (at - use.at).total_seconds()
        else:
            took = now - use.arrived
        executor = "mcp" if use.name.startswith("mcp__") else "harness"
        if decision == "reject":
            outcome, reason = "error", "withheld"
        elif block.get("is_error") is True:
            outcome, reason = "error", "tool_error"
        else:
            outcome, reason = "ok", None
        self.write(
            "INFO tool "
            + log_fields(
                name=use.name,
                executor=executor,
                duration=seconds(took),
                outcome=outcome,
                reason=reason,
            )
        )
        if reason == "tool_error":
            error = bounded(_text(block.get("content")))
            if error:
                self.write("WARNING tool " + log_fields(name=use.name, error=error))

    # --- the end -------------------------------------------------------------------------------

    def _result(self, event: Mapping[str, Any]) -> None:
        """The run's end: the reply first, then what the stream itemized against what was billed."""
        self.result_seen = True
        reply = event.get("result")
        if isinstance(reply, str) and reply:
            self.write(reply)
        errors = event.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, str) and error:
                    self.write(error)
        turns = _count(event.get("num_turns"))
        ending = event.get("terminal_reason") or event.get("subtype")
        if turns is not None or isinstance(ending, str):
            count = str(turns) if turns is not None else "an unstated number of"
            why = f" ({ending})" if isinstance(ending, str) and ending else ""
            self.write(f"INFO Claude Code ended after {count} turn(s){why}.")
        billed = event.get("modelUsage")
        if isinstance(billed, Mapping):
            for model, figures in billed.items():
                if isinstance(model, str) and isinstance(figures, Mapping):
                    self._reconcile(model, figures)

    def _reconcile(self, model: str, figures: Mapping[str, Any]) -> None:
        stated = figures.get("costUSD")
        if isinstance(stated, bool) or not isinstance(stated, (int, float)):
            return
        if not math.isfinite(stated):
            return
        prompt = sum(
            _count(figures.get(key)) or 0
            for key in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
        )
        output = _count(figures.get("outputTokens")) or 0
        mine = self._ledger.get(model, _Ledger())
        if (prompt, output) == (mine.prompt, mine.output) and not mine.unitemized:
            if mine.unpriced or abs(stated - mine.cost) < 5e-7:
                return
            self.write(
                f"WARNING Claude Code billed {model} ${money(stated)} for the same tokens this "
                f"translator priced at ${money(mine.cost)}: its rate table may be stale "
                f"({rates.SOURCE})."
            )
            return
        callers = (
            f"{mine.unitemized} subagent call(s) seen without usage"
            if mine.unitemized
            else "calls Claude Code made that the stream never shows, such as a helper model's"
        )
        priced = f"${money(mine.cost)}" + (
            f", with {mine.unpriced} itemized call(s) unpriced" if mine.unpriced else ""
        )
        self.write(
            f"INFO Claude Code billed {model} ${money(stated)} for {prompt} input and {output} "
            f"output tokens; the stream itemized {mine.prompt} input and {mine.output} output "
            f"tokens, priced at {priced}. The rest is {callers}."
        )
