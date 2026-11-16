"""The builder's stream translator: Claude Code's stream in, the telemetry contract's lines out.

Driven over streams captured from Claude Code 2.1.325 for basecradle-router#481 and rebuilt as
fabricated fixtures (``tests/fixtures/claude_stream/``): every identifier, path and text is
invented, and the numbers (tokens, costs, attempts, statuses) are the captures' own, because
they are what is under test. The clock is fake, so every ``duration`` is exact.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest

from basecradle_router.claude_stream import LLM_HEAD, Translator, fault_slug, money

FIXTURES = Path(__file__).parent / "fixtures" / "claude_stream"
TICK = 0.25

#: The contract's fields per head (basecradle-harness ``docs/telemetry.md``), as far as this
#: translator can fill them. A field outside these is a contract change, never a translator's.
LLM_FIELDS = {
    "provider", "purpose", "kind", "endpoint", "residency", "model", "duration", "tokens_in",
    "tokens_out", "tokens_total", "cached_tokens", "cache_write_tokens", "tokens_reasoning",
    "cost", "cost_basis", "cost_gap", "outcome", "category", "attempt", "reason", "detail",
    "retry_after", "next_in", "generation_id",
}  # fmt: skip
TOOL_FIELDS = {
    "name", "executor", "provider", "kind", "model", "count", "duration", "outcome", "reason",
    "error", "cost", "cost_basis", "cost_gap", "generation_id",
}  # fmt: skip
UNPRICED_FIELDS = {"provider", "model", "charge", "reason"}
HEADS = {f"{LLM_HEAD}": LLM_FIELDS, "tool": TOOL_FIELDS, "cost unpriced": UNPRICED_FIELDS}

#: One structured line: severity, a head, then key=value fields (a value is bare or quoted).
STRUCTURED = re.compile(r"\A(INFO|WARNING) (llm shadow|tool|cost unpriced) (.+)\Z")
FIELD = re.compile(r'([a-z_]+)=("[^"]*"|[^ "]+)(?: |\Z)')


def _events(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]


def translate(
    lines: list[str], *, stopped_by: str | None = None, base_url: str | None = None
) -> list[str]:
    ticks = itertools.count()
    out: list[str] = []
    translator = Translator(write=out.append, clock=lambda: next(ticks) * TICK, base_url=base_url)
    for line in lines:
        translator.feed(line)
    translator.finish(stopped_by=stopped_by)
    return out


def run(name: str, **kwargs: Any) -> list[str]:
    return translate((FIXTURES / f"{name}.jsonl").read_text().splitlines(), **kwargs)


def rewrite(name: str, change) -> list[str]:
    """The fixture's lines with ``change(event)`` applied to each decoded event."""
    events = _events(name)
    for event in events:
        change(event)
    return [json.dumps(event) for event in events]


def fields(line: str) -> dict[str, str]:
    match = STRUCTURED.match(line)
    assert match, line
    parsed = dict(FIELD.findall(match.group(3)))
    rebuilt = " ".join(f"{k}={v}" for k, v in FIELD.findall(match.group(3)))
    assert rebuilt == match.group(3), f"not a clean key=value run: {line}"
    return parsed


def llm_lines(lines: list[str]) -> list[dict[str, str]]:
    return [fields(line) for line in lines if f" {LLM_HEAD} " in line]


ALL = sorted(path.stem for path in FIXTURES.glob("*.jsonl"))


def test_the_fixtures_are_all_here() -> None:
    assert ALL == [
        "failed_model", "max_turns", "retried", "stopped", "subagent", "tools", "websearch"
    ]  # fmt: skip


# --- the shape of every line ---------------------------------------------------------------------


@pytest.mark.parametrize("name", ALL)
def test_every_structured_line_is_a_contract_head_with_contract_fields(name: str) -> None:
    for line in run(name):
        match = STRUCTURED.match(line)
        if match is None:
            continue
        head = match.group(2)
        assert set(fields(line)) <= HEADS[head], (head, set(fields(line)) - HEADS[head])


@pytest.mark.parametrize("name", ALL)
def test_no_line_is_read_by_a_noc_call_column(name: str) -> None:
    # The comparison mark (#481): every column that counts, times or prices a call is gated on
    # ` llm provider=`, so while the receiver still writes these calls, no line here may match.
    for line in run(name):
        assert "llm provider=" not in line, line


