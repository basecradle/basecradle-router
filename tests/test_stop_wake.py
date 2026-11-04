"""``deploy/bin/stop-wake`` — the operator's sanctioned stop, driven offline.

The script is loaded as a module and driven over a fabricated process table: no real
process is ever signalled, and ``/proc`` is a fixture tree under ``tmp_path``. What a real
Linux process chain does with the signals (``sudo`` re-raising, ``runuser`` re-raising
even over a trapping binary, the shared process group) was proven in a container for
basecradle-router#386 and is recorded in the script's docstring; this suite pins the
logic that decides *which* processes are signalled and *what is recorded*.

Cast: Nova Digital (``nova``, uid 1001) is the agent being stopped; John Doe (``john``) is
the operator; the daemon runs as ``router`` (uid 999).
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import signal
import stat
import sys
from pathlib import Path

import pytest

from basecradle_router.models import Agent, Event, EventKind, Recipient
from basecradle_router.wake import DELIVERY_ID_ENV, WAKE_RUNNER, HomeServerWaker

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "stop-wake"


def _load():
    loader = importlib.machinery.SourceFileLoader("stop_wake", str(SCRIPT))
    spec = importlib.util.spec_from_loader("stop_wake", loader)
    module = importlib.util.module_from_spec(spec)
    # Registered before it runs: a dataclass resolves its own module through sys.modules.
    sys.modules["stop_wake"] = module
    loader.exec_module(module)
    return module


sw = _load()

ROUTER_UID = 999
NOVA_UID = 1001
DELIVERY = "0192f3a4-5b6c-7d8e-9f01-00000000000a"
OTHER_DELIVERY = "0192f3a4-5b6c-7d8e-9f01-00000000000b"
NOVA_BIN = "/home/nova/venv/bin/basecradle-harness-wake"


def _wake_argv(user: str = "nova", delivery: str | None = DELIVERY) -> tuple[str, ...]:
    return (
        "sudo",
        WAKE_RUNNER,
        "--user",
        user,
        "--cwd",
        f"/home/{user}/harness",
        *(("--delivery", delivery) if delivery else ()),
        "--",
        f"/home/{user}/venv/bin/basecradle-harness-wake",
        "--timeline",
        "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000",
    )


def _p(pid, ppid, uid, argv, *, start=None, state="S", mark=None):
    return sw.Proc(
        pid=pid,
        ppid=ppid,
        uid=uid,
        start=start or pid * 10,
        state=state,
        argv=tuple(argv),
        delivery=mark,
    )


def _table(**extra) -> dict[int, object]:
    """The daemon, one wake of nova's, and the bystanders a stop must never touch."""
    procs = [
        _p(1, 0, 0, ["/sbin/init"]),
        _p(100, 1, ROUTER_UID, ["/home/router/.local/bin/uv", "run", "uvicorn"]),
        _p(101, 100, ROUTER_UID, ["python3", "-m", "uvicorn"]),  # the daemon
        # sudo — the wake's root. Started by the daemon, so /proc's (real) uid is the
        # router's, exactly as on the box (#404); only its effective uid is root.
        _p(200, 101, ROUTER_UID, _wake_argv()),
        _p(201, 200, 0, ["runuser", "-u", "nova", "--", "/bin/bash", "-c", "…", NOVA_BIN]),
        _p(202, 201, NOVA_UID, [NOVA_BIN, "--timeline", "0192aaaa"], mark=DELIVERY),  # harness
        _p(203, 202, NOVA_UID, ["claude", "-p", "…"], mark=DELIVERY),  # the session's child
        _p(204, 203, NOVA_UID, ["bash", "-c", "pytest"], mark=DELIVERY),  # a tool it runs
        # A tool's `nohup job &` whose shell exited: reparented to init, out of the tree.
        _p(206, 1, NOVA_UID, ["nohup", "long-job"], mark=DELIVERY),
        # Bystanders. A `pkill -f basecradle-harness-wake` would hit 300 and 400.
        _p(300, 1, NOVA_UID, ["tail", "-f", NOVA_BIN]),  # names the binary, not the wake
        _p(301, 1, NOVA_UID, ["old-job"], mark=OTHER_DELIVERY),  # nova's, another wake's mark
        _p(302, 1, 1002, ["forged"], mark=DELIVERY),  # another account naming this delivery
        _p(400, 1, 0, _wake_argv()),  # a wake-runner sudo the DAEMON did not launch
        _p(500, 101, ROUTER_UID, _wake_argv(user="orion", delivery=OTHER_DELIVERY)),
        _p(501, 500, 0, ["runuser", "-u", "orion"]),
    ]
    table = {p.pid: p for p in procs}
    table.update(extra)
    return table


