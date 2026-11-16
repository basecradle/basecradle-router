"""The builder's stream translator: Claude Code's stream in, the telemetry contract's lines out.

Driven over streams captured from Claude Code 2.1.325 for basecradle-router#481 and rebuilt as
fabricated fixtures (``tests/fixtures/claude_stream/``): every identifier, path and text is
invented, and the numbers (tokens, costs, attempts, statuses) are the captures' own, because
they are what is under test. A fixture whose run had subagents carries their transcripts beside
it, as Claude Code lays them out under its home (``transcripts/<fixture>/projects/…``), rebuilt
the same way. The clock is fake, so every ``duration`` is exact, and the close-out never sleeps.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from typing import Any

import pytest

from basecradle_router import claude_stream as cs
from basecradle_router.claude_stream import (
    LLM_HEAD,
    TRANSCRIPT_WAIT,
    Translator,
    aimed_at,
    claude_home,
    fault_slug,
    money,
    read_subagent_transcripts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "claude_stream"
TRANSCRIPTS = FIXTURES / "transcripts"
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
HEADS = {
    LLM_HEAD: LLM_FIELDS,
    "tool": TOOL_FIELDS,
    "tool shadow": TOOL_FIELDS,
    "cost unpriced shadow": UNPRICED_FIELDS,
}

#: One structured line: severity, a head, then key=value fields (a value is bare or quoted).
STRUCTURED = re.compile(
    r"\A(INFO|WARNING) (llm shadow|tool shadow|tool|cost unpriced shadow) ([a-z_]+=.+)\Z"
)
FIELD = re.compile(r'([a-z_]+)=("[^"]*"|[^ "]+)(?: |\Z)')


def _events(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines()]


def translate(
    lines: list[str],
    *,
    stopped_by: str | None = None,
    base_url: str | None = None,
    home: Path | None = None,
) -> list[str]:
    ticks = itertools.count()
    out: list[str] = []
    translator = Translator(
        write=out.append,
        clock=lambda: next(ticks) * TICK,
        base_url=base_url,
        home=str(home) if home is not None else None,
        sleep=lambda _seconds: None,
    )
    for line in lines:
        translator.feed(line)
    translator.finish(stopped_by=stopped_by)
    return out


def home_of(name: str) -> Path | None:
    """The fixture's Claude Code home, holding its subagent transcripts, if its run had any."""
    home = TRANSCRIPTS / name
    return home if home.is_dir() else None


def run(name: str, **kwargs: Any) -> list[str]:
    kwargs.setdefault("home", home_of(name))
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
        "background_subagents", "failed_model", "max_turns", "retried", "stopped",
        "stopped_subagent", "subagent", "subagent_websearch", "tools", "websearch",
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
        assert "cost unpriced provider=" not in line, line


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


#: Each fixture whose run was stopped, so its stream ends after the stop's SIGTERM.
STOPPED = {"stopped", "stopped_subagent"}


def stopped_by(name: str) -> str | None:
    return "TERM" if name in STOPPED else None


def _starts(name: str) -> list[dict[str, Any]]:
    return [
        e
        for e in _events(name)
        if e["type"] == "stream_event"
        and e["event"]["type"] == "message_start"
        and e["parent_tool_use_id"] is None
    ]


def _last_result(name: str) -> dict[str, Any] | None:
    results = [e for e in _events(name) if e["type"] == "result"]
    return results[-1] if results else None


@pytest.mark.parametrize("name", ["tools", "max_turns", "websearch", "subagent"])
def test_one_line_per_main_call(name: str) -> None:
    main = [line for line in llm_lines(run(name)) if "duration" in line]
    assert len(main) == len(_starts(name))
    assert all(line["purpose"] == "main" and "cost" in line for line in main)


@pytest.mark.parametrize("name", [n for n in ALL if n not in STOPPED])
def test_a_wakes_lines_add_up_to_claude_codes_stated_total_to_the_last_digit(name: str) -> None:
    # The second comparison's bar (#481): the dollars on every line, the closing lines included,
    # are Claude Code's own `total_cost_usd`, and so are the tokens, per model.
    out = run(name)
    result = _last_result(name)
    assert result is not None
    dollars = sum(
        float(fields(line)["cost"])
        for line in out
        if (f" {LLM_HEAD} " in line or line.startswith("INFO tool shadow ")) and " cost=" in line
    )
    assert dollars == pytest.approx(result["total_cost_usd"], abs=1e-9)
    for model, figures in result["modelUsage"].items():
        lines = [line for line in llm_lines(out) if line["model"] == model and "tokens_in" in line]
        stated_in = sum(
            figures[key]
            for key in ("inputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
        )
        assert sum(int(line["tokens_in"]) for line in lines) == stated_in
        assert sum(int(line["tokens_out"]) for line in lines) == figures["outputTokens"]


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
        "> Done: the work is finished.",
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
        "> API Error: Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)",
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
        "WARNING Claude Code's stream ended after a SIGTERM: 1 model call(s) and 0 tool use(s)"
        " in flight have no line, and with no final result there is no stated total, so no"
        " closing line: a call neither the stream nor the transcripts itemize, such as a helper"
        " model's, has no line.",
    ]
    assert run("stopped")[-1].startswith("WARNING Claude Code's stream ended without a result:")


def test_every_line_before_a_crash_is_written_as_it_arrives() -> None:
    # A wake that dies leaves every completed call: cut the tools stream after its second call.
    lines = (FIXTURES / "tools.jsonl").read_text().splitlines()
    stops = [i for i, line in enumerate(lines) if '"message_stop"' in line]
    out = translate(lines[: stops[1] + 1])
    assert len(llm_lines(out)) == 2
    assert out[-1].startswith("WARNING Claude Code's stream ended without a result")


# --- what the stream does not itemize ------------------------------------------------------------


def test_a_subagent_with_no_transcript_is_counted_its_dollars_in_the_closing_line() -> None:
    # The `subagent` capture kept no transcripts: its two subagent calls get lines with no
    # figures, the gap is said, and the closing line carries what they billed.
    out = run("subagent")
    unitemized = [line for line in llm_lines(out) if "cost" not in line]
    assert len(unitemized) == 2
    for line in unitemized:
        assert line["cost_gap"] == "not_reported"
        assert not {"tokens_in", "tokens_out", "tokens_total", "duration"} & set(line)
    gaps = [line for line in out if line.startswith("WARNING cost unpriced ")]
    assert gaps == [
        "WARNING cost unpriced shadow provider=anthropic model=claude-opus-5-5 charge=tokens"
        " reason=not_reported"
    ]  # once per wake per gap
    closing = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing == [fields(
        "INFO llm shadow provider=anthropic purpose=helper model=claude-opus-5-5 tokens_in=42152"
        " tokens_out=128 tokens_total=42280 cached_tokens=20668 cache_write_tokens=21480"
        " cost=0.1141096 cost_basis=computed outcome=ok"
    )]  # fmt: skip
    assert (
        "INFO The helper line above for claude-opus-5-5 is not one call: it is the rest of the"
        " $0.25844 Claude Code billed for claude-opus-5-5 in this run, where 4 call(s) above"
        " itemize $0.1443304 of it. It holds the calls Claude Code never shows one by one, such"
        " as a helper model's, and the figures of the 2 call(s) above that carry none."
    ) in out
    assert out[-1] == (
        "WARNING The stream translator could not read every subagent call from the transcripts"
        " Claude Code 2.1.325 wrote: 2 subagent call(s) the stream named, with no session id or"
        " home to look in. Those calls' lines carry no tokens or cost, and their dollars are in"
        " the closing line, if the run stated a total. The transcript shape it reads is Claude"
        " Code 2.1.325's."
    )
    assert run("subagent", home=TRANSCRIPTS / "nowhere")[-1].startswith(
        "WARNING The stream translator could not read every subagent call from the transcripts"
        " Claude Code 2.1.325 wrote: 2 subagent call(s) the stream named, with no transcript"
        " directory for the session."
    )