@pytest.mark.parametrize("name", ALL)
def test_no_wake_line_is_written(name: str) -> None:
    # The capital's ruling on #481: a builder has no wake in the contract's sense, and the
    # router's own `stage=wake` line is its record.
    for line in run(name):
        assert not re.match(r"\A(INFO|WARNING|ERROR) wake ", line), line


@pytest.mark.parametrize("name", ALL)
def test_prose_carries_no_field_a_noc_column_extracts(name: str) -> None:
    # A prose line carrying `provider=` or `source=` would feed a column whatever its head.
    for line in run(name):
        if STRUCTURED.match(line) is None:
            assert "provider=" not in line and "source=" not in line, line


# --- model calls ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["tools", "max_turns", "websearch", "subagent"])
def test_one_line_per_main_call_priced_to_claude_codes_own_figure(name: str) -> None:
    events = _events(name)
    starts = [
        e
        for e in events
        if e["type"] == "stream_event"
        and e["event"]["type"] == "message_start"
        and e["parent_tool_use_id"] is None
    ]
    priced = [line for line in llm_lines(run(name)) if "cost" in line]
    assert len(priced) == len(starts)
    result = next(e for e in events if e["type"] == "result")
    main = result["modelUsage"]["claude-opus-5-5"]
    total = sum(float(line["cost"]) for line in priced)
    if name == "subagent":
        assert total < main["costUSD"]  # the subagent's two calls are billed and never itemized
    else:
        assert total == pytest.approx(main["costUSD"], abs=1e-7)
        itemized = sum(int(line["tokens_out"]) for line in priced)
        assert itemized == main["outputTokens"]


def test_a_tool_run_renders_the_contract_lines() -> None:
    assert run("tools") == [
        "INFO llm shadow provider=anthropic purpose=main residency=global model=claude-opus-5-5"
        " duration=2.50s tokens_in=27040 tokens_out=74 tokens_total=27114 cached_tokens=11379"
        " cache_write_tokens=15659 tokens_reasoning=0 cost=0.1290358 cost_basis=computed"
        " outcome=ok generation_id=req_011FabNova01Call0001",
        "INFO tool name=Bash executor=harness duration=0.16s outcome=ok",
        "INFO llm shadow provider=anthropic purpose=main residency=global model=claude-opus-5-5"
        " duration=3.00s tokens_in=27160 tokens_out=85 tokens_total=27245 cached_tokens=27038"
        " cache_write_tokens=120 tokens_reasoning=0 cost=0.0080756 cost_basis=computed"
        " outcome=ok generation_id=req_011FabNova01Call0002",
        "INFO tool name=Bash executor=harness duration=0.05s outcome=error reason=tool_error",
        'WARNING tool name=Bash error="Exit code 2 ls: cannot access'
        " '/home/basecradle-nova-ai/clone/missing': No such file or directory\"",
        "INFO tool name=Write executor=harness duration=0.02s outcome=error reason=withheld",
        "INFO llm shadow provider=anthropic purpose=main residency=global model=claude-opus-5-5"
        " duration=5.50s tokens_in=27320 tokens_out=150 tokens_total=27470 cached_tokens=27158"
        " cache_write_tokens=160 tokens_reasoning=0 cost=0.0097196 cost_basis=computed"
        " outcome=ok generation_id=req_011FabNova01Call0003",
        "INFO llm shadow provider=anthropic purpose=main residency=global model=claude-opus-5-5"
        " duration=3.75s tokens_in=27624 tokens_out=156 tokens_total=27780 cached_tokens=27318"
        " cache_write_tokens=304 tokens_reasoning=151 cost=0.0110236 cost_basis=computed"
        " outcome=ok generation_id=req_011FabNova01Call0004",
        "Done: the work is finished.",
        "INFO Claude Code ended after 4 turn(s) (completed).",
    ]


def test_the_call_line_uses_the_final_usage_never_the_assistant_lines_placeholder() -> None:
    # Each `assistant` line carries `message_start`'s snapshot, whose output_tokens is a
    # placeholder; only `message_delta` states the final count (#481 finding 1).
    placeholders = {
        e["message"]["usage"]["output_tokens"]
        for e in _events("tools")
        if e["type"] == "assistant" and e["message"]["usage"]
    }
    finals = {line["tokens_out"] for line in llm_lines(run("tools"))}
    assert finals == {"74", "85", "150", "156"}
    assert not finals & {str(n) for n in placeholders}