# --- which wake ----------------------------------------------------------------------


def test_the_parser_reads_exactly_the_argv_the_router_assembles() -> None:
    # Not a hand-copied shape: the waker's own invocation, so a change to the argv the
    # router builds that the stop op cannot read fails here, in the same commit.
    agent = Agent(
        key="nova",
        os_user="nova",
        clone_path="/home/nova/harness",
        bot_slug="nova",
    )
    event = Event(
        source="github",
        kind=EventKind.HANDOFF,
        recipient=Recipient(by="repo", value="nova"),
        wake_arg="Cross-repo handoff: work https://example.com/1",
        delivery_id=DELIVERY,
    )
    argv = HomeServerWaker().invocation_for(agent, event).argv
    assert sw.parse_wake(argv) == ("nova", DELIVERY)
    assert sw.WAKE_RUNNER == WAKE_RUNNER


def test_a_wake_without_a_carried_delivery_still_parses() -> None:
    assert sw.parse_wake(_wake_argv(delivery=None)) == ("nova", None)


@pytest.mark.parametrize(
    "argv",
    [
        ("sudo", WAKE_RUNNER, "--user", "nova", "--cwd", "/home/nova/h", "--probe", "BCNOC1 x y"),
        ("sudo", "/tmp/wake-runner", "--user", "nova", "--cwd", "/h", "--", "x"),
        ("sudo", WAKE_RUNNER, "--user", "nova", "--cwd", "/h", "--bogus", "1", "--", "x"),
        ("sudo", WAKE_RUNNER, "--user", "nova", "--cwd", "/h"),  # no command
        ("su", WAKE_RUNNER, "--user", "nova", "--", "x"),
        (WAKE_RUNNER, "--user", "nova", "--", "x"),
        (),
    ],
)
def test_anything_but_a_real_wake_invocation_is_not_a_wake(argv) -> None:
    # A probe acks and exits without launching anything, so there is nothing to stop.
    assert sw.parse_wake(argv) is None


def test_only_the_daemons_own_wake_of_that_agent_is_found() -> None:
    wakes = sw.find_wakes(_table(), "nova", NOVA_UID, None, ROUTER_UID)
    assert [(w.root.pid, w.agent, w.delivery) for w in wakes] == [(200, "nova", DELIVERY)]


def test_delivery_narrows_the_match_and_a_wrong_one_matches_nothing() -> None:
    def find(agent, delivery):
        return [w.root.pid for w in sw.find_wakes(_table(), agent, NOVA_UID, delivery, ROUTER_UID)]

    assert find("nova", DELIVERY) == [200]
    assert find("nova", OTHER_DELIVERY) == []
    assert find("jt", None) == []


def _wake(table=None):
    (wake,) = sw.find_wakes(table or _table(), "nova", NOVA_UID, None, ROUTER_UID)
    return wake


def test_the_tree_is_the_wake_and_everything_beneath_it() -> None:
    assert sw.tree(_table(), [200]) == {200, 201, 202, 203, 204}


def test_the_members_add_the_agents_own_processes_carrying_this_wakes_mark() -> None:
    # 206 left the tree when its parent exited; its environment still says whose it is.
    # 301 is nova's but marked for another delivery; 302 names this delivery from
    # another account. Neither is this wake's.
    assert sw.members(_table(), _wake()) == {200, 201, 202, 203, 204, 206}


def test_a_wake_with_no_carried_delivery_is_stopped_by_its_tree_alone() -> None:
    table = _table()
    table[200] = _p(200, 101, ROUTER_UID, _wake_argv(delivery=None))
    # Unmarked processes of nova's that happen to carry no id must not be matched either.
    table[207] = _p(207, 1, NOVA_UID, ["stray"])
    assert sw.members(table, _wake(table)) == {200, 201, 202, 203, 204}