def test_a_helper_model_the_stream_never_shows_is_the_closing_line() -> None:
    out = run("websearch")
    assert [line["model"] for line in llm_lines(out) if line["purpose"] == "main"] == [
        "claude-opus-5-5"
    ] * 3
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing == fields(
        "INFO llm shadow provider=anthropic purpose=helper model=claude-haiku-5-5 tokens_in=14249"
        " tokens_out=507 tokens_total=14756 cached_tokens=0 cost=0.0116784 cost_basis=computed"
        " outcome=ok"
    )
    # No `kind`, no `duration`, no `generation_id`: the contract's builder auxiliary call.
    assert not {"kind", "duration", "generation_id"} & set(closing)
    assert out[-1] == (
        "INFO The helper line above for claude-haiku-5-5 is not one call: it is the rest of the"
        " $0.0116784 Claude Code billed for claude-haiku-5-5 in this run, where no line above"
        " itemizes any of it. It holds the calls Claude Code never shows one by one, such as a"
        " helper model's."
    )


def test_the_reply_is_written_before_anything_the_end_computes() -> None:
    out = run("websearch")
    assert out.index("> Done: the work is finished.") < out.index(
        next(line for line in out if " purpose=helper " in line)
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
        if event["type"] == "result":
            usage = event["modelUsage"]
            usage["claude-unknown-9"] = usage.pop("claude-opus-5-5")

    out = translate(rewrite("tools", rename))
    calls = llm_lines(out)
    assert len(calls) == 4
    assert {line["cost_gap"] for line in calls} == {"unknown_model"}
    assert all("cost" not in line and "tokens_total" in line for line in calls)
    assert [line for line in out if "cost unpriced" in line] == [
        "WARNING cost unpriced shadow provider=anthropic model=claude-unknown-9 charge=tokens"
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
        if event["type"] == "result":  # Claude Code's own figure carries the fee
            event["modelUsage"]["claude-opus-5-5"]["costUSD"] += 0.02

    out = translate(rewrite("max_turns", search))
    call = out.index(next(line for line in out if f" {LLM_HEAD} " in line))
    assert out[call + 1] == (
        "INFO tool shadow name=web_search executor=server provider=anthropic kind=search.web"
        " count=2"
        " outcome=ok cost=0.02 cost_basis=computed generation_id=req_011FabNova03Call0001"
    )
    # The fee is part of what was priced, so the end finds Claude Code's figure matched.
    assert not [line for line in out if "rate table may be stale" in line]


# --- nothing it reads can cost the wake ----------------------------------------------------------


def test_a_line_that_is_not_json_passes_through_as_quoted_prose() -> None:
    assert translate(["claude: a warning printed as plain text"])[0] == (
        "> claude: a warning printed as plain text"
    )


@pytest.mark.parametrize("line", ["200", "null", "true", '"quoted"', "[1, 2]"])
def test_json_that_is_no_event_passes_through_as_quoted_prose(line: str) -> None:
    assert translate([line])[0] == "> " + line


def test_json_nested_past_the_parsers_depth_is_counted_never_raised() -> None:
    out = translate(["[" * 100_000 + "]" * 100_000])
    assert out[0].startswith("WARNING The stream translator could not read 1 line(s)")


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
    assert any(
        line.startswith("WARNING The stream translator could not read 3 line(s)") for line in out
    )
    assert "> Done: the work is finished." in out


@pytest.mark.parametrize(
    ("value", "text"),
    [(0.0, "0"), (0.1297006, "0.1297006"), (0.00000001, "0.00000001"), (1.5, "1.5"), (2, "2")],
)
def test_money_is_plain_fixed_point(value: float, text: str) -> None:
    assert money(value) == text


def test_a_server_tools_fee_is_marked_because_the_receivers_call_cost_holds_it() -> None:
    # A harness or MCP tool line is a fact the receiver never had, so it stays bare.
    def search(event: dict[str, Any]) -> None:
        if event["type"] == "stream_event" and event["event"]["type"] == "message_delta":
            event["event"]["usage"]["server_tool_use"] = {"web_fetch_requests": 1}

    out = translate(rewrite("max_turns", search))
    assert any(line.startswith("INFO tool shadow name=web_fetch executor=server") for line in out)
    assert any(line.startswith("INFO tool name=Bash executor=harness") for line in out)
    assert not any(" tool name=" in line and "executor=server" in line for line in out)


def test_stream_json_cut_mid_line_is_counted_and_never_written() -> None:
    # A stop that lands mid-write leaves part of a line, which can hold a tool's whole result.
    secretish = '{"type": "user", "message": {"content": [{"type": "tool_result", "content": "DB_'
    out = translate([secretish])
    assert not [line for line in out if "DB_" in line]
    assert out[0].startswith("WARNING The stream translator could not read 1 line(s)")


def test_a_subagents_failed_call_leaves_the_main_threads_call_alone() -> None:
    lines = (FIXTURES / "tools.jsonl").read_text().splitlines()
    first_start = next(i for i, line in enumerate(lines) if '"message_start"' in line)
    failure = {
        "type": "assistant",
        "parent_tool_use_id": "toolu_01FabNova99Tool0001",
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error"}]},
        "is_api_error_message": True,
        "api_error_status": 529,
    }
    spliced = lines[:first_start] + [json.dumps(failure)] + lines[first_start:]
    calls = llm_lines(translate(spliced))
    failed = [line for line in calls if line["outcome"] == "failed"]
    assert len(failed) == 1 and failed[0]["reason"] == "server_error"
    assert "duration" not in failed[0] and "attempt" not in failed[0]
    # The main thread's call still times from its own `status requesting`: unspliced it is 2.50s,
    # and the spliced line adds one tick of the fake clock inside the call.
    assert llm_lines(run("tools"))[0]["duration"] == "2.50s"
    assert calls[1]["duration"] == "2.75s"


@pytest.mark.parametrize(
    ("environ", "host"),
    [
        ({}, None),
        ({"ANTHROPIC_BASE_URL": "https://gateway.example.com"}, "https://gateway.example.com"),
        ({"CLAUDE_CODE_USE_BEDROCK": "1"}, "bedrock"),
        ({"CLAUDE_CODE_USE_VERTEX": "true", "ANTHROPIC_BASE_URL": "x"}, "vertex"),
        ({"CLAUDE_CODE_USE_FOUNDRY": "0"}, None),
    ],
)
def test_where_claude_code_sends_its_calls(environ: dict, host: str | None) -> None:
    assert aimed_at(environ) == host


def test_a_cloud_providers_switch_leaves_every_call_unpriced() -> None:
    calls = llm_lines(run("tools", base_url=aimed_at({"CLAUDE_CODE_USE_BEDROCK": "1"})))
    assert {line["cost_gap"] for line in calls} == {"unpriced_host"}


def test_a_host_named_only_at_the_end_is_said_out_loud() -> None:
    def elsewhere(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            event["modelUsage"]["claude-opus-5-5"]["provider"] = "bedrock"

    out = translate(rewrite("tools", elsewhere))
    assert (
        "WARNING Claude Code served claude-opus-5-5 through bedrock, so the cost on its calls"
        " above is Anthropic's list price, not what that host charges."
    ) in out


@pytest.mark.parametrize("details", [[], "eight", 3])
def test_a_usage_field_of_the_wrong_shape_costs_no_line(details) -> None:
    def odd(event: dict[str, Any]) -> None:
        if event["type"] == "stream_event" and event["event"]["type"] == "message_delta":
            event["event"]["usage"]["output_tokens_details"] = details

    calls = llm_lines(translate(rewrite("tools", odd)))
    assert len(calls) == 4
    assert all("tokens_reasoning" not in line and "cost" in line for line in calls)


def _without_message_ids(name: str) -> list[str]:
    """The fixture as Claude Code 2.1.286 writes it: no ``api_message_id`` on a stream event."""

    def strip(event: dict[str, Any]) -> None:
        event.pop("api_message_id", None)

    return rewrite(name, strip)


@pytest.mark.parametrize("name", ALL)
def test_a_stream_that_names_no_message_on_its_events_renders_the_same_lines(name: str) -> None:
    # Claude Code 2.1.286 names a message on `message_start` only; the captures (2.1.325) name
    # it on every stream event. The lines must not depend on which wrote them.
    stopped = stopped_by(name)
    assert translate(_without_message_ids(name), stopped_by=stopped, home=home_of(name)) == run(
        name, stopped_by=stopped
    )


def test_a_cut_call_without_message_ids_still_has_no_line() -> None:
    assert translate(_without_message_ids("stopped"), stopped_by="TERM") == run(
        "stopped", stopped_by="TERM"
    )


# --- a stream the translator does not understand is said, never silent (#481) --------------------

UNPLACED = "WARNING The stream translator could not place what Claude Code 2.1.325 wrote: "


#: The one fixture captured without its transcripts, whose gap is the point of its own test.
NO_TRANSCRIPTS = {"subagent"}


@pytest.mark.parametrize("name", [n for n in ALL if n not in NO_TRANSCRIPTS])
def test_a_stream_of_a_known_shape_places_everything(name: str) -> None:
    assert not [line for line in run(name, stopped_by=stopped_by(name)) if "could not" in line]


def _drop(name: str, keep) -> list[str]:
    return [json.dumps(e) for e in _events(name) if keep(e)]


def test_calls_that_never_open_are_said_loudly() -> None:
    # What the box's 2.1.286 stream did to the code that keyed every event on its message id:
    # no call ever matched, and the wake looked like one that made no model calls.
    out = translate(
        _drop(
            "tools",
            lambda e: not (e["type"] == "stream_event" and e["event"]["type"] == "message_start"),
        )
    )
    # Nothing was itemized, so the closing line holds the whole stated total: dollars right,
    # and the warning says why no call has a line of its own.
    assert [line["purpose"] for line in llm_lines(out)] == ["helper"]
    (warning,) = [line for line in out if "could not place" in line]
    assert warning.startswith(UNPLACED)
    for shape in (
        "4 main-thread message(s) that came with no stream events",
        "4 message_delta(s) that matched no open model call",
        "4 message_stop(s) that matched no open model call",
    ):
        assert shape in warning
    assert warning.endswith("The shapes it reads are Claude Code 2.1.286 and 2.1.325's.")


def test_a_stream_with_no_stream_events_at_all_is_said_loudly() -> None:
    # What a missing --include-partial-messages, or a Claude Code that dropped them, looks like.
    out = translate(_drop("tools", lambda e: e["type"] != "stream_event"))
    assert [line["purpose"] for line in llm_lines(out)] == ["helper"]
    assert any(
        line.startswith(UNPLACED)
        and "4 main-thread message(s) that came with no stream events" in line
        for line in out
    )


def test_calls_still_open_at_the_result_are_said_loudly() -> None:
    out = translate(
        _drop(
            "tools",
            lambda e: not (e["type"] == "stream_event" and e["event"]["type"] == "message_stop"),
        )
    )
    (warning,) = [line for line in out if "could not place" in line]
    assert "4 model call(s) still open at the result" in warning
    assert "3 message_start(s) while another call was still open" in warning


def test_a_tool_result_for_no_tool_use_is_counted() -> None:
    out = translate(_drop("tools", lambda e: e["type"] != "assistant"))
    (warning,) = [line for line in out if "could not place" in line]
    assert "3 tool result(s) that matched no tool use" in warning


START = {"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_start"}}


def _start(message_id: str) -> str:
    event = json.loads(json.dumps(START))
    event["event"]["message"] = {
        "id": message_id,
        "model": "claude-opus-5-5",
        "usage": {"input_tokens": 2, "output_tokens": 1},
    }
    return json.dumps(event)


def _event(kind: str, message_id: str | None = None) -> str:
    event: dict[str, Any] = {"type": "stream_event", "parent_tool_use_id": None}
    event["event"] = {"type": kind}
    if kind == "message_delta":
        event["event"]["usage"] = {"input_tokens": 2, "output_tokens": 40}
        event["event"]["delta"] = {"stop_reason": "end_turn"}
    if message_id is not None:
        event["api_message_id"] = message_id
    return json.dumps(event)


def test_a_named_event_goes_to_the_call_it_names() -> None:
    out = translate(
        [
            _start("msg_01FabNova90Call0001"),
            _start("msg_01FabNova90Call0002"),
            _event("message_delta", "msg_01FabNova90Call0001"),
            _event("message_stop", "msg_01FabNova90Call0001"),
        ]
    )
    (call,) = llm_lines(out)
    assert call["tokens_out"] == "40"


def test_an_unnamed_event_with_two_calls_open_is_counted_never_guessed() -> None:
    out = translate(
        [
            _start("msg_01FabNova91Call0001"),
            _start("msg_01FabNova91Call0002"),
            _event("message_delta"),
            _event("message_stop"),
        ]
    )
    assert not llm_lines(out)
    (warning,) = [line for line in out if "could not place" in line]
    assert "1 message_delta(s) that matched no open model call" in warning
    assert "1 message_stop(s) that matched no open model call" in warning
    assert "1 message_start(s) while another call was still open" in warning


def test_an_unnamed_event_with_no_call_open_is_counted() -> None:
    out = translate([_event("message_stop")])
    assert any("1 message_stop(s) that matched no open model call" in line for line in out)


def test_a_main_thread_failure_closes_the_call_it_ended() -> None:
    failure = {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {"model": "<synthetic>", "content": [{"type": "text", "text": "API Error"}]},
        "is_api_error_message": True,
        "api_error_status": 529,
    }
    out = translate(
        [
            _start("msg_01FabNova92Call0001"),
            json.dumps(failure),
            _event("message_delta"),
            _event("message_stop"),
        ]
    )
    assert [line["outcome"] for line in llm_lines(out)] == ["failed"]
    assert any("matched no open model call" in line for line in out)


def test_the_box_shape_reconciles_against_claude_codes_alias() -> None:
    # The box: Claude Code 2.1.286 (no message ids on stream events) running the 1M-context
    # alias, which Claude Code's `modelUsage` names `claude-opus-5-5[1m]` and the API's message
    # names `claude-opus-5-5`. The first real builder wakes (2026-11-16) had both.
    def alias(event: dict[str, Any]) -> None:
        event.pop("api_message_id", None)
        if event["type"] == "result":
            usage = event["modelUsage"]
            usage["claude-opus-5-5[1m]"] = usage.pop("claude-opus-5-5")

    out = translate(rewrite("tools", alias))
    assert len(llm_lines(out)) == 4
    assert not [line for line in out if "billed" in line or "could not place" in line]


def test_a_stated_canonical_model_is_the_one_reconciled() -> None:
    def named(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            usage = event["modelUsage"]
            figures = usage.pop("claude-opus-5-5")
            usage["opus-alias"] = figures | {"canonicalModel": "claude-opus-5-5"}

    assert not [line for line in translate(rewrite("tools", named)) if "billed" in line]


def test_an_alias_on_the_streams_own_message_is_reconciled_too() -> None:
    # The other direction: the call names the alias or a snapshot, Claude Code's total the id.
    def alias(event: dict[str, Any]) -> None:
        if event["type"] == "stream_event" and event["event"]["type"] == "message_start":
            event["event"]["message"]["model"] = "claude-opus-5-5[1m]"

    out = translate(rewrite("tools", alias))
    assert {line["model"] for line in llm_lines(out)} == {"claude-opus-5-5[1m]"}
    assert not [line for line in out if "billed" in line]


# --- subagent transcripts, the closing line, and a run of several results (#481 rework) ----------

SESSION = "0199c0de-5e55-7000-8000-000099000001"
BACKGROUND = TRANSCRIPTS / "background_subagents"


def _subagent_lines(out: list[str]) -> list[dict[str, str]]:
    """The subagent calls' lines: main purpose, no duration (the stream's own carry one)."""
    return [line for line in llm_lines(out) if line["purpose"] == "main" and "duration" not in line]


def _transcript_entries(name: str) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for path in sorted((TRANSCRIPTS / name).rglob("agent-*.jsonl"))
        for line in path.read_text().splitlines()
    ]


def _home(tmp_path: Path, entries: list[dict[str, Any]], session: str = SESSION) -> Path:
    """A Claude Code home holding one subagent transcript of ``entries`` for ``session``."""
    directory = tmp_path / "projects" / "-home-basecradle-nova-ai-clone" / session / "subagents"
    directory.mkdir(parents=True)
    (directory / "agent-a0fab990000000001.jsonl").write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries)
    )
    return tmp_path


