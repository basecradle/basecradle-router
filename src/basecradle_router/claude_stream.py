"""Claude Code's own structured output in, the telemetry contract's lines out (router#481).

A Claude Code builder's wake runs ``claude -p <trigger> --output-format stream-json --verbose
--include-partial-messages``, and ``wake-runner`` pipes that stream through this translator into
the journal stream it already opened for the builder (``basecradle-wake-<os_user>``). So a
builder's model calls and tool uses reach the fleet's record the way every harness agent's do:
written by the builder's own wake, into a journald stream root opened and named for it, never
through a port any local account can write to under any name (basecradle/basecradle#789).

**The interface is "a runtime's own structured output in, contract lines out."** Claude Code is
the first runtime; a second runtime is a second module like this one, never a port.
The lines are the contract's (basecradle-harness ``docs/telemetry.md``), rendered by the router's
one renderer, :func:`basecradle_router.logfmt.log_fields`, and the final reply is written as prose
exactly where the reply went before, each line led by ``> ``.

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
  ``stream_event``\\ s, so the stream never states their final usage. Claude Code writes each
  subagent's transcript to the builder's own disk, ``<claude home>/projects/<project>/<session
  id>/subagents/agent-<id>.jsonl``, with each call's finished usage and the same ``req_…`` the
  stream names, so at the close of the run the translator reads those files and writes one real
  ``llm`` line per distinct API response (:func:`read_subagent_transcripts`). A response that is
  several entries (one per content block) is grouped by its message id and read from its finished
  entry, the one with a stop reason, never summed. In a run that ended, a call with no finished
  record still gets its line, with no token fields and ``cost_gap=not_reported``, and the gap is
  said in a WARNING.
- **Claude Code's helper calls** (the model a WebSearch runs on, say) never appear one by one,
  in the stream or on disk. So at the end, for each model, whatever Claude Code's own stated total
  for the run (the last ``result``'s ``modelUsage``) still exceeds what was itemized becomes one
  ``llm … purpose=helper`` line with the remainder's tokens and ``cost`` = the stated
  ``costUSD`` less the dollars itemized, so the wake's lines add up to the stated total by
  construction (the capital's ruling on #481). It carries no ``kind``, ``duration`` or
  ``generation_id``, as the contract writes a builder's auxiliary call, and a prose line beside it
  says it is a remainder, not one call. A stated total below what was itemized writes no line,
  only a WARNING, because something would be counted twice.
- **A run can write several ``result`` lines.** With background subagents, Claude Code answers
  once per turn they wake (``result_index`` 0, 1, …), each with the run's cumulative
  ``modelUsage``. Each reply is written as it arrives; the closing lines are computed once, at the
  end of the stream, from the last, and the turn count is the sum of every ``result``'s own.
- **``tool``, one line per tool use**, joining a ``tool_use`` block to its ``tool_result``. The
  stream's only verdict is ``is_error``, so a non-zero shell exit is ``outcome=error`` (the contract
  calls one ``ok``; the stream states no exit code but in text, and a verdict is never read off
  text). A refusal is structural (``permission_decision.decision=reject``): ``reason=withheld``.
- **A call cut short** (a stop or a crash before ``message_stop``, or a subagent call with no
  finished record in a run that did not end) has no word in the contract, so it gets no ``llm``
  line. A run that did not end on its final ``result`` (a stop, which can land between a
  background run's results, or a crash) has no stated total, so it gets no closing line either;
  every finished subagent call its transcripts hold is still written, and a WARNING says what is
  missing.

**These lines are a builder's record** (basecradle/basecradle#789, basecradle-router#486). Each
fact is written once, under the contract's bare head: a call's ``llm`` line carries the price of
its tokens, and a server-side tool's fee rides its own ``tool … executor=server`` line beside it,
so a wake's ``llm`` and ``tool`` dollars together are Claude Code's stated total.

**Model-written text never stands where a head does** (basecradle/basecradle#868, routed in
basecradle-noc#1518). The reply, the run's ``errors`` and whatever ``claude`` printed to stdout are
text a model or a tool wrote, which a prompt injection can steer: a reply line reading ``INFO llm
provider=… cost=…`` would be byte for byte a real line at the head's position, where the NOC
counts calls and dollars. So every line of such text is written as the contract's prose with a
lead no head can stand behind, ``> `` (:func:`quoted`): a record that begins with it never matches
the NOC's ``HEAD_POSITION``, which needs a severity at the record's start. It is cut at every
boundary journald starts a new record at (a newline, a NUL) and into pieces well under journald's
48K ``LineMax``, past which journald would split the line into a record with no lead.
``wake-runner``'s ``jq`` fallback writes the reply by the same rule.

**No line carries a credential-named assignment** (basecradle-router#491). A refused or failed
command's text is quoted in its tool line's ``error=``, and almost every builder command opens by
minting a token (``export GH_TOKEN="$(gh-app-token …)"``), so an error, a reply or a stdout line
can carry the shape the NOC's log-store canary pages on whatever follows the ``=``. Every value
the renderer writes, every bounded text, and every quoted line passes
:func:`basecradle_router.logfmt.redact_credentials` first: the name stays and the value becomes
``'<a redacted value>'``, by the canary's own definition of a credential's name.

**No wake lines.** A builder has no wake in the contract's sense, and the router's own
``event=wake_start``, ``event=wake_end`` and ``stage=wake outcome=`` lines, observed by root from
the exit status, are already its record (the capital's ruling on #481). The run's turn count and
the reason it ended go on a prose line at the close.

**Nothing here may cost the wake or its reply.** The translator never raises out of :meth:`feed`
or :meth:`finish` (a line it cannot read is counted and passed over), and it writes each reply
the moment its ``result`` arrives. The transcripts are read as data, by the builder, from the
builder's own home: nothing in them is ever loaded as code, and only their numbers and their
inert ids reach a line. The launcher (``deploy/bin/claude-stream-translate``) keeps reading
through a stop's SIGTERM until the stream ends, and ``wake-runner`` drains whatever is left should
the translator die anyway.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from basecradle_router import anthropic_rates as rates
from basecradle_router.logfmt import bounded, log_fields, redact_credentials

#: The vendor that bills every call a Claude Code builder makes.
PROVIDER = "anthropic"

#: The contract's heads this translator writes (basecradle-harness ``docs/telemetry.md``).
LLM_HEAD = "llm"
UNPRICED_HEAD = "cost unpriced"
TOOL_HEAD = "tool"

#: The Claude Code versions whose stream shapes this translator reads: 2.1.325, from the streams
#: captured for #481, and 2.1.286, the box's, whose one difference (a stream event names its
#: message only on ``message_start``) was read off its binary. Named in the WARNING a stream that
#: does not fit writes, so a reader knows which shapes were expected.
KNOWN_VERSIONS = ("2.1.286", "2.1.325")

#: A Claude Code version, as ``system/init`` states it, when it is inert enough to name in a line.
_VERSION = re.compile(r"[0-9][0-9A-Za-z.+-]{0,31}")

#: The model id Claude Code gives the ``assistant`` line it writes when a call failed.
SYNTHETIC_MODEL = "<synthetic>"

#: An id the line may carry as ``generation_id``: an inert charset, so no id can carry a field.
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")

#: A model id as it may appear inside a prose line: a field-like token (``provider=``) never can.
_PROSE_MODEL = re.compile(r"[A-Za-z0-9._\[\]-]{1,64}")

#: A session id as ``system/init`` states it, the one name the transcript directory is found by.
_SESSION = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: The Claude Code versions whose subagent transcripts this translator has read: 2.1.325, from
#: the runs captured on the laptop for #481 (2026-11-16 UTC): background and foreground subagents, a
#: run beside a helper call, and a run stopped mid-subagent.
TRANSCRIPT_VERSIONS = ("2.1.325",)

#: Before this Claude Code version a subagent's transcript recorded placeholder usage (fixed in
#: 2.1.307; basecradle/basecradle#789, the capital's research of 2026-11-16 UTC), so an entry an
#: older version wrote is never read for its numbers.
TRANSCRIPT_USAGE_SINCE = (2, 1, 307)

#: Seconds the close-out waits, once the stream has ended, for every subagent call the stream
#: named to have its finished record on disk, re-reading every :data:`TRANSCRIPT_POLL` seconds.
#: By the end of the stream ``claude`` has exited and written its files, so this is a bound on a
#: write still landing, never a wait the wake pays: the wake ended when ``claude`` did.
TRANSCRIPT_WAIT = 2.0
TRANSCRIPT_POLL = 0.25

#: The lead on every line of model-written text (see the module docstring): no contract head can
#: stand behind it, because a record that begins with it has no severity at its start.
QUOTE = "> "

#: The longest piece of one quoted line, in characters: at four UTF-8 bytes each at most, well
#: under the 48K bytes past which journald splits a line into a second record with no lead.
QUOTED_PIECE = 8000

#: Every place journald, or Python reading a line, starts a new record or line: a CRLF is one.
#: ``wake-runner``'s ``jq`` fallback splits by this pattern, spelled for jq (pinned by a test).
RECORD_BREAKS = "\r\n|[\n\r\x00\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]"
_RECORD_BREAKS = re.compile(RECORD_BREAKS)

#: Half a micro-dollar: two dollar figures closer than this are the same figure.
_SAME_DOLLARS = 5e-7

Write = Callable[[str], None]
Clock = Callable[[], float]
Sleep = Callable[[float], None]


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


#: Claude Code's switches that send every call to a cloud provider's host, whose price is its own.
CLOUD_SWITCHES = {
    "CLAUDE_CODE_USE_BEDROCK": "bedrock",
    "CLAUDE_CODE_USE_VERTEX": "vertex",
    "CLAUDE_CODE_USE_FOUNDRY": "foundry",
}


def aimed_at(environ: Mapping[str, str]) -> str | None:
    """Where Claude Code sends its calls, as far as its environment says; ``None`` is Anthropic.

    A cloud provider's switch wins, as it does in Claude Code; its name is no Anthropic host, so
    the rates price nothing there.
    """
    for switch, provider in CLOUD_SWITCHES.items():
        if environ.get(switch, "").strip().lower() not in ("", "0", "false"):
            return provider
    return environ.get("ANTHROPIC_BASE_URL") or None


def quoted(text: str) -> list[str]:
    """``text`` as prose lines that can never stand at a head's position: each led by :data:`QUOTE`.

    Cut at every record break (an empty line becomes a bare ``>``) and into pieces of at most
    :data:`QUOTED_PIECE` characters, each piece led again, so no record journald writes for it
    begins with anything but the lead. A credential-named assignment's value is redacted first,
    on the whole text, so a quoted value that spans lines goes whole (#491).
    """
    lines = []
    text = redact_credentials(text)
    for line in _RECORD_BREAKS.split(re.sub(f"(?:{RECORD_BREAKS})\\Z", "", text)):
        if not line:
            lines.append(QUOTE.rstrip())
        for start in range(0, len(line), QUOTED_PIECE):
            lines.append(QUOTE + line[start : start + QUOTED_PIECE])
    return lines


def claude_home(environ: Mapping[str, str]) -> str | None:
    """Where Claude Code keeps this account's state: ``CLAUDE_CONFIG_DIR``, else ``~/.claude``.

    The translator runs as the builder, with the builder's environment, so this is the builder's
    own home, the one its ``claude`` wrote the transcripts into.
    """
    configured = environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if configured:
        return configured
    home = environ.get("HOME", "").strip()
    return os.path.join(home, ".claude") if home else None


def _version(value: Any) -> tuple[int, ...] | None:
    """An inert Claude Code version's numbers (``2.1.325`` → ``(2, 1, 325)``), or ``None``."""
    if not (isinstance(value, str) and _VERSION.fullmatch(value)):
        return None
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", value)
    return tuple(int(part) for part in match.groups()) if match else None


@dataclass(slots=True)
class SubagentCall:
    """One subagent API response as its transcript records it.

    ``usage`` is the finished entry's usage (the entry carrying a stop reason, with token counts),
    or ``None`` when no entry of the response is finished, or when its numbers are not to be read;
    ``unread`` then says why, once per call.
    """

    model: str
    request_id: str | None
    usage: Mapping[str, Any] | None = None
    unread: str | None = None


@dataclass(slots=True)
class Transcripts:
    """What a session's subagent transcripts hold, and what in them could not be read."""

    #: Whether the session's ``subagents`` directory was found at all.
    found: bool = False
    #: Every subagent response, by message id, in the order the files first name it.
    calls: dict[str, SubagentCall] = field(default_factory=dict)
    #: What could not be read, by shape, never silent (said in :meth:`Translator.finish`).
    problems: Counter[str] = field(default_factory=Counter)
    #: The Claude Code versions that wrote the entries read.
    versions: set[str] = field(default_factory=set)


def read_subagent_transcripts(home: str, session: str) -> Transcripts:
    """Every subagent call a session's transcripts hold: ``<home>/projects/*/<session>/subagents``.

    The project directory's name is Claude Code's own encoding of the run's working directory, so
    it is found by the session id inside it, never re-derived: the session id is a UUID, unique to
    the run, and a name that is not one is never looked up. Only ``agent-*.jsonl`` files there are
    read, line by line, as data. A line that is not an ``assistant`` entry is passed over; one that
    is, and whose shape is not what this reads, is counted.
    """
    found = Transcripts()
    if not _SESSION.fullmatch(session):
        found.problems["session id(s) that are not a UUID"] += 1
        return found
    projects = os.path.join(home, "projects")
    try:
        names = sorted(entry.name for entry in os.scandir(projects) if entry.is_dir())
    except OSError:
        return found
    directories = [
        path
        for name in names
        if os.path.isdir(path := os.path.join(projects, name, session, "subagents"))
    ]
    if not directories:
        return found
    found.found = True
    if len(directories) > 1:
        found.problems["session directories under more than one project"] += 1
        return found
    try:
        files = sorted(
            entry.path
            for entry in os.scandir(directories[0])
            if entry.name.startswith("agent-") and entry.name.endswith(".jsonl") and entry.is_file()
        )
    except OSError:
        found.problems["subagents directories that could not be listed"] += 1
        return found
    for path in files:
        try:
            with open(path, "rb") as fh:
                for raw in fh:
                    if b'"assistant"' in raw:
                        _read_entry(raw, found)
        except OSError:
            found.problems["transcript files that could not be read"] += 1
    return found


def _read_entry(raw: bytes, found: Transcripts) -> None:
    try:
        entry = json.loads(raw)
    except (ValueError, RecursionError):
        found.problems["transcript lines that are not JSON"] += 1
        return
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return
    message = entry.get("message")
    if not isinstance(message, Mapping):
        found.problems["assistant entries with no message"] += 1
        return
    message_id, model = message.get("id"), message.get("model")
    if model == SYNTHETIC_MODEL:
        return  # a failed call: the stream's own `assistant` line already wrote it
    if not (isinstance(message_id, str) and message_id and isinstance(model, str) and model):
        found.problems["assistant entries that name no message or model"] += 1
        return
    if entry.get("isSidechain") is False:
        # Marked as no subagent's: a parent's history carried into a fork, say. Never billed here.
        found.problems["entries not marked as a subagent's"] += 1
        return
    call = found.calls.setdefault(
        message_id, SubagentCall(model=model, request_id=_safe_id(entry.get("requestId")))
    )
    call.request_id = call.request_id or _safe_id(entry.get("requestId"))
    version = entry.get("version")
    numbers = _version(version)
    if numbers is None:
        # A version is what says the numbers are real (placeholders before 2.1.307), so an entry
        # that states none is never read for them.
        _unread(call, "subagent call(s) whose transcript states no Claude Code version")
        return
    found.versions.add(version)
    if numbers < TRANSCRIPT_USAGE_SINCE:
        _unread(call, f"subagent call(s) from Claude Code {version}, whose usage is a placeholder")
        return
    usage = message.get("usage")
    if isinstance(message.get("stop_reason"), str) and isinstance(usage, Mapping):
        if rates.usage_of(usage) is None:
            _unread(call, "subagent call(s) whose finished usage states no token counts")
            return
        call.usage = usage  # the last finished entry of the response: never summed


def _unread(call: SubagentCall, reason: str) -> None:
    """Why ``call``'s numbers are not read, unless a finished, readable entry already gave them."""
    if call.usage is None:
        call.unread = reason


#: The ``modelUsage`` fields a closing line subtracts from, summed per model by :func:`_by_model`.
_SUMMED = (
    "inputTokens",
    "outputTokens",
    "cacheReadInputTokens",
    "cacheCreationInputTokens",
    "cacheCreationEphemeral5mInputTokens",
    "cacheCreationEphemeral1hInputTokens",
    "webSearchRequests",
    "thinkingTokens",
)


def _by_model(billed: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Claude Code's ``modelUsage``, one entry per model as the lines name it, never per alias.

    Claude Code may state one model under two names (``claude-opus-5-5[1m]`` for the main
    thread, ``claude-opus-5-5`` for a subagent), and the calls of both are on one ledger, so their
    figures are summed before anything is subtracted. A count either name leaves unstated stays
    unstated; a cost either leaves unstated leaves the sum unstated. ``provider`` is kept when
    any name states a host other than Anthropic's own.
    """
    models: dict[str, dict[str, Any]] = {}
    for model, figures in billed.items():
        if not (isinstance(model, str) and isinstance(figures, Mapping)):
            continue
        named = figures.get("canonicalModel")
        key = rates.canonical(named if isinstance(named, str) and named else model)
        cost = figures.get("costUSD")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost):
            cost = None
        if key not in models:
            models[key] = {name: rates.count(figures.get(name)) for name in _SUMMED}
            models[key]["costUSD"] = cost
            models[key]["provider"] = figures.get("provider")
            continue
        into = models[key]
        for name in _SUMMED:
            more = rates.count(figures.get(name))
            into[name] = None if into[name] is None or more is None else into[name] + more
        into["costUSD"] = (
            None if into["costUSD"] is None or cost is None else into["costUSD"] + cost
        )
        served = figures.get("provider")
        if isinstance(served, str) and served not in ("", "firstParty"):
            into["provider"] = served
    return models


def money(value: float) -> str:
    """Fixed-point US dollars, at most eight decimals, never an exponent."""
    text = f"{round(value, 8):.8f}".rstrip("0").rstrip(".")
    return text or "0"


def seconds(value: float) -> str:
    """A length of time in the contract's unit: seconds, two decimals, a trailing ``s``."""
    return f"{max(value, 0.0):.2f}s"


def _mapping(value: Any) -> Mapping[str, Any]:
    """``value`` if it is a mapping, else an empty one: a field of the wrong shape says nothing."""
    return value if isinstance(value, Mapping) else {}


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
    usage["cache_creation"] = start.get("cache_creation")
    return _iteration_writes(usage)


def _iteration_writes(usage: Mapping[str, Any]) -> dict[str, Any]:
    """``usage`` with its cache-write breakdown summed over its ``iterations``, when it has them.

    A call that ran more than one iteration (a server-side tool loop) states its written tokens
    in total, and their 5-minute/1-hour split only per iteration, so the breakdown is their sum
    and the written tokens stay split the way they were billed. A stream's call and a
    transcript's alike.
    """
    out = dict(usage)
    iterations = usage.get("iterations")
    if not (isinstance(iterations, list) and iterations):
        return out
    five = hour = 0
    for item in iterations:
        breakdown = item.get("cache_creation") if isinstance(item, Mapping) else None
        if not isinstance(breakdown, Mapping):
            return out
        five += rates.count(breakdown.get("ephemeral_5m_input_tokens")) or 0
        hour += rates.count(breakdown.get("ephemeral_1h_input_tokens")) or 0
    out["cache_creation"] = {"ephemeral_5m_input_tokens": five, "ephemeral_1h_input_tokens": hour}
    return out


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
    """What the lines itemized for one model: its calls' tokens and the dollars priced on them.

    The tokens are kept in the buckets Claude Code's ``modelUsage`` states, so the closing line's
    remainder is a subtraction bucket by bucket.
    """

    uncached: int = 0
    read: int = 0
    write_5m: int = 0
    write_1h: int = 0
    output: int = 0
    searches: int = 0
    reasoning: int = 0
    #: Whether some itemized call stated no thinking count, so no reasoning remainder is known.
    reasoning_unknown: bool = False
    #: Whether some itemized call wrote to the cache with no 5-minute/1-hour split, so its write
    #: sits in ``write_5m`` whatever it was, and only the writes' total can be subtracted.
    unsplit: bool = False
    cost: float = 0.0
    #: Itemized calls, figures or not.
    calls: int = 0
    #: Itemized calls this table could not price, so ``cost`` is short by theirs.
    unpriced: int = 0
    #: Calls written with no figures (no final usage, or no finished transcript record), so the
    #: tokens are short by theirs, and the closing line's remainder holds them.
    unitemized: int = 0

    @property
    def prompt(self) -> int:
        return self.uncached + self.read + self.write_5m + self.write_1h

    def add(self, usage: rates.Usage, thinking: int | None) -> None:
        self.uncached += usage.uncached
        self.read += usage.read
        self.write_5m += usage.write_5m
        self.write_1h += usage.write_1h
        self.output += usage.output
        self.unsplit = self.unsplit or not usage.split_known
        if thinking is None:
            self.reasoning_unknown = True
        else:
            self.reasoning += thinking


@dataclass(slots=True)
class Translator:
    """One builder wake's stream, translated line by line. Feed it each line; finish it at EOF.

    ``write`` receives each output line without its newline. ``clock`` is a monotonic clock in
    seconds, read once per line as it arrives. ``base_url`` is the host Claude Code was aimed at
    (``ANTHROPIC_BASE_URL``, or the cloud provider its switch names, see :func:`aimed_at`), which
    decides whether the published rates are its price. ``home`` is Claude Code's state directory
    for the builder (:func:`claude_home`), where its subagent transcripts are read at the close;
    ``sleep`` is what the close-out waits with.
    """

    write: Write
    clock: Clock
    base_url: str | None = None
    home: str | None = None
    sleep: Sleep = time.sleep

    model: str | None = None
    version: str | None = None
    session: str | None = None
    unreadable: int = 0
    #: Everything the stream wrote that the translator expected and could not place, by shape.
    #: Never silent: :meth:`finish` names each count, because a stream that changed shape under
    #: the translator otherwise looks exactly like a wake that made no model calls (#481).
    unplaced: Counter[str] = field(default_factory=Counter)
    #: What the subagent transcripts held that could not be read, by shape: said in one WARNING.
    unread_transcripts: Counter[str] = field(default_factory=Counter)
    _transcript_versions: set[str] = field(default_factory=set)
    result_seen: bool = False
    #: Whether the last thing the run did was answer: a ``result`` with no model or tool work
    #: after it. A run stopped after an intermediate ``result`` (background subagents still
    #: working) has one, and its total is not the run's.
    ended: bool = False
    _calls: dict[str, _Call] = field(default_factory=dict)
    _request_ids: dict[str, str] = field(default_factory=dict)
    _requested_at: float | None = None
    _retries: int = 0
    _no_response: bool = False
    _max_retries: int | None = None
    #: The subagent calls the stream named, by message id, in the order it named them.
    _subagent_calls: dict[str, SubagentCall] = field(default_factory=dict)
    #: Every main-thread call that opened, by message id: a transcript never bills one again.
    _main_ids: set[str] = field(default_factory=set)
    _tools: dict[str, _ToolUse] = field(default_factory=dict)
    _ledger: dict[str, _Ledger] = field(default_factory=dict)
    _gaps: set[tuple[str, str, str]] = field(default_factory=set)
    #: The last ``result``'s ``modelUsage``: the run's cumulative stated total.
    _billed: Mapping[str, Any] | None = None
    #: Subagent calls a stop or a crash cut short: unfinished, so, like a main call, no line.
    _cut_short: int = 0
    #: Whether a call reported an inference geography the list price is multiplied for.
    _geo_priced: bool = False
    _turns: int | None = None
    _ending: str | None = None

    @property
    def _priced(self) -> bool:
        """Whether any line this translator wrote carries a ``cost=``."""
        return any(ledger.cost for ledger in self._ledger.values())

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
            if text.lstrip().startswith(("{", "[")):
                # Stream JSON cut short (a stop or a crash mid-line): it can hold a tool's whole
                # result, which never reached the journal before, so it is counted, never written.
                self.unreadable += 1
            else:
                self._quote(text)
            return
        except RecursionError:  # nested past the parser's depth: JSON nobody can read here
            self.unreadable += 1
            return
        if not isinstance(event, dict):
            # Valid JSON but no event (a bare number, `null`, a list): plain stdout, as above.
            self._quote(text)
            return
        try:
            self._dispatch(event, now)
        except Exception:  # noqa: BLE001 — a translator fault must never cost the wake
            self.unreadable += 1

    def finish(self, *, stopped_by: str | None = None) -> None:
        """The stream has ended. Say what could not be read or placed, and how it ended.

        ``stopped_by`` is the signal the translator received while reading (``TERM`` from a stop),
        or ``None``. In order: the run's turn count; a line per subagent call, from its transcript;
        and, from the last ``result``, a closing line per model for what the stated total still
        exceeds. Whatever was in flight writes no contract line: a call cut short has no word in
        the contract, and the wake's own record is the router's ``stage=wake`` line. But a call or
        a tool use still open after a ``result``, a main-thread message that arrived with no stream
        events, or a transcript that could not be read, is something the translator did not
        understand, and that is said loudly. Never raises.
        """
        try:
            self._close(stopped_by)
        except Exception:  # noqa: BLE001 — a translator fault must never cost the wake
            self.write(
                "WARNING The stream translator failed while closing the run, so its subagent "
                "and closing lines may be missing."
            )

    def _close(self, stopped_by: str | None) -> None:
        if stopped_by:
            # A stop can land between a background run's results, after one that was not the
            # last; nothing in the stream tells the two apart, so a stopped run is never closed
            # on a total, and the stop is always said.
            self.ended = False
        if self.result_seen:
            count = str(self._turns) if self._turns is not None else "an unstated number of"
            why = f" ({self._ending})" if self._ending else ""
            self.write(f"INFO Claude Code ended after {count} turn(s){why}.")
        self._close_subagents()
        if isinstance(self._billed, Mapping):
            for key, figures in _by_model(self._billed).items():
                self._served_by(key, figures)
                if self.ended:
                    self._close_model(key, figures)
        wrote = f"Claude Code {self.version}" if self.version else "Claude Code"
        if self.unreadable:
            self.write(
                f"WARNING The stream translator could not read {self.unreadable} line(s) of "
                "Claude Code's stream; their model calls or tool uses have no line."
            )
        if self.ended:
            if self._calls:
                self.unplaced["model call(s) still open at the result"] += len(self._calls)
            if self._tools:
                self.unplaced["tool use(s) never answered by the result"] += len(self._tools)
        if self._request_ids:
            self.unplaced["main-thread message(s) that came with no stream events"] += len(
                self._request_ids
            )
        if self.unplaced:
            found = "; ".join(f"{count} {shape}" for shape, count in sorted(self.unplaced.items()))
            known = " and ".join(KNOWN_VERSIONS)
            self.write(
                f"WARNING The stream translator could not place what {wrote} wrote: {found}. The"
                " model calls and tool uses involved have no lines, so this wake's telemetry is"
                f" incomplete. The shapes it reads are Claude Code {known}'s."
            )
        if self.unread_transcripts:
            found = "; ".join(
                f"{count} {shape}" for shape, count in sorted(self.unread_transcripts.items())
            )
            known = " and ".join(TRANSCRIPT_VERSIONS)
            read = (
                " (entries from Claude Code " + ", ".join(sorted(self._transcript_versions)) + ")"
                if self._transcript_versions
                else ""
            )
            self.write(
                f"WARNING The stream translator could not read every subagent call from the "
                f"transcripts {wrote} wrote{read}: {found}. Those calls' lines carry no tokens or"
                " cost, and their dollars are in the closing line, if the run stated a total. The"
                f" transcript shape it reads is Claude Code {known}'s."
            )
        if self.ended:
            return
        how = (
            f"after a SIG{stopped_by}"
            if stopped_by
            else ("without a final result" if self.result_seen else "without a result")
        )
        self.write(
            f"WARNING Claude Code's stream ended {how}: "
            f"{len(self._calls) + self._cut_short} model call(s) and "
            f"{len(self._tools)} tool use(s) in flight have no line, and with no final result"
            " there is no stated total, so no closing line: a call neither the stream nor the"
            " transcripts itemize, such as a helper model's, has no line."
        )

    # --- dispatch ------------------------------------------------------------------------------

    #: The ``system`` subtypes that are work in progress: a ``result`` before one was not the end.
    _WORK = frozenset({"init", "api_retry", "task_started", "task_progress"})

    def _dispatch(self, event: dict[str, Any], now: float) -> None:
        kind = event.get("type")
        if kind in ("stream_event", "assistant", "user") or (
            kind == "system"
            and (
                event.get("subtype") in self._WORK
                or (event.get("subtype") == "status" and event.get("status") == "requesting")
            )
        ):
            self.ended = False
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
            else:
                self.unplaced["init line(s) that named no model"] += 1
            version = event.get("claude_code_version")
            if isinstance(version, str) and _VERSION.fullmatch(version):
                self.version = version
            session = event.get("session_id")
            if isinstance(session, str) and _SESSION.fullmatch(session):
                self.session = session
        elif subtype == "status" and event.get("status") == "requesting":
            self._requested_at = now
            self._retries = 0
            self._no_response = False
        elif subtype == "api_retry":
            self._retry(event)

    def _stream_event(self, event: Mapping[str, Any], now: float) -> None:
        inner = event.get("event")
        if not isinstance(inner, Mapping):
            self.unplaced["stream event(s) that carried no event"] += 1
            return
        kind = inner.get("type")
        # Some Claude Code versions (2.1.286) name the message on `message_start` alone, others
        # (2.1.325) on every stream event. An event that names none belongs to the one call that
        # is open, and only when exactly one is: with none or several open, it is counted as
        # unplaced, never guessed.
        message_id = event.get("api_message_id")
        if not message_id and kind != "message_start" and len(self._calls) == 1:
            message_id = next(iter(self._calls))
        if kind == "message_start":
            message = inner.get("message")
            message_id = message.get("id") if isinstance(message, Mapping) else None
            if not isinstance(message_id, str) or not message_id:
                self.unplaced["message_start(s) that named no message"] += 1
                return
            if self._calls:
                self.unplaced["message_start(s) while another call was still open"] += 1
            started = self._requested_at
            if started is None:
                ttft = rates.count(event.get("ttft_ms"))
                started = now - (ttft or 0) / 1000
            usage = message.get("usage")
            model = message.get("model")
            self._main_ids.add(message_id)
            self._calls[message_id] = _Call(
                model=model if isinstance(model, str) and model else (self.model or ""),
                started=started,
                start_usage=usage if isinstance(usage, Mapping) else {},
            )
            self._requested_at = None
        elif kind in ("message_delta", "message_stop") and not (
            isinstance(message_id, str) and message_id in self._calls
        ):
            self.unplaced[f"{kind}(s) that matched no open model call"] += 1
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
            # A subagent's call: its line is written at the close, from its transcript.
            model = message.get("model")
            self._subagent_calls[message_id] = SubagentCall(
                model=model if isinstance(model, str) and model else (self.model or ""),
                request_id=request_id,
            )
        elif self._subagent_calls[message_id].request_id is None:
            self._subagent_calls[message_id].request_id = request_id

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
                self.unplaced["tool result(s) that matched no tool use"] += 1
                continue
            self._tool_ended(use, block, decisions.get(block["tool_use_id"]), at, now)

    # --- model calls ---------------------------------------------------------------------------

    def _ledger_for(self, model: str) -> _Ledger:
        """One model's ledger, under the id Claude Code and the API both reduce it to."""
        return self._ledger.setdefault(rates.canonical(model), _Ledger())

    def _unpriced(self, model: str, charge: str, reason: str) -> None:
        """The contract's ``cost unpriced`` WARNING, once per wake per gap."""
        key = (model, charge, reason)
        if key in self._gaps:
            return
        self._gaps.add(key)
        self.write(
            f"WARNING {UNPRICED_HEAD} "
            + log_fields(provider=PROVIDER, model=model, charge=charge, reason=reason)
        )

    def _quote(self, text: str) -> None:
        """Write model-written ``text`` as :func:`quoted` prose, never where a head can stand."""
        for line in quoted(text):
            self.write(line)

    def _llm(self, level: str, **fields: object) -> None:
        self.write(f"{level} {LLM_HEAD} " + log_fields(provider=PROVIDER, **fields))

    def _call_ended(self, call: _Call, now: float) -> None:
        raw = (
            _final_usage(call.start_usage, call.delta_usage)
            if call.delta_usage is not None
            else None
        )
        self._billed_line(
            call.model,
            raw,
            call.request_id,
            duration=seconds(now - call.started),
            refused=call.stop_reason == "refusal",
        )

    def _billed_line(
        self,
        model: str,
        raw: Mapping[str, Any] | None,
        request_id: str | None,
        *,
        duration: str | None = None,
        refused: bool = False,
    ) -> None:
        """One call's ``llm`` line from its final usage, or with no figures when there is none.

        The one writer of a billed call's line, a main-thread call's from its stream events and a
        subagent's from its transcript, so the two can never be priced or shaped apart.
        """
        ledger = self._ledger_for(model)
        ledger.calls += 1
        usage = rates.usage_of(raw) if raw is not None else None
        if usage is None:
            # No final usage: the call is counted, and its tokens are not invented.
            ledger.unitemized += 1
            self._unpriced(model, "tokens", rates.NOT_REPORTED)
            self._llm(
                "INFO",
                purpose="main",
                model=model,
                duration=duration,
                cost_gap=rates.NOT_REPORTED,
                outcome="ok",
                generation_id=request_id,
            )
            return
        tokens: dict[str, object] = {}
        price: rates.Price | None = None
        if usage.prompt or usage.output:
            thinking = rates.count(
                _mapping(raw.get("output_tokens_details")).get("thinking_tokens")
            )
            tokens = {
                "tokens_in": usage.prompt,
                "tokens_out": usage.output,
                "tokens_total": usage.prompt + usage.output,
                "cached_tokens": usage.read if "cache_read_input_tokens" in raw else None,
                "cache_write_tokens": usage.written or None,
                "tokens_reasoning": thinking,
            }
            price = rates.call_price(
                model,
                usage,
                geo=raw.get("inference_geo"),
                tier=raw.get("service_tier"),
                speed=raw.get("speed"),
                base_url=self.base_url,
            )
            ledger.add(usage, thinking)
            if price.cost is not None:
                ledger.cost += price.cost
            else:
                ledger.unpriced += 1
                self._unpriced(model, "tokens", price.gap or rates.NOT_REPORTED)
        geo = raw.get("inference_geo")
        if isinstance(geo, str) and geo.strip().lower() not in ("", "global"):
            self._geo_priced = True
        self._llm(
            "WARNING" if refused else "INFO",
            purpose="main",
            residency=geo if isinstance(geo, str) and geo else None,
            model=model,
            duration=duration,
            **tokens,
            cost=money(price.cost) if price and price.cost is not None else None,
            cost_basis="computed" if price and price.cost is not None else None,
            cost_gap=price.gap if price else None,
            outcome="refused" if refused else "ok",
            generation_id=request_id,
        )
        self._server_tools(model, raw, request_id)

    def _server_tools(self, model: str, usage: Mapping[str, Any], request_id: str | None) -> None:
        """One ``tool executor=server`` line per server-side tool the call used, after its call.

        The per-search fee joins the model's ledger, because Claude Code's ``costUSD`` for the
        model includes it: left out, every wake that searched would read as a stale rate.
        """
        block = usage.get("server_tool_use")
        if not isinstance(block, Mapping):
            return
        searches = rates.count(block.get("web_search_requests")) or 0
        if searches:
            self._ledger_for(model).searches += searches
            price = rates.web_search_price(searches, base_url=self.base_url)
            if price.cost is None:
                self._unpriced(model, "search", price.gap or rates.NOT_REPORTED)
                self._ledger_for(model).unpriced += 1
            else:
                self._ledger_for(model).cost += price.cost
            self.write(
                f"INFO {TOOL_HEAD} "
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
        fetches = rates.count(block.get("web_fetch_requests")) or 0
        if fetches:
            # Web fetch carries "no additional charges beyond standard token costs".
            self.write(
                f"INFO {TOOL_HEAD} "
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
        attempt = rates.count(event.get("attempt"))
        max_retries = rates.count(event.get("max_retries"))
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
        if event.get("parent_tool_use_id") is not None:
            # A subagent's call failed: its own line, and the main thread's call state untouched.
            self._llm(
                "WARNING",
                purpose="main",
                model=self.model,
                outcome="failed",
                reason=fault_slug(event.get("api_error_status")),
                detail=bounded(_text(message.get("content"))) or None,
                generation_id=_safe_id(event.get("request_id")),
            )
            return
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
        # The call the failure ended, if it had begun streaming: it is accounted for by the line
        # above, so it is closed, and no later event can close it again as a success.
        self._calls.clear()

    def _read_transcripts(self) -> Transcripts:
        """The session's transcripts, re-read until every named call is finished or time is up.

        Claude Code writes a transcript as the run goes, so by the end of the stream every call
        is normally on disk; the bounded wait (:data:`TRANSCRIPT_WAIT`) covers a write still
        landing, a half-written line included. It waits only on a run that ended with its final
        ``result``, and only for calls the stream named: a stopped or crashed run's files are as
        final as they will be, and a run with no subagents reads once.
        """
        if self.home is None or self.session is None:
            return Transcripts()
        deadline = self.clock() + TRANSCRIPT_WAIT
        while True:
            found = read_subagent_transcripts(self.home, self.session)
            if (
                not self.ended
                or self.clock() >= deadline
                or not any(
                    self._still_writing(found.calls.get(message_id))
                    for message_id in self._subagent_calls
                )
            ):
                return found
            self.sleep(TRANSCRIPT_POLL)

    @staticmethod
    def _still_writing(call: SubagentCall | None) -> bool:
        """Whether a named call's finished record may yet land: none yet, and no reason it won't.

        A reason not to read its numbers (no version, a placeholder version, a usage with no
        counts) is the version's or the shape's, never a write still landing, so it ends the wait.
        """
        return call is None or (call.usage is None and call.unread is None)

    def _close_subagents(self) -> None:
        """One ``llm`` line per subagent call: from its transcript, or with no figures."""
        found = self._read_transcripts()
        self.unread_transcripts.update(found.problems)
        self._transcript_versions.update(found.versions)
        if self._subagent_calls and not found.found:
            where = (
                "no session id or home to look in"
                if self.home is None or self.session is None
                else "no transcript directory for the session"
            )
            self.unread_transcripts[f"subagent call(s) the stream named, with {where}"] += len(
                self._subagent_calls
            )
        for message_id, call in found.calls.items():
            if message_id in self._main_ids:
                self.unread_transcripts["transcript entries for a main-thread call"] += 1
                continue
            named = self._subagent_calls.pop(message_id, None)
            if call.usage is None and call.unread is None and not self.ended:
                self._cut_short += 1  # unfinished in a run that never ended: no word for it
                continue
            if call.usage is None:
                shape = call.unread or "subagent call(s) with no finished transcript record"
                self.unread_transcripts[shape] += 1
            self._billed_line(
                call.model,
                _iteration_writes(call.usage) if call.usage is not None else None,
                call.request_id or (named.request_id if named else None),
            )
        if self._subagent_calls and not self.ended:
            # Named, never finished on disk, in a run that never ended: cut short, so no line.
            self._cut_short += len(self._subagent_calls)
            self._subagent_calls.clear()
        if self._subagent_calls and found.found:
            shape = "subagent call(s) the stream named with no transcript record"
            self.unread_transcripts[shape] += len(self._subagent_calls)
        for call in self._subagent_calls.values():
            self._billed_line(call.model, None, call.request_id)
        self._subagent_calls.clear()

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
            f"INFO {TOOL_HEAD} "
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
                self.write(f"WARNING {TOOL_HEAD} " + log_fields(name=use.name, error=error))

    # --- the end -------------------------------------------------------------------------------

    def _result(self, event: Mapping[str, Any]) -> None:
        """A reply, written as it arrives, and the run's cumulative total, kept for the close.

        A run with background subagents writes one ``result`` per turn they wake, each with the
        run's ``modelUsage`` so far, so nothing that adds up the run is written here.
        """
        self.result_seen = True
        self.ended = True
        reply = event.get("result")
        if isinstance(reply, str) and reply:
            self._quote(reply)
        errors = event.get("errors")
        if isinstance(errors, list):
            for error in errors:
                if isinstance(error, str) and error:
                    self._quote(error)
        turns = rates.count(event.get("num_turns"))
        if turns is not None:
            self._turns = (self._turns or 0) + turns
        ending = event.get("terminal_reason") or event.get("subtype")
        if isinstance(ending, str) and ending:
            self._ending = bounded(ending, 40)
        billed = event.get("modelUsage")
        if isinstance(billed, Mapping):
            self._billed = billed

    def _served_by(self, model: str, figures: Mapping[str, Any]) -> None:
        """Say so when Claude Code reports a host whose price is not Anthropic's list price.

        The environment the translator can read names most such hosts (``ANTHROPIC_BASE_URL``,
        a cloud provider's switch), and a call there is priced nowhere. A host set some other way
        (in Claude Code's own settings, say) is only known here, at the end, from the provider
        Claude Code names, so the calls already written were priced as Anthropic's: say so.
        """
        served = figures.get("provider")
        if not isinstance(served, str) or served in ("", "firstParty") or not self._priced:
            return
        shown = model if _PROSE_MODEL.fullmatch(model) else "a model"
        host = served if _PROSE_MODEL.fullmatch(served) else "another host"
        self.write(
            f"WARNING Claude Code served {shown} through {host}, so the cost "
            "on its calls above is Anthropic's list price, not what that host charges."
        )

    def _close_model(self, model: str, figures: Mapping[str, Any]) -> None:
        """The closing line for one model: what Claude Code's stated total still exceeds.

        The remainder is taken bucket by bucket (uncached, cache read, 5-minute and 1-hour cache
        writes, output, searches), and its ``cost`` is the stated ``costUSD`` less the dollars
        itemized, so the wake's lines add up to the stated total by construction. The table's
        own price for the remainder is the check on that difference: when they disagree, a rate
        may be stale, and that is said, never hidden in the difference.
        """
        stated = figures.get("costUSD")
        key = model  # already canonical: `_by_model` keys each model by the id it is priced under
        shown = key if _PROSE_MODEL.fullmatch(key) else "a model"
        unstated = [
            name
            for name in ("costUSD", "inputTokens", "outputTokens", "cacheReadInputTokens")
            if figures.get(name) is None
        ]
        if unstated:
            self.write(
                f"WARNING Claude Code's total for {shown} states no {', '.join(unstated)}, so no"
                " closing line is written for it: what its calls billed beyond the lines above is"
                " unknown."
            )
            return
        mine = self._ledger.get(key, _Ledger())
        written = figures.get("cacheCreationInputTokens") or 0
        five = figures.get("cacheCreationEphemeral5mInputTokens")
        hour = figures.get("cacheCreationEphemeral1hInputTokens")
        split = five is not None and hour is not None and five + hour == written
        # A split that disagrees with the itemized calls' (one bucket below them, the total not)
        # is subtracted as a total too, never refused: the tokens were billed either way.
        if split and not mine.unsplit and five >= mine.write_5m and hour >= mine.write_1h:
            write_5m, write_1h = five - mine.write_5m, hour - mine.write_1h
        else:
            # No split the rates can trust, on the total or on a call itemized: the writes are
            # subtracted as one total, carried as 5-minute, which keeps the prompt count right,
            # and the remainder is then never priced from the table.
            split = False
            write_5m, write_1h = written - mine.write_5m - mine.write_1h, 0
        left = rates.Usage(
            uncached=(figures.get("inputTokens") or 0) - mine.uncached,
            write_5m=write_5m,
            write_1h=write_1h,
            read=(figures.get("cacheReadInputTokens") or 0) - mine.read,
            output=(figures.get("outputTokens") or 0) - mine.output,
            split_known=split or write_5m == 0,
        )
        stated_searches = figures.get("webSearchRequests")
        searches = stated_searches - mine.searches if stated_searches is not None else 0
        dollars = stated - mine.cost
        below = [
            name
            for name, amount in (
                ("uncached input tokens", left.uncached),
                ("cache writes", left.write_5m + left.write_1h),
                ("cache reads", left.read),
                ("output tokens", left.output),
                ("web searches", searches),
                ("dollars", -1 if dollars < -_SAME_DOLLARS else 0),
            )
            if amount < 0
        ]
        if below:
            self.write(
                f"WARNING Claude Code stated less for {shown} than this translator itemized over "
                f"{mine.calls} call(s), in {', '.join(below)} (${money(stated)} stated, "
                f"${money(mine.cost)} itemized), so no closing line is written for it: something"
                " would be counted twice."
            )
            return
        dollars = max(dollars, 0.0)  # within half a micro-dollar of nothing: never negative
        buckets = (left.uncached, left.write_5m, left.write_1h, left.read, left.output, searches)
        if not any(buckets):
            if mine.unpriced or abs(dollars) < _SAME_DOLLARS:
                return
            self.write(
                f"WARNING Claude Code billed {shown} ${money(stated)} for the same tokens this "
                f"translator priced at ${money(mine.cost)}: its rate table may be stale "
                f"({rates.SOURCE})."
            )
            return
        table = rates.call_price(key, left, base_url=self.base_url)
        if table.cost is not None and searches:
            fee = rates.web_search_price(searches, base_url=self.base_url)
            table = rates.Price(table.cost + fee.cost) if fee.cost is not None else fee
        # The cost is a difference of two stated figures, so it needs no row of the table: it is
        # withheld only when the itemized side is short (a call it could not price) or the host's
        # price is not the list price Claude Code states.
        host_priced = rates.priced_host(self.base_url)
        priced = host_priced and not mine.unpriced
        gap = None if priced else (rates.NOT_REPORTED if host_priced else rates.UNPRICED_HOST)
        if gap is not None:
            self._unpriced(key, "tokens", gap)
        thinking = figures.get("thinkingTokens")
        reasoning = (
            thinking - mine.reasoning
            if thinking is not None and not mine.reasoning_unknown and thinking >= mine.reasoning
            else None
        )
        self._llm(
            "INFO",
            purpose="helper",
            model=key,
            tokens_in=left.prompt,
            tokens_out=left.output,
            tokens_total=left.prompt + left.output,
            cached_tokens=left.read,
            cache_write_tokens=left.written or None,
            tokens_reasoning=reasoning,
            cost=money(dollars) if priced else None,
            cost_basis="computed" if priced else None,
            cost_gap=gap,
            outcome="ok",
        )
        itemized = (
            f"{mine.calls} call(s) above itemize ${money(mine.cost)} of it"
            if mine.calls
            else "no line above itemizes any of it"
        )
        unfigured = (
            f", and the figures of the {mine.unitemized} call(s) above that carry none"
            if mine.unitemized
            else ""
        )
        self.write(
            f"INFO The helper line above for {shown} is not one call: it is the rest of the "
            f"${money(stated)} Claude Code billed for {shown} in this run, where {itemized}. It "
            f"holds the calls Claude Code never shows one by one, such as a helper model's"
            f"{unfigured}."
        )
        row = rates.RATES.get(key)
        checkable = (
            table.cost is not None
            and not self._geo_priced  # Claude Code's dollars carry a multiplier the table can't see
            and not (  # the long row applies per request, and the remainder may be several
                row is not None
                and row.long is not None
                and left.prompt > rates.HAIKU_LONG_PROMPT_THRESHOLD
            )
        )
        if priced and checkable and abs(table.cost - dollars) > _SAME_DOLLARS:
            self.write(
                f"WARNING The published rates price the remainder for {shown} at "
                f"${money(table.cost)}, not the ${money(dollars)} Claude Code's total leaves: a "
                f"rate may be stale ({rates.SOURCE}), or a call was priced on another row."
            )
