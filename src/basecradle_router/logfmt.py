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

import json
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
#: red) and so the escape can never land inside a token. Four families, decided by
#: @origin 2026-08-17: success (green), the ``wake_end`` bookend's own identity (blue —
#: its trailing ``outcome=`` token carries the verdict's colour), the warning tier that
#: is neither success nor failure (yellow), and failure (red).
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


def log_fields(**pairs: object) -> str:
    """Render ``pairs`` as a ``key=value`` run, dropping the empty ones.

    The one renderer behind every line the router logs, so a stage line, a retry
    warning, a breaker trip, and the startup banner cannot drift apart in shape. A
    value carrying whitespace or a quote — an error message, always — is JSON-quoted,
    so it stays a *single* field and a grep for the field after it still matches; a
    ``None`` or ``""`` value is dropped rather than logged as ``key=`` (``0`` is a
    value, and is kept).

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
        if value is None or value == "":
            continue
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        if any(char.isspace() for char in text) or '"' in text:
            text = json.dumps(text)
        parts.append(f"{key}={text}")
    return " ".join(parts)
