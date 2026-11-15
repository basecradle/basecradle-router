"""The synthetic mark: how the fleet's own test traffic says so, in a way no one else can.

The router's probe has its own route, so every line it produces already says
``source=probe`` (#459). The deploy smoke test is different. It exists to prove the
*real* routes' boundaries on the running bytes, so its requests must reach the github
and basecradle routes' own verify path and be judged exactly as a stranger's would be.
Most of them are built to be rejected. Without a mark, every deploy left a burst of red
``stage=verify outcome=rejected source=github`` lines in the operator's view, each one
the fleet testing itself and indistinguishable from an attack (#461).

**A mark changes what is logged about a request, and keeps it off a real route's delivery
sink, never how it is judged.** A request
carrying a mark that verifies is routed, verified, normalized, resolved and woken
exactly as it would be without one, and its response is the same. Two things change.
The first is what a real route's delivery sink records: nothing (#464). A sink's
``accepted`` is evidence that the *source's* secret matches this box's, and the fleet's
own traffic is signed from this box's own ``router.env``, so it is evidence about no
one (:meth:`~basecradle_router.pipeline.Pipeline._counts_on_sink`). The second is how
its lines read: each ``source=<route>`` the daemon writes for it
reads ``source=<synthetic> via=<route>``. ``<synthetic>`` is the synthetic route whose
secret verified the mark, so the fleet's one filter (``source=probe`` hidden) hides it.
Its access line is dropped, as a probe's is. A ``via=`` key keeps the route it
exercised readable. The rendering is :func:`basecradle_router.logfmt.log_fields`'s,
so no call site can forget it.

**Only the fleet can produce one, and the daemon checks it itself** (the founder's
condition on #461). The mark is an HMAC under the secret a registered *synthetic*
route already holds (:attr:`Route.synthetic`, today the probe route's
``BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET``), so whoever can mint one can already inject
a probe, and nobody else can. Nothing depends on the edge stripping a header. A
request whose mark does not verify is treated exactly as an unmarked one and logged in
full, with one ``event=synthetic_mark_refused`` line saying why. No caller on the
internet can hide a request by claiming to be a test.

**What the mark binds, and why not more.** ``BCSYN1 <unix-seconds> <nonce> <hmac>``,
the HMAC over ``BCSYN1 <unix-seconds> <nonce>`` under a key derived from the secret
(:func:`_mark_key`):

- **Fresh**: the timestamp must be within :data:`WINDOW` seconds of the daemon's
  clock. The minter is on the same box.
- **Once**: a nonce that has verified is refused for as long as its timestamp could
  still be fresh. A mark does get copied after its request arrives: the edge's access
  log carries request headers, so every mark reaches the journal. That copy is dead.
- **Not from before this daemon started.** The memory of used nonces is the process's
  own, so a restart would forget it, and every merge restarts the daemon. A mark
  stamped before the daemon booted is therefore refused as ``stale``. A replay of a
  logged mark into a fresh process finds no memory to fool. The smoke test mints each
  mark the instant before it sends, after the restart it is testing.
- **Not the body or the path.** The mark must be checked before a byte of the body is
  read, because a request for an unknown source is answered from its path alone
  (#427), and the smoke test's public probe case is one of those. The path is rewritten
  by the edge before the daemon sees it, so binding it would tie the smoke test to the
  Caddyfile's mask target, which the smoke test must not depend on (it runs against a
  new tree before that tree's Caddyfile is applied). Freshness and single use bound
  what a stolen mark can do instead: hide one request, once, within two minutes, and
  only if it was stolen before the request it rode on arrived.

**The key is derived, so a mark is never a probe signature.** The probe route signs the
raw body under its secret, so an HMAC under that same secret over ``BCSYN1 …`` would be
a valid probe signature for a body of those bytes: a logged mark would let anyone pass
the probe route's verify. The mark's key is ``HMAC(secret, MARK_KEY_LABEL)`` instead, a
key nothing else on the box signs with.

**What it leaves untagged, on purpose.** The tag rides every ``source=``, so a line that
carries none is not tagged: the lines that state the box's condition rather than the
request's (``wake_lock_stale``, ``wake_lock_unreadable``, the breaker's trip and reset, a
failed evidence write, a saturated scheduler). #459 decided those stay untagged, because
a fault the fleet's own traffic happened to find is still a fault the fleet must see.
"""

from __future__ import annotations

import hmac
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256

from basecradle_router.secret import Secret

