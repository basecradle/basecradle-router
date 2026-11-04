"""``deploy/bin/stop-wake`` against a REAL process tree: real ``sudo``, real ``runuser``.

The offline suite (``test_stop_wake.py``) pins which processes are signalled over a
fabricated table. What it cannot pin is what the real links of the chain then do with
those signals, and that is where basecradle-router#404 lived: ``runuser`` catches a
SIGTERM, signals its child, and sleeps 2 s before SIGKILL, and a second SIGTERM landing
inside that sleep cuts it short (util-linux 2.39 ``su-common.c``). The old stop sent
``runuser`` two — its own, and ``sudo``'s relayed copy — and on 2026-11-03 the copy won the
race and an ``@jt`` wake was SIGKILLed 3 ms into its grace.

So this suite builds the wake's real shape — ``sudo -> <wake-runner> -> runuser -> bash ->
<binary>`` — and stops it with the real program. Only two things are stand-ins, both for a
reason that has nothing to do with signals: the wake-runner is a few lines that keep its
shape (``exec runuser``, the delivery mark exported after the drop) without its registry
gates, and the daemon is this test process, so the ``router`` account is root here. The
agent is ``nobody``. The binary traps SIGTERM, holds its handler for :data:`HOLD` — well
inside ``runuser``'s 2 s — and reports every SIGTERM it receives, so a test reads both
whether the handler ran to its end and how many copies of the signal reached it.

It needs root, Linux, ``sudo`` and ``runuser``, so it skips on the laptop and anywhere
else that lacks them. CI runs it as root in its own step with ``STOP_WAKE_TREE_REQUIRED=1``,
which turns a skip into a failure: a proof that silently did not run is not a proof.
"""

from __future__ import annotations

import contextlib
import os
import pwd
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from test_stop_wake import sw

AGENT = "nobody"
DELIVERY = "0192f3a4-5b6c-7d8e-9f01-0000000000aa"
#: How long the binary's SIGTERM handler runs: inside runuser's 2 s grace, and long enough
#: that a grace cut short (by a second TERM ~0.3 s in) cannot be mistaken for a finish.
HOLD = 1.0
#: The pause the reordering tests put after each signal the stop sends, so a relayed copy
#: has landed — and runuser has entered its sleep — before the next one goes out.
SPACING = 0.3


