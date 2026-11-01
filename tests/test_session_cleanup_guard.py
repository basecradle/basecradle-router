"""A laptop builder session leaves the laptop as it found it (basecradle-noc#790).

**This file is shared, byte for byte, by every laptop builder** (basecradle-noc#1156), beside the
scripts it tests, and anchored in ``basecradle-noc``. A change is made there and re-synced by
handoff, never edited in a carrier. The one value that differs per carrier, the repo's name, is read
from the carrier's own ``.claude/session-guard.env`` by the rule the guard itself uses, so the label
and builder prefix below are this checkout's, never a constant naming one carrier. The bytes pass
every carrier's lint and every Python the carriers test on (3.10 and up).

On 2026-09-20 @origin found **Docker Desktop still running** and a **Chrome tab still open**,
both left by builder sessions that had already self-exited. The engine had degraded to answering
500 to every API call, so nothing could even be listed; a second session, an engine restart and
an app update were the price of cleaning up after a session that was already gone.

*Whatever creates, cleans up* says every leftover has an owner and a stated end and that the
cleanup ships with the thing that creates it. A session that has self-exited cannot clean up, so
the cleanup has to happen **before** the exit and be **checked** there. ``.claude/self-exit.sh``
now runs ``.claude/session-guard.sh done`` first and refuses on a finding.

Four properties are pinned here, each of which fails silently if it is dropped:

* **The fail-closed hinge.** ``check`` refuses when there is **no baseline** — the state a session
  is in when its ``SessionStart`` hook never fired. A guard that no-ops when it cannot run is the
  silent death itself (``basecradle-noc#190``), and without this the *whole* instrument reads clean
  on the one session that never armed it.
* **Prove-it-is-ours, or report.** The sweep removes only what carries this session's own label
  or builder prefix. Everything else that appeared during the session is **reported with the exact
  command and never touched** — an image somebody else pulled mid-session is theirs. The capital's
  ruling on this issue: *an empty Docker today is not a licence for ``docker system prune``.*
* **A stop is read back off the process list.** ``osascript quit app "Docker"`` exited 0 while
  every Docker process kept running (``basecradle-noc#716``). An exit code is not a stopped
  process, so a quit that "succeeds" while the processes live must NOT be reported as stopped.
* **The predicates are EXECUTED, never grepped.** A test asserting a label filter *appears* in the
  source cannot tell a filter that fires from one that never does (``basecradle-noc#773``). So the
  guard runs against a stub ``docker`` that records every argv, and the assertions are about what it
  was — and was not — asked to remove. The stub also carries a **positive control**: an unlabelled
  container and image sitting right beside the labelled ones, which the sweep must leave alone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / ".claude" / "session-guard.sh"
SELF_EXIT = REPO / ".claude" / "self-exit.sh"
SETTINGS = REPO / ".claude" / "settings.json"

CONFIG = REPO / ".claude" / "session-guard.env"
_REPO_LINE = re.compile(r"SESSION_GUARD_REPO=([a-z0-9][a-z0-9.-]*)")


def _configured_repo(text: str) -> str:
    """The repo a ``session-guard.env`` names, by the guard's own ``read_repo`` rule: comment and
    blank lines dropped, then exactly one ``SESSION_GUARD_REPO=<repo>`` line and nothing else. A
    config the guard would refuse fails the suite at import, never quietly yields some label."""
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    match = _REPO_LINE.fullmatch(lines[0]) if len(lines) == 1 else None
    if match is None:
        raise ValueError(f"{CONFIG} must hold exactly one line SESSION_GUARD_REPO=<repo>")
    return match.group(1)


# This carrier's own values, derived exactly as the guard derives them, so the same bytes serve
# every carrier (basecradle-noc#1156).
REPO_NAME = _configured_repo(CONFIG.read_text())
LABEL = f"{REPO_NAME}-builder=1"
BUILDER_PREFIX = f"{REPO_NAME}-builder-"

# A `docker` that answers from a JSON scenario, mutates it on a removal, and records every argv.
# Executing the guard against this is the only way to tell a filter that fires from one that does
# not — and the calls log is what lets a test assert what was NEVER asked for.
DOCKER_STUB = r"""#!/usr/bin/env python3
import json, os, sys

state_path = os.environ["DOCKER_STUB_STATE"]
with open(state_path) as fh:
    st = json.load(fh)
with open(os.environ["DOCKER_STUB_CALLS"], "a") as fh:
    fh.write("\t".join(sys.argv[1:]) + "\n")


def save():
    with open(state_path, "w") as fh:
        json.dump(st, fh)


a = sys.argv[1:]
labelled = "--filter" in a and f"label={os.environ['DOCKER_STUB_LABEL']}" in a

if a[:1] == ["version"]:
    if not st["engine_up"]:
        sys.stderr.write("Cannot connect to the Docker daemon\n")
        sys.exit(1)
    print("29.8.0")
elif a[:2] == ["desktop", "status"]:
    print("Name                Value")
    print("Status              %s" % st["desktop_status"])
elif a[:2] == ["desktop", "start"]:
    st["desktop_status"] = "running"
    st["engine_up"] = True
    if "ps_lines_after_start" in st:
        st["ps_lines"] = st["ps_lines_after_start"]
    save()
elif a[:2] == ["desktop", "stop"]:
    # The stub always REPORTS success. Whether the processes actually go away is the
    # scenario's `desktop_stop_really_stops` — that gap is basecradle-noc#716 itself.
    st["desktop_status"] = "stopped" if st.get("desktop_stop_really_stops", True) else "running"
    if st.get("desktop_stop_really_stops", True):
        st["engine_up"] = False
        st["ps_lines"] = []
    save()
elif a[:1] == ["ps"]:
    if not st["engine_up"]:
        sys.exit(1)
    if st.get("ps_kills_group") and "-a" in a:
        # The hook's timeout, simulated: the whole census is killed mid-run.
        import signal
        os.killpg(os.getpgrp(), signal.SIGKILL)
    ident = next((x.split("=", 1)[1] for x in a if x.startswith("id=")), None)
    for c in st["containers"]:
        if ident is not None:
            if c["id"] == ident:
                print("%s (%s)" % (c["image"], c["status"]))
        elif labelled:
            if c["labelled"]:
                print(c["id"])
        else:
            print(c["id"])
elif a[:3] == ["image", "ls", "-a"] or a[:3] == ["image", "ls", "-aq"]:
    if not st["engine_up"]:
        sys.exit(1)
    long_form = any("Repository" in x for x in a)
    for i in st["images"]:
        if labelled:
            if i["labelled"]:
                print(i["id"])
        elif long_form:
            print("%s %s" % (i["id"], i["tag"]))
        else:
            print(i["id"])
elif a[:2] == ["volume", "ls"]:
    if not st["engine_up"]:
        sys.exit(1)
    for v in st["volumes"]:
        print(v)
elif a[:2] == ["buildx", "ls"]:
    if not st["engine_up"]:
        sys.exit(1)
    print("NAME/NODE           DRIVER/ENDPOINT     STATUS    BUILDKIT   PLATFORMS")
    for i, b in enumerate(st["builders"]):
        print("%s%s      docker" % (b, "*" if i == 0 else ""))
        print(" \\_ %s0       \\_ desktop-linux   running   v0.33.0    linux/arm64" % b)
elif a[:2] == ["system", "df"]:
    if not st["engine_up"]:
        sys.exit(1)
    if st.get("df_failures", 0) > 0:
        # An engine still settling: `docker version` answers, `system df` does not, yet.
        st["df_failures"] -= 1
        save()
        sys.exit(1)
    print('{"Active":"0","Reclaimable":"0B","Size":"0B","TotalCount":"0","Type":"Images"}')
    print('{"Active":"0","Reclaimable":"0B","Size":"%s","TotalCount":"0","Type":"Build Cache"}'
          % st["build_cache"])
elif a[:1] == ["rm"]:
    st["containers"] = [c for c in st["containers"] if c["id"] not in a]
    save()
