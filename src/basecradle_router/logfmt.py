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
from collections.abc import Mapping
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


#: One C0 or C1 control character, or DEL — Unicode's ``Cc`` category exactly, which is
#: what the ``\\p{Cc}`` of the router's own ``detail=`` remap matches (``vector.yaml``,
#: ``ai_otel_llm``), so the two renderers flatten the same bytes.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _render_value(value: object) -> str:
    """One field's value, rendered by the fleet's one value rule — or ``""`` when empty.

    The rule is basecradle-harness ``docs/telemetry.md`` → The Grammar, the one
    definition (the harness's ``_observability._value``); this is the router's copy of
    it, and the ``detail=`` remap in ``vector.yaml`` is the other, byte for byte
    (``tests/test_logfmt.py`` reads the remap's case and holds the two together):

    1. **Flatten.** Every run of whitespace becomes one space, trimmed at both ends, so
       a record stays one line. A control character that is not whitespace (an escape,
       a NUL) becomes a space first, as the remap's ``\\p{Cc}`` and the harness's
       ``_flatten`` do (harness 0.177.0, so the fleet's three renderers flatten the
       same bytes) — what keeps a value from painting the line outside :func:`paint`.
    2. **Quote** a value holding a space, an ``=`` or a double quote, so it reads as
       *one* value rather than as extra fields — and turn its own ``"`` into ``'``.

    Step 2's swap is the point (basecradle-router#396). Every fleet consumer anchors a
    token **outside every quoted value** by counting the ``"`` before it, so a quote is
    never escaped: a ``\\"`` (what ``json.dumps`` wrote here before) is still a ``"`` to
    that count, flips it, and lets a value such as ``upstream said " stage=wake
    outcome=failed`` forge a line's verdict. A backslash is an ordinary character and
    passes through untouched, as in the harness.

    A ``bool`` renders lowercase (see :func:`log_fields`). Two of the harness's ``kv``
    jobs are not this function's: bounding a value's length (240 characters there),
    which is a separate job from quoting and one the router has never done, and
    credential scrubbing, which is the box's ``ai_scrub``, run over every journald line.
    """
    text = ("true" if value else "false") if isinstance(value, bool) else str(value)
    text = " ".join(_CONTROL.sub(" ", text).split())
    if " " in text or "=" in text or '"' in text:
        return '"' + text.replace('"', "'") + '"'
    return text


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
    """
    parts = []
    for key, value in pairs.items():
        if value is None:
            continue
        text = _render_value(value)
        if text:
            parts.append(f"{key}={text}")
    return " ".join(parts)
