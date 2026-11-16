"""The shape and the colour of one journal line — the router's log grammar, in one place.

Two concerns live here because they are the same concern seen twice: **what a log
line says** (:func:`log_fields`, the one ``key=value`` renderer) and **how it reads
at a glance** (:func:`paint`, the fleet ANSI palette). Both are the journal's
*presentation* boundary, and both must be applied in exactly one place or the
surfaces drift apart.

It is a module of its own, below the pipeline rather than inside it, because the
renderer is shared by layers the pipeline *imports* — the breaker and the wake-lock
guard log their own lines — and a renderer that lived in :mod:`basecradle_router.pipeline`
could only be shared by duplicating it, which is the drift its docstring forbids.

**The token-integrity rule.** A colour wraps a whole ``key=value`` token and nothing
smaller::

    \\x1b[32mevent=wake_start\\x1b[0m delivery=…      # right
    event=\\x1b[32mwake_start\\x1b[0m delivery=…      # WRONG

Escape bytes between the ``=`` and the value break every substring search for
``event=wake_start`` — in Better Stack Live Tail, in ``journalctl | grep``, in a
ClickHouse ``match()``. So the palette is keyed by the **whole token**: there is no
call shape that can put an escape inside one, and a token with no entry is returned
untouched (``outcome=ignored``, ``event=delivery_decision``). Note the rule protects
one *token*, not a multi-token *phrase*: a consumer matching ``stage=wake outcome=``
across the space between two tokens sees the escape and must be updated — which is a
cross-repo fact about the NOC's extraction, recorded on basecradle-router#228.

**Colour is presentation, never data.** It is added at the moment a line is handed to
the logger, and never enters :class:`~basecradle_router.pipeline.StageRecord` or any
admin/status payload: the in-memory record and the journal still agree on every *value*
they carry — the journal line merely paints some of them. ``tests/test_logfmt.py`` pins
both halves.

The palette itself is fleet law, decided by @origin on 2026-08-17 and carried on the
harness's and the NOC's journald surfaces too, so a colour means the same thing in a
Live Tail whichever daemon emitted the line.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType

#: The record envelope every router line is formatted with — ``asctime level name
#: message``. It lives here beside the renderer and the palette because it is the third
#: half of the same concern: the NOC lifts its ``level`` column out of the **message
#: text** with ``extract(…, ' (CRITICAL|ERROR|WARNING|INFO|DEBUG) ')``, so this envelope
#: is part of the log grammar rather than a detail of how the daemon happens to boot. It
#: is shared by the daemon's stdout handler and by the log-grammar probe, which must
#: format the bytes it puts in the journal exactly as the daemon formats its own
#: (basecradle-router#232).
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"

#: ANSI SGR colours, and the reset that must follow each painted token.
GREEN = "\x1b[32m"
BLUE = "\x1b[34m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
RESET = "\x1b[0m"

#: Whole ``key=value`` token → its colour. Keyed by the token rather than by the key
#: so the colour can depend on the *value* (``outcome=ok`` green, ``outcome=failed``
#: red) and so the escape can never land inside a token. The tiers are the fleet's, defined
#: once in basecradle-harness ``docs/telemetry.md`` → The Palette: success (green), the
#: ``wake_end`` bookend's own identity (blue — its trailing ``outcome=`` token carries the
#: verdict's colour), the warning tier that is neither success nor failure (yellow), the
#: Operator's Choice tier, an outcome an operator chose such as stopping a wake (yellow,
#: the warning tier's SGR on purpose — never red, because nothing failed;
#: basecradle-harness#799), and failure (red).
PALETTE: Mapping[str, str] = MappingProxyType(
    {
        # success
        "event=wake_start": GREEN,
        "outcome=ok": GREEN,
        "event=breaker_reset": GREEN,
        # the bookend's identity
        "event=wake_end": BLUE,
        # warning tier — a wake that did not run, or has not run yet
        "event=wake_retry": YELLOW,
        "event=wake_refused": YELLOW,
        "event=wake_lock_stale": YELLOW,
        # Operator's Choice tier — an operator stopped the wake; nothing failed
        "event=wake_stopped": YELLOW,
        "outcome=stopped": YELLOW,
        # failure
        "outcome=failed": RED,
        "outcome=rejected": RED,
        "event=breaker_tripped": RED,
        "event=wake_lock_unreadable": RED,
    }
)


def paint(token: str) -> str:
    """``token`` wrapped whole in its palette colour — or returned untouched.

    The only way colour enters a log line. ``token`` is a complete ``key=value``
    string, so the escape bytes can only ever bracket it (see the module docstring's
    token-integrity rule), and a token the palette does not name is passed through
    rather than guessed at.
    """
    colour = PALETTE.get(token)
    return f"{colour}{token}{RESET}" if colour else token


#: One C0 or C1 control character, or DEL — Unicode's ``Cc`` category exactly.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# --- credential-named assignments (basecradle-router#491) -------------------------------------
#
# A line this repository renders never carries a credential-named assignment: the name stays,
# the value becomes REDACTED_VALUE, whatever the value looks like and whatever quoting surrounds
# it. "Names a credential" is the NOC's log-store canary's definition, its `credential_assignment`
# rule (basecradle-noc `observability/log-canary.json`), never a list of this repository's own:
#
#     [A-Z][A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE)=[^ ",}]{8,}
#
# `tests/conftest.py` holds that pattern byte for byte, and the tests hold the pieces below to
# it: the words, the value's stop characters and its floor must spell it exactly, and the canary's
# own pattern is the oracle every redacted line is checked against.

#: The words that make a name a credential's, in the canary's order.
CREDENTIAL_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSPHRASE")

#: The canary reads a value as a run of anything but these, at least :data:`CANARY_VALUE_FLOOR`
#: characters long, from the character after the ``=``.
CANARY_VALUE_STOPS = ' ",}'
CANARY_VALUE_FLOOR = 8

#: A credential name, up to the ``=`` it is assigned with. The canary's own name pattern,
#: ``[A-Z][A-Z0-9_]*(…)=``, run unanchored in a backtracking engine (Python's ``re``, jq's
#: Oniguruma) tries every start inside a long run of capitals and is quadratic in it: 50,000 of
#: them took jq 30 seconds, and a translator that stalls stops reading the pipe ``claude`` writes
#: into, which hangs the wake. This spelling matches before exactly the same ``=``s (pinned against
#: the canary's pattern by a test), and is linear: it starts only where a run of name characters
#: starts, skips digits and underscores to the run's first capital, which the canary's ``[A-Z]``
#: needs, and extends lazily to a credential word. ``wake-runner``'s ``jq`` fallback carries the
#: same spelling.
CREDENTIAL_NAME = r"(?<![A-Z0-9_])[0-9_]*[A-Z][A-Z0-9_]*?(?:" + "|".join(CREDENTIAL_WORDS) + ")="
_CREDENTIAL_NAME = re.compile(CREDENTIAL_NAME)

#: What stands where a credential's value stood. It must read as one value to the redactor and as
#: no value to the canary: a single-quoted word, so redacting it again (bounded, then rendered)
#: leaves it as it is, with a space at its fourth character, so the canary's run after the ``=``
#: is ``'<a``, far under its floor, whatever follows, and still under it when a bound cuts the
#: marker and adds its ``…``. ``<redacted>`` alone is ten characters the canary reads as a value,
#: so ``GH_TOKEN=<redacted>`` would be the finding it replaced.
REDACTED_VALUE = "'<a redacted value>'"

#: Every character journald, or Python reading a line, starts a new record or line at; a CRLF is
#: one. The translator cuts model-written text at these (``claude_stream.quoted``), and
#: ``wake-runner``'s ``jq`` fallback splits by this pattern, spelled for jq (pinned by a test).
_BREAK_CHARS = "\n\r\x00\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
RECORD_BREAKS = "\r\n|[" + _BREAK_CHARS + "]"

#: The canary's value run, where it ends in every record the text can be written into: at one of
#: the canary's own stops, or at a record break, since no record holds a run across one.
_CANARY_RUN = re.compile("[^" + re.escape(CANARY_VALUE_STOPS + _BREAK_CHARS) + "]*")

#: The characters that end an unquoted shell word: the shell's blanks and its newline.
_SHELL_BLANKS = " \t\n"


def _shell_word_end(text: str, start: int) -> int:
    """Where the shell word from ``start`` ends: at a blank outside every quote, or the end.

    One word as the shell reads it, so a value is taken whole whatever surrounds it: a single- or
    double-quoted string, an ANSI-C ``$'…'``, a ``$(…)``, ``${…}`` or ``(…)`` (each nesting as the
    shell's does, quotes inside included), a backquoted command, and a backslash escape. Only a
    space, a tab or a newline ends an unquoted word, as in the shell, so no other character can cut
    a value short. A quote or a substitution still open at a record break ends there: it is prose
    (an apostrophe, a markdown backquote) far more often than a value that spans lines, and read
    on it would hide the rest of a reply. Iterative, with its own stack, so no depth of nesting can
    exhaust the interpreter's.
    """
    stack: list[str] = []  # what closes each open context: ' " ` ) } and $' for ANSI-C
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        top = stack[-1] if stack else ""
        if top and c in _BREAK_CHARS:
            return i
        if top == "'":
            if c == "'":
                stack.pop()
            i += 1
        elif top == "$'":
            if c == "\\":
                i += 2
                continue
            if c == "'":
                stack.pop()
            i += 1
        elif c == "\\":
            i += 2
        elif top == "`":
            if c == "`":
                stack.pop()
            i += 1
        elif top == '"':
            if c == '"':
                stack.pop()
                i += 1
            elif text.startswith(("$(", "${"), i):
                stack.append(")" if text[i + 1] == "(" else "}")
                i += 2
            else:
                if c == "`":
                    stack.append("`")
                i += 1
        else:  # outside every quote: the word itself, or inside a ( ), $( ) or ${ }
            if not top and c in _SHELL_BLANKS:
                break
            if top and c == top:
                stack.pop()
                i += 1
            elif text.startswith("$'", i):
                stack.append("$'")
                i += 2
            elif text.startswith(("$(", "${"), i):
                stack.append(")" if text[i + 1] == "(" else "}")
                i += 2
            else:
                if c in "'\"`":
                    stack.append(c)
                elif c == "(" and top != "}":
                    stack.append(")")  # an array, a subshell, arithmetic
                i += 1
    return min(i, n)


def _value_end(text: str, start: int) -> int:
    """Where a credential's value from ``start`` ends: the longer of the two readings."""
    return max(_shell_word_end(text, start), _CANARY_RUN.match(text, start).end())


def redact_credentials(text: str) -> str:
    """``text`` with every credential-named assignment's value replaced by :data:`REDACTED_VALUE`.

    The name stays. The value is the longer of two readings from the character after the ``=``
    (:func:`_value_end`): the shell word there (:func:`_shell_word_end`), so a quoted value or a
    ``"$(…)"`` goes whole, and the canary's own run, so whatever the canary would read as the value
    is gone in every record the text is written into, a prose line that keeps a tab included. An
    empty value (a space, a record break or the text's end follows the ``=``) has nothing to hide
    and is left as it is. Linear in ``text``, and idempotent: the marker is redacted to itself, so
    a value redacted where it is bounded and again where it is rendered stays one marker.
    """
    if "=" not in text:
        return text
    out: list[str] = []
    pos = 0
    while (match := _CREDENTIAL_NAME.search(text, pos)) is not None:
        start = match.end()
        end = _value_end(text, start)
        out.append(text[pos:start])
        if end > start:
            out.append(REDACTED_VALUE)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _render_value(value: object) -> str:
    """One field's value, rendered by the fleet's one value rule — or ``""`` when empty.

    The rule is basecradle-harness ``docs/telemetry.md`` → The Grammar, the one
    definition (the harness's ``_observability._value``); this is the router's copy of
    it. (The ``detail=`` remap in ``vector.yaml`` that was once a third copy retired with
    the builders' telemetry receiver, #486.)

    1. **Flatten.** Every run of whitespace becomes one space, trimmed at both ends, so
       a record stays one line. A control character that is not whitespace (an escape,
       a NUL) becomes a space first, as the harness's ``_flatten`` does (harness
       0.177.0, so the fleet's renderers flatten the same bytes) — what keeps a value
       from painting the line outside :func:`paint`.
    2. **Quote** a value holding a space, an ``=`` or a double quote, so it reads as
       *one* value rather than as extra fields — and turn its own ``"`` into ``'``.

    Step 2's swap is the point (basecradle-router#396). Every fleet consumer anchors a
    token **outside every quoted value** by counting the ``"`` before it, so a quote is
    never escaped: a ``\\"`` (what ``json.dumps`` wrote here before) is still a ``"`` to
    that count, flips it, and lets a value such as ``upstream said " stage=wake
    outcome=failed`` forge a line's verdict. A backslash is an ordinary character and
    passes through untouched, as in the harness.

    Before either step, a credential-named assignment's value is redacted
    (:func:`redact_credentials`, #491), on the text as it came, so a quoted value that
    spans lines is still read as one. Every field of every line both writers render
    passes here, so no field, now or added later, can carry one.

    A ``bool`` renders lowercase (see :func:`log_fields`). Two of the harness's ``kv``
    jobs are not this function's: bounding a value's length (240 characters there),
    which is a separate job from quoting and is :func:`bounded`'s, applied by the caller
    to foreign text only (the router's own values are never bounded), and every other
    credential shape, which is the box's ``ai_scrub``, run over every journald line.
    """
    text = ("true" if value else "false") if isinstance(value, bool) else str(value)
    text = " ".join(_CONTROL.sub(" ", redact_credentials(text)).split())
    if " " in text or "=" in text or '"' in text:
        return '"' + text.replace('"', "'") + '"'
    return text


#: The fleet's bound on a free-text value, in characters (basecradle-harness ``docs/telemetry.md`` →
#: The Grammar), the ellipsis included.
BOUND = 240

#: One character of the run every length-gated credential rule on the box matches. A cut never falls
#: inside such a run: a credential cut short of its rule's minimum would match no rule and ship.
_CREDENTIAL_CHAR = re.compile(r"[A-Za-z0-9_-]")
_TRAILING_CREDENTIAL_RUN = re.compile(r"[A-Za-z0-9_-]*\Z")


def bounded(value: object, limit: int = BOUND) -> str:
    """``value`` flattened and cut to at most ``limit`` characters, ending in ``…`` when cut.

    For foreign text only (a vendor's error, a tool's failure): the router's own values are never
    bounded, which is why :func:`log_fields` does not call this. It is the harness's ``bounded``
    (#385): the cut backs off to the last character outside a ``[A-Za-z0-9_-]`` run, so a token
    is either whole, and so redactable by the box's ``ai_scrub`` after it, or absent. A value that
    is one such run past the bound is returned empty, and :func:`log_fields` then drops the field.

    A credential-named assignment's value is redacted first (:func:`redact_credentials`, #491),
    so the bound is spent on what a reader may see, and a value the cut would have halved is gone
    whole. The harness's ``bounded`` scrubs in its caller, ``kv``, before the cut, to the same end.
    An assignment is one token to the cut, too: one the cut would reach (a marker it would halve,
    a marker or an ``=`` the ``…`` would be glued to) goes whole, name and all. Otherwise the
    renderer, redacting again, reads the cut marker or the ``…`` as a value and writes a whole
    marker in its place, past the bound and with no ``…`` to say the text was cut.
    """
    text = " ".join(_CONTROL.sub(" ", redact_credentials(str(value))).split())
    if len(text) <= limit:
        return text
    head = text[: limit - 1]
    if _CREDENTIAL_CHAR.fullmatch(text[limit - 1]):
        head = _TRAILING_CREDENTIAL_RUN.sub("", head)
    head = head.strip()
    while (name := _last_name(text, len(head))) is not None:
        if _value_end(text, name.end()) < len(head):
            break  # the last assignment before the cut ends before it: nothing reaches the cut
        head = _TRAILING_CREDENTIAL_RUN.sub("", text[: name.start()]).strip()
    return f"{head}…" if head else ""


def _last_name(text: str, end: int) -> re.Match[str] | None:
    """The last credential name in ``text`` whose ``=`` lies before ``end``, or ``None``."""
    last = None
    for last in _CREDENTIAL_NAME.finditer(text, 0, end):  # noqa: B007 — the loop keeps the last
        pass
    return last


def log_fields(**pairs: object) -> str:
    """Render ``pairs`` as a ``key=value`` run, dropping the empty ones.

    The one renderer behind every line the router logs, so a stage line, a retry
    warning, a breaker trip, and the startup banner cannot drift apart in shape. Each
    value is rendered by :func:`_render_value`, the fleet's one value rule: a value
    carrying whitespace, an ``=`` or a quote — an error message, always — is
    double-quoted with its own ``"`` turned into ``'``, so it stays a *single* field
    and a grep for the field after it still matches; a ``None`` value, or one that
    renders empty (``""``, whitespace only), is dropped rather than logged as ``key=``
    (``0`` is a value, and is kept).

    A ``bool`` renders lowercase — ``key=true``, never Python's ``True`` — and ``False``
    is a value like ``0``, never dropped as empty. No field passes a bool today
    (basecradle-router#222 retired the one that did), but this is the *renderer's*
    contract rather than that field's: a log value is a label a metric extractor lifts
    and a dashboard filters on literally, so it must be spelled the way every other
    structured surface in the fleet spells it (JSON evidence, logfmt, the NOC's queries)
    rather than the way ``str()`` happens to render a Python object — and a key whose
    ``false`` was dropped as empty would leave a filter matching nothing, silently. Both
    are pinned by their own test, so the next bool field is correct on arrival instead
    of re-deriving this.

    It renders *values*, never colour: a painted token is assembled by :func:`paint` at
    the call site, so no field's value can smuggle an escape into the middle of a token.

    **Inside a marked request, ``source=`` names the synthetic route that vouched for it**
    (:func:`synthetic_origin`, #461): ``source=github`` renders ``source=probe
    via=github``, so every line the daemon writes for the fleet's own test traffic carries
    the fleet's one synthetic tag, from whichever layer writes it. A ``source`` that
    already is the synthetic route renders unchanged.
    """
    origin = _SYNTHETIC_ORIGIN.get()
    parts = []
    for key, value in pairs.items():
        if value is None:
            continue
        text = _render_value(value)
        if not text:
            continue
        if key == "source" and origin is not None and text != origin:
            parts.append(f"source={origin}")
            parts.append(f"via={text}")
        else:
            parts.append(f"{key}={text}")
    return " ".join(parts)


#: The synthetic route that vouched for the request being served, or ``None``. Set only by
#: :func:`synthetic_origin`, around one request, after its mark has verified.
_SYNTHETIC_ORIGIN: ContextVar[str | None] = ContextVar(
    "basecradle_router_synthetic_origin", default=None
)


@contextmanager
def synthetic_origin(source: str | None) -> Iterator[None]:
    """Render every ``source=`` logged in this context as ``source``'s, ``via`` its own.

    ``source`` is the synthetic route whose secret verified the request's mark
    (:mod:`basecradle_router.synthetic`); ``None``, for a request with no verified mark,
    changes nothing. A context variable rather than a parameter because the lines about one
    request are written by the server, the pipeline, every route, the wake-lock guard and
    the breaker, and a parameter threaded through all of them is one a new line forgets:
    here a line written later carries the tag by construction. It holds for the request's
    task, and the wake scheduler carries it onto the thread that runs the request's wake.
    """
    if source is None:
        yield
        return
    token = _SYNTHETIC_ORIGIN.set(source)
    try:
        yield
    finally:
        _SYNTHETIC_ORIGIN.reset(token)


def active_synthetic_origin() -> str | None:
    """The synthetic route vouching for the request being served, or ``None``."""
    return _SYNTHETIC_ORIGIN.get()