elif a[:1] == ["rmi"]:
    if st.get("rmi_fails"):
        sys.stderr.write("conflict: image is being used by a container\n")
        sys.exit(1)
    st["images"] = [i for i in st["images"] if i["id"] not in a]
    save()
elif a[:2] == ["buildx", "rm"]:
    st["builders"] = [b for b in st["builders"] if b not in a]
    save()
else:
    sys.stderr.write("stub docker: unhandled %r\n" % (a,))
    sys.exit(2)
"""

# `ps -Ao pid=,comm=` answers from the scenario, so the Docker-Desktop process probe is exercised
# on any OS — including a CI runner that has never seen /Applications.
#
# `ps -o etime= -p PID` answers from the scenario's `proc_start` (pid -> epoch), rendered the way
# the real `ps` renders it — [[dd-]hh:]mm:ss, zero-padded — so the guard's own parser is what turns
# it back into a start time. A pid the scenario does not know prints nothing and exits 1, which is
# what a process that has already exited looks like.
PS_STUB = r"""#!/usr/bin/env python3
import json, os, sys, time

with open(os.environ["DOCKER_STUB_STATE"]) as fh:
    st = json.load(fh)
a = sys.argv[1:]
if "-Ao" in a:
    for line in st.get("ps_lines", []):
        print(line)
elif "lstart=" in a and "-p" in a:
    # A process's fixed start-time string, the second half of the guard's session key.
    start = st.get("lstart", {}).get(a[a.index("-p") + 1])
    if start is None:
        sys.exit(1)
    if isinstance(start, int):
        # An epoch, rendered as the real ps renders it: in the caller's time zone.
        time.tzset()
        start = time.strftime("%a %b %e %H:%M:%S %Y", time.localtime(start))
    print(start)
elif "etime=" in a and "-p" in a:
    start = st.get("proc_start", {}).get(a[a.index("-p") + 1])
    if start is None:
        sys.exit(1)
    e = max(0, int(time.time()) - int(start))
    d, e = divmod(e, 86400)
    h, e = divmod(e, 3600)
    m, s = divmod(e, 60)
    if d:
        print("%d-%02d:%02d:%02d" % (d, h, m, s))
    elif h:
        print("%02d:%02d:%02d" % (h, m, s))
    else:
        print("   %02d:%02d" % (m, s))
"""

FAKE_APP_PREFIX = "/Fake/Docker.app/"


def scenario(**over):
    base = {
        "engine_up": True,
        "desktop_status": "running",
        "desktop_stop_really_stops": True,
        "containers": [],
        "images": [],
        "volumes": [],
        "builders": ["desktop-linux"],
        "build_cache": "0B",
        "ps_lines": [f"  4242 {FAKE_APP_PREFIX}Contents/MacOS/com.docker.backend"],
        "listeners": [],
    }
    base.update(over)
    return base


# The lab's `lsof`: prints the listeners the scenario declares, in `lsof -F pcn` form.
LSOF_STUB = r"""#!/usr/bin/env python3
import json, os
import sys
with open(os.environ["DOCKER_STUB_STATE"]) as fh:
    st = json.load(fh)
if st.get("lsof_error"):
    # The real lsof's failure: exit 1, the same status as "nothing matched", with its reason on
    # stderr (measured on macOS's lsof 4.91).
    sys.stderr.write("lsof: can't get UID for nosuchuser\n")
    sys.stderr.flush()
    if st.get("lsof_err_raced"):
        # A sibling run under the same key removing a shared stderr file mid-census.
        root = os.environ["SESSION_GUARD_STATE_ROOT"]
        for name in os.listdir(root):
            path = os.path.join(root, name, "lsof.err")
            if os.path.exists(path):
                os.remove(path)
    sys.exit(1)
if not st.get("listeners"):
    sys.exit(1)  # nothing matched: exit 1, silent
for pid, cmd, addr in st.get("listeners", []):
    print("p%s" % pid)
    print("c%s" % cmd)
    print("n%s" % addr)