def test_the_mark_is_the_variable_the_wake_runner_exports() -> None:
    assert sw.DELIVERY_ENV == DELIVERY_ID_ENV
    assert f'export {DELIVERY_ID_ENV}="$1"' in (SCRIPT.parent / "wake-runner").read_text()


def test_the_tree_skips_a_zombie_that_has_already_exited() -> None:
    table = _table()
    table[204] = _p(204, 203, NOVA_UID, ["bash"], state="Z")
    assert sw.tree(table, [200]) == {200, 201, 202, 203}


# --- the stop ----------------------------------------------------------------------


class _Box:
    """A process table that signals mutate — the kernel, as far as the stopper can tell."""

    def __init__(self, table, *, ignores_term=(), spawns_on_term=None) -> None:
        self.table = dict(table)
        self.ignores_term = set(ignores_term)
        self.spawns_on_term = spawns_on_term  # (parent_pid, Proc) — a child born in the window
        self.sent: list[tuple[int, int]] = []
        self.relayed: list[int] = []  # the children runuser signalled itself
        self.lines: list[str] = []
        self.t = 0.0

    def read(self):
        return dict(self.table)

    def kill(self, pid: int, sig: int) -> None:
        if pid not in self.table:
            raise ProcessLookupError(pid)
        self.sent.append((pid, sig))
        self._deliver(pid, sig)

    def _deliver(self, pid: int, sig: int) -> None:
        if sig == signal.SIGTERM and self.spawns_on_term and pid == self.spawns_on_term[0]:
            child = self.spawns_on_term[1]
            self.table[child.pid] = child
            self.spawns_on_term = None
        if sig == signal.SIGTERM and self._named(pid, "runuser"):
            # runuser relays the TERM to its child — the copy the stop leaves to it.
            for child in [p.pid for p in self.table.values() if p.ppid == pid]:
                self.relayed.append(child)
                self._deliver(child, signal.SIGTERM)
        if sig == signal.SIGKILL or pid not in self.ignores_term:
            self._exit(pid)

    def _named(self, pid: int, name: str) -> bool:
        # Which processes really ARE sudo and runuser — the kernel's knowledge, kept apart
        # from stop-wake's own (positional) reading of the tree, so the two can disagree and
        # a test can catch it: the chain's links run as root or as the router; a process of
        # the agent's that calls itself sudo or runuser is neither, and relays nothing.
        proc = self.table[pid]
        return proc.uid in (0, ROUTER_UID) and Path(proc.argv[0]).name == name

    def _exit(self, pid: int) -> None:
        parent = self.table.pop(pid).ppid
        # sudo exits the way its command did, so it goes when its last child goes.
        if (
            parent in self.table
            and self._named(parent, "sudo")
            and not any(p.ppid == parent for p in self.table.values())
        ):
            self._exit(parent)

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def now(self) -> float:
        return self.t

    def stopper(self):
        return sw.Stopper(
            read=self.read, kill=self.kill, log=self.lines.append, sleep=self.sleep, now=self.now
        )


def _stop(box: _Box, *, grace: float = 10.0, delivery=None) -> list[int]:
    wakes = sw.find_wakes(box.read(), "nova", NOVA_UID, delivery, ROUTER_UID)
    return box.stopper().stop(wakes, by="john", grace=grace)


def test_a_stop_terms_the_whole_tree_and_touches_no_bystander() -> None:
    box = _Box(_table())
    assert _stop(box) == []
    # sudo (200) and runuser's child (202) are left to the wake's own relays (#404).
    assert {pid for pid, _ in box.sent} == {201, 203, 204, 206}
    assert {sig for _, sig in box.sent} == {signal.SIGTERM}
    assert box.relayed == [202]
    assert not {200, 201, 202, 203, 204, 206} & set(box.table)  # the whole wake is gone
    # The daemon, the other agent's wake, the stray sudo, the pkill decoy, nova's process
    # for another delivery, and the forged mark on another account all live.
    for bystander in (100, 101, 300, 301, 302, 400, 500, 501):
        assert bystander in box.table