def test_retries_are_their_own_lines_and_the_failure_names_its_attempts() -> None:
    assert run("retried") == [
        "WARNING llm shadow provider=anthropic purpose=main model=claude-opus-5-5"
        " outcome=retrying attempt=1/3 reason=transport next_in=0.51s",
        "WARNING llm shadow provider=anthropic purpose=main model=claude-opus-5-5"
        " outcome=retrying attempt=2/3 reason=transport next_in=1.04s",
        "WARNING llm shadow provider=anthropic purpose=main model=claude-opus-5-5"
        ' duration=0.75s outcome=failed attempt=3/3 reason=transport detail="API Error:'
        ' Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)"',
        "API Error: Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)",
        "INFO Claude Code ended after 1 turn(s) (api_error).",
    ]


def test_a_failed_call_is_named_by_its_status_and_states_no_tokens_or_cost() -> None:
    (line,) = llm_lines(run("failed_model"))
    assert line["outcome"] == "failed"
    assert line["reason"] == "config:model_not_found"
    assert line["model"] == "claude-nonexistent-model-9"  # the model as configured
    assert "attempt" not in line  # no retry was seen, so the attempts are not counted
    assert not {"tokens_in", "tokens_out", "tokens_total", "cost", "cost_basis"} & set(line)


@pytest.mark.parametrize(
    ("status", "no_response", "slug"),
    [
        (None, False, "transport"),
        (None, True, "timeout"),
        (401, False, "config:auth"),
        (403, False, "config:auth"),
        (402, False, "config:billing"),
        (404, False, "config:model_not_found"),
        (413, False, "payload_too_large"),
        (429, False, "rate_limited"),
        (504, False, "timeout"),
        (500, False, "server_error"),
        (529, False, "server_error"),
        (400, False, "api_error"),
        (True, False, "transport"),  # a bool is no status
    ],
)
def test_the_fault_slugs_are_the_contracts_table(status, no_response, slug) -> None:
    assert fault_slug(status, no_response=no_response) == slug


def test_a_refusal_is_its_own_outcome() -> None:
    def refuse(event: dict[str, Any]) -> None:
        if event["type"] == "stream_event" and event["event"]["type"] == "message_delta":
            event["event"]["delta"]["stop_reason"] = "refusal"

    first = llm_lines(translate(rewrite("max_turns", refuse)))[0]
    assert first["outcome"] == "refused"
    assert "duration" in first  # the contract requires it on `refused`


def test_a_stopped_stream_writes_no_line_for_the_call_it_cut_and_says_so() -> None:
    assert run("stopped", stopped_by="TERM") == [
        "INFO Claude Code's stream ended after a SIGTERM: 1 model call(s) and 0 tool use(s)"
        " in flight have no line.",
    ]
    assert run("stopped")[-1].startswith("INFO Claude Code's stream ended without a result:")


def test_every_line_before_a_crash_is_written_as_it_arrives() -> None:
    # A wake that dies leaves every completed call: cut the tools stream after its second call.
    lines = (FIXTURES / "tools.jsonl").read_text().splitlines()
    stops = [i for i, line in enumerate(lines) if '"message_stop"' in line]
    out = translate(lines[: stops[1] + 1])
    assert len(llm_lines(out)) == 2
    assert out[-1].startswith("INFO Claude Code's stream ended without a result")


# --- what the stream does not itemize ------------------------------------------------------------


def test_a_subagents_calls_are_counted_with_their_usage_left_out() -> None:
    out = run("subagent")
    unitemized = [line for line in llm_lines(out) if "cost" not in line]
    assert len(unitemized) == 2
    for line in unitemized:
        assert line["cost_gap"] == "not_reported"
        assert not {"tokens_in", "tokens_out", "tokens_total", "duration"} & set(line)
    gaps = [line for line in out if line.startswith("WARNING cost unpriced ")]
    assert gaps == [
        "WARNING cost unpriced provider=anthropic model=claude-opus-5-5 charge=tokens"
        " reason=not_reported"
    ]  # once per wake per gap
    assert out[-1] == (
        "INFO Claude Code billed claude-opus-5-5 $0.25844 for 97060 input and 316 output"
        " tokens; the stream itemized 54908 input and 188 output tokens, priced at"
        " $0.1443304. The rest is 2 subagent call(s) seen without usage."
    )