"""


def _pinned(stub: str) -> str:
    """The stub with its interpreter pinned to the one running the suite.

    ``#!/usr/bin/env python3`` resolves ``python3`` through ``PATH`` at exec time, and on a laptop
    that is a version-manager shim reading its own config from ``$HOME``. A test that points
    ``HOME`` at a temp dir (``test_a_carriers_label_prefix_and_state_root_come_from_its_config``)
    then gets a shim that exits 126 with nothing on stderr, so the ``docker`` stub never answers,
    the guard reads "no Docker engine", sweeps nothing, and the test fails on the laptop while CI
    passes. Measured 2026-10-31 under asdf (basecradle-noc#1119 passed it by). Pinning the
    interpreter takes ``PATH`` and ``HOME`` out of the stub's own startup.
    """
    first, sep, rest = stub.partition("\n")
    assert first == "#!/usr/bin/env python3", first
    return f"#!{sys.executable}{sep}{rest}"


class Lab:
    """One isolated laptop: a stub docker + ps, a private state root, one session key."""

    def __init__(self, tmp_path: Path, state: dict):
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        (self.bin / "docker").write_text(_pinned(DOCKER_STUB))
        (self.bin / "ps").write_text(_pinned(PS_STUB))
        # lsof too, ALWAYS: without the stub the listener census reads this account's REAL
        # listening ports, so a port that opens or closes anywhere on the machine between `begin`
        # and `check` (another test's local server, any app) flips a test that has nothing to do
        # with listeners. Seen as an intermittent failure in the full suite.
        (self.bin / "lsof").write_text(_pinned(LSOF_STUB))
        for name in ("docker", "ps", "lsof"):
            (self.bin / name).chmod(0o755)
        self.state_file = tmp_path / "docker-state.json"
        self.state_file.write_text(json.dumps(state))
        self.calls = tmp_path / "docker-calls.log"
        self.calls.write_text("")
        self.state_root = tmp_path / "sessions"

    @property
    def env(self) -> dict:
        env = dict(os.environ)
        # The suite runs inside Claude Code sessions too, whose CLAUDE_PID would key the guard's
        # state on the real session; a test that wants a process key sets its own.
        env.pop("CLAUDE_PID", None)
        env.update(
            {
                "PATH": f"{self.bin}:{env['PATH']}",
                "DOCKER_STUB_STATE": str(self.state_file),
                "DOCKER_STUB_CALLS": str(self.calls),
                "DOCKER_STUB_LABEL": LABEL,
                "SESSION_GUARD_STATE_ROOT": str(self.state_root),
                "SESSION_GUARD_DOCKER_APP_PREFIX": FAKE_APP_PREFIX,
                "CLAUDE_CODE_SESSION_ID": "test-session",
                "SESSION_GUARD_STOP_WAIT": "2",
                "SESSION_GUARD_START_WAIT": "2",
                "SESSION_GUARD_POLL": "1",
            }
        )
        return env

    def run(self, *args: str, script: Path = GUARD) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(script), *args],
            env=self.env,
            capture_output=True,
            text=True,
            cwd=self.tmp,
            check=False,
        )

    def docker_state(self) -> dict:
        return json.loads(self.state_file.read_text())

    def calls_log(self) -> list[list[str]]:
        return [ln.split("\t") for ln in self.calls.read_text().splitlines() if ln]

    def mutate(self, **over) -> None:
        st = self.docker_state()
        st.update(over)
        self.state_file.write_text(json.dumps(st))

    def clean_tabs(self) -> None:
        """Satisfy the browser half so a Docker assertion is not masked by it."""
        assert self.run("tabs-checked").returncode == 0


@pytest.fixture
def lab(tmp_path):
    def build(**over):
        return Lab(tmp_path, scenario(**over))

    return build


CONTAINER = {"id": "c" * 64, "image": "ubuntu:24.04", "status": "Up 3 seconds", "labelled": True}
FOREIGN_CONTAINER = {"id": "f" * 64, "image": "someone/else", "status": "Up", "labelled": False}
IMAGE = {"id": "sha256:" + "a" * 64, "tag": "noc/replica:latest", "labelled": True}
FOREIGN_IMAGE = {"id": "sha256:" + "b" * 64, "tag": "postgres:17", "labelled": False}


# ------------------------------------------------------------------ the fail-closed hinge


def test_check_refuses_when_the_session_never_took_a_baseline(lab):
    """The state a session is in when its SessionStart hook never fired.

    Without this the whole instrument reads clean on the one session that never armed it —
    ``basecradle-noc#190``'s rule (*an ERROR is not clean*) turned on the guard itself.
    """
    got = lab().run("check")
    assert got.returncode != 0
    assert "no session baseline" in got.stdout
    assert "begin" in got.stdout


def test_self_exit_refuses_while_the_guard_refuses_and_never_reaches_the_kill(lab):
    """The exit path is the enforcement point, so it is executed, not read.

    The state root is empty, which the test above proves ``check`` cannot pass on — so the kill is
    unreachable here by the same property, not by hope.
    """
    got = lab().run(script=SELF_EXIT)
    assert got.returncode != 0
    assert "REFUSING to exit" in got.stderr
    assert "terminating" not in got.stdout


def test_self_exit_refuses_when_the_guard_itself_is_missing(tmp_path):
    """A guard that has been deleted must not read as a guard that passed."""
    shadow = tmp_path / ".claude"
    shadow.mkdir()
    exit_copy = shadow / "self-exit.sh"
    shutil.copy(SELF_EXIT, exit_copy)
    exit_copy.chmod(0o755)
    got = subprocess.run([str(exit_copy)], capture_output=True, text=True, check=False)
    assert got.returncode != 0
    assert "refusing to exit without a cleanup check" in got.stderr
    assert "terminating" not in got.stdout


# --------------------------------------------------------- prove-it-is-ours, or report


def test_a_labelled_container_blocks_the_exit_and_the_sweep_removes_it(lab):
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(containers=[CONTAINER])
    lb.clean_tabs()

    got = lb.run("check")
    assert got.returncode != 0
    assert CONTAINER["id"] in got.stdout

    assert lb.run("sweep").returncode == 0
    assert lb.docker_state()["containers"] == []
    assert lb.run("check").returncode == 0


def test_the_sweep_only_ever_removes_what_this_session_labelled(lab):
    """The positive control: unlabelled artefacts sit right beside the labelled ones.

    The capital's ruling on this issue — *a sweep only ever touches what the session labelled as
    its own; an empty Docker today is not a licence for ``docker system prune``.*
    """
    lb = lab(
        containers=[FOREIGN_CONTAINER],
        images=[FOREIGN_IMAGE],
        builders=["desktop-linux", "someone-elses-builder"],
    )
    assert lb.run("begin").returncode == 0
    # ...and now the session creates its own, on top of the foreign ones.
    lb.mutate(
        containers=[FOREIGN_CONTAINER, CONTAINER],
        images=[FOREIGN_IMAGE, IMAGE],
        builders=["desktop-linux", "someone-elses-builder", f"{BUILDER_PREFIX}demo"],
    )
    assert lb.run("sweep").returncode == 0

    st = lb.docker_state()
    assert [c["id"] for c in st["containers"]] == [FOREIGN_CONTAINER["id"]]
    assert [i["id"] for i in st["images"]] == [FOREIGN_IMAGE["id"]]
    assert st["builders"] == ["desktop-linux", "someone-elses-builder"]

    removals = [c for c in lb.calls_log() if c[0] in {"rm", "rmi"} or c[:2] == ["buildx", "rm"]]
    removed = {arg for call in removals for arg in call}
    assert FOREIGN_CONTAINER["id"] not in removed
    assert FOREIGN_IMAGE["id"] not in removed
    assert "someone-elses-builder" not in removed
    assert not any("prune" in arg for call in lb.calls_log() for arg in call)


def test_an_unlabelled_artefact_created_during_the_session_is_reported_and_never_removed(lab):
    """An image somebody else pulled mid-session is theirs — reported, with the exact command."""
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(images=[FOREIGN_IMAGE], containers=[FOREIGN_CONTAINER], volumes=["new-volume"])
    lb.clean_tabs()

    got = lb.run("check")
    assert got.returncode != 0
    assert FOREIGN_IMAGE["id"] in got.stdout
    assert FOREIGN_CONTAINER["id"] in got.stdout
    assert "new-volume" in got.stdout
    assert "only if it is yours" in got.stdout

    assert lb.run("sweep").returncode == 0
    st = lb.docker_state()
    assert [i["id"] for i in st["images"]] == [FOREIGN_IMAGE["id"]]
    assert [c["id"] for c in st["containers"]] == [FOREIGN_CONTAINER["id"]]
    assert st["volumes"] == ["new-volume"]


def test_a_labelled_artefact_is_not_reported_twice(lab):
    """It is this session's to sweep, not an unattributable stray — one finding, one repair."""
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(containers=[CONTAINER], images=[IMAGE])
    lb.clean_tabs()
    out = lb.run("check").stdout
    assert out.count(CONTAINER["id"]) == 1
    assert out.count(IMAGE["id"]) == 1
    assert "only if it is yours" not in out


def test_what_was_already_there_at_session_start_is_not_a_finding(lab):
    """The baseline is what keeps the guard from blaming a session for the laptop it inherited."""
    lb = lab(containers=[FOREIGN_CONTAINER], images=[FOREIGN_IMAGE], volumes=["pre-existing"])
    assert lb.run("begin").returncode == 0
    lb.clean_tabs()
    assert lb.run("check").returncode == 0


def test_a_build_on_a_shared_builder_is_reported(lab):
    """The 233 MB of orphaned build cache the capital swept, caught at its source.

    A cache record carries no label we control, so this reports and points at the throwaway
    builder — whose ``docker buildx rm`` takes its whole cache with it.
    """
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(build_cache="233MB")
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode != 0
    assert "233MB" in got.stdout
    assert BUILDER_PREFIX in got.stdout


# ------------------------------------------------------------------------ Docker Desktop


def test_docker_desktop_started_by_this_session_is_quit_and_the_stop_is_read_back(lab):
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[])
    assert lb.run("begin").returncode == 0
    assert lb.run("docker-up").returncode == 0
    lb.mutate(ps_lines=[f"  4242 {FAKE_APP_PREFIX}Contents/MacOS/com.docker.backend"])
    lb.clean_tabs()

    got = lb.run("check")
    assert got.returncode != 0
    assert "started Docker Desktop and it is still running" in got.stdout

    swept = lb.run("sweep")
    assert swept.returncode == 0, swept.stderr
    assert "no process under" in swept.stdout
    assert lb.run("check").returncode == 0


def test_a_quit_that_exits_zero_while_the_processes_live_is_not_reported_as_stopped(lab):
    """``basecradle-noc#716``: an ``osascript quit`` exited 0 while every Docker process kept
    running.

    An exit code is not a stopped process; the process list is. So the stub's ``desktop stop``
    succeeds and the processes stay — and the sweep must FAIL and say so, never claim a stop.
    """
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[])
    assert lb.run("begin").returncode == 0
    assert lb.run("docker-up").returncode == 0
    lb.mutate(
        desktop_stop_really_stops=False,
        ps_lines=[f"  4242 {FAKE_APP_PREFIX}Contents/MacOS/com.docker.backend"],
    )

    swept = lb.run("sweep")
    assert swept.returncode != 0
    assert "STILL RUNNING" in swept.stderr
    assert "no process under" not in swept.stdout
    # ...and the session is still held, because the flag was never cleared.
    lb.clean_tabs()
    assert lb.run("check").returncode != 0