def test_no_member_is_sent_a_sigterm_a_relay_already_delivers() -> None:
    # #404: sudo relays a TERM to runuser, and a second TERM inside runuser's 2 s sleep
    # skips it straight to SIGKILL; runuser relays one to its child. So neither sudo nor
    # runuser's child is signalled, and the op sends no member a second copy.
    box = _Box(_table())
    _stop(box)
    sent = [pid for pid, _ in box.sent]
    assert 200 not in sent  # sudo: dies the way runuser does
    assert 202 not in sent and box.relayed == [202]  # the binary: runuser's copy only
    assert len(sent) == len(set(sent))


def test_the_wakes_sudo_is_a_relay_by_its_place_in_the_tree_whatever_its_uid() -> None:
    # The box's shape: /proc's uid for the wake's sudo is the router's (999), not root's.
    # Reading a relay off its uid left that sudo signalled, and its relayed copy was
    # runuser's second TERM on the box (#404). Its position as the wake's root is the test.
    box = _Box(_table())
    assert box.table[200].uid == ROUTER_UID
    _stop(box)
    assert 200 not in {pid for pid, _ in box.sent}
    assert "signalled=4" in box.lines[-1]


def test_a_link_that_forks_instead_of_execing_does_not_hide_runuser() -> None:
    # Today wake-runner and systemd-cat exec straight through, so sudo's child IS runuser.
    # Should a link ever fork instead, the path runs through it: runuser is still found,
    # and its child is still left to it. The link itself is signalled like any member.
    table = _table()
    table[210] = _p(210, 200, 0, ["/bin/bash", WAKE_RUNNER])  # a forking wake-runner
    table[201] = _p(201, 210, 0, ["runuser", "-u", "nova", "--", "/bin/bash"])
    wake = _wake(table)
    assert sw.left_to_relay(table, wake, sw.members(table, wake)) == {200, 202}


def test_a_pty_monitor_sudo_is_left_to_relay_too() -> None:
    # A host whose sudoers ran the command under a pty would put a second sudo between
    # the root and runuser. It relays exactly as the root does, so it is spared the same.
    table = _table()
    table[201] = _p(201, 200, ROUTER_UID, ["sudo", WAKE_RUNNER, "--user", "nova"])
    table[207] = _p(207, 201, 0, ["runuser", "-u", "nova", "--", "/bin/bash"])
    table[202] = _p(202, 207, NOVA_UID, [NOVA_BIN], mark=DELIVERY)
    box = _Box(table)
    _stop(box)
    assert {pid for pid, _ in box.sent} == {207, 203, 204, 206}


def test_a_sudo_child_caught_before_its_exec_is_signalled_once_it_is_runuser() -> None:
    # The stop lands between sudo's fork and its child's exec: the child is still `sudo`,
    # a relay, so it is left alone. When it has become runuser it must get its TERM, or
    # nothing in the chain is signalled and the binary loses its grace to the SIGKILL.
    table = _table()
    del table[202], table[203], table[204]
    table[201] = _p(201, 200, ROUTER_UID, _wake_argv())  # forked, not yet exec'd
    box = _Box(table, ignores_term={206})
    reads = 0
    real_read = box.read

    def read():
        nonlocal reads
        reads += 1
        if reads == 3 and 201 in box.table:  # the exec lands during the grace period
            box.table[201] = _p(201, 200, 0, ["runuser", "-u", "nova"], start=2010)
        return real_read()

    stopper = box.stopper()
    stopper.read = read
    wakes = sw.find_wakes(real_read(), "nova", NOVA_UID, None, ROUTER_UID)
    stopper.stop(wakes, by="john", grace=5.0)
    assert (201, signal.SIGTERM) in box.sent
    assert box.sent.count((201, signal.SIGTERM)) == 1


def test_a_process_of_the_agents_that_names_itself_a_relay_is_still_signalled() -> None:
    # Any process in the session can call itself `sudo` or `runuser`; only root-owned ones
    # are the chain's relays. A forged name must not opt it, or its children, out.
    table = _table()
    table[204] = _p(204, 203, NOVA_UID, ["runuser", "fake"], mark=DELIVERY)
    table[205] = _p(205, 204, NOVA_UID, ["sudo", "-n", "true"], mark=DELIVERY)
    table[208] = _p(208, 205, NOVA_UID, ["tool"], mark=DELIVERY)
    box = _Box(table)
    _stop(box)
    assert {204, 205, 208} <= {pid for pid, sig in box.sent if sig == signal.SIGTERM}