def test_a_helper_model_the_stream_never_shows_is_stated_at_the_end() -> None:
    out = run("websearch")
    assert all(line["model"] == "claude-opus-5-5" for line in llm_lines(out))
    assert out[-1] == (
        "INFO Claude Code billed claude-haiku-5-5 $0.0116784 for 14249 input and 507 output"
        " tokens; the stream itemized 0 input and 0 output tokens, priced at $0. The rest is"
        " calls Claude Code made that the stream never shows, such as a helper model's."
    )


def test_the_reply_is_written_before_anything_the_end_computes() -> None:
    out = run("websearch")
    assert out.index("Done: the work is finished.") < out.index(
        next(line for line in out if line.startswith("INFO Claude Code billed"))
    )


def test_a_stale_rate_is_loud() -> None:
    def overstate(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            event["modelUsage"]["claude-opus-5-5"]["costUSD"] += 0.01

    out = translate(rewrite("tools", overstate))
    assert out[-1].startswith("WARNING Claude Code billed claude-opus-5-5 $0.1678546 for the same")
    assert "rate table may be stale" in out[-1]


def test_an_unknown_model_is_unpriced_and_says_why_once() -> None:
    def rename(event: dict[str, Any]) -> None:
        if event["type"] == "stream_event" and event["event"]["type"] == "message_start":
            event["event"]["message"]["model"] = "claude-unknown-9"

    out = translate(rewrite("tools", rename))
    calls = llm_lines(out)
    assert len(calls) == 4
    assert {line["cost_gap"] for line in calls} == {"unknown_model"}
    assert all("cost" not in line and "tokens_total" in line for line in calls)
    assert [line for line in out if "cost unpriced" in line] == [
        "WARNING cost unpriced provider=anthropic model=claude-unknown-9 charge=tokens"
        " reason=unknown_model"
    ]


def test_a_host_that_is_not_anthropics_is_unpriced() -> None:
    calls = llm_lines(run("tools", base_url="https://gateway.example.com/v1"))
    assert {line["cost_gap"] for line in calls} == {"unpriced_host"}
    assert all(line["cost_basis"] == "computed" for line in llm_lines(run("tools")))


def test_a_server_side_search_is_a_server_tool_line_with_its_fee() -> None:
    def search(event: dict[str, Any]) -> None:
        if event["type"] == "stream_event" and event["event"]["type"] == "message_delta":
            event["event"]["usage"]["server_tool_use"] = {"web_search_requests": 2}

    out = translate(rewrite("max_turns", search))
    call = out.index(next(line for line in out if f" {LLM_HEAD} " in line))
    assert out[call + 1] == (
        "INFO tool name=web_search executor=server provider=anthropic kind=search.web count=2"
        " outcome=ok cost=0.02 cost_basis=computed generation_id=req_011FabNova03Call0001"
    )


# --- nothing it reads can cost the wake ----------------------------------------------------------


def test_a_line_that_is_not_json_passes_through_as_prose() -> None:
    assert translate(["claude: a warning printed as plain text"])[0] == (
        "claude: a warning printed as plain text"
    )


@pytest.mark.parametrize(
    "line",
    [
        "[]",
        "42",
        '{"type": "assistant", "message": []}',
        '{"type": "assistant", "message": {"content": "text", "id": 7}}',
        '{"type": "user", "message": {"content": [{"type": "tool_result"}]}}',
        '{"type": "stream_event", "event": {"type": "message_start", "message": {}}}',
        '{"type": "stream_event", "event": {"type": "message_delta"}, "api_message_id": 3}',
        '{"type": "system", "subtype": "api_retry", "attempt": "x", "retry_delay_ms": "y"}',
        '{"type": "result", "modelUsage": {"m": {"costUSD": "free"}}, "num_turns": -1}',
        '{"type": "result", "modelUsage": {"m": {"costUSD": NaN}}}',
    ],
)
def test_a_malformed_event_never_raises(line: str) -> None:
    translate([line])


def test_an_internal_fault_is_counted_and_said_never_raised(monkeypatch) -> None:
    def boom(*_args: Any) -> None:
        raise RuntimeError("fabricated fault")

    monkeypatch.setattr(Translator, "_user", boom)
    out = run("tools")
    assert out[-1].startswith("WARNING The stream translator could not read 3 line(s)")
    assert "Done: the work is finished." in out


@pytest.mark.parametrize(
    ("value", "text"),
    [(0.0, "0"), (0.1297006, "0.1297006"), (0.00000001, "0.00000001"), (1.5, "1.5"), (2, "2")],
)
def test_money_is_plain_fixed_point(value: float, text: str) -> None:
    assert money(value) == text