def test_docker_desktop_this_session_did_not_start_is_reported_and_never_quit(lab):
    """It may be @origin's. Report, ask — never quit what cannot be proved to be ours."""
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[])
    assert lb.run("begin").returncode == 0
    lb.mutate(
        engine_up=True,
        desktop_status="running",
        ps_lines=[f"  4242 {FAKE_APP_PREFIX}Contents/MacOS/com.docker.backend"],
    )
    lb.clean_tabs()

    got = lb.run("check")
    assert got.returncode != 0
    assert "did not record starting it" in got.stdout
    assert lb.run("sweep").returncode == 0
    assert ["desktop", "stop"] not in lb.calls_log()


def test_docker_desktop_that_was_running_at_session_start_is_left_running(lab):
    """The issue's rule, in its quiet direction: if it was already up, this session leaves it up."""
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.clean_tabs()
    assert lb.run("check").returncode == 0
    assert lb.run("sweep").returncode == 0
    assert ["desktop", "stop"] not in lb.calls_log()
    assert lb.docker_state()["desktop_status"] == "running"


def test_docker_desktop_stopped_during_the_session_is_a_finding_too(lab):
    """ "As it found it" runs both ways — the laptop is not left more stopped than it was."""
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(engine_up=False, desktop_status="stopped", ps_lines=[])
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode != 0
    assert "was running at session start and is not now" in got.stdout


def test_an_engine_that_died_mid_session_cannot_certify_anything(lab):
    """The 2026-09-20 state exactly: the engine answering 500, nothing listable.

    "We could not look" is not "there is nothing there" — the founding rule of this repo.
    """
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(engine_up=False)
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode != 0
    assert "not reachable" in got.stdout


# ------------------------------------------------------------------------- browser tabs


def test_a_recorded_open_tab_blocks_the_exit_until_it_is_closed(lab):
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.clean_tabs()
    assert lb.run("tab-open", "tab-7", "docs.example").returncode == 0

    got = lb.run("check")
    assert got.returncode != 0
    assert "tab-7" in got.stdout

    assert lb.run("tab-close", "tab-7").returncode == 0
    lb.clean_tabs()
    assert lb.run("check").returncode == 0


def test_the_browser_check_is_mandatory_even_when_no_tab_was_recorded(lab):
    """The record cannot catch a tab nobody recorded, so the LOOK is what is required.

    Gating this on "did the session record a tab" would make the guard inert in exactly the case
    it exists for — the session that opened one and forgot to write it down.
    """
    lb = lab()
    assert lb.run("begin").returncode == 0
    got = lb.run("check")
    assert got.returncode != 0
    assert "browser was never checked" in got.stdout
    lb.clean_tabs()
    assert lb.run("check").returncode == 0


def test_touching_the_browser_after_the_last_check_invalidates_it(lab):
    """Keyed on tab ACTIVITY, not on the rows still open — the first spelling went blind here.

    ``tab-close`` deletes the row, so a check that read the open set saw nothing left and called a
    session clean although it had opened a tab *after* attesting that the browser held none of its
    own. A session that opened one tab late may have opened an unrecorded one too, so the look is
    stale either way and the close is no exemption.
    """
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.clean_tabs()
    assert lb.run("tab-open", "tab-9").returncode == 0
    assert lb.run("tab-close", "tab-9").returncode == 0
    got = lb.run("check")
    assert got.returncode != 0
    assert "after the last browser check" in got.stdout
    lb.clean_tabs()
    assert lb.run("check").returncode == 0


# --------------------------------------------------------------------- wiring and shape


def test_done_clears_the_state_only_on_a_clean_check(lab):
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(containers=[CONTAINER])
    lb.clean_tabs()
    assert lb.run("done").returncode != 0
    assert (lb.state_root / "test-session").is_dir()

    assert lb.run("sweep").returncode == 0
    assert lb.run("done").returncode == 0
    assert not (lb.state_root / "test-session").exists()


def test_the_session_start_hook_is_wired_and_its_command_takes_the_baseline(tmp_path, lab):
    """``begin`` is a hook, not a step to remember — so the hook's own command is EXECUTED here.

    The stdin payload is the shape Claude Code sends, and the assertion is that the baseline lands
    under the ``session_id`` the payload carries: a hook that wrote under some other key would be
    invisible to every later ``check`` in the session, which is a guard that silently never ran.
    """
    settings = json.loads(SETTINGS.read_text())
    entries = settings["hooks"]["SessionStart"]
    commands = [h["command"] for entry in entries for h in entry["hooks"] if h["type"] == "command"]
    assert any("session-guard.sh" in c and "begin" in c for c in commands)
    command = next(c for c in commands if "session-guard.sh" in c)

    lb = lab()
    env = lb.env
    env["CLAUDE_PROJECT_DIR"] = str(REPO)
    env.pop("CLAUDE_CODE_SESSION_ID")
    got = subprocess.run(
        ["bash", "-c", command],
        input=json.dumps({"session_id": "from-stdin", "hook_event_name": "SessionStart"}),
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )
    assert got.returncode == 0, got.stderr
    assert (lb.state_root / "from-stdin" / "began_at").is_file()


def test_self_exit_runs_the_cleanup_check_before_the_kill(lab):
    """Order is the property: a kill that happens first cannot be refused after the fact."""
    source = SELF_EXIT.read_text()
    code = [ln for ln in source.splitlines() if not ln.lstrip().startswith("#")]
    guard_at = next(i for i, ln in enumerate(code) if "session-guard.sh" in ln or '"$guard"' in ln)
    kill_at = next(i for i, ln in enumerate(code) if ln.strip().startswith("kill -TERM"))
    assert guard_at < kill_at


def test_the_guard_reaches_for_no_blanket_prune_and_no_override_flag(lab):
    """Two absences, each of which would quietly undo the design.

    A blanket prune would sweep whatever else shares this Docker; an override flag would put
    *"I mean to leave this behind"* and *"I forgot"* one byte apart (``basecradle-noc#481``'s ruling
    on ``retract-model-params`` in the NOC's wrapper). Comment lines are stripped, because this file
    *describes* both in prose and the commentary is not what runs.
    """
    for path in (GUARD, SELF_EXIT):
        code = "\n".join(
            ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")
        )
        assert "prune" not in code, path
        assert "--force" not in code, path


# ------------------------------------------------------------------ listening ports (#899)


def _with_lsof(lab_obj, listeners):
    lab_obj.mutate(listeners=listeners)
    return lab_obj


def _began_epoch(lab_obj) -> int:
    """The baseline's own clock, read back from the state ``begin`` wrote."""
    return int((lab_obj.state_root / "test-session" / "began_epoch").read_text())


def test_a_dev_server_started_during_the_session_blocks_the_exit_and_is_never_killed(lab):
    """The capital, on @origin's word (#899): what a session launched on the laptop is the
    session's to close — a dev server or a listening port included. Read off the kernel, reported
    with the exact command, never killed by the guard."""
    lab_obj = _with_lsof(lab(), [["616", "ControlCe", "*:7000"]])
    assert lab_obj.run("begin").returncode == 0
    lab_obj.clean_tabs()
    lab_obj.mutate(
        listeners=[["616", "ControlCe", "*:7000"], ["5150", "node", "127.0.0.1:3000"]],
        proc_start={"616": _began_epoch(lab_obj) - 8 * 3600, "5150": _began_epoch(lab_obj)},
    )
    done = lab_obj.run("check")
    assert done.returncode == 1
    assert "node (pid 5150) on 127.0.0.1:3000" in done.stdout
    assert "kill 5150" in done.stdout
    assert "ControlCe" not in done.stdout  # there at session start: not a finding
    swept = lab_obj.run("sweep")
    assert "kill" not in " ".join(" ".join(c) for c in lab_obj.calls_log())
    assert swept.returncode == 0


def test_a_listener_that_restarted_under_a_new_pid_is_not_a_new_one(lab):
    lab_obj = _with_lsof(lab(), [["616", "ControlCe", "*:7000"]])
    assert lab_obj.run("begin").returncode == 0
    lab_obj.clean_tabs()
    lab_obj.mutate(listeners=[["9001", "ControlCe", "*:7000"]])
    done = lab_obj.run("check")
    assert "started listening" not in done.stdout