def test_a_relay_name_off_the_path_from_the_root_is_an_ordinary_member() -> None:
    # A process of the session's that is a real sudo or runuser binary — setuid root, so
    # the kernel itself would call it one — is still not one of the wake's relays: it is
    # not on the path down from the wake's root, and it gets its TERM like any member.
    table = _table()
    table[204] = _p(204, 203, 0, ["sudo", "-n", "true"], mark=DELIVERY)
    table[208] = _p(208, 204, 0, ["runuser", "-u", "nova", "--", "tool"], mark=DELIVERY)
    table[209] = _p(209, 208, NOVA_UID, ["tool"], mark=DELIVERY)
    wake = _wake(table)
    assert sw.left_to_relay(table, wake, sw.members(table, wake)) == {200, 202}
    box = _Box(table)
    _stop(box)
    assert {204, 208} <= {pid for pid, sig in box.sent if sig == signal.SIGTERM}


def test_runuser_is_signalled_before_anything_beneath_it_even_when_pids_wrap() -> None:
    # runuser's TERM must be pending before anything below it can exit, or it passes the
    # binary's plain status up and the daemon reads a crash, not a stop. Pid order is not
    # that order once pids wrap: here every descendant has a LOWER pid than runuser.
    table = _table()
    table[900] = _p(900, 200, 0, ["runuser", "-u", "nova", "--", "/bin/bash"])
    del table[201]
    table[202] = _p(202, 900, NOVA_UID, [NOVA_BIN], mark=DELIVERY)
    box = _Box(table, ignores_term={900})  # runuser lingers, as it does, in its 2 s sleep
    _stop(box)
    order = [pid for pid, sig in box.sent if sig == signal.SIGTERM]
    beneath = sw.tree(table, [900]) - {900}
    assert beneath & set(order) == {203, 204}
    assert all(order.index(900) < order.index(pid) for pid in (203, 204)), order


def test_a_member_left_to_relay_that_outlives_the_grace_is_killed() -> None:
    # The binary ignores the TERM runuser relays; sudo waits on it. Neither was sent a
    # TERM by the stop, and neither escapes it: both are watched and SIGKILLed.
    box = _Box(_table(), ignores_term={202})
    box.table[201] = _p(201, 200, 0, ["runuser", "-u", "nova"])  # stays until its child goes
    box.ignores_term.add(201)
    assert _stop(box, grace=1.0) == []
    assert (202, signal.SIGKILL) in box.sent
    assert (200, signal.SIGKILL) in box.sent
    assert (200, signal.SIGTERM) not in box.sent and (202, signal.SIGTERM) not in box.sent


def test_a_child_born_while_the_tree_drains_is_stopped_too() -> None:
    # The session spawns a tool as it handles its TERM, and lingers past it.
    late = _p(205, 203, NOVA_UID, ["node", "tool.js"])
    box = _Box(_table(), ignores_term={203}, spawns_on_term=(203, late))
    assert _stop(box, grace=1.0) == []
    assert (205, signal.SIGTERM) in box.sent
    assert 205 not in box.table


def test_whatever_outlives_the_grace_period_is_killed() -> None:
    box = _Box(_table(), ignores_term={204})
    assert _stop(box, grace=1.0) == []
    assert box.sent.count((204, signal.SIGKILL)) == 1
    assert all(sig == signal.SIGTERM for pid, sig in box.sent if pid != 204)
    done = box.lines[-1]
    assert "outcome=killed" in done
    assert "killed=1" in done


