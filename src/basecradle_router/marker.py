"""The ``BCNOC1`` probe marker — the router's *carrying* half of a three-party contract.

A synthetic wake proves nothing unless the thing at the far end can tell a probe from
a real trigger **and** prove the probe came from someone entitled to send it. That is
what the marker is: a one-line, ASCII, HMAC-signed token::

    BCNOC1 <nonce> <hex-hmac-sha256>

minted by a holder of one agent's ``NOC_PROBE_SECRET`` and verified by that agent's own
copy of the same secret. The scheme is basecradle-noc's (``src/basecradle_noc/marker.py``
is the reference); the harness carries a verifying mirror (``_probe.py``); this module is
the **third** half, and it is deliberately the smallest of the three:

**The router neither mints nor verifies.** It holds no agent's probe secret and must not
— it holds no agent secret of any kind, which is the whole point of the wake-runner
privilege boundary (``deploy/bin/wake-runner``: the router passes *no* environment, and
the agent's own ``agent.env`` is read by the agent, after the drop). So the router's
entire relationship with a marker is: **shape-check it, carry it, hand it across the
sudo boundary, and let the agent's own side decide.** Minting is the NOC's; verifying is
the agent's.

That split is what makes the probe honest. If the router could verify the marker, a
green probe would prove only that the router agrees with itself. Because verification
happens *after* the privilege drop, against a file only that agent can read, a passing
probe proves the wake reached **that agent's own context with that agent's own
credentials loaded** — which is exactly the property the retired timeline probe proved
socially, now proved by file permissions instead of by a trust edge
(basecradle-router#208, `basecradle-noc#421`).

**Why the shape is checked here even though the meaning is not.** The marker becomes an
``argv`` element crossing ``sudo`` into a root-owned wrapper, the same crossing
:data:`~basecradle_router.wake.DELIVERY_ID_ENV`'s id makes. So it is pinned to a fully
anchored, inert charset at the router's edge — no whitespace beyond its two separators,
no shell metacharacter, nothing to re-interpret. The **fail-direction is the opposite**
of the delivery id's, and deliberately: an unsafe delivery id is *dropped* because
observability must never cost a wake, whereas an unsafe marker is *refused* at every
layer that sees it, because a probe that cannot carry its marker has nothing left to
prove and must never fall through to something that would wake the model.

The regex is anchored where the other two implementations ``search`` — they locate a
marker inside a larger body (a message, a webhook payload); here the marker **is** the
whole wake argument, so anything around it is a defect rather than context.
"""

from __future__ import annotations

import re

#: The scheme tag and version, identical to basecradle-noc's ``marker.SCHEME``. The two
#: halves must agree byte-for-byte; a v2 would be a new tag, never a reshaped v1.
SCHEME = "BCNOC1"

#: The environment variable the *agent* holds its own probe secret under, in its
#: ``agent.env`` — delivered per agent by ``basecradle-noc mint-probe-secret``
#: (basecradle-noc#424). Named here only so the router's documentation and the
#: wake-runner's refusal message can spell it identically to the two repos that
#: actually read it; **nothing in this package ever reads this variable**, because the
#: router holds no agent's secret.
PROBE_SECRET_ENV = "NOC_PROBE_SECRET"

_NONCE = r"[A-Za-z0-9_-]{1,128}"

#: Fully anchored: the wake argument must be a marker and nothing else. Bounded token
#: charsets, so matching is linear and a hostile value cannot backtrack.
MARKER_RE = re.compile(rf"\A{SCHEME} (?P<nonce>{_NONCE}) (?P<hmac>[0-9a-f]{{64}})\Z")


def is_marker(value: str) -> bool:
    """Whether ``value`` is a well-formed marker line — shape only, never authenticity.

    True says the string is safe to carry across the sudo boundary and *claims* to be a
    probe; it says nothing about whether the signature is good, which only the recipient
    agent can answer with its own secret. Callers must treat a ``True`` here as
    permission to carry, never as proof.
    """
    return isinstance(value, str) and MARKER_RE.match(value) is not None


def nonce_of(value: str) -> str | None:
    """The marker's nonce, or ``None`` if ``value`` is not a well-formed marker.

    Used only to log and report *which* probe a wake belongs to — the nonce is the
    correlation id the NOC minted, so it is what joins the router's stage lines to the
    prober's own record of the cycle. It is not a secret: it is a one-time opaque token
    whose whole job is to be echoed back.
    """
    match = MARKER_RE.match(value) if isinstance(value, str) else None
    return match.group("nonce") if match else None


__all__ = ["MARKER_RE", "PROBE_SECRET_ENV", "SCHEME", "is_marker", "nonce_of"]