# Elapsed times that exercise every `etime` shape the real `ps` prints — mm:ss, hh:mm:ss and
# dd-hh:mm:ss — each with a zero-padded "08"/"09" field, which bash reads as a bad octal number
# unless the parser forces base 10.
@pytest.mark.parametrize("elapsed", [9 * 60 + 8, 8 * 3600 + 9 * 60 + 8, 3 * 86400 + 8 * 3600 + 9])
def test_a_process_older_than_the_session_that_moved_to_a_new_port_is_not_reported(lab, elapsed):
    """``basecradle-noc#1073``: macOS's ``rapportd`` runs for hours and rotates its ephemeral port.

    By address alone it read as session-started, and the only repair the guard offered was to kill
    a system daemon the session does not own — so ``self-exit.sh`` refused with nothing it could
    legitimately do. Newness is judged by the PROCESS: one that predates the baseline is not this
    session's, whatever port it holds now.
    """
    lab_obj = _with_lsof(lab(), [["718", "rapportd", "*:54001"]])
    assert lab_obj.run("begin").returncode == 0
    lab_obj.clean_tabs()
    lab_obj.mutate(
        listeners=[["718", "rapportd", "*:54558"]],
        proc_start={"718": _began_epoch(lab_obj) - elapsed},
    )
    done = lab_obj.run("check")
    assert done.returncode == 0, done.stdout
    assert "started listening" not in done.stdout
    assert "not this session's: rapportd (pid 718)" in done.stdout


def test_a_process_older_than_the_session_that_starts_listening_is_not_reported(lab):
    """The DoD's rule in full: a process that predates the baseline is never reported, including
    one that held no port at all when the baseline was taken."""
    lab_obj = _with_lsof(lab(), [])
    assert lab_obj.run("begin").returncode == 0
    lab_obj.clean_tabs()
    lab_obj.mutate(
        listeners=[["718", "rapportd", "*:54558"]],
        proc_start={"718": _began_epoch(lab_obj) - 3600},
    )
    assert lab_obj.run("check").returncode == 0


def test_a_new_listener_beside_an_old_process_on_a_new_port_is_still_reported(lab):
    """The narrowing must not buy quiet by going blind: the pre-existing daemon is excused and the
    dev server started in the same window is reported, by pid, with its kill line."""
    lab_obj = _with_lsof(lab(), [["718", "rapportd", "*:54001"]])
    assert lab_obj.run("begin").returncode == 0
    lab_obj.clean_tabs()
    began = _began_epoch(lab_obj)
    lab_obj.mutate(
        listeners=[["718", "rapportd", "*:54558"], ["5150", "node", "127.0.0.1:3000"]],
        proc_start={"718": began - 3600, "5150": began + 5},
    )
    done = lab_obj.run("check")
    assert done.returncode == 1
    assert "node (pid 5150) on 127.0.0.1:3000" in done.stdout
    assert "kill 5150" in done.stdout
    assert "kill 718" not in done.stdout


def test_a_new_listener_whose_start_time_cannot_be_read_is_not_clean(lab):
    """``ps`` had nothing for the pid (it exited, or the form was not recognised): "we could not
    look" is never read as "it predates the session"."""
    lab_obj = _with_lsof(lab(), [])
    assert lab_obj.run("begin").returncode == 0
    lab_obj.clean_tabs()
    lab_obj.mutate(listeners=[["5150", "node", "127.0.0.1:3000"]], proc_start={})
    done = lab_obj.run("check")
    assert done.returncode == 1
    assert "start time could not be read: node (pid 5150)" in done.stdout


def test_a_session_with_no_listener_baseline_is_not_clean_until_it_attests_one(lab):
    lab_obj = _with_lsof(lab(), [["616", "ControlCe", "*:7000"]])
    assert lab_obj.run("begin").returncode == 0
    lab_obj.clean_tabs()
    for f in lab_obj.state_root.rglob("listeners.base"):
        f.unlink()  # a session that began before the census existed
    assert "no listening-port baseline" in lab_obj.run("check").stdout
    late = lab_obj.run("baseline-listeners")
    assert late.returncode == 0 and "LATE" in late.stdout
    after = lab_obj.run("check")
    assert after.returncode == 0 and "not-clean" not in after.stdout, after.stdout


def test_no_lsof_means_we_could_not_look_which_is_never_clean(lab):
    lab_obj = lab()
    (lab_obj.bin / "lsof").unlink()  # the lab's own stub goes, and the system's is hidden below
    env = lab_obj.env
    env["PATH"] = f"{lab_obj.bin}"  # nothing but the stubs: lsof is absent
    for tool in (
        "bash",
        "date",
        "mkdir",
        "rm",
        "find",
        "sort",
        "comm",
        "cut",
        "sed",
        "awk",
        "wc",
        "tr",
        "cat",
        "id",
        "head",
        "grep",
        "env",
        "python3",
        "dirname",
        "basename",
    ):
        path = shutil.which(tool)
        if path:
            (lab_obj.bin / tool).symlink_to(path)

    def run(*a):
        return subprocess.run(
            [str(GUARD), *a], env=env, capture_output=True, text=True, cwd=lab_obj.tmp, check=False
        )

    assert run("begin").returncode == 0
    assert run("tabs-checked").returncode == 0
    assert "could not be censused" in run("check").stdout


# ------------------------------------------- the router port's findings (basecradle#707, #1106)
#
# basecradle-router#358 ported this guard and found six defects in the shape; each fix is mirrored
# here with a test that goes red when the fix is reverted.


def test_a_second_session_start_keeps_the_baseline_and_what_the_session_recorded(lab):
    """The hook fires on resume, ``/clear`` and compaction too. Re-taking the baseline would erase
    the record that this session started Docker Desktop, and the sweep would leave it running."""
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[])
    assert lb.run("begin").returncode == 0
    assert lb.run("docker-up").returncode == 0
    again = lb.run("begin")
    assert again.returncode == 0
    assert "kept" in again.stdout
    assert (lb.state_root / "test-session" / "docker_started_by_session").read_text() == "1\n"


def test_a_census_cut_off_part_way_leaves_no_baseline(lab):
    """``began_at`` is the "a baseline exists" marker, so it is written last. A census that dies
    mid-run must leave ``check`` refusing, never a baseline that reads complete without its
    ``.base`` files (which would make every new container invisible)."""
    lb = lab(ps_kills_group=True)
    got = subprocess.run(
        [str(GUARD), "begin"],
        env=lb.env,
        capture_output=True,
        text=True,
        start_new_session=True,
        check=False,
    )
    assert got.returncode < 0, "the stub was meant to kill the census"
    lb.mutate(ps_kills_group=False)
    check = lb.run("check")
    assert check.returncode != 0
    assert "no session baseline" in check.stdout


def test_a_running_desktop_whose_engine_is_not_answering_is_never_claimed(lab):
    """Running but not answering is the incident's own state (``basecradle-noc#790``). Claiming that
    start would let ``sweep`` quit an app that was there before the session."""
    lb = lab(engine_up=False)
    assert lb.run("begin").returncode == 0
    got = lb.run("docker-up")
    assert got.returncode != 0
    assert "already running" in got.stderr
    assert not (lb.state_root / "test-session" / "docker_started_by_session").exists()
    assert ["desktop", "start"] not in lb.calls_log()


def test_a_live_session_is_not_swept_as_stale_by_another_sessions_start(lab):
    """The stale sweep keys on the state directory's mtime, which an overwrite does not move — so
    every write touches it, and a long session that only overwrites stays alive."""
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.clean_tabs()
    live = lb.state_root / "test-session"
    old = time.time() - 30 * 86400
    os.utime(live, (old, old))
    lb.clean_tabs()  # an overwrite of tabs_checked, not a new entry
    env = lb.env | {"CLAUDE_CODE_SESSION_ID": "another-session"}
    other = subprocess.run(
        [str(GUARD), "begin"], env=env, capture_output=True, text=True, check=False
    )
    assert other.returncode == 0, other.stderr
    assert live.is_dir()