def test_a_reused_pid_is_never_signalled() -> None:
    # 204 exits during the grace period and an unrelated process is born on its pid.
    box = _Box(_table(), ignores_term={204})
    stopper = box.stopper()
    original_sleep = box.sleep

    def reuse_then_sleep(seconds: float) -> None:
        original_sleep(seconds)
        if box.table.get(204) is not None and box.table[204].start == 2040:
            box.table[204] = _p(204, 1, 0, ["cron"], start=999_999)

    stopper.sleep = reuse_then_sleep
    wakes = sw.find_wakes(box.read(), "nova", NOVA_UID, None, ROUTER_UID)
    assert stopper.stop(wakes, by="john", grace=1.0) == []
    assert (204, signal.SIGKILL) not in box.sent
    assert box.table[204].argv == ("cron",)


def test_the_stop_records_who_stopped_which_wake_on_both_lines() -> None:
    box = _Box(_table())
    _stop(box)
    requested, done = box.lines
    assert requested == (
        f"event=wake_stop_requested delivery={DELIVERY} agent=nova by=john signal=TERM pids=6"
    )
    # pids= counts every member; signalled= only those this op sent a signal itself.
    assert done.startswith(
        f"event=wake_stop_done delivery={DELIVERY} agent=nova by=john outcome=stopped "
        "signalled=4 killed=0 duration="
    )


def test_a_process_that_survives_even_sigkill_is_named() -> None:
    box = _Box(_table(), ignores_term={204})
    real_kill = box.kill

    def unkillable(pid: int, sig: int) -> None:
        if pid == 204 and sig == signal.SIGKILL:
            box.sent.append((pid, sig))  # uninterruptible sleep: the signal is pending
            return
        real_kill(pid, sig)

    stopper = box.stopper()
    stopper.kill = unkillable
    wakes = sw.find_wakes(box.read(), "nova", NOVA_UID, None, ROUTER_UID)
    assert stopper.stop(wakes, by="john", grace=1.0) == [204]
    assert "outcome=survived" in box.lines[-1]
    assert "survivors=204" in box.lines[-1]


def test_each_wake_reports_its_own_verdict_when_two_are_stopped_at_once() -> None:
    # Two wakes for one agent (one lingering beside a new one): one drains on TERM, the
    # other needs SIGKILL. Each delivery's done line must say what happened to *it*.
    table = _table()
    table[600] = _p(600, 101, ROUTER_UID, _wake_argv(delivery=OTHER_DELIVERY))
    table[601] = _p(601, 600, NOVA_UID, [NOVA_BIN], mark=OTHER_DELIVERY)
    box = _Box(table, ignores_term={601})
    assert _stop(box, grace=1.0) == []
    done = {line.split()[1]: line for line in box.lines if "wake_stop_done" in line}
    assert "outcome=stopped signalled=4 killed=0" in done[f"delivery={DELIVERY}"]
    # 301 carries OTHER_DELIVERY too, so it is that wake's member and stopped with it. The
    # lingering wake's sudo, never sent a TERM, is killed with the child it still waits on.
    assert "outcome=killed signalled=3 killed=2" in done[f"delivery={OTHER_DELIVERY}"]


# --- /proc parsing ---------------------------------------------------------------