#: The request header carrying the mark.
MARK_HEADER = "X-BaseCradle-Synthetic"

#: The mark's scheme tag, and the prefix of the message its HMAC is over.
SCHEME = "BCSYN1"

#: How far, in seconds, a mark's timestamp may sit from the daemon's clock either way.
WINDOW = 120.0

_MARK_RE = re.compile(
    rf"\A{SCHEME} (?P<ts>[0-9]{{1,12}}) "
    r"(?P<nonce>[A-Za-z0-9_-]{16,64}) (?P<hmac>[0-9a-f]{64})\Z"
)


def _message(timestamp: int, nonce: str) -> bytes:
    return f"{SCHEME} {timestamp} {nonce}".encode()


#: What the mark's key is derived from the secret with (see the module docstring).
MARK_KEY_LABEL = b"basecradle-router synthetic-mark key v1"


def _mark_key(secret: Secret) -> bytes:
    return hmac.new(secret.reveal().encode("utf-8"), MARK_KEY_LABEL, sha256).digest()


def _digest(secret: Secret, timestamp: int, nonce: str) -> str:
    return hmac.new(_mark_key(secret), _message(timestamp, nonce), sha256).hexdigest()


def mint(secret: Secret, *, timestamp: int, nonce: str) -> str:
    """A mark for ``timestamp`` and ``nonce`` under ``secret``: the header's value.

    The daemon never mints; this is the scheme's other half, written once, for the
    tests and any in-repo injector. The smoke test mints in its own shell, and
    ``tests/test_smoke_test_assertions.py`` holds its bytes to this.
    """
    mark = f"{SCHEME} {timestamp} {nonce} {_digest(secret, timestamp, nonce)}"
    if not _MARK_RE.match(mark):
        raise ValueError("a mark's nonce is 16 to 64 of [A-Za-z0-9_-]")
    return mark


@dataclass(frozen=True, slots=True)
class MarkVerdict:
    """What a request's mark proved.

    ``source`` is the synthetic route whose secret verified it, or ``None``.
    ``refused`` is why a mark that was present did not verify, or ``None`` when it did
    or when the request carried none. At most one of the two is set.
    """

    source: str | None = None
    refused: str | None = None


#: The verdict for a request that carried no mark.
UNMARKED = MarkVerdict()


class SyntheticMarks:
    """Verifies marks, and remembers each one that verified until it could not be fresh.

    Only a mark whose HMAC verifies is remembered, so the memory is bounded by what the
    holders of a synthetic secret send, never by what the internet does.
    """

    def __init__(
        self,
        *,
        window: float = WINDOW,
        clock: Callable[[], float] = time.time,
        not_before: float | None = None,
    ) -> None:
        self.window = window
        self._clock = clock
        # The whole second this process started in: a mark stamped earlier predates the
        # memory of used nonces, so it is refused (the module docstring's third bullet).
        self.not_before = int(clock() if not_before is None else not_before)
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def verify(self, value: str | None, secrets: Mapping[str, Secret]) -> MarkVerdict:
        """The verdict on the mark ``value`` under each synthetic route's secret.

        ``secrets`` maps each registered synthetic route's name to its secret. The
        refusal reasons are a closed set: ``malformed`` (not the scheme's shape),
        ``unarmed`` (no synthetic route on this box holds a secret), ``forged`` (no
        secret verifies it), ``stale`` (verified, but outside the window or stamped
        before this process started) and
        ``replayed`` (verified, fresh, and already used).
        """
        if value is None:
            return UNMARKED
        match = _MARK_RE.match(value)
        if match is None:
            return MarkVerdict(refused="malformed")
        if not secrets:
            return MarkVerdict(refused="unarmed")
        timestamp, nonce, claimed = int(match["ts"]), match["nonce"], match["hmac"]
        source = next(
            (
                name
                for name, secret in sorted(secrets.items())
                if hmac.compare_digest(_digest(secret, timestamp, nonce), claimed)
            ),
            None,
        )
        if source is None:
            return MarkVerdict(refused="forged")
        now = self._clock()
        with self._lock:
            self._seen = {seen: until for seen, until in self._seen.items() if until >= now}
            if abs(now - timestamp) > self.window or timestamp < self.not_before:
                return MarkVerdict(refused="stale")
            if nonce in self._seen:
                return MarkVerdict(refused="replayed")
            self._seen[nonce] = timestamp + self.window
        return MarkVerdict(source=source)