def test_a_failed_removal_fails_the_sweep_and_keeps_the_engine_up(lab):
    """A sweep that could not remove something is not complete, and quitting the engine under it
    would hide the leftover from the ``check`` that follows."""
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[], images=[IMAGE], rmi_fails=True)
    assert lb.run("begin").returncode == 0
    assert lb.run("docker-up").returncode == 0
    got = lb.run("sweep")
    assert got.returncode != 0
    assert "INCOMPLETE" in got.stderr
    assert ["desktop", "stop"] not in lb.calls_log()


def test_self_exit_keeps_the_state_when_it_cannot_find_its_kill_target(lab):
    """``done`` clears the state, so it runs only once the kill target is in hand; a refused exit
    must leave the baseline for the next attempt."""
    lb = lab()
    # Safety precondition: under the lab's stub ``ps`` the ancestry walk sees nothing, so this
    # test can never find, let alone signal, a real ``claude`` process.
    walk = subprocess.run(
        ["ps", "-o", "command=", "-p", str(os.getpid())],
        env=lb.env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert walk.stdout.strip() == ""
    assert lb.run("begin").returncode == 0
    lb.clean_tabs()
    got = lb.run(script=SELF_EXIT)
    assert got.returncode != 0
    assert "no ancestor 'claude' process found" in got.stderr
    assert (lb.state_root / "test-session" / "began_at").is_file()


def test_docker_desktop_status_is_read_when_no_process_sits_under_the_bundle(lab):
    """With no process under the bundle path, ``docker desktop status`` alone decides whether
    Docker Desktop was running, and a stop is still seen."""
    lb = lab(ps_lines=[])
    assert lb.run("begin").returncode == 0
    lb.mutate(desktop_status="stopped")
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode != 0
    assert "was running at session start and is not now" in got.stdout


def test_the_guard_never_pipes_into_a_short_circuiting_consumer():
    """Both scripts run under ``pipefail``, where ``… | grep -q`` or ``… | head`` returns 141
    whenever the producer is still writing when the consumer quits, and a MATCH reads as no match
    (``basecradle-noc#205``; the same ban the NOC's ``test_fleet_deploy_runner`` holds on its
    wrapper). Capture, then match. Comment lines are stripped, because the comments name the trap to
    warn about it.

    The pattern carries its own positive control: a ``\\b`` after the ``q`` once let
    ``| grep -qE`` through, the very shape this guard used to carry."""
    pattern = re.compile(
        r"(?<!\|)\|(?!\|)[^|\n]*\b(?:grep\s+(?:-[a-zA-Z]*[qm]|--quiet|--max-count)|head\b)"
    )
    for shape in (
        "x | grep -q y",
        "x | grep -qE y",
        "x | grep -Eq y",
        "x | grep -m1 y",
        "x | head -1",
    ):
        assert pattern.search(shape), shape
    assert not pattern.search('[[ -r f ]] || ! grep -qE "^k=" f')  # `||` is not a pipe
    for path in (GUARD, SELF_EXIT):
        text = path.read_text()
        assert "set -uo pipefail" in text, path
        code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
        offenders = [m.group(0) for m in pattern.finditer(code)]
        assert not offenders, f"{path.name}: {offenders}"


def test_the_guard_scripts_are_shellcheck_clean():
    """``"$guard" done`` read to shellcheck as a stray loop keyword (SC1010). Every shellcheck
    finding fails here, not only that one. CI's runner image carries shellcheck, so there it is
    required; a laptop without it skips."""
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        assert not os.environ.get("CI"), "shellcheck is missing on the CI runner"
        pytest.skip("shellcheck is not installed")
    got = subprocess.run(
        [shellcheck, str(GUARD), str(SELF_EXIT)], capture_output=True, text=True, check=False
    )
    assert got.returncode == 0, got.stdout


# ------------------------------------------- the open design questions (basecradle#707 a-d, #1106)


def test_a_clear_keeps_the_baseline_because_the_key_is_the_claude_process(lab):
    """(d): ``/clear`` gives the same ``claude`` process a new session id, in the hook payload and
    in ``CLAUDE_CODE_SESSION_ID`` alike. Keyed on the session id, the hook then took a fresh
    baseline and orphaned the record that this session started Docker Desktop, which then sat in
    the new baseline as pre-existing and was never quit — the #790 leftover. Keyed on the process
    (``CLAUDE_PID`` plus its start time), the hook's re-fire finds the baseline and keeps it."""
    lb = lab(
        engine_up=False,
        desktop_status="stopped",
        ps_lines=[],
        lstart={"4711": "Thu Oct 22 00:12:56 2026"},
    )
    env = lb.env | {"CLAUDE_PID": "4711", "CLAUDE_CODE_SESSION_ID": "before-clear"}

    def run(*a):
        return subprocess.run(
            [str(GUARD), *a], env=env, capture_output=True, text=True, cwd=lb.tmp, check=False
        )

    assert run("begin", "--session-id", "before-clear").returncode == 0
    assert run("docker-up").returncode == 0

    env["CLAUDE_CODE_SESSION_ID"] = "after-clear"
    again = run("begin", "--session-id", "after-clear")
    assert again.returncode == 0
    assert "kept" in again.stdout
    swept = run("sweep")
    assert swept.returncode == 0, swept.stderr
    assert ["desktop", "stop"] in lb.calls_log()


def test_a_recycled_pid_is_a_different_process_and_takes_its_own_baseline(lab):
    """The process key carries the start time, so a later process that drew the same pid never
    inherits (or keeps) a dead session's baseline."""
    lb = lab(lstart={"4711": "Thu Oct 22 00:12:56 2026"})
    env = lb.env | {"CLAUDE_PID": "4711"}

    def run(*a):
        return subprocess.run(
            [str(GUARD), *a], env=env, capture_output=True, text=True, cwd=lb.tmp, check=False
        )

    assert run("begin").returncode == 0
    lb.mutate(lstart={"4711": "Fri Oct 23 09:00:00 2026"})
    got = run("begin")
    assert "kept" not in got.stdout
    assert sorted(d.name for d in lb.state_root.iterdir()) == [
        "claude-4711-FriOct230900002026",
        "claude-4711-ThuOct220012562026",
    ]


def test_a_census_failure_at_check_time_is_never_read_as_no_listeners(lab):
    """(a): ``lsof`` exits 1 both for "nothing matched" and for a failure, so the pipe the census
    used to be read a failure as an empty census, and a dev server the session left behind as
    clean. Its stderr is what tells them apart."""
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(lsof_error=True, listeners=[["9001", "node", "127.0.0.1:3000"]])
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode != 0
    assert "census failed" in got.stdout


def test_a_census_failure_at_session_start_leaves_no_listener_baseline(lab):
    """(a), at ``begin``: a failed census must not be recorded as a baseline of nothing."""
    lb = lab(lsof_error=True)
    assert lb.run("begin").returncode == 0
    assert not (lb.state_root / "test-session" / "listeners.base").exists()
    lb.mutate(lsof_error=False)
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode != 0
    assert "no listening-port baseline" in got.stdout


def test_an_engine_down_at_session_start_is_censused_by_docker_up(lab):
    """(b): with the engine down at ``begin`` there is nothing to list, and comparing against that
    empty list read every image already on the laptop as this session's — with ``docker rmi`` of
    someone else's image as the repair — the moment ``docker-up`` brought the engine up."""
    lb = lab(
        engine_up=False,
        desktop_status="stopped",
        ps_lines=[],
        images=[FOREIGN_IMAGE],
        containers=[FOREIGN_CONTAINER],
    )
    assert lb.run("begin").returncode == 0
    assert lb.run("docker-up").returncode == 0
    lb.clean_tabs()
    got = lb.run("check")
    assert FOREIGN_IMAGE["id"] not in got.stdout
    assert FOREIGN_CONTAINER["id"] not in got.stdout
    lb.mutate(images=[FOREIGN_IMAGE, {**IMAGE, "labelled": False}])
    assert IMAGE["id"] in lb.run("check").stdout  # what came after the census still shows


def test_an_engine_that_came_up_from_outside_is_a_finding_never_blamed_on_what_was_there(lab):
    """(b), as decided on basecradle#709 item 3: an engine that came up after ``begin`` without a
    ``docker-up`` has no baseline to compare against. The labelled checks still run and nothing
    that was already there is blamed — but the gap is a FINDING, because "we could not look" is
    never clean and a session that used the engine out of protocol would otherwise exit clean. Its
    lever is a late census, which every later ``check`` names."""
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[])
    assert lb.run("begin").returncode == 0
    lb.mutate(engine_up=True, images=[FOREIGN_IMAGE, IMAGE])
    lb.clean_tabs()
    got = lb.run("check")
    assert FOREIGN_IMAGE["id"] not in got.stdout
    assert f"labelled image still present: {IMAGE['id']}" in got.stdout

    lb.mutate(images=[FOREIGN_IMAGE])  # the labelled finding is not what makes this one fail
    got = lb.run("check")
    assert got.returncode != 0
    assert "not-clean: no Docker baseline" in got.stdout
    assert lb.run("docker-up").returncode == 0
    got = lb.run("check")
    assert got.returncode == 0, got.stdout
    assert "LATE: the Docker baseline was taken at" in got.stdout