def _entry(
    message_id: str,
    *,
    output: int = 100,
    stop: str | None = "end_turn",
    version: str = "2.1.325",
    content: str = "text",
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "sessionId": SESSION,
        "isSidechain": True,
        "version": version,
        "requestId": message_id.replace("msg_01", "req_011"),
        "message": {
            "id": message_id,
            "model": "claude-opus-5-5",
            "stop_reason": stop,
            "content": [{"type": content}],
            "usage": {
                "input_tokens": 2,
                "output_tokens": output,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 0,
                "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
                "output_tokens_details": {"thinking_tokens": 0},
            },
        },
    }


def _init(session: str = SESSION) -> str:
    return json.dumps(
        {
            "type": "system",
            "subtype": "init",
            "model": "claude-opus-5-5",
            "claude_code_version": "2.1.325",
            "session_id": session,
        }
    )


RESULT = json.dumps({"type": "result", "subtype": "success", "num_turns": 1, "result": "Done."})


def _named(message_id: str) -> str:
    """The stream's ``assistant`` line for a subagent call: named, never with final usage."""
    return json.dumps(
        {
            "type": "assistant",
            "parent_tool_use_id": "toolu_01FabNova99Tool0001",
            "request_id": message_id.replace("msg_01", "req_011"),
            "message": {"id": message_id, "model": "claude-opus-5-5", "content": [], "usage": {}},
        }
    )


