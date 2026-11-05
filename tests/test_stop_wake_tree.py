"""``deploy/bin/stop-wake`` against a REAL process tree: real ``sudo``, real ``runuser``.

The offline suite (``test_stop_wake.py``) pins which processes are signalled over a
fabricated table. What it cannot pin is what the real links of the chain then do with
those signals, and that is where basecradle-router#404 lived: ``runuser`` catches a
SIGTERM, signals its child, and sleeps 2 s before SIGKILL, and a second SIGTERM landing
inside that sleep cuts it short (util-linux 2.39 ``su-common.c``). The old stop sent
``runuser`` two — its own, and ``sudo``'s relayed copy — and on 2026-11-03 the copy won the
race and an ``@jt`` wake was SIGKILLed 3 ms into its grace.

So this suite builds the wake's real shape — ``daemon -> sudo -> <wake-runner> -> runuser ->
bash -> <binary>`` — and stops it with the real program. The daemon is a NON-ROOT account
(``daemon``) that reaches ``sudo`` through a locked sudoers rule copied from the box's own
(``deploy/sudoers/basecradle-router``), so the wake's ``sudo`` is what it is on the box: the
daemon's process, whose /proc uid is the daemon's and only whose effective uid is root. A
``sudo`` started as root would hide any reading of the tree that leans on that uid. The
stand-ins left,
each for a reason that has nothing to do with signals: the daemon is a few lines of
Python that spawns ``sudo`` and dies the way it did; the wake-runner is a few lines that
keep its shape (``exec runuser``, the delivery mark exported after the drop) without its
registry gates; and the agent is ``nobody``. The binary traps SIGTERM, holds its handler
for :data:`HOLD` — well inside ``runuser``'s 2 s — and reports every SIGTERM it receives,
so a test reads both whether the handler ran to its end and how many copies of the signal
reached it.

It needs root (to install and remove the sudoers rule), Linux, ``sudo``, ``visudo``,
``runuser`` and the ``daemon`` and ``nobody`` accounts, so it skips on the laptop and
anywhere else that lacks them. CI runs it as root in its own step with
``STOP_WAKE_TREE_REQUIRED=1``, which turns a skip into a failure: a proof that silently did
not run is not a proof.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import pwd
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

import pytest

from test_stop_wake import sw

AGENT = "nobody"
#: The daemon's stand-in account: anything but root, as `router` is on the box.
DAEMON = "daemon"
SUDOERS = Path(__file__).resolve().parents[1] / "deploy" / "sudoers" / "basecradle-router"
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
    for tool in ("sudo", "visudo", "runuser"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    # The boxes run the original sudo (1.9.15p5 on Ubuntu 24.04). A different one — sudo-rs,
    # Ubuntu 26.04's default — relays signals by its own code, so a pass would prove a stop
    # against a sudo no box runs: not this suite's claim, so it does not run.
    banner = subprocess.run(["sudo", "-V"], capture_output=True, text=True).stdout
    if not banner.startswith("Sudo version 1."):
        return f"sudo is not the boxes' sudo: {banner.splitlines()[:1]}"
    for account in (AGENT, DAEMON):
        try:
            if pwd.getpwnam(account).pw_uid == 0:
                return f"the {account!r} account is root here"
        except KeyError:
            return f"no {account!r} account"
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
# The one account it drops to. The sudoers rule granting it outlives a killed run, so the
# most a leftover grant can ever do is run a command as this account.
[ "$user" = nobody ] || { echo "fake wake-runner: drops only to nobody" >&2; exit 1; }
exec runuser -u "$user" -- /usr/bin/env "BASECRADLE_DELIVERY_ID=$delivery" \
	/bin/bash -c 'exec "$@"' wake-runner "$@"
"""