def test_a_build_cache_that_shrank_is_not_a_build(lab):
    """(c): BuildKit's garbage collection shrinks the cache on its own schedule. Compared as
    strings, that read as a build this session ran and blocked an exit nothing could clear."""
    lb = lab(build_cache="1.234GB")
    assert lb.run("begin").returncode == 0
    lb.mutate(build_cache="233MB")
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode == 0, got.stdout


def test_a_build_cache_whose_size_cannot_be_read_is_not_clean(lab):
    """(c): a size the guard cannot parse is "we could not look", never "it did not grow"."""
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(build_cache="garbled")
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode != 0
    assert "could not be read" in got.stdout


def test_a_build_cache_unreadable_at_the_baseline_is_cleared_by_a_late_reading(lab):
    """basecradle#709 item 6: a cache size that could not be read at the census never becomes
    readable by re-running ``check``, so the finding it raised could never clear, and self-exit has
    no override. ``docker-up`` re-reads it, and ``check`` names the reading as late."""
    lb = lab(build_cache="garbled")
    assert lb.run("begin").returncode == 0
    lb.mutate(build_cache="1.2GB")
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode != 0
    assert "could not be read when the Docker baseline was taken" in got.stdout

    assert lb.run("docker-up").returncode == 0
    got = lb.run("check")
    assert got.returncode == 0, got.stdout
    assert "LATE: the build cache's baseline was read at" in got.stdout


def test_a_late_listener_baseline_is_named_on_every_check(lab):
    """basecradle#709 item 5: a late baseline is an attestation, so no clean verdict hides it."""
    lb = lab(lsof_error=True)
    assert lb.run("begin").returncode == 0
    lb.mutate(lsof_error=False)
    assert lb.run("baseline-listeners").returncode == 0
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode == 0, got.stdout
    assert "LATE: the listening-port baseline was taken at" in got.stdout


def test_a_census_whose_stderr_file_a_sibling_removed_is_still_a_failure(lab):
    """basecradle#709 item 1 (basecradle-router#360): a forked subagent shares ``CLAUDE_PID``, so
    it shares the key and the state dir. A fixed ``lsof.err`` there could be removed by a sibling
    run between the write and the read, and a failed census then read as an empty one."""
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(lsof_error=True, lsof_err_raced=True, listeners=[["9001", "node", "127.0.0.1:3000"]])
    lb.clean_tabs()
    got = lb.run("check")
    assert got.returncode != 0
    assert "census failed" in got.stdout


def test_the_process_key_does_not_depend_on_the_callers_time_zone(lab):
    """basecradle#709 item 2 (basecradle-router#360): ``lstart`` is rendered in the caller's time
    zone, and a Bash call sources the user's profile where the hook may not. Unpinned, the hook and
    a later call computed two keys for one process, and ``check`` found no baseline."""
    lb = lab(lstart={"4711": 1792656776})

    def run(tz, *a):
        return subprocess.run(
            [str(GUARD), *a],
            env=lb.env | {"CLAUDE_PID": "4711", "TZ": tz},
            capture_output=True,
            text=True,
            cwd=lb.tmp,
            check=False,
        )

    assert run("America/Chicago", "begin").returncode == 0
    assert "kept" in run("Asia/Tokyo", "begin").stdout


# ------------------------------------------ a resume orphans no Docker Desktop (#709 item 4)

DESKTOP_PID = "4242"
DESKTOP_LINE = f"  {DESKTOP_PID} {FAKE_APP_PREFIX}Contents/MacOS/com.docker.backend"


def _resume_lab(lab):
    """Session A (``claude`` 4711) starts Docker Desktop and is killed; B (4712) is the resume."""
    lb = lab(
        engine_up=False,
        desktop_status="stopped",
        ps_lines=[],
        ps_lines_after_start=[DESKTOP_LINE],
        lstart={"4711": 1792656776, "4712": 1792660000, DESKTOP_PID: 1792656800},
    )

    def as_session(pid):
        env = lb.env | {"CLAUDE_PID": pid}
        return lambda *a: subprocess.run(
            [str(GUARD), *a], env=env, capture_output=True, text=True, cwd=lb.tmp, check=False
        )

    a = as_session("4711")
    assert a("begin").returncode == 0
    assert a("docker-up").returncode == 0
    return lb, as_session


def _state_of(lb, pid):
    (only,) = lb.state_root.glob(f"claude-{pid}-*")
    return only


def _kill(lb, pid):
    starts = lb.docker_state()["lstart"]
    del starts[pid]
    lb.mutate(lstart=starts)


def test_a_resume_adopts_the_docker_desktop_its_dead_predecessor_started(lab):
    """A resume is a new ``claude`` process, so it takes a new key and a new baseline, which saw
    Docker Desktop running and called it pre-existing: the record that A started it was orphaned
    under A's dead key and nothing ever quit it — the #790 leftover on the resume path. A recorded
    the identity of every Desktop process, so B can prove the running one is that same one."""
    lb, as_session = _resume_lab(lab)
    _kill(lb, "4711")
    b = as_session("4712")
    began = b("begin")
    assert began.returncode == 0
    assert "ADOPTED" in began.stdout
    assert [d.name.split("-")[1] for d in lb.state_root.iterdir()] == ["4712"]

    assert b("tabs-checked").returncode == 0
    got = b("check")
    assert got.returncode != 0
    assert "adopted the Docker Desktop" in got.stdout
    assert b("sweep").returncode == 0
    assert ["desktop", "stop"] in lb.calls_log()
    final = b("check")
    assert final.returncode == 0, final.stdout


def test_a_docker_desktop_restarted_since_is_not_the_dead_sessions_and_is_never_quit(lab):
    """Prove it is ours, or report: a Docker Desktop whose processes are not the ones the dead
    session recorded was started by somebody since. Its record is stale and is cleared."""
    lb, as_session = _resume_lab(lab)
    _kill(lb, "4711")
    starts = lb.docker_state()["lstart"]
    starts[DESKTOP_PID] = 1792659000
    lb.mutate(lstart=starts)
    b = as_session("4712")
    began = b("begin")
    assert "ADOPTED" not in began.stdout
    assert "is no longer running" in began.stdout
    assert b("tabs-checked").returncode == 0
    assert b("sweep").returncode == 0
    assert ["desktop", "stop"] not in lb.calls_log()
    assert b("check").returncode == 0