def test_every_subagent_call_is_one_real_line_from_its_transcript() -> None:
    out = run("background_subagents")
    sub = _subagent_lines(out)
    distinct = {e["message"]["id"] for e in _transcript_entries("background_subagents")
                if e["type"] == "assistant"}  # fmt: skip
    assert len(sub) == len(distinct) == 6
    assert all("cost" in line and line["cost_basis"] == "computed" for line in sub)
    assert all(line["generation_id"].startswith("req_011FabNova08") for line in sub)
    # The same request ids the stream named for them, so the two records join call for call.
    named = {e["request_id"] for e in _events("background_subagents")
             if e["type"] == "assistant" and e["parent_tool_use_id"]}  # fmt: skip
    assert {line["generation_id"] for line in sub} == named
    assert not [line for line in out if "cost_gap" in line]


def test_a_response_of_several_entries_is_read_once_never_summed() -> None:
    # The capture's second call of the first subagent is two entries (thinking, then tool use),
    # each with the response's whole usage. Summed, it would read 350 output tokens.
    entries = [e for e in _transcript_entries("background_subagents")
               if e["type"] == "assistant"
               and e["requestId"] == "req_011FabNova08Call0005"]  # fmt: skip
    assert len(entries) == 2
    (line,) = [x for x in llm_lines(run("background_subagents"))
               if x.get("generation_id") == "req_011FabNova08Call0005"]  # fmt: skip
    assert line["tokens_out"] == "175"


def test_the_finished_entry_is_the_one_read(tmp_path: Path) -> None:
    home = _home(
        tmp_path,
        [
            _entry("msg_01FabNova99Call0001", output=7, stop=None, content="thinking"),
            _entry("msg_01FabNova99Call0001", output=300, content="tool_use"),
            _entry("msg_01FabNova99Call0001", output=9, stop=None, content="text"),
        ],
    )
    out = translate([_init(), _named("msg_01FabNova99Call0001")], home=home)
    (line,) = _subagent_lines(out)
    assert line["tokens_out"] == "300"  # never the first, the last, or the sum


def test_a_response_with_no_finished_entry_has_no_figures_and_is_said(tmp_path: Path) -> None:
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001", stop=None)])
    out = translate([_init(), _named("msg_01FabNova99Call0001"), RESULT], home=home)
    (line,) = _subagent_lines(out)
    assert line["cost_gap"] == "not_reported" and "tokens_in" not in line
    assert any("1 subagent call(s) with no finished transcript record" in x for x in out)


def test_a_named_call_with_no_transcript_record_is_said(tmp_path: Path) -> None:
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001")])
    out = translate(
        [_init(), _named("msg_01FabNova99Call0001"), _named("msg_01FabNova99Call0002"), RESULT],
        home=home,
    )
    lines = _subagent_lines(out)
    assert ["cost" in line for line in lines] == [True, False]
    assert any("1 subagent call(s) the stream named with no transcript record" in x for x in out)


def test_a_transcript_call_the_stream_never_named_is_still_written(tmp_path: Path) -> None:
    # A stream cut short (a crash mid-line) loses the `assistant` line; the transcript has it.
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001")])
    out = translate([_init()], home=home)
    (line,) = _subagent_lines(out)
    assert line["tokens_out"] == "100" and "cost" in line