def _write_proc(
    root: Path, pid: int, *, comm: str, ppid: int, uid: int, argv: list[str], env=()
) -> None:
    d = root / str(pid)
    d.mkdir()
    # Fields after the comm: state ppid pgrp session tty tpgid flags minflt cminflt majflt
    # cmajflt utime stime cutime cstime priority nice threads itrealvalue starttime …
    rest = ["S", str(ppid)] + ["0"] * 17 + ["4242"] + ["0"] * 10
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(rest) + "\n")
    (d / "status").write_text(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    if env is not None:
        (d / "environ").write_bytes(b"".join(f"{e}".encode() + b"\0" for e in env))


def test_proc_is_read_with_a_comm_that_contains_spaces_and_parens(tmp_path) -> None:
    _write_proc(tmp_path, 200, comm="sudo", ppid=101, uid=0, argv=list(_wake_argv()))
    _write_proc(
        tmp_path,
        202,
        comm="a) (b c",
        ppid=201,
        uid=NOVA_UID,
        argv=[NOVA_BIN],
        env=["HOME=/home/nova", f"{DELIVERY_ID_ENV}={DELIVERY}", "X=1"],
    )
    _write_proc(tmp_path, 203, comm="kthread", ppid=2, uid=0, argv=[], env=None)
    (tmp_path / "self").mkdir()  # non-numeric entries are not processes
    procs = sw.read_procs(str(tmp_path))
    assert set(procs) == {200, 202, 203}
    assert procs[200].argv == _wake_argv()
    assert (procs[200].delivery, procs[202].delivery, procs[203].delivery) == (None, DELIVERY, None)
    assert (procs[202].ppid, procs[202].uid, procs[202].start, procs[202].state) == (
        201,
        NOVA_UID,
        4242,
        "S",
    )


def test_an_unreadable_environment_is_known_unknown_not_silently_unmarked(tmp_path) -> None:
    _write_proc(tmp_path, 202, comm="harness", ppid=201, uid=NOVA_UID, argv=[NOVA_BIN])
    environ = tmp_path / "202" / "environ"
    environ.chmod(0)
    try:
        proc = sw.read_procs(str(tmp_path))[202]
    finally:
        environ.chmod(0o600)
    if proc.marked_known:  # root reads a mode-0 file: nothing to prove as root
        pytest.skip("running as root, which reads a mode-0 file")
    assert proc.delivery is None


def test_the_operator_is_warned_when_the_marks_could_not_be_read(monkeypatch, capsys) -> None:
    table = _table()
    table[203] = sw.Proc(203, 202, NOVA_UID, 2030, "S", ("claude",), None, marked_known=False)
    monkeypatch.setattr(sw.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        sw.pwd,
        "getpwnam",
        lambda n: type("P", (), {"pw_uid": ROUTER_UID if n == "router" else NOVA_UID}),
    )
    box = _Box(table)
    monkeypatch.setattr(sw, "read_procs", lambda *a: box.read())
    monkeypatch.setattr(sw.syslog, "openlog", lambda *a: None)
    monkeypatch.setattr(sw.syslog, "syslog", lambda *a: None)
    monkeypatch.setattr(sw.os, "kill", box.kill)
    monkeypatch.setattr(sw.time, "sleep", lambda _s: None)
    assert sw.main(["--agent", "nova", "--grace", "0"]) == sw.EXIT_STOPPED
    err = capsys.readouterr().err
    assert "could not read 1 of nova's process environments" in err


def test_a_process_that_vanished_mid_read_is_skipped(tmp_path) -> None:
    (tmp_path / "300").mkdir()  # stat already gone
    assert sw.read_procs(str(tmp_path)) == {}


# --- the command line ---------------------------------------------------------------


def test_it_refuses_to_run_as_anyone_but_root(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sw.os, "geteuid", lambda: 999)
    assert sw.main(["--agent", "nova"]) == sw.EXIT_NOT_ROOT
    assert "must run as root" in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        ["--agent", "nova; rm -rf /"],
        ["--agent", "../nova"],
        ["--agent", "nova", "--delivery", "id with spaces"],
        ["--agent", "nova", "--grace", "-1"],
        [],
    ],
)
def test_malformed_arguments_are_refused_before_anything_is_read(args, monkeypatch) -> None:
    monkeypatch.setattr(sw, "read_procs", lambda *a: pytest.fail("read /proc on bad input"))
    with pytest.raises(SystemExit) as excinfo:
        sw.main(args)
    assert excinfo.value.code == 2


def test_no_matching_wake_is_its_own_exit_code(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sw.os, "geteuid", lambda: 0)
    monkeypatch.setattr(sw.pwd, "getpwnam", lambda _n: type("P", (), {"pw_uid": ROUTER_UID}))
    monkeypatch.setattr(sw, "read_procs", lambda *a: _table())
    assert sw.main(["--agent", "jt"]) == sw.EXIT_NO_WAKE
    assert "no running wake matched agent=jt" in capsys.readouterr().err


def test_the_operator_is_sudos_record_of_the_caller(monkeypatch) -> None:
    monkeypatch.setenv("SUDO_USER", "john")
    assert sw._operator() == "john"


def test_the_script_is_executable_and_runs_isolated_python() -> None:
    assert SCRIPT.stat().st_mode & stat.S_IXUSR
    # -I: a root-run script must never import from the caller's cwd or PYTHONPATH.
    assert SCRIPT.read_text().splitlines()[0] == "#!/usr/bin/python3 -I"