def test_a_live_sessions_docker_desktop_is_never_adopted(lab):
    """Two live sessions of one repo: the one whose ``claude`` is still running keeps its record."""
    lb, as_session = _resume_lab(lab)
    b = as_session("4712")
    assert "ADOPTED" not in b("begin").stdout
    assert _state_of(lb, "4711").is_dir()


def test_a_record_with_no_process_to_prove_it_is_reported_and_never_adopted(lab):
    """A record written before the processes were recorded proves nothing about the Docker Desktop
    running now, so it is reported and left, never adopted."""
    lb, as_session = _resume_lab(lab)
    procs = _state_of(lb, "4711") / "docker_desktop_procs"
    assert procs.read_text().startswith(f"{DESKTOP_PID}-")  # what docker-up recorded
    procs.write_text("")
    _kill(lb, "4711")
    began = as_session("4712")("begin")
    assert "ADOPTED" not in began.stdout
    assert "not adopted" in began.stdout
    assert _state_of(lb, "4711").is_dir()


# ------------------------------------- one script, every laptop builder (basecradle-noc#1110)


def _carrier(tmp_path, config):
    """A copy of the shared scripts in another carrier's ``.claude``, with its own config."""
    claude = tmp_path / "carrier" / ".claude"
    claude.mkdir(parents=True)
    for src in (GUARD, SELF_EXIT):
        shutil.copy2(src, claude / src.name)
    if config is not None:
        (claude / "session-guard.env").write_text(config)
    return claude / "session-guard.sh"


@pytest.mark.parametrize(
    "config",
    [
        None,
        "",
        "SESSION_GUARD_REPO=\n",
        "SESSION_GUARD_REPO=Basecradle-Router\n",
        "SESSION_GUARD_REPO=basecradle-router\nSESSION_GUARD_REPO=basecradle-harness\n",
        "SESSION_GUARD_REPO=basecradle-router\nrm -rf /\n",
        "SESSION_GUARD_REPO=$(whoami)\n",
    ],
)
def test_a_carrier_without_a_valid_config_takes_no_baseline_and_cannot_exit(lab, tmp_path, config):
    """The config is read, never sourced, and a missing or malformed one refuses every command: a
    carrier that forgot it must never run under some other repo's label, and with no baseline its
    ``check`` refuses, so the gap is loud."""
    guard = _carrier(tmp_path, config)
    lb = lab()
    begun = lb.run("begin", script=guard)
    assert begun.returncode == 78
    assert "session-guard.env" in begun.stderr
    assert not lb.state_root.exists()
    assert lb.run("check", script=guard).returncode != 0


def test_a_carriers_label_prefix_and_state_root_come_from_its_config(lab, tmp_path):
    """The one per-repo value: the label the sweep filters on, the builder prefix and the default
    state root are all derived from it, so the same bytes serve every carrier. The other carrier
    is a name no real one holds, and the builder it must leave alone is this checkout's own, so the
    copy's config, never the checkout it was copied from, is what decides."""
    other = "basecradle-example"
    guard = _carrier(tmp_path, f"# a comment\n\nSESSION_GUARD_REPO={other}\n")
    lb = lab(
        containers=[CONTAINER],
        builders=["desktop-linux", f"{other}-builder-x", f"{BUILDER_PREFIX}y"],
    )
    env = lb.env | {"DOCKER_STUB_LABEL": f"{other}-builder=1", "HOME": str(tmp_path)}
    del env["SESSION_GUARD_STATE_ROOT"]

    def run(*a):
        return subprocess.run(
            [str(guard), *a], env=env, capture_output=True, text=True, cwd=lb.tmp, check=False
        )

    assert run("begin").returncode == 0
    assert (tmp_path / f".cache/{other}/builder-sessions/test-session/began_at").is_file()
    assert run("sweep").returncode == 0
    calls = lb.calls_log()
    assert ["rm", "-f", CONTAINER["id"]] in calls
    assert ["buildx", "rm", f"{other}-builder-x"] in calls
    assert ["buildx", "rm", f"{BUILDER_PREFIX}y"] not in calls


def test_the_shared_scripts_name_no_carrier_in_their_code():
    """Byte-identical across carriers means nothing repo-specific outside a comment: a literal
    label, prefix or env name here would be one carrier's value baked into every other's guard."""
    for path in (GUARD, SELF_EXIT):
        code = "\n".join(
            ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")
        )
        assert "basecradle-" not in code, path
        assert "BASECRADLE_" not in code, path


def _project_name() -> str:
    """``[project] name`` from this checkout's ``pyproject.toml``, read without ``tomllib``, which
    the carriers that test on Python 3.10 do not have."""
    text = (REPO / "pyproject.toml").read_text()
    table = re.search(r"(?ms)^\[project\]\n(.*?)(?=^\[|\Z)", text)
    assert table, "pyproject.toml has no [project] table"
    name = re.search(r'(?m)^name\s*=\s*"([^"]+)"', table.group(1))
    assert name, "pyproject.toml's [project] table has no name"
    return name.group(1)


def test_the_config_names_the_checkout_it_sits_in():
    """The suite takes its label from the config, so a carrier that copied ANOTHER carrier's
    ``session-guard.env`` would pass every test above under the wrong label. A second spelling of
    the repo's name, its own package metadata, is what catches that."""
    assert _project_name() == REPO_NAME


# ------------------------------- two census paths that read a failed look (basecradle#710)


def test_a_claude_pid_whose_start_time_cannot_be_read_refuses_the_run_never_moves_it(lab):
    """basecradle#710 item 1: one failed ``ps`` used to drop a run onto the session-id key. Its
    writes then landed in a directory no later call reads (here: the record that it started Docker
    Desktop, so nothing would ever quit it), and its lsof stderr redirect into that missing
    directory read as "no listeners". A CLAUDE_PID that cannot be read now refuses the run."""
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[], lstart={"4711": 1792656776})
    env = lb.env | {"CLAUDE_PID": "4711"}

    def run(*a):
        return subprocess.run(
            [str(GUARD), *a], env=env, capture_output=True, text=True, cwd=lb.tmp, check=False
        )

    assert run("begin").returncode == 0
    lb.mutate(lstart={})  # one `ps -o lstart=` that fails
    up = run("docker-up")
    assert up.returncode == 75
    assert "cannot be read" in up.stderr
    assert [d.name.split("-")[0] for d in lb.state_root.iterdir()] == ["claude"]
    assert ["desktop", "start"] not in lb.calls_log()


def test_an_lsof_stderr_redirect_that_fails_is_a_failed_census(lab, tmp_path):
    """basecradle#710 item 1: a redirect that fails never runs lsof and returns 1 with nothing
    printed, which is the "no listeners" shape exactly. Here the redirect's directory is gone."""
    lb = lab()
    assert lb.run("begin").returncode == 0
    lb.mutate(listeners=[["9001", "node", "127.0.0.1:3000"]])
    lb.clean_tabs()
    env = lb.env | {"TMPDIR": str(tmp_path / "gone")}
    shim = tmp_path / "bin" / "mktemp"
    shim.write_text('#!/bin/sh\necho "$TMPDIR/session-guard-lsof.x"\n')  # a path, never created
    shim.chmod(0o755)
    got = subprocess.run(
        [str(GUARD), "check"], env=env, capture_output=True, text=True, cwd=lb.tmp, check=False
    )
    assert got.returncode != 0
    assert "census failed" in got.stdout


def test_one_failed_cache_read_at_the_census_is_read_again_before_it_is_stored(lab):
    """basecradle#710 item 2: the ``started`` census runs the moment ``docker version`` first
    answers, and one failed ``docker system df`` stored an unreadable baseline for the session."""
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[], build_cache="1.2GB")
    assert lb.run("begin").returncode == 0
    lb.mutate(df_failures=1)
    assert lb.run("docker-up").returncode == 0
    assert (lb.state_root / "test-session" / "build_cache.base").read_text() == "1.2GB\n"