def _missing() -> str | None:
    if not sys.platform.startswith("linux"):
        return "Linux only: the chain under test is Linux's sudo and util-linux runuser"
    if os.geteuid() != 0:
        return "needs root: sudo -> runuser is the privilege drop under test"
    for tool in ("sudo", "runuser"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    try:
        pwd.getpwnam(AGENT)
    except KeyError:
        return f"no {AGENT!r} account to drop to"
    return None


_REASON = _missing()
if _REASON and os.environ.get("STOP_WAKE_TREE_REQUIRED") == "1":
    raise RuntimeError(f"STOP_WAKE_TREE_REQUIRED=1, but this suite cannot run: {_REASON}")
pytestmark = pytest.mark.skipif(_REASON is not None, reason=_REASON or "")

# The wake-runner's SHAPE, not its gates: a bash that execs runuser, which runs a bash
# that execs the binary, with the delivery mark exported only after the drop. The same
# pids and parents a real wake has: sudo -> runuser (this script's pid) -> <binary>.
FAKE_RUNNER = r"""#!/bin/bash
set -euo pipefail
user="" delivery=""
while [ "$1" != "--" ]; do
	case "$1" in
	--user) user=$2 ;;
	--delivery) delivery=$2 ;;
	esac
	shift 2
done
shift
exec runuser -u "$user" -- /usr/bin/env "BASECRADLE_DELIVERY_ID=$delivery" \
	/bin/bash -c 'exec "$@"' wake-runner "$@"
"""

# The binary: traps SIGTERM, says so for every copy it receives, and holds its handler
# for HOLD seconds before a clean exit 0 — the harness's shape, a binary that settles.
BINARY = r"""
import os, signal, sys, time
hold = float(sys.argv[1])
started = False
def on_term(signum, frame):
    global started
    print("term", flush=True)
    if started:
        return
    started = True
    print("begin", flush=True)
    time.sleep(hold)
    print("end", flush=True)
    os._exit(0)
signal.signal(signal.SIGTERM, on_term)
print("ready", flush=True)
while True:
    time.sleep(60)
"""


class _Wake:
    """One real wake: ``sudo`` spawned by this process, its output read as it lands."""

    def __init__(self, runner: Path) -> None:
        self.proc = subprocess.Popen(
            [
                "sudo",
                str(runner),
                "--user",
                AGENT,
                "--cwd",
                "/",
                "--delivery",
                DELIVERY,
                "--",
                "/usr/bin/python3",
                "-I",
                "-c",
                BINARY,
                str(HOLD),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd="/",
            text=True,
        )
        self.out: list[str] = []
        self.err: list[str] = []
        self._drains = [
            threading.Thread(target=self._drain, args=(stream, sink), daemon=True)
            for stream, sink in ((self.proc.stdout, self.out), (self.proc.stderr, self.err))
        ]
        for drain in self._drains:
            drain.start()

    @staticmethod
    def _drain(stream, sink: list[str]) -> None:
        for line in stream:
            sink.append(line.rstrip("\n"))

    def wait_for(self, what: Callable[[], bool], timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not what():
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out: out={self.out} err={self.err}")
            time.sleep(0.02)

    def chain(self) -> tuple[int, int]:
        """``(runuser, binary)`` pids under this wake's ``sudo``, read from /proc."""
        procs = sw.read_procs()
        below = [p for p in procs.values() if p.ppid == self.proc.pid]
        assert len(below) == 1, f"sudo has {len(below)} children: {below}"
        runuser = below[0]
        assert os.path.basename(runuser.argv[0]) == "runuser", runuser.argv
        binary = [p for p in procs.values() if p.ppid == runuser.pid]
        assert len(binary) == 1, f"runuser has {len(binary)} children: {binary}"
        assert binary[0].uid == pwd.getpwnam(AGENT).pw_uid
        return runuser.pid, binary[0].pid

    def ended(self, timeout: float = 15.0) -> int:
        """``sudo``'s exit status — what the daemon reads — once every line has landed."""
        code = self.proc.wait(timeout=timeout)
        for drain in self._drains:
            drain.join(timeout=timeout)  # EOF: every process holding the pipes is gone
        return code


@pytest.fixture
def runner(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "wake-runner"
    path.write_text(FAKE_RUNNER)
    path.chmod(0o755)
    # The real program's two constants, pointed at this test's tree: the wake-runner it
    # matches a wake by, and the account the daemon runs as (this process: root).
    monkeypatch.setattr(sw, "WAKE_RUNNER", str(path))
    monkeypatch.setattr(sw, "ROUTER_USER", "root")
    return path


@pytest.fixture
def wake(runner: Path) -> Iterator[_Wake]:
    w = _Wake(runner)
    w.wait_for(lambda: "ready" in w.out)
    yield w
    if w.proc.poll() is None:  # a failed test never leaves the tree behind
        for pid in sw.tree(sw.read_procs(), [w.proc.pid]):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        w.proc.wait(timeout=10)


def _stop(
    wake: _Wake, monkeypatch, *, spaced: bool = False, reverse: bool = False
) -> tuple[list[str], float]:
    """Run the real :class:`Stopper` on ``wake``; return its two lines and its wall time.

    ``spaced`` pauses after every signal it sends, so each relayed copy lands — and
    ``runuser`` is asleep — before the next one; ``reverse`` sends them children first.
    Together they force the orderings the 2026-11-03 race could only fall into.
    """
    if reverse:
        real = sw.by_depth
        monkeypatch.setattr(sw, "by_depth", lambda procs, pids: real(procs, pids)[::-1])

    def kill(pid: int, sig: int) -> None:
        os.kill(pid, sig)
        if spaced and sig == signal.SIGTERM:
            time.sleep(SPACING)

    procs = sw.read_procs()
    uid = pwd.getpwnam(AGENT).pw_uid
    wakes = sw.find_wakes(procs, AGENT, uid, DELIVERY, 0)
    assert [w.root.pid for w in wakes] == [wake.proc.pid]
    lines: list[str] = []
    started = time.monotonic()
    survivors = sw.Stopper(read=sw.read_procs, kill=kill, log=lines.append).stop(
        wakes, by="john", grace=10.0
    )
    assert survivors == []
    return lines, time.monotonic() - started


# --- the defect, reproduced ----------------------------------------------------------


def test_a_second_sigterm_inside_runusers_grace_skips_straight_to_sigkill(wake) -> None:
    """runuser itself, no stop-wake: the second copy is what cuts the grace."""
    runuser, _ = wake.chain()
    os.kill(runuser, signal.SIGTERM)
    wake.wait_for(lambda: "begin" in wake.out)  # runuser relayed it, and is about to sleep
    time.sleep(SPACING)  # ...and now is asleep
    cut = time.monotonic()
    os.kill(runuser, signal.SIGTERM)
    assert wake.ended() == -signal.SIGTERM
    assert time.monotonic() - cut < HOLD, "runuser waited out its grace after all"
    assert "end" not in wake.out, wake.out  # the handler never finished
    assert any("...killed." in line for line in wake.err), wake.err


@pytest.mark.parametrize("reverse", [False, True], ids=["parents-first", "children-first"])
def test_the_old_stop_cut_the_grace_whichever_order_its_copies_landed_in(
    wake, monkeypatch, reverse
) -> None:
    """The stop as it was: every member signalled, sudo and the binary included."""
    monkeypatch.setattr(sw, "left_to_relay", lambda procs, pids: set())
    _stop(wake, monkeypatch, spaced=True, reverse=reverse)
    assert wake.ended() == -signal.SIGTERM
    assert "begin" in wake.out
    assert "end" not in wake.out, wake.out  # SIGKILLed inside its own handler
    assert any("...killed." in line for line in wake.err), wake.err


# --- the fix -------------------------------------------------------------------------


def test_the_stop_lets_the_handler_run_to_its_end(wake, monkeypatch) -> None:
    """The fixed stop, spaced exactly as the reproduction above is.

    There is no second order to try here, and that is the fix: the op sends ``runuser``
    its one TERM and sends ``sudo`` and the binary nothing, so no copy exists whose
    landing could fall on either side of ``runuser``'s sleep. What this pins is that the
    one copy is all the chain needed: the binary heard exactly one TERM and finished.
    """
    lines, took = _stop(wake, monkeypatch, spaced=True)
    # sudo died of the TERM runuser re-raised: the daemon reads a stop, never a crash.
    assert wake.ended() == -signal.SIGTERM
    assert wake.out == ["ready", "term", "begin", "end"]  # one copy, handler finished
    assert took >= 2.0, "runuser's 2 s grace was cut short"
    assert "outcome=stopped" in lines[-1]
    assert "signalled=1 killed=0" in lines[-1]  # runuser alone; the relays did the rest


def test_the_real_program_stops_the_real_wake_end_to_end(wake, monkeypatch, capsys) -> None:
    """``main()`` itself, nothing reordered or spaced: the operator's actual command."""
    lines: list[str] = []
    monkeypatch.setattr(sw, "_journal", lines.append)
    assert sw.main(["--agent", AGENT, "--delivery", DELIVERY]) == sw.EXIT_STOPPED
    assert wake.ended() == -signal.SIGTERM
    assert wake.out == ["ready", "term", "begin", "end"]
    requested, done = lines
    assert f"delivery={DELIVERY} agent={AGENT}" in requested
    assert "outcome=stopped signalled=1 killed=0" in done
    assert float(done.rsplit("duration=", 1)[1].rstrip("s")) >= 2.0