def test_a_stopped_run_writes_every_subagent_call_and_no_closing_line() -> None:
    out = run("stopped_subagent", stopped_by="TERM")
    sub = _subagent_lines(out)
    assert len(sub) == 4 and all("cost" in line for line in sub)
    assert not [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert out[-1].startswith("WARNING Claude Code's stream ended after a SIGTERM:")
    assert "no stated total, so no closing line" in out[-1]


def test_a_transcript_from_before_2_1_307_is_never_read_for_its_numbers(tmp_path: Path) -> None:
    # Claude Code recorded placeholder usage for subagents before 2.1.307 (basecradle#789).
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001", version="2.1.306")])
    out = translate([_init(), _named("msg_01FabNova99Call0001")], home=home)
    (line,) = _subagent_lines(out)
    assert line["cost_gap"] == "not_reported" and "tokens_in" not in line
    (warning,) = [x for x in out if "could not read every subagent call" in x]
    # One call, one count: never once per entry, and never again as "no finished record".
    assert "1 subagent call(s) from Claude Code 2.1.306, whose usage is a placeholder" in warning
    assert "finished transcript record" not in warning
    clean = _home(tmp_path / "clean", [_entry("msg_01FabNova99Call0001", version="2.1.307")])
    assert "cost" in _subagent_lines(translate([_init(), _named("msg_01FabNova99Call0001")],
                                               home=clean))[0]  # fmt: skip


def test_a_transcript_line_it_cannot_read_is_counted_never_raised(tmp_path: Path) -> None:
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001")])
    path = next(home.rglob("agent-*.jsonl"))
    with path.open("a") as fh:
        fh.write('{"type": "assistant", "message": {"id": "msg_x"\n')  # cut mid-write
        fh.write('{"type": "assistant", "message": []}\n')
        fh.write('{"type": "assistant", "message": {"id": 7, "model": null}}\n')
        fh.write('{"type": "user", "message": {"content": "an assistant said"}}\n')
        fh.write("[" * 100_000 + '"assistant"' + "]" * 100_000 + "\n")
    out = translate([_init(), _named("msg_01FabNova99Call0001")], home=home)
    assert "cost" in _subagent_lines(out)[0]
    (warning,) = [line for line in out if "could not read every subagent call" in line]
    assert "2 transcript lines that are not JSON" in warning
    assert "1 assistant entries with no message" in warning
    assert "1 assistant entries that name no message or model" in warning


def test_the_session_id_must_be_a_uuid_to_be_looked_up(tmp_path: Path) -> None:
    found = read_subagent_transcripts(str(tmp_path), "../../etc")
    assert not found.found and found.problems
    out = translate([_init("../escape"), _named("msg_01FabNova99Call0001")], home=tmp_path)
    assert any("with no session id or home to look in" in line for line in out)


def test_a_session_under_two_projects_is_said_and_read_from_neither(tmp_path: Path) -> None:
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001")])
    second = home / "projects" / "-home-another-clone" / SESSION / "subagents"
    second.mkdir(parents=True)
    out = translate([_init(), _named("msg_01FabNova99Call0001"), RESULT], home=home)
    assert "cost" not in _subagent_lines(out)[0]
    assert any("1 session directories under more than one project" in line for line in out)


def test_a_main_thread_call_on_disk_is_never_written_twice(tmp_path: Path) -> None:
    events = _events("tools")
    session = next(e["session_id"] for e in events if e.get("subtype") == "init")
    message_id = next(e["message"]["id"] for e in events if e["type"] == "assistant")
    out = run("tools", home=_home(tmp_path, [_entry(message_id)], session=session))
    assert len(llm_lines(out)) == 4
    assert any("1 transcript entries for a main-thread call" in line for line in out)


def test_the_close_out_waits_a_bounded_time_for_a_write_still_landing(tmp_path: Path) -> None:
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001", stop=None)])
    path = next(home.rglob("agent-*.jsonl"))
    naps: list[float] = []

    def sleep(seconds: float) -> None:
        naps.append(seconds)
        if len(naps) == 2:  # the finished entry lands on the third read
            with path.open("a") as fh:
                fh.write(json.dumps(_entry("msg_01FabNova99Call0001", output=42)) + "\n")

    out: list[str] = []
    clock = itertools.count()
    translator = Translator(
        write=out.append, clock=lambda: next(clock) * 0.01, home=str(home), sleep=sleep
    )
    for line in (_init(), _named("msg_01FabNova99Call0001"), RESULT):
        translator.feed(line)
    translator.finish()
    assert naps == [cs.TRANSCRIPT_POLL] * 2
    assert _subagent_lines(out)[0]["tokens_out"] == "42"


def test_the_close_out_gives_up_at_its_deadline(tmp_path: Path) -> None:
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001", stop=None)])
    naps: list[float] = []
    now = [0.0]

    def sleep(seconds: float) -> None:
        naps.append(seconds)
        now[0] += seconds

    out: list[str] = []
    translator = Translator(write=out.append, clock=lambda: now[0], home=str(home), sleep=sleep)
    for line in (_init(), _named("msg_01FabNova99Call0001"), RESULT):
        translator.feed(line)
    translator.finish()
    assert sum(naps) == pytest.approx(TRANSCRIPT_WAIT, abs=cs.TRANSCRIPT_POLL)
    assert "cost" not in _subagent_lines(out)[0]


def test_a_half_written_line_does_not_end_the_wait(tmp_path: Path) -> None:
    # Claude Code still flushing its last entry: the cut line is not JSON yet, and one more read
    # finds it whole.
    home = _home(tmp_path, [])
    path = next(home.rglob("agent-*.jsonl"))
    whole = json.dumps(_entry("msg_01FabNova99Call0001", output=42)) + "\n"
    path.write_text(whole[:40])
    naps: list[float] = []

    def sleep(seconds: float) -> None:
        naps.append(seconds)
        path.write_text(whole)

    out: list[str] = []
    translator = Translator(write=out.append, clock=lambda: 0.0, home=str(home), sleep=sleep)
    for line in (_init(), _named("msg_01FabNova99Call0001"), RESULT):
        translator.feed(line)
    translator.finish()
    assert naps == [cs.TRANSCRIPT_POLL]
    assert _subagent_lines(out)[0]["tokens_out"] == "42"
    assert not [line for line in out if "could not read" in line]


def test_a_stopped_run_never_waits_on_its_transcripts(tmp_path: Path) -> None:
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001", stop=None)])

    def sleep(_seconds: float) -> None:
        raise AssertionError("waited on a run that has no result")

    out: list[str] = []
    translator = Translator(write=out.append, clock=lambda: 0.0, home=str(home), sleep=sleep)
    for line in (_init(), _named("msg_01FabNova99Call0001")):
        translator.feed(line)
    translator.finish(stopped_by="TERM")
    # Unfinished in a stopped run: cut short, so no line, as a main-thread call cut short has none.
    assert not _subagent_lines(out)
    assert out[-1].startswith("WARNING Claude Code's stream ended after a SIGTERM: 1 model call(s)")


def test_a_finished_usage_with_no_token_counts_is_never_a_free_call(tmp_path: Path) -> None:
    entry = _entry("msg_01FabNova99Call0001")
    entry["message"]["usage"] = {"tokens": 100}  # a renamed shape
    out = translate([_init(), _named("msg_01FabNova99Call0001")], home=_home(tmp_path, [entry]))
    (line,) = _subagent_lines(out)
    assert line["cost_gap"] == "not_reported"
    assert any("1 subagent call(s) whose finished usage states no token counts" in x for x in out)


def test_an_entry_that_states_no_version_is_never_read_for_its_numbers(tmp_path: Path) -> None:
    entry = _entry("msg_01FabNova99Call0001")
    del entry["version"]
    out = translate([_init(), _named("msg_01FabNova99Call0001")], home=_home(tmp_path, [entry]))
    assert _subagent_lines(out)[0]["cost_gap"] == "not_reported"
    assert any(
        "1 subagent call(s) whose transcript states no Claude Code version" in x for x in out
    )


def test_an_entry_marked_as_no_subagents_is_never_billed(tmp_path: Path) -> None:
    # A parent's history carried into a fork's transcript would be marked so.
    entry = _entry("msg_01FabNova99Call0002")
    entry["isSidechain"] = False
    out = translate([_init()], home=_home(tmp_path, [entry]))
    assert not llm_lines(out)
    assert any("1 entries not marked as a subagent's" in line for line in out)


def test_a_main_call_cut_short_is_never_billed_from_a_transcript(tmp_path: Path) -> None:
    # A main-thread call that opened and never stopped is still the main thread's.
    entry = _entry("msg_01FabNova99Call0003")
    out = translate([_init(), _start("msg_01FabNova99Call0003")], home=_home(tmp_path, [entry]))
    assert not llm_lines(out)
    assert any("1 transcript entries for a main-thread call" in line for line in out)


def test_an_itemized_write_with_no_split_is_subtracted_as_one_total(tmp_path: Path) -> None:
    # A subagent call wrote 500 to the cache with no breakdown; Claude Code's total splits its
    # 900 written as 400 five-minute and 500 one-hour. Bucket by bucket the five-minute remainder
    # would be -100 and the close would wrongly say the total was understated.
    entry = _entry("msg_01FabNova99Call0001")
    usage = entry["message"]["usage"]
    usage["cache_creation_input_tokens"] = 500
    del usage["cache_creation"]
    result = {
        "type": "result",
        "subtype": "success",
        "num_turns": 1,
        "modelUsage": {
            "claude-opus-5-5": {
                "inputTokens": 12, "outputTokens": 300, "cacheReadInputTokens": 2000,
                "cacheCreationInputTokens": 900, "cacheCreationEphemeral5mInputTokens": 400,
                "cacheCreationEphemeral1hInputTokens": 500, "webSearchRequests": 0,
                "costUSD": 0.05,
            }
        },
    }  # fmt: skip
    out = translate(
        [_init(), _named("msg_01FabNova99Call0001"), json.dumps(result)],
        home=_home(tmp_path, [entry]),
    )
    assert not [line for line in out if "stated less" in line]
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing["cache_write_tokens"] == "400" and closing["tokens_in"] == "1410"
    assert "cost" not in closing  # the itemized write was unpriced, so the remainder is too


def test_a_run_with_no_subagents_never_waits(tmp_path: Path) -> None:
    def sleep(_seconds: float) -> None:
        raise AssertionError("waited with nothing to wait for")

    out: list[str] = []
    translator = Translator(write=out.append, clock=lambda: 0.0, home=str(tmp_path), sleep=sleep)
    for line in (FIXTURES / "tools.jsonl").read_text().splitlines():
        translator.feed(line)
    translator.finish()


def test_several_results_write_each_reply_and_close_once_on_the_last() -> None:
    out = run("background_subagents")
    replies = [line for line in out if line.startswith("> Fabricated reply")]
    assert replies == [
        "> Fabricated reply 8.11.",
        "> Fabricated reply 8.12.",
        "> Fabricated reply 8.14.",
    ]
    # Each reply lands where its result did: after the main calls before it, before the close.
    assert out.index(replies[0]) < out.index(replies[2])
    assert [line for line in out if "turn(s)" in line] == [
        "INFO Claude Code ended after 5 turn(s) (success)."
    ]
    assert not [line for line in out if "billed" in line]  # fully itemized: no closing line
    # Judged against the last result's cumulative total, never an earlier, partial one, which
    # this run's calls outspent: read against that, the close would say a total was understated.
    assert not [line for line in out if line.startswith("WARNING")]


def test_the_closing_line_carries_the_remainder_of_each_model() -> None:
    out = run("subagent_websearch")
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing == fields(
        "INFO llm shadow provider=anthropic purpose=helper model=claude-haiku-5-5 tokens_in=14014"
        " tokens_out=781 tokens_total=14795 cached_tokens=0 tokens_reasoning=399 cost=0.0117919"
        " cost_basis=computed outcome=ok"
    )  # the helper's tokens and its search fee, as Claude Code billed them


def test_a_stated_total_below_what_was_itemized_writes_no_line_and_says_so() -> None:
    def understate(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            event["modelUsage"]["claude-opus-5-5"]["outputTokens"] -= 1

    out = translate(rewrite("tools", understate))
    assert not [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert (
        "WARNING Claude Code stated less for claude-opus-5-5 than this translator itemized over 4"
        " call(s), in output tokens ($0.1578546 stated, $0.1578546 itemized), so no closing line"
        " is written for it: something would be counted twice."
    ) in out


def test_a_remainder_the_rates_price_otherwise_is_loud_and_keeps_the_stated_total() -> None:
    def inflate(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            haiku = event["modelUsage"]["claude-haiku-5-5"]
            haiku["costUSD"] += 0.001  # the stated total says more than its tokens price at

    out = translate(rewrite("websearch", inflate))
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing["cost"] == "0.0126784"  # the stated total, by construction
    assert any(
        line.startswith("WARNING The published rates price the remainder for claude-haiku-5-5")
        for line in out
    )


def test_a_remainder_on_a_host_the_rates_do_not_price_carries_its_gap() -> None:
    out = run("websearch", base_url="https://gateway.example.com/v1")
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing["cost_gap"] == "unpriced_host" and "cost" not in closing
    assert closing["tokens_in"] == "14249"


def test_a_remainder_with_no_cache_split_still_carries_the_stated_difference() -> None:
    def unsplit(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            haiku = event["modelUsage"]["claude-haiku-5-5"]
            haiku["cacheCreationInputTokens"] = 40
            haiku["inputTokens"] -= 40
            haiku.pop("cacheCreationEphemeral5mInputTokens", None)
            haiku.pop("cacheCreationEphemeral1hInputTokens", None)

    out = translate(rewrite("websearch", unsplit))
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing["cache_write_tokens"] == "40" and closing["tokens_in"] == "14249"
    # The table cannot price an unsplit write, but the cost is the stated total less nothing
    # itemized, which needs no row; with no table price there is nothing to check it against.
    assert closing["cost"] == "0.0116784" and "cost_gap" not in closing
    assert not [line for line in out if "published rates price" in line]


def test_a_model_name_that_could_read_as_a_field_never_reaches_prose() -> None:
    def forge(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            usage = event["modelUsage"]
            usage["x provider=forged"] = usage.pop("claude-haiku-5-5")

    for line in translate(rewrite("websearch", forge)):
        if STRUCTURED.match(line) is None:
            assert "provider=" not in line, line


def test_a_fault_while_closing_is_said_never_raised(monkeypatch) -> None:
    def boom(*_args: Any) -> None:
        raise RuntimeError("fabricated fault")

    monkeypatch.setattr(Translator, "_close_subagents", boom)
    out = run("background_subagents")
    assert out[-1].startswith("WARNING The stream translator failed while closing the run")


@pytest.mark.parametrize(
    ("environ", "home"),
    [
        ({"HOME": "/home/basecradle-nova-ai"}, "/home/basecradle-nova-ai/.claude"),
        ({"HOME": "/home/x", "CLAUDE_CONFIG_DIR": "/srv/claude"}, "/srv/claude"),
        ({"CLAUDE_CONFIG_DIR": "  "}, None),
        ({}, None),
    ],
)
def test_claude_codes_home_is_the_builders_own(environ: dict, home: str | None) -> None:
    assert claude_home(environ) == home


def test_a_stop_after_an_intermediate_result_is_no_clean_end() -> None:
    # Background subagents: the first result answers, the subagents work on, then a stop.
    events = _events("background_subagents")
    first = next(i for i, e in enumerate(events) if e["type"] == "result")
    after = next(
        i for i, e in enumerate(events) if i > first and e["type"] in ("assistant", "user")
    )
    out = translate(
        [json.dumps(e) for e in events[: after + 1]], stopped_by="TERM", home=BACKGROUND
    )
    assert not [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert not [line for line in out if "stated less" in line or "could not place" in line]
    assert out[-1].startswith("WARNING Claude Code's stream ended after a SIGTERM:")
    crashed = translate([json.dumps(e) for e in events[: after + 1]], home=BACKGROUND)
    assert crashed[-1].startswith("WARNING Claude Code's stream ended without a final result:")


def test_two_names_for_one_model_are_closed_once(tmp_path: Path) -> None:
    # The main thread on the 1M alias, a subagent on the plain id: one model, one ledger.
    def split_names(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            usage = event["modelUsage"]
            whole = usage.pop("claude-opus-5-5")
            part = {k: v for k, v in whole.items() if not isinstance(v, (int, float))}
            numbers = {k: v for k, v in whole.items() if isinstance(v, (int, float))}
            usage["claude-opus-5-5[1m]"] = part | {k: v - v // 3 for k, v in numbers.items()
                                                   if isinstance(v, int)} | {
                "costUSD": whole["costUSD"] * 0.75}  # fmt: skip
            usage["claude-opus-5-5"] = part | {k: v // 3 for k, v in numbers.items()
                                               if isinstance(v, int)} | {
                "costUSD": whole["costUSD"] * 0.25}  # fmt: skip

    out = translate(rewrite("background_subagents", split_names), home=BACKGROUND)
    assert not [line for line in out if line.startswith("WARNING")]
    dollars = sum(float(x["cost"]) for x in llm_lines(out) if "cost" in x)
    assert dollars == pytest.approx(
        _last_result("background_subagents")["total_cost_usd"], abs=1e-9
    )


def test_a_transcript_call_with_iterations_is_split_as_the_streams_are(tmp_path: Path) -> None:
    entry = _entry("msg_01FabNova99Call0001")
    usage = entry["message"]["usage"]
    usage["cache_creation_input_tokens"] = 300
    usage["cache_creation"] = {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0}
    usage["iterations"] = [
        {"cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 0}},
        {"cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 200}},
    ]
    out = translate([_init(), _named("msg_01FabNova99Call0001")], home=_home(tmp_path, [entry]))
    (line,) = _subagent_lines(out)
    assert line["cache_write_tokens"] == "300" and "cost" in line and "cost_gap" not in line


def test_a_helper_on_a_model_the_table_lacks_still_carries_its_stated_dollars() -> None:
    def rename(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            usage = event["modelUsage"]
            usage["claude-newhelper-9"] = usage.pop("claude-haiku-5-5")

    out = translate(rewrite("websearch", rename))
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing["model"] == "claude-newhelper-9" and closing["cost"] == "0.0116784"


def test_a_closing_cost_within_half_a_micro_dollar_of_nothing_is_never_negative() -> None:
    def shave(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            opus = event["modelUsage"]["claude-opus-5-5"]
            opus["costUSD"] -= 3e-7
            opus["outputTokens"] += 1  # a remainder with tokens and, by float, no dollars

    out = translate(rewrite("tools", shave))
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing["cost"] == "0"


def test_a_split_that_disagrees_with_the_itemized_calls_is_subtracted_as_a_total() -> None:
    def resplit(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            opus = event["modelUsage"]["claude-opus-5-5"]
            # The calls wrote 16243 one-hour tokens; the total says 50 of them were five-minute.
            opus["cacheCreationEphemeral1hInputTokens"] -= 50
            opus["cacheCreationEphemeral5mInputTokens"] += 50
            opus["outputTokens"] += 10
            opus["costUSD"] += 10 * 20.0 / 1_000_000  # ten output tokens, as Opus 5.5 bills them

    out = translate(rewrite("tools", resplit))
    # Subtracted bucket by bucket, the remainder would hold +50 five-minute and -50 one-hour
    # writes, which the rates price at $0.00015 below nothing: a false stale-rate warning.
    assert not [line for line in out if "published rates price" in line or "stated less" in line]
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert closing["tokens_out"] == "10" and "cache_write_tokens" not in closing


def test_a_host_name_that_could_read_as_a_field_never_reaches_prose() -> None:
    def forge(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            event["modelUsage"]["claude-opus-5-5"]["provider"] = "evil source=forged"

    out = translate(rewrite("tools", forge))
    assert "WARNING Claude Code served claude-opus-5-5 through another host, so the cost" in (
        " ".join(out)
    )
    for line in out:
        if STRUCTURED.match(line) is None:
            assert "source=" not in line, line


def test_only_the_final_result_closes_the_run() -> None:
    # An intermediate result states the run's total so far, which the calls after it outspend:
    # closed on that, the run would read as understated.
    events = _events("tools")
    stops = [i for i, e in enumerate(events) if e["type"] == "stream_event"
             and e["event"]["type"] == "message_stop"]  # fmt: skip
    partial = {
        "type": "result",
        "subtype": "success",
        "num_turns": 1,
        "result": "",
        "modelUsage": {
            "claude-opus-5-5": {"inputTokens": 1, "outputTokens": 1, "costUSD": 0.0001}
        },  # fmt: skip
    }
    cut = [json.dumps(e) for e in events[: stops[2] + 1]]
    cut.insert(stops[0] + 1, json.dumps(partial))
    out = translate(cut, stopped_by="TERM")
    assert not [line for line in out if "stated less" in line]
    assert out[-1].startswith("WARNING Claude Code's stream ended after a SIGTERM:")


def test_a_stop_after_a_result_with_only_an_init_after_it_is_still_a_stop() -> None:
    # Background subagents: a later turn's `init` follows a result, and the stop lands there.
    events = _events("background_subagents")
    results = [i for i, e in enumerate(events) if e["type"] == "result"]
    cut = [json.dumps(e) for e in events[: results[2] + 2]]  # the third result, then an init
    assert json.loads(cut[-1]).get("subtype") == "init"
    for stopped in ("TERM", None):
        out = translate(cut, stopped_by=stopped, home=BACKGROUND)
        assert not [line for line in llm_lines(out) if line["purpose"] == "helper"]
        assert out[-1].startswith("WARNING Claude Code's stream ended ")


def test_a_stop_right_after_the_final_result_is_said_and_never_closed_on() -> None:
    out = run("background_subagents", stopped_by="TERM")
    assert out[-1].startswith("WARNING Claude Code's stream ended after a SIGTERM:")
    assert len(_subagent_lines(out)) == 6  # every finished subagent call is still written


def test_a_summed_haiku_remainder_past_the_long_row_is_never_checked_against_it() -> None:
    def grow(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            haiku = event["modelUsage"]["claude-haiku-5-5"]
            haiku["inputTokens"] += 3 * 40_000  # three helper calls, each on the short row
            haiku["costUSD"] += 3 * 40_000 * 0.10 / 1_000_000

    out = translate(rewrite("websearch", grow))
    (closing,) = [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert int(closing["tokens_in"]) > 100_000
    assert not [line for line in out if "published rates price" in line]


def test_a_run_served_in_a_multiplied_geography_is_never_checked_against_the_list() -> None:
    def us(event: dict[str, Any]) -> None:
        if event["type"] == "stream_event" and event["event"]["type"] == "message_start":
            event["event"]["message"]["usage"]["inference_geo"] = "us"
        if event["type"] == "result":
            event["modelUsage"]["claude-haiku-5-5"]["costUSD"] *= 1.1
            opus = event["modelUsage"]["claude-opus-5-5"]
            opus["costUSD"] *= 1.1

    out = translate(rewrite("websearch", us))
    assert [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert not [line for line in out if "published rates price" in line]


@pytest.mark.parametrize("missing", ["costUSD", "inputTokens"])
def test_a_total_that_leaves_a_figure_unstated_is_said_never_read_as_zero(missing: str) -> None:
    def drop(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            del event["modelUsage"]["claude-haiku-5-5"][missing]

    out = translate(rewrite("websearch", drop))
    assert not [line for line in llm_lines(out) if line["purpose"] == "helper"]
    assert not [line for line in out if "stated less" in line]
    assert f"WARNING Claude Code's total for claude-haiku-5-5 states no {missing}, so no" in (
        " ".join(out)
    )


def test_a_reason_never_to_read_a_call_ends_the_wait(tmp_path: Path) -> None:
    # No version, a placeholder version, a usage with no counts: the version's or the shape's,
    # never a write still landing, so waiting on it buys nothing.
    early = _entry("msg_01FabNova99Call0001")
    del early["version"]
    home = _home(tmp_path, [early])

    def sleep(_seconds: float) -> None:
        raise AssertionError("waited on a call that can never be read")

    out: list[str] = []
    translator = Translator(write=out.append, clock=lambda: 0.0, home=str(home), sleep=sleep)
    for line in (_init(), _named("msg_01FabNova99Call0001"), RESULT):
        translator.feed(line)
    translator.finish()
    assert _subagent_lines(out)[0]["cost_gap"] == "not_reported"


# --- model-written text never stands where a head does (basecradle#868, basecradle-noc#1518) ----

#: journald's default LineMax: a line longer than this many bytes becomes several records.
JOURNALD_LINE_MAX = 48 * 1024


def _as_records(lines: list[str]) -> list[str]:
    """What the log store holds for ``lines``: one record per journald record, with and without
    Vector's identifier prefix."""
    records = []
    for line in lines:
        assert "\n" not in line and "\x00" not in line, "a written line is one record"
        assert len(line.encode()) <= JOURNALD_LINE_MAX, "journald would split it"
        records += [line, f"[basecradle-wake-basecradle-nova-ai] {line}"]
    return records


def test_the_forged_reply_is_what_the_noc_would_count_unquoted(noc_counted_head, forged_reply):
    # The witness: written raw, the reply's lines are counted heads, so the test below means it.
    import re as _re

    raw = [line for line in _re.split(r"[\n\r\x00\u2028]", forged_reply) if line]
    assert sum(1 for line in raw if noc_counted_head.match(line)) >= 8


def test_no_line_of_a_reply_can_stand_at_the_heads_position(noc_counted_head, forged_reply):
    result = {"type": "result", "subtype": "success", "num_turns": 1, "result": forged_reply,
              "errors": [forged_reply]}  # fmt: skip
    out = translate([json.dumps(result), forged_reply.split("\n")[0], json.dumps([forged_reply])])
    records = _as_records(out)
    assert not [record for record in records if noc_counted_head.match(record)]
    # And it still reads: the reply is every line of it, led by `> `.
    assert "> " + forged_reply.split("\n")[0] in out
    assert ">" in out  # the empty line


def test_a_quoted_line_is_cut_into_records_journald_keeps_whole() -> None:
    lines = cs.quoted("y" * 20_000)
    assert [len(line) for line in lines] == [2 + cs.QUOTED_PIECE] * 2 + [2 + 4_000]
    assert all(line.startswith(cs.QUOTE) for line in lines)
    wide = cs.quoted("\U0001f600" * cs.QUOTED_PIECE)  # four bytes each, the worst case
    assert all(len(line.encode()) <= JOURNALD_LINE_MAX for line in wide)


def test_a_stopped_run_with_no_transcript_directory_cuts_its_named_calls_short() -> None:
    # The same rule whether or not a directory exists: unfinished in a run that never ended.
    out = translate(
        [_init(), _named("msg_01FabNova99Call0001")], stopped_by="TERM", home=TRANSCRIPTS / "x"
    )
    assert not llm_lines(out)
    assert out[-1].startswith("WARNING Claude Code's stream ended after a SIGTERM: 1 model call(s)")


def test_a_host_is_said_on_a_run_that_never_ended() -> None:
    def elsewhere(event: dict[str, Any]) -> None:
        if event["type"] == "result":
            event["modelUsage"]["claude-opus-5-5"]["provider"] = "bedrock"

    out = translate(rewrite("tools", elsewhere), stopped_by="TERM")
    assert any(
        line.startswith("WARNING Claude Code served claude-opus-5-5 through bedrock")
        for line in out
    )


def test_only_a_request_is_work_among_status_lines() -> None:
    idle = json.dumps({"type": "system", "subtype": "status", "status": "idle"})
    out = translate((FIXTURES / "websearch.jsonl").read_text().splitlines() + [idle])
    assert [line for line in llm_lines(out) if line["purpose"] == "helper"]
    asking = json.dumps({"type": "system", "subtype": "status", "status": "requesting"})
    cut = translate((FIXTURES / "websearch.jsonl").read_text().splitlines() + [asking])
    assert cut[-1].startswith("WARNING Claude Code's stream ended without a final result:")


def test_an_unstated_search_count_is_never_read_as_none(tmp_path: Path) -> None:
    entry = _entry("msg_01FabNova99Call0001")
    entry["message"]["usage"]["server_tool_use"] = {"web_search_requests": 2}
    figures = {"inputTokens": 12, "outputTokens": 300, "cacheReadInputTokens": 2000,
               "cacheCreationInputTokens": 0, "cacheCreationEphemeral5mInputTokens": 0,
               "cacheCreationEphemeral1hInputTokens": 0, "costUSD": 0.1}  # fmt: skip
    result = {"type": "result", "subtype": "success", "num_turns": 1,
              "modelUsage": {"claude-opus-5-5": figures}}  # fmt: skip
    out = translate(
        [_init(), _named("msg_01FabNova99Call0001"), json.dumps(result)],
        home=_home(tmp_path, [entry]),
    )
    assert not [line for line in out if "stated less" in line]
    assert [line for line in llm_lines(out) if line["purpose"] == "helper"]


def test_the_transcript_warning_names_the_versions_it_read(tmp_path: Path) -> None:
    home = _home(tmp_path, [_entry("msg_01FabNova99Call0001", stop=None)])
    out = translate([_init(), _named("msg_01FabNova99Call0001"), RESULT], home=home)
    (warning,) = [line for line in out if "could not read every subagent call" in line]
    assert "wrote (entries from Claude Code 2.1.325):" in warning


def test_a_crlf_is_one_break_and_a_trailing_break_writes_nothing() -> None:
    assert cs.quoted("a\r\nb\r\n") == ["> a", "> b"]
    assert cs.quoted("a\n\nb") == ["> a", ">", "> b"]