# The daemon's stand-in, run as DAEMON: spawns the wake's sudo as its child, as the router
# does, and dies the way sudo did, so the test reads the exit status the daemon would.
DAEMON_STANDIN = r"""
import os, signal, subprocess, sys
code = subprocess.call(sys.argv[1:])
if code < 0:
    signal.signal(-code, signal.SIG_DFL)
    os.kill(os.getpid(), -code)
sys.exit(code)
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
    """One real wake: ``sudo`` spawned by the daemon's stand-in, its output read as it lands."""

    def __init__(self, runner: Path) -> None:
        self.proc = subprocess.Popen(
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                DAEMON_STANDIN,
                "sudo",  # the daemon's exact argv: stdin is /dev/null, so a prompt fails
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
            user=DAEMON,
            group=pwd.getpwnam(DAEMON).pw_gid,
            extra_groups=[],
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

    def sudo(self, procs: Mapping[int, sw.Proc]) -> int:
        """The wake's ``sudo`` in ``procs``: the daemon stand-in's one child."""
        below = [p for p in procs.values() if p.ppid == self.proc.pid]
        assert len(below) == 1, f"the daemon has {len(below)} children: {below}"
        return below[0].pid

    def chain(self) -> tuple[int, int]:
        """``(runuser, binary)`` pids under this wake's ``sudo``, read from /proc.

        It also pins the box's shape: ``sudo`` is the daemon's process, not root's."""
        procs = sw.read_procs()
        sudo = procs[self.sudo(procs)]
        assert sudo.uid == pwd.getpwnam(DAEMON).pw_uid, sudo
        below = [p for p in procs.values() if p.ppid == sudo.pid]
        assert len(below) == 1, f"sudo has {len(below)} children: {below}"
        runuser = below[0]
        assert os.path.basename(runuser.argv[0]) == "runuser", runuser.argv
        binary = [p for p in procs.values() if p.ppid == runuser.pid]
        assert len(binary) == 1, f"runuser has {len(binary)} children: {binary}"
        assert binary[0].uid == pwd.getpwnam(AGENT).pw_uid
        return runuser.pid, binary[0].pid

    def ended(self, timeout: float = 15.0) -> int:
        """``sudo``'s exit status, as the daemon reads it, once every line has landed."""
        code = self.proc.wait(timeout=timeout)
        for drain in self._drains:
            drain.join(timeout=timeout)  # EOF: every process holding the pipes is gone
        return code


DROPIN_PREFIX = "zz-basecradle-router-test-"


def _rule(runner: Path) -> str:
    """The box's sudoers rule, for DAEMON and this test's runner, and nothing else.

    Comments go, every ``router`` token becomes DAEMON — a ``Defaults:router`` line added
    to the box's rule one day comes along rather than silently staying the router's — and
    the grant is pinned to the runner's exact bytes (``sha256:``), so it cannot run
    anything else that is put at that path."""
    lines = [
        line
        for line in SUDOERS.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    rule = "\n".join(lines).replace(sw.WAKE_RUNNER, str(runner)) + "\n"
    rule = re.sub(r"(?<![\w/.-])router(?![\w.-])", DAEMON, rule)
    assert "router" not in rule.replace(str(runner), ""), rule
    digest = hashlib.sha256(runner.read_bytes()).hexdigest()
    grant = f"{DAEMON} ALL=(root) NOPASSWD: {runner}"
    assert grant in rule, rule
    return rule.replace(grant, f"{DAEMON} ALL=(root) NOPASSWD: sha256:{digest} {runner}")


@pytest.fixture
def runner(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    """The fake wake-runner, and the box's sudoers rule granting DAEMON exactly it.

    The rule is the one write outside ``tmp_path`` in this repo's tests, and it cannot be
    avoided: the box's own sudoers rule, granting a non-root account the wake-runner, is
    part of the shape under test. So it is the narrowest write that does it: a uniquely
    named ``/etc/sudoers.d`` drop-in, checked by ``visudo`` before it is installed and
    removed on every exit. A run killed before its ``finally`` cannot leave it for long:
    every drop-in carrying this suite's prefix is swept when the next run starts, and until
    then it grants only the fake runner's own bytes, which drop only to ``nobody``."""
    for stale in Path("/etc/sudoers.d").glob(f"{DROPIN_PREFIX}*"):
        stale.unlink(missing_ok=True)  # a killed run's: this suite's prefix, nobody else's
    path = tmp_path / "wake-runner"
    path.write_text(FAKE_RUNNER)
    path.chmod(0o755)
    staged = tmp_path / "sudoers"
    staged.write_text(_rule(path))
    staged.chmod(0o440)
    subprocess.run(["visudo", "-cqf", str(staged)], check=True)
    dropin = Path("/etc/sudoers.d") / f"{DROPIN_PREFIX}{secrets.token_hex(4)}"
    try:
        shutil.copy2(staged, dropin)
        # The real program's two constants, pointed at this test's tree: the wake-runner
        # it matches a wake by, and the account the daemon runs as.
        monkeypatch.setattr(sw, "WAKE_RUNNER", str(path))
        monkeypatch.setattr(sw, "ROUTER_USER", DAEMON)
        yield path
    finally:
        dropin.unlink(missing_ok=True)


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
    wakes = sw.find_wakes(procs, AGENT, uid, DELIVERY, pwd.getpwnam(DAEMON).pw_uid)
    assert [w.root.pid for w in wakes] == [wake.sudo(procs)]
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
    monkeypatch.setattr(sw, "left_to_relay", lambda procs, wake, pids: set())
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
