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
* **You clean up what you created, and you know because you wrote it down** (``@origin``,
  ``basecradle-noc#1715``). Every Docker artefact a session makes goes through the guard, which
  records it on the session's own ledger as it is made; ``check`` reads only that ledger and the
  sweep removes only what is on it. Nothing else blocks and nothing else is touched — a sibling's
  pulled image included. *An empty Docker today is not a licence for ``docker system prune``.*
* **A stop is read back off the process list.** ``osascript quit app "Docker"`` exited 0 while
  every Docker process kept running (``basecradle-noc#716``). An exit code is not a stopped
  process, so a quit that "succeeds" while the processes live must NOT be reported as stopped.
* **The predicates are EXECUTED, never grepped.** A test asserting a label filter *appears* in the
  source cannot tell a filter that fires from one that never does (``basecradle-noc#773``). So the
  guard runs against a stub ``docker`` that records every argv, and the assertions are about what it
  was — and was not — asked to remove. The stub also carries a **positive control**: foreign and
  this-repo-labelled artefacts sitting right beside the recorded ones, which the sweep must leave
  alone.
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

# A `docker` that answers from a JSON scenario, mutates it as the real engine would, and records
# every argv. Executing the guard against this is the only way to tell a predicate that fires from
# one that does not — and the calls log is what lets a test assert what was NEVER asked for. Its
# removal rules are the ones measured on Docker 29.8's containerd image store (basecradle-noc#1715):
# an unforced `rmi` of an image a container uses, running or stopped, is refused; an image with
# several tags is not removed by its id unforced; removing one of several tags only untags.
DOCKER_STUB = r"""#!/usr/bin/env python3
import hashlib, json, os, sys

state_path = os.environ["DOCKER_STUB_STATE"]
with open(state_path) as fh:
    st = json.load(fh)
with open(os.environ["DOCKER_STUB_CALLS"], "a") as fh:
    fh.write("\t".join(sys.argv[1:]) + "\n")


def save():
    with open(state_path, "w") as fh:
        json.dump(st, fh)


def down():
    if not st["engine_up"]:
        sys.stderr.write("Cannot connect to the Docker daemon\n")
        sys.exit(1)


def tags(i):
    return i.get("tags") if "tags" in i else ([i["tag"]] if i.get("tag") else [])


def set_tags(i, new):
    i.pop("tag", None)
    i["tags"] = new


def image_by(ref):
    return next((i for i in st["images"] if i["id"] == ref or ref in tags(i)), None)


def new_id():
    st["next_id"] = st.get("next_id", 0) + 1
    return "%064x" % (0xC0FFEE000 + st["next_id"])


def take_tag(tag):
    for i in st["images"]:
        if tag in tags(i):
            set_tags(i, [t for t in tags(i) if t != tag])


def in_use(image_id):
    return next((c for c in st["containers"] if c.get("image_id") == image_id), None)


def labels_of(args):
    out = {}
    for n, x in enumerate(args):
        if x == "--label":
            k, _, v = args[n + 1].partition("=")
            out[k] = v
    return out


a = sys.argv[1:]
# The label a `--filter label=K=V` asks for. An artefact carries it when its `labels` hold K=V, or,
# for `labelled`, when it is the label of the session asking (DOCKER_STUB_LABEL, set per session).
want = next((x[len("label="):] for x in a if x.startswith("label=")), None)
labelled = "--filter" in a and want is not None


def carries(o):
    key, _, value = want.partition("=")
    own = o.get("labelled") and want == os.environ["DOCKER_STUB_LABEL"]
    return bool(own) or (o.get("labels") or {}).get(key) == value

if a[:1] == ["version"]:
    down()
    print("29.8.0")
elif a[:2] == ["desktop", "status"]:
    print("Name                Value")
    print("Status              %s" % st["desktop_status"])
elif a[:2] == ["desktop", "start"]:
    st["desktop_status"] = "running"
    # A Docker Desktop still booting: its processes are up, its engine does not answer yet.
    st["engine_up"] = not st.get("desktop_boots_slowly")
    if "ps_lines_after_start" in st:
        st["ps_lines"] = st["ps_lines_after_start"]
    if "desktop_start_epoch" in st:
        # A started Docker Desktop is new processes, each start with a start time of its own.
        st["desktop_start_epoch"] += 60
        for line in st["ps_lines"]:
            st["lstart"][line.split()[0]] = st["desktop_start_epoch"]
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
    down()
    if st.get("ps_fails"):
        sys.exit(1)
    ident = next((x.split("=", 1)[1] for x in a if x.startswith("id=")), None)
    for c in st["containers"]:
        if ident is not None:
            if c["id"] == ident:
                print("%s (%s)" % (c["image"], c["status"]))
        elif labelled:
            if carries(c):
                print(c["id"])
        else:
            print(c["id"])
elif a[:1] in (["run"], ["create"]):
    down()
    # The guard's own flags come first, the caller's after them; the image is the first argument
    # that is neither a flag nor a flag's value.
    with_value = {"--cidfile", "--pull", "--label", "--name", "-e", "-v", "--volume", "--mount"}
    rest, image, n = a[1:], None, 0
    while n < len(rest):
        if rest[n] in with_value:
            n += 2
            continue
        if rest[n].startswith("-"):
            n += 1
            continue
        image = rest[n]
        break
    img = image_by(image) if image else None
    if img is None:
        sys.stderr.write("docker: Error response from daemon: No such image: %s\n" % image)
        sys.exit(125)
    cid = new_id()
    with open(rest[rest.index("--cidfile") + 1], "w") as fh:
        fh.write(cid)
    if not (a[0] == "run" and "--rm" in rest):
        st["containers"].append({
            "id": cid,
            "image": image,
            "image_id": img["id"],
            "status": "Exited (0) 1 second ago" if a[0] == "run" else "Created",
            "labels": labels_of(rest),
        })
    save()
elif a[:1] == ["pull"]:
    down()
    rest = a[1:]
    ref = next(
        x for n, x in enumerate(rest) if not x.startswith("-") and rest[n - 1] != "--platform"
    )
    if st.get("pull_hangs"):
        import time
        time.sleep(60)
    if st.get("pull_fails"):
        sys.stderr.write("Error response from daemon: pull access denied for %s\n" % ref)
        sys.exit(1)
    # What the registry serves for a reference: the scenario's `registry`, else an id of its own.
    want_id = st.get("registry", {}).get(ref)
    want_id = want_id or "sha256:" + hashlib.sha256(ref.encode()).hexdigest()
    have = image_by(want_id)
    take_tag(ref)
    if have is None:
        st["images"].append({"id": want_id, "tags": [ref]})
    else:
        set_tags(have, tags(have) + [ref])
    save()
    print("%s: Pulled" % ref)
elif a[:2] == ["image", "inspect"]:
    down()
    img = image_by(a[-1])
    if img is None:
        sys.stderr.write("Error response from daemon: No such image: %s\n" % a[-1])
        sys.exit(1)
    if "--format" in a:
        fmt = a[a.index("--format") + 1]
        print(img["id"] if fmt == "{{.Id}}" else ",".join(tags(img)))
    else:
        print(json.dumps([img]))
elif a[:3] == ["image", "ls", "-a"] or a[:3] == ["image", "ls", "-aq"]:
    down()
    long_form = any("Repository" in x for x in a)
    for i in st["images"]:
        if labelled:
            if carries(i):
                print(i["id"])
        elif long_form:
            print("%s %s" % (i["id"], (tags(i) or ["<none>:<none>"])[0]))
        else:
            print(i["id"])
elif a[:2] == ["volume", "ls"]:
    down()
    for v in st["volumes"]:
        print(v if isinstance(v, str) else v["name"])
elif a[:2] == ["volume", "create"]:
    down()
    if st.get("volume_create_hangs"):
        import time
        time.sleep(60)
    name = a[-1]
    if not any((v if isinstance(v, str) else v["name"]) == name for v in st["volumes"]):
        st["volumes"].append({"name": name, "labels": labels_of(a[2:])})
        save()
    print(name)
elif a[:2] == ["volume", "rm"]:
    down()
    st["volumes"] = [v for v in st["volumes"] if (v if isinstance(v, str) else v["name"]) != a[-1]]
    save()
elif a[:2] == ["buildx", "ls"]:
    down()
    print("NAME/NODE           DRIVER/ENDPOINT     STATUS    BUILDKIT   PLATFORMS")
    for i, b in enumerate(st["builders"]):
        print("%s%s      docker" % (b, "*" if i == 0 else ""))
        print(" \\_ %s0       \\_ desktop-linux   running   v0.33.0    linux/arm64" % b)
elif a[:2] == ["buildx", "create"]:
    down()
    st["builders"].append(a[a.index("--name") + 1])
    save()
elif a[:2] == ["buildx", "build"]:
    down()
    if st.get("build_hangs"):
        import time
        time.sleep(60)
    if a[a.index("--builder") + 1] not in st["builders"] or st.get("build_fails"):
        sys.stderr.write("ERROR: failed to build\n")
        sys.exit(1)
    built = [a[n + 1] for n, x in enumerate(a) if x == "-t"]
    for t in built:
        take_tag(t)
    st["images"].append({"id": "sha256:" + new_id(), "tags": built, "labels": labels_of(a[2:])})
    save()
elif a[:1] == ["rm"]:
    down()
    # `rm_lies`: an exit 0 that removed nothing, the basecradle-noc#716 shape on the engine's side.
    if not st.get("rm_lies"):
        st["containers"] = [c for c in st["containers"] if c["id"] not in a]
        save()
elif a[:1] == ["rmi"]:
    down()
    ref = a[-1]
    img = image_by(ref)
    if st.get("rmi_fails") or img is None:
        sys.stderr.write("Error response from daemon: No such image: %s\n" % ref)
        sys.exit(1)
    user = in_use(img["id"])
    if ref != img["id"] and len(tags(img)) > 1:
        set_tags(img, [t for t in tags(img) if t != ref])
    elif ref == img["id"] and len(tags(img)) > 1:
        sys.stderr.write("conflict: unable to delete (must be forced) - image is referenced in "
                         "multiple repositories\n")
        sys.exit(1)
    elif user is not None:
        sys.stderr.write("conflict: unable to delete (must be forced) - image is being used by "
                         "stopped container %s\n" % user["id"][:12])
        sys.exit(1)
    else:
        st["images"] = [i for i in st["images"] if i is not img]
    save()
elif a[:2] == ["buildx", "rm"]:
    down()
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
elif any(f in a for f in ("ppid=", "pgid=", "command=", "comm=")) and "-p" in a:
    # The parent chain, from the scenario's `procs` (pid -> ppid, pgid, command, comm; comm defaults
    # to command). A pid the scenario does not know, or a field it does not declare, prints nothing
    # and exits 1, which is what a process that has exited looks like.
    proc = st.get("procs", {}).get(a[a.index("-p") + 1])
    field = next(f for f in ("ppid=", "pgid=", "command=", "comm=") if f in a)[:-1]
    value = None if proc is None else proc.get(field, proc["command"] if field == "comm" else None)
    if value is None:
        sys.exit(1)
    print(value)
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
if st.get("lsof_kills_group"):
    # The hook's timeout, simulated: the whole census is killed mid-run.
    import signal
    os.killpg(os.getpgrp(), signal.SIGKILL)
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
        # The laptop-wide Docker Desktop ledger every carrier shares (basecradle-noc#1255).
        self.shared_root = tmp_path / "shared"
        # What a test adds on top of the lab's own environment (a CLAUDE_PID, say).
        self.extra_env: dict[str, str] = {}

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
                "SESSION_GUARD_SHARED_ROOT": str(self.shared_root),
                "SESSION_GUARD_DOCKER_APP_PREFIX": FAKE_APP_PREFIX,
                "CLAUDE_CODE_SESSION_ID": "test-session",
                "SESSION_GUARD_STOP_WAIT": "2",
                "SESSION_GUARD_START_WAIT": "2",
                "SESSION_GUARD_POLL": "1",
            }
        )
        env.update(self.extra_env)
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


# -------------------------------- the ledger: you clean up what you created (basecradle-noc#1715)


def _begun(lb):
    """A session that has begun and looked at the browser, so only Docker can refuse its exit."""
    _ok(lb.run("begin"))
    lb.clean_tabs()
    return lb


def _cid_files(lb):
    return sorted(lb.state_root.glob("*/containers/*"))


def test_a_container_run_through_the_guard_is_on_the_ledger_until_swept(lab):
    """docker writes the id into the ledger itself (``--cidfile``), so a session killed mid-run is
    still on the record. The run carries the label for a reader and ``--pull never``, because an
    implicit pull would make an image off the record."""
    lb = _begun(lab(images=[UBUNTU]))
    _ok(lb.run("run", "-d", "ubuntu:24.04", "sleep", "60"))
    (run,) = [c for c in lb.calls_log() if c[:1] == ["run"]]
    (cid_file,) = _cid_files(lb)
    assert run[1:7] == ["--cidfile", str(cid_file), "--pull", "never", "--label", LABEL]
    assert run[7:] == ["-d", "ubuntu:24.04", "sleep", "60"]
    (container,) = lb.docker_state()["containers"]
    assert cid_file.read_text() == container["id"]

    got = lb.run("check")
    assert got.returncode != 0
    assert f"this session created container {container['id']}" in _findings(got.stdout)
    assert "repair: .claude/session-guard.sh sweep" in got.stdout

    _ok(lb.run("sweep"))
    assert lb.docker_state()["containers"] == []
    assert ["rm", "-f", "-v", container["id"]] in lb.calls_log()
    assert [i["id"] for i in lb.docker_state()["images"]] == [UBUNTU["id"]]  # never this session's
    assert not any(c[:1] == ["rmi"] for c in lb.calls_log())
    _ok(lb.run("check"))


def test_a_removal_that_exits_zero_and_removes_nothing_is_read_back_as_not_done(lab):
    """An exit code is not a removed artefact; the engine's own list is (basecradle-noc#716's
    lesson, turned on the sweep's removals). The entry stays on the ledger and the exit is held."""
    lb = _begun(lab(images=[UBUNTU], rm_lies=True))
    _ok(lb.run("run", "-d", "ubuntu:24.04"))
    (container,) = lb.docker_state()["containers"]
    swept = lb.run("sweep")
    assert swept.returncode != 0
    assert f"still on the engine: container {container['id']}" in swept.stderr
    assert "INCOMPLETE" in swept.stderr
    assert container["id"] in _findings(lb.run("check").stdout)


def test_a_container_that_removed_itself_is_gone_and_needs_no_sweep(lab):
    lb = _begun(lab(images=[UBUNTU]))
    _ok(lb.run("run", "--rm", "ubuntu:24.04", "true"))
    assert len(_cid_files(lb)) == 1
    got = _ok(lb.run("check"))
    assert "1 artefact(s) recorded, 0 still on the engine" in got.stdout


def test_a_run_never_pulls_an_image_implicitly(lab):
    """An image the engine does not hold is pulled through ``pull``, which records it; ``run`` asks
    for none, so nothing reaches the laptop off the record."""
    lb = _begun(lab())
    got = lb.run("run", "--rm", "ubuntu:24.04", "true")
    assert got.returncode == 125
    assert "pull it first with .claude/session-guard.sh pull" in got.stderr
    assert lb.docker_state()["images"] == []
    assert not _cid_files(lb)
    _ok(lb.run("check"))


@pytest.mark.parametrize(
    "command",
    [
        ["run", "--cidfile=/tmp/x", "ubuntu:24.04"],
        ["run", "--pull", "always", "ubuntu:24.04"],
        ["create", "--pull=missing", "ubuntu:24.04"],
        ["build", "--builder", "default", "."],
        ["build", "--push", "."],
        ["build", "-otype=local,dest=out", "."],
        ["build", "--output=type=registry", "."],
        ["pull", "--all-tags", "ubuntu"],
    ],
)
def test_the_flags_that_record_an_artefact_are_the_guards_own(lab, command):
    """A caller's ``--cidfile`` or ``--pull`` would override the guard's and take the artefact off
    the ledger; a ``--builder`` or an output other than ``--load`` would build where nothing records
    it. Each is refused before docker is asked anything that makes something."""
    lb = _begun(lab(images=[UBUNTU]))
    got = lb.run(*command)
    assert got.returncode != 0
    assert "session-guard:" in got.stderr
    made = {"run", "create", "pull", "buildx"}
    assert not [c for c in lb.calls_log() if c[0] in made and c[:2] != ["buildx", "ls"]]


def test_a_pulled_image_blocks_the_exit_until_it_is_gone(lab):
    """The issue's case from the puller's side: the session that pulled it answers for it, with no
    container referencing it and with one of its own (removed first, so the image can go)."""
    lb = _begun(lab())
    pulled = _ok(lb.run("pull", "alpine:3.20"))
    assert "recorded image" in pulled.stdout
    (image,) = lb.docker_state()["images"]
    got = lb.run("check")
    assert got.returncode != 0
    assert f"this session created image {image['id']} (alpine:3.20)" in _findings(got.stdout)

    _ok(lb.run("run", "alpine:3.20", "true"))  # a stopped container of its own, using the image
    _ok(lb.run("sweep"))
    st = lb.docker_state()
    assert st["images"] == [] and st["containers"] == []
    removals = [c for c in lb.calls_log() if c[:1] in (["rm"], ["rmi"])]
    assert [c[0] for c in removals] == [
        "rm",
        "rmi",
    ]  # the container first, or the image could not go
    assert all("-f" not in c and "--force" not in c for c in removals if c[0] == "rmi")
    _ok(lb.run("check"))


def test_a_pulled_image_a_siblings_container_uses_holds_the_puller_and_is_never_forced(lab):
    """``@origin``'s rule read both ways: the session that pulled the image answers for it even
    while a sibling's container uses it, and it never pulls the image out from under that
    container — removal is unforced, so the sweep fails and says so until the container is gone."""
    lb = _begun(lab())
    _ok(lb.run("pull", "alpine:3.20"))
    (image,) = lb.docker_state()["images"]
    sibling = {
        "id": "5" * 64,
        "image": "alpine:3.20",
        "image_id": image["id"],
        "status": "Up 2 minutes",
        "labels": {f"{OTHER_REPO}-builder": "1"},
    }
    _add(lb, "containers", sibling)

    swept = lb.run("sweep")
    assert swept.returncode != 0
    assert "INCOMPLETE" in swept.stderr
    assert "being used by stopped container" in swept.stderr
    assert [c["id"] for c in lb.docker_state()["containers"]] == [sibling["id"]]
    assert not any(c[:1] == ["rm"] for c in lb.calls_log())
    assert image["id"] in _findings(lb.run("check").stdout)

    _drop(lb, "containers", sibling["id"])  # its owner is done with it
    _ok(lb.run("sweep"))
    _ok(lb.run("check"))


def test_a_pull_of_an_image_already_on_the_engine_makes_nothing_of_this_sessions(lab):
    """Recording it would hand this session's sweep a sibling's image."""
    lb = _begun(lab(images=[UBUNTU], registry={"ubuntu:24.04": UBUNTU["id"]}))
    got = _ok(lb.run("pull", "ubuntu:24.04"))
    assert "was on the engine before this pull, so it is not recorded" in got.stdout
    _ok(lb.run("check"))
    _ok(lb.run("sweep"))
    assert [i["id"] for i in lb.docker_state()["images"]] == [UBUNTU["id"]]
    assert not any(c[:1] == ["rmi"] for c in lb.calls_log())


def test_an_image_its_owner_removed_and_this_session_pulled_again_is_this_sessions(lab):
    """The 2026-11-04 overlap's second half: the same content id, gone and then pulled again by this
    session. What decides is the engine just before the pull, never what was there at ``begin``."""
    lb = _begun(lab(images=[UBUNTU], registry={"ubuntu:24.04": UBUNTU["id"]}))
    lb.mutate(images=[])  # its owner removes it
    _ok(lb.run("pull", "ubuntu:24.04"))
    assert UBUNTU["id"] in _findings(lb.run("check").stdout)


def test_a_build_runs_on_the_sessions_own_builder_and_records_all_it_makes(lab):
    """A build on the shared builder grows a cache nobody can attribute, so the guard builds on a
    builder of this session's own, made on first use (its image pulled through ``pull`` first) and
    recorded before docker makes it. Its cache leaves with it. It is never made the current builder,
    which is laptop-wide."""
    lb = _begun(lab())
    _ok(lb.run("build", "-t", "noc/replica:dev", "."))
    _ok(lb.run("build", "-t", "noc/replica:dev2", "."))
    calls = lb.calls_log()
    (create,) = [c for c in calls if c[:2] == ["buildx", "create"]]  # made once, then reused
    own = create[create.index("--name") + 1]
    assert own.startswith(BUILDER_PREFIX)
    assert "--use" not in create
    assert "image=moby/buildkit:buildx-stable-1" in create
    builds = [c for c in calls if c[:2] == ["buildx", "build"]]
    assert len(builds) == 2
    for build in builds:
        assert build[2:7] == ["--builder", own, "--load", "--label", LABEL]
    pull_at = calls.index(["pull", "-q", "moby/buildkit:buildx-stable-1"])
    assert pull_at < calls.index(create)

    got = lb.run("check")
    assert got.returncode != 0
    findings = _findings(got.stdout)
    assert f"buildx builder {own}" in findings
    assert "noc/replica:dev" in findings and "noc/replica:dev2" in findings
    assert "moby/buildkit:buildx-stable-1" in findings
    assert "4 artefact(s) recorded, 4 still on the engine" in got.stdout

    _ok(lb.run("sweep"))
    st = lb.docker_state()
    assert st["images"] == [] and st["builders"] == ["desktop-linux"]
    calls = lb.calls_log()
    assert calls.index(["buildx", "rm", own]) < min(
        n for n, c in enumerate(calls) if c[:1] == ["rmi"]
    )
    _ok(lb.run("check"))


def test_a_build_that_failed_records_its_builder_and_no_image(lab):
    lb = _begun(lab(build_fails=True))
    assert lb.run("build", ".").returncode != 0
    got = lb.run("check")
    assert "left no image on the engine" not in got.stdout  # resolved by the build itself
    assert "2 artefact(s) recorded, 2 still on the engine" in got.stdout  # its builder and buildkit
    _ok(lb.run("sweep"))
    _ok(lb.run("check"))


def test_a_volume_is_recorded_before_docker_makes_it_and_a_taken_name_is_refused(lab):
    """``docker volume create`` of a name that exists succeeds and makes nothing, so recording it
    would hand this session's sweep a sibling's volume."""
    lb = _begun(lab(volumes=[{"name": "sib-data", "labels": {f"{OTHER_REPO}-builder": "1"}}]))
    taken = lb.run("volume-create", "sib-data")
    assert taken.returncode != 0
    assert "already exists" in taken.stderr
    assert not any(c[:2] == ["volume", "create"] for c in lb.calls_log())

    _ok(lb.run("volume-create", "replica-data"))
    assert ["volume", "create", "--label", LABEL, "replica-data"] in lb.calls_log()
    assert "this session created volume replica-data" in _findings(lb.run("check").stdout)
    _ok(lb.run("sweep"))
    assert [v["name"] for v in lb.docker_state()["volumes"]] == ["sib-data"]
    _ok(lb.run("check"))


def test_the_sweep_never_removes_what_its_ledger_does_not_record(lab):
    """The positive control. Beside what this session made sit a foreign container and image, a
    container and an image carrying THIS repo's own label (an ended session's, or a bare
    ``docker run``'s), a builder under this repo's prefix, and a volume: none is on the ledger, so
    none blocks and none is touched. A label is no longer ownership; the ledger is."""
    own_labelled = {**CONTAINER, "id": "7" * 64}
    lb = _begun(
        lab(
            containers=[FOREIGN_CONTAINER, own_labelled],
            images=[FOREIGN_IMAGE, IMAGE, UBUNTU],
            volumes=["someone-elses"],
            builders=["desktop-linux", f"{BUILDER_PREFIX}demo"],
        )
    )
    _ok(lb.run("check"))  # nothing on the ledger: nothing blocks
    _ok(lb.run("run", "-d", "ubuntu:24.04"))
    _ok(lb.run("pull", "alpine:3.20"))
    _ok(lb.run("sweep"))

    st = lb.docker_state()
    assert [c["id"] for c in st["containers"]] == [FOREIGN_CONTAINER["id"], own_labelled["id"]]
    assert [i["id"] for i in st["images"]] == [FOREIGN_IMAGE["id"], IMAGE["id"], UBUNTU["id"]]
    assert st["builders"] == ["desktop-linux", f"{BUILDER_PREFIX}demo"]
    assert st["volumes"] == ["someone-elses"]
    removals = [c for c in lb.calls_log() if c[0] in {"rm", "rmi"} or c[1:2] == ["rm"]]
    removed = {arg for call in removals for arg in call}
    for foreign in (FOREIGN_CONTAINER["id"], own_labelled["id"], FOREIGN_IMAGE["id"], IMAGE["id"]):
        assert foreign not in removed
    assert UBUNTU["id"] not in removed and "ubuntu:24.04" not in removed
    assert f"{BUILDER_PREFIX}demo" not in removed and "someone-elses" not in removed
    assert not any("prune" in arg for call in lb.calls_log() for arg in call)
    _ok(lb.run("check"))


def test_a_session_that_recorded_nothing_exits_past_a_siblings_container_and_pulled_image(
    lab, tmp_path
):
    """The issue's case (2026-11-22): a session that never used Docker could not exit, because a
    sibling's pulled base image appeared while it ran and can carry no label; the only repair it was
    offered was removing a sibling's image while the sibling's container used it."""
    lb, a, b = _overlap_lab(lab, tmp_path)
    _ok(a("begin"))
    _ok(b("begin"))
    _ok(b("docker-up"))
    _ok(b("pull", "python:3.11-slim-bookworm"))
    _ok(b("run", "-d", "python:3.11-slim-bookworm", "sleep", "3600"))
    before = len(lb.calls_log())

    got = _clean_exit(a)
    assert "Docker ledger: empty — nothing this session made is left on the engine" in got.stdout
    assert "not-clean" not in got.stdout
    st = lb.docker_state()
    assert len(st["images"]) == 1 and len(st["containers"]) == 1
    after = lb.calls_log()[before:]
    assert not [c for c in after if c[:1] in (["rm"], ["rmi"]) or c[1:2] == ["rm"]]


def test_a_session_exits_clean_while_a_sibling_builds_an_image(lab, tmp_path):
    """2026-11-26: a builder that ran no Docker command was held open because the shared build cache
    grew by a sibling's build. A quantity nobody can attribute is not a finding against whoever is
    exiting; and the sibling's build ran on its own builder, whose cache leaves with it."""
    lb, a, b = _overlap_lab(lab, tmp_path, build_cache="333.7MB")
    _ok(a("begin"))
    _ok(b("begin"))
    _ok(b("docker-up"))
    _ok(b("build", "-t", "replica:dev", "."))
    lb.mutate(build_cache="336.6MB")
    got = _clean_exit(a)
    assert "build cache" not in got.stdout
    assert "not-clean" not in got.stdout
    # ...and the builder answers for everything its build made.
    assert "3 artefact(s) recorded, 3 still on the engine" in b("check").stdout
    _ok(b("sweep"))
    _clean_exit(b)
    assert lb.docker_state()["images"] == []


def _cut_off(lb, *command):
    """Run a guard command whose docker call hangs, check while it runs, then kill it mid-call."""
    proc = subprocess.Popen(
        [str(GUARD), *command],
        env=lb.env,
        cwd=lb.tmp,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.time() + 10
    hung = command[0] if command[0] != "volume-create" else "volume"
    while time.time() < deadline:
        calls = lb.calls_log()
        if any(c[:1] == [hung] or c[1:2] in (["build"], ["create"]) for c in calls):
            break
        time.sleep(0.05)
    time.sleep(0.2)
    try:
        return lb.run("check")
    finally:
        os.killpg(proc.pid, 9)
        proc.wait()


def test_a_pull_cut_off_part_way_holds_the_exit_while_it_runs_and_is_never_guessed_at(lab):
    """While the pull's own process lives, its pending record is in flight and holds the exit. Once
    it is gone, an image under its reference may be the one it landed or one a sibling pulled since,
    and nothing tells them apart: recording it would hand this session's sweep a sibling's image. It
    is named, never recorded (``@origin``: it never guesses)."""
    lb = _begun(lab(pull_hangs=True))
    got = _cut_off(lb, "pull", "alpine:3.20")
    assert "this session started has not finished" in _findings(got.stdout)
    assert list(lb.state_root.glob("*/pending/*")), "the record outlives the pull that wrote it"

    _add(lb, "images", {"id": "sha256:" + "9" * 64, "tags": ["alpine:3.20"]})  # a pull landed
    got = _ok(lb.run("check"))
    assert "was cut off before it said what it made" in got.stdout
    assert "NOT recorded" in got.stdout
    assert not list(lb.state_root.glob("*/pending/*"))
    _ok(lb.run("sweep"))
    assert [i["id"] for i in lb.docker_state()["images"]] == ["sha256:" + "9" * 64]


def test_a_build_cut_off_part_way_is_resolved_by_the_next_check(lab):
    """A build's tag is this session's alone, so the image under it can only be its own: a build
    cut off is resolved by the next ``check`` exactly as it would have resolved itself."""
    lb = _begun(lab(build_hangs=True))
    got = _cut_off(lb, "build", ".")
    assert "this session started has not finished" in _findings(got.stdout)
    (pending,) = lb.state_root.glob("*/pending/*")
    tag = pending.read_text().splitlines()[0]
    assert tag.startswith(BUILDER_PREFIX)

    _add(lb, "images", {"id": "sha256:" + "8" * 64, "tags": [tag]})  # the build had loaded
    got = lb.run("check")
    assert "recorded image sha256:" + "8" * 64 in got.stdout
    assert "sha256:" + "8" * 64 in _findings(got.stdout)
    _ok(lb.run("sweep"))
    _ok(lb.run("check"))


def test_a_record_written_before_its_artefact_exists_is_never_discharged_while_it_is_made(lab):
    """A volume (and a builder) is recorded BEFORE docker makes it. A concurrent ``check`` of the
    same session that finds it not there yet must not discharge the record, or the volume, made a
    moment later, would be off the ledger; it is in flight, and holds the exit."""
    lb = _begun(lab(volume_create_hangs=True))
    got = _cut_off(lb, "volume-create", "replica-data")
    assert "volume replica-data this session is making has not finished" in _findings(got.stdout)
    (record,) = lb.state_root.glob("*/records/*")
    assert record.read_text().startswith("volume\treplica-data\t")

    _add(lb, "volumes", {"name": "replica-data", "labels": {}})  # docker had made it
    assert "this session created volume replica-data" in _findings(lb.run("check").stdout)


def test_a_failed_volume_create_leaves_no_record_behind(lab):
    """A record that outlived a failed create would hand this session's sweep a sibling's volume
    that later took the name."""
    lb = _begun(lab())
    stub = lb.bin / "docker"
    real = stub.read_text()
    stub.write_text(
        real.replace(
            'elif a[:2] == ["volume", "create"]:\n    down()',
            'elif a[:2] == ["volume", "create"]:\n    sys.exit(1)',
            1,
        )
    )
    assert lb.run("volume-create", "replica-data").returncode != 0
    stub.write_text(real)
    assert not list(lb.state_root.glob("*/records/*"))
    _add(lb, "volumes", {"name": "replica-data", "labels": {}})  # a sibling takes the name
    _ok(lb.run("check"))
    _ok(lb.run("sweep"))
    assert [v["name"] for v in lb.docker_state()["volumes"]] == ["replica-data"]


@pytest.mark.parametrize(
    "mount",
    [
        ["-v", "pgdata:/var/lib/postgresql/data"],
        ["--volume=pgdata:/data"],
        ["-vpgdata:/data"],
        ["--mount", "type=volume,src=pgdata,dst=/data"],
        ["--mount=source=pgdata,target=/data"],
    ],
)
def test_a_run_that_would_make_a_named_volume_off_the_record_is_refused(lab, mount):
    """Docker makes a named volume a run mounts if it is missing, and ``rm -v`` removes only
    anonymous ones, so it would outlive the session off the record. Made first through
    ``volume-create`` (or already there), the same run goes ahead."""
    lb = _begun(lab(images=[UBUNTU]))
    got = lb.run("run", *mount, "ubuntu:24.04")
    assert got.returncode != 0
    assert "pgdata do not exist" in got.stderr
    assert not any(c[:1] == ["run"] for c in lb.calls_log())
    _ok(lb.run("volume-create", "pgdata"))
    _ok(lb.run("run", *mount, "ubuntu:24.04"))


@pytest.mark.parametrize(
    "mount",
    [
        ["-v", "/tmp/x:/x"],
        ["-v", "./x:/x"],
        ["-v", "/data"],
        ["--mount", "type=bind,src=/x,dst=/x"],
    ],
)
def test_a_bind_mount_or_an_anonymous_volume_is_not_refused(lab, mount):
    lb = _begun(lab(images=[UBUNTU]))
    _ok(lb.run("run", "--rm", *mount, "ubuntu:24.04", "true"))


def test_a_pull_names_its_one_reference_wherever_it_sits(lab):
    lb = _begun(lab())
    got = _ok(lb.run("pull", "alpine:3.20", "-q"))
    assert "recorded image" in got.stdout and "(alpine:3.20)" in got.stdout
    _ok(lb.run("pull", "--platform", "linux/arm64", "busybox:1"))
    assert "busybox:1" in _findings(lb.run("check").stdout)
    assert lb.run("pull", "alpine:3.20", "busybox:1").returncode != 0
    assert lb.run("pull", "-q").returncode != 0


def test_a_begin_re_run_after_one_cut_off_keeps_the_ledger_it_found(lab):
    """A ``begin`` cut off after it adopted a predecessor's ledger leaves no ``began_at``, so the
    next one replaces the state directory: everything but the ledger, which is never deleted
    unread."""
    lb = _begun(lab(images=[UBUNTU]))
    _ok(lb.run("run", "-d", "ubuntu:24.04"))
    (lb.state_root / "test-session" / "began_at").unlink()  # the shape a cut-off begin leaves
    again = _ok(lb.run("begin"))
    assert "its Docker ledger, if any, is kept" in again.stderr
    assert "docker ledger: 1 artefact(s) recorded" in again.stdout
    lb.clean_tabs()
    assert lb.docker_state()["containers"][0]["id"] in _findings(lb.run("check").stdout)


def test_an_engine_that_died_mid_session_cannot_show_the_ledger_is_clear(lab):
    """The 2026-09-20 state exactly: the engine answering 500, nothing listable. "We could not
    look" is not "there is nothing there" — but only a session with something on its ledger needs
    a look."""
    lb = _begun(lab(images=[UBUNTU]))
    _ok(lb.run("check"))
    lb.mutate(engine_up=False)
    _ok(lb.run("check"))  # it made nothing: a dead engine is no finding of its own

    lb.mutate(engine_up=True)
    _ok(lb.run("run", "-d", "ubuntu:24.04"))
    lb.mutate(engine_up=False)
    got = lb.run("check")
    assert got.returncode != 0
    assert "engine is not reachable" in _findings(got.stdout)
    swept = lb.run("sweep")
    assert swept.returncode != 0 and "not reachable" in swept.stderr


def test_what_was_already_there_at_session_start_is_not_a_finding(lab):
    """The baseline is what keeps the guard from blaming a session for the laptop it inherited."""
    lb = lab(containers=[FOREIGN_CONTAINER], images=[FOREIGN_IMAGE], volumes=["pre-existing"])
    assert lb.run("begin").returncode == 0
    lb.clean_tabs()
    assert lb.run("check").returncode == 0


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
    lb = _begun(lab(images=[UBUNTU]))
    _ok(lb.run("run", "-d", "ubuntu:24.04"))
    assert lb.run("done").returncode != 0
    assert (lb.state_root / "test-session").is_dir()

    _ok(lb.run("sweep"))
    _ok(lb.run("done"))
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


# ------------------------------------- whose a listener is, by its parent chain (#1230)

# Every laptop builder runs as one macOS account, so the account's listeners hold every concurrent
# sibling's test run too: the router captain's exit was refused twice by a noc session's pytest. A
# listener is this session's only when its parent chain says so. The chains below are one laptop:
# this session's claude and a sibling's, both in one Terminal the operator also types into.
OWN = "4711"
SIBLING = "4800"
TERMINAL = "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal"
PYTHON_APP = (
    "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/"
    "Resources/Python.app/Contents/MacOS/Python"
)
PROCS = {
    OWN: {"ppid": "4700", "command": "claude --effort high"},
    "4700": {"ppid": "4690", "command": "-zsh"},
    "4690": {"ppid": "1", "pgid": "4690", "command": TERMINAL, "comm": TERMINAL},
    SIBLING: {"ppid": "4790", "command": "/opt/homebrew/bin/claude --model claude-fable-5-1"},
    "4790": {"ppid": "4690", "command": "-zsh"},
    # this session's dev server, under its Bash tool's shell
    "5150": {"ppid": "5140", "command": "node server.js"},
    "5140": {"ppid": OWN, "command": "/bin/zsh -c node server.js"},
    # the sibling's pytest, the shape that refused basecradle-router#389's exit
    "6150": {"ppid": "6140", "command": ".venv/bin/python -m pytest"},
    "6140": {"ppid": SIBLING, "command": "/bin/zsh -c uv run pytest"},
    # the operator's own server, typed into the Terminal
    "7150": {"ppid": "7140", "command": "bin/rails server"},
    "7140": {"ppid": "4690", "command": "-zsh"},
    # a server detached from the shell that started it: launchd adopted it, and it still carries the
    # dead shell's process group. Its binary is inside an app bundle, as `/usr/bin/python3`'s is.
    "8150": {"ppid": "1", "pgid": "8100", "command": "python3 -m http.server", "comm": PYTHON_APP},
    # a `claude -p` this session ran, and the server it started
    "9150": {"ppid": "9140", "command": "python3 -m http.server"},
    "9140": {"ppid": OWN, "command": "claude -p 'serve it'"},
}
LISTENERS = {
    "5150": ["5150", "node", "127.0.0.1:3000"],
    "6150": ["6150", "python3.1", "127.0.0.1:52811"],
    "7150": ["7150", "ruby", "127.0.0.1:3001"],
    "8150": ["8150", "python3.1", "127.0.0.1:8000"],
    "9150": ["9150", "python3.1", "127.0.0.1:8001"],
}


def _attributed_check(lab, *pids, claude_pid=OWN, procs=None):
    """``begin`` in a session that knows its own claude, then ``check`` with ``pids`` listening,
    every one of them started after the baseline."""
    lab_obj = lab(lstart={OWN: 1792656776}, procs=PROCS if procs is None else procs)
    if claude_pid is not None:
        lab_obj.extra_env["CLAUDE_PID"] = claude_pid
    assert lab_obj.run("begin").returncode == 0
    lab_obj.clean_tabs()
    (began,) = (int(f.read_text()) for f in lab_obj.state_root.glob("*/began_epoch"))
    lab_obj.mutate(listeners=[LISTENERS[p] for p in pids], proc_start={p: began + 5 for p in pids})
    return lab_obj, lab_obj.run("check")


def _findings(out: str) -> str:
    return "\n".join(ln for ln in out.splitlines() if ln.startswith("not-clean:"))


def test_a_listener_this_sessions_claude_started_blocks_the_exit(lab):
    _, got = _attributed_check(lab, "5150")
    assert got.returncode == 1
    assert "node (pid 5150) on 127.0.0.1:3000" in _findings(got.stdout)
    assert "kill 5150" in got.stdout


def test_a_sibling_sessions_listener_is_named_and_never_blocks_the_exit(lab):
    """The DoD's case: a sibling's pytest listening while this builder exits."""
    lab_obj, got = _attributed_check(lab, "6150")
    assert got.returncode == 0, got.stdout
    assert "not-clean" not in got.stdout
    assert (
        f"belongs to a sibling session — its parent chain reaches claude pid {SIBLING}"
        in got.stdout
    )
    assert "kill 6150" not in got.stdout
    # ...and the exit path itself gets past the guard: it stops only at the kill-target walk,
    # which finds no `claude` above it in the lab.
    exited = lab_obj.run(script=SELF_EXIT)
    assert "has not cleaned up" not in exited.stderr
    assert "no ancestor 'claude' process found" in exited.stderr


def test_a_listener_started_outside_any_claude_session_never_blocks_the_exit(lab):
    _, got = _attributed_check(lab, "7150")
    assert got.returncode == 0, got.stdout
    assert (
        "started outside any Claude session — its parent chain is whole and reaches no claude "
        f"(top: {TERMINAL})" in got.stdout
    )


def test_a_detached_listener_whose_chain_was_cut_still_blocks_the_exit(lab):
    """A server this session backgrounded and whose shell then exited is adopted by launchd, and
    its chain names nobody. Excusing it would reopen #899 for the commonest leftover there is."""
    _, got = _attributed_check(lab, "8150")
    assert got.returncode == 1
    assert "parent chain was cut" in _findings(got.stdout)
    assert f"adopted by launchd at {PYTHON_APP}" in got.stdout
    assert "kill 8150" in got.stdout
    assert "run_in_background" in got.stdout


def test_a_claude_this_session_ran_is_still_this_session(lab):
    """The walk goes on past the nearest ``claude``: a ``claude -p`` this session ran is its own."""
    _, got = _attributed_check(lab, "9150")
    assert got.returncode == 1
    assert "python3.1 (pid 9150) on 127.0.0.1:8001" in _findings(got.stdout)
    assert "sibling" not in got.stdout.replace("to a sibling session,", "")


def test_a_chain_that_cannot_be_read_is_never_read_as_a_siblings(lab):
    """The sibling's shell exited mid-walk: "we could not look" is not "it is someone else's"."""
    procs = {k: v for k, v in PROCS.items() if k != "6140"}
    _, got = _attributed_check(lab, "6150", procs=procs)
    assert got.returncode == 1
    assert "parent chain could not be read: python3.1 (pid 6150)" in _findings(got.stdout)


def test_a_top_whose_process_group_cannot_be_read_is_never_read_as_outside(lab):
    procs = PROCS | {"4690": {k: v for k, v in PROCS["4690"].items() if k != "pgid"}}
    _, got = _attributed_check(lab, "7150", procs=procs)
    assert got.returncode == 1
    assert "parent chain could not be read: ruby (pid 7150)" in _findings(got.stdout)


def test_a_session_that_cannot_name_its_own_claude_excuses_nothing(lab):
    """No CLAUDE_PID and no ``claude`` above the guard: a sibling-shaped chain still refuses."""
    _, got = _attributed_check(lab, "6150", claude_pid=None)
    assert got.returncode == 1
    assert "python3.1 (pid 6150) on 127.0.0.1:52811" in _findings(got.stdout)
    assert "own claude process could not be found" in got.stdout


def test_the_census_is_printed_and_only_this_sessions_share_refuses(lab):
    _, got = _attributed_check(lab, "5150", "6150", "7150")
    assert got.returncode == 1
    findings = _findings(got.stdout)
    assert "pid 5150" in findings
    assert "pid 6150" not in findings and "pid 7150" not in findings
    assert (
        "listening-port census: 3 process(es) listening on this account; 3 started since the "
        "baseline — 1 attributed to this session, 1 to a sibling session, 1 outside any Claude "
        "session." in got.stdout
    )


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
    listener census (which would make every new listener invisible)."""
    lb = lab(lsof_kills_group=True)
    got = subprocess.run(
        [str(GUARD), "begin"],
        env=lb.env,
        capture_output=True,
        text=True,
        start_new_session=True,
        check=False,
    )
    assert got.returncode < 0, "the stub was meant to kill the census"
    lb.mutate(lsof_kills_group=False)
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
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[], rmi_fails=True)
    assert lb.run("begin").returncode == 0
    assert lb.run("docker-up").returncode == 0
    _ok(lb.run("pull", "alpine:3.20"))
    got = lb.run("sweep")
    assert got.returncode != 0
    assert "INCOMPLETE" in got.stderr
    assert "could not remove image" in got.stderr
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


def test_an_engine_down_at_session_start_needs_no_census(lab):
    """(b) is gone by construction: nothing is compared against a census, so an image already on
    the laptop when ``docker-up`` brought the engine up is never read as this session's — with
    ``docker rmi`` of someone else's image as the repair."""
    lb = lab(
        engine_up=False,
        desktop_status="stopped",
        ps_lines=[],
        images=[FOREIGN_IMAGE],
        containers=[FOREIGN_CONTAINER],
    )
    _ok(lb.run("begin"))
    _ok(lb.run("docker-up"))
    lb.clean_tabs()
    findings = _findings(lb.run("check").stdout)
    assert FOREIGN_IMAGE["id"] not in findings and FOREIGN_CONTAINER["id"] not in findings
    _ok(lb.run("pull", "alpine:3.20"))
    assert "this session created image" in _findings(lb.run("check").stdout)


def test_an_engine_that_came_up_from_outside_is_no_finding_for_a_session_that_made_nothing(lab):
    """basecradle#709 item 3 made this a finding, because a session that reached such an engine out
    of protocol would otherwise exit clean. Under the ledger it needs no look at all: a session
    answers for what it recorded, and an artefact made around the guard is the capital's
    leftovers check's to name (basecradle-noc#1715)."""
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[])
    _ok(lb.run("begin"))
    lb.mutate(engine_up=True, images=[FOREIGN_IMAGE, IMAGE])
    lb.clean_tabs()
    got = _ok(lb.run("check"))
    assert "not-clean" not in got.stdout


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


def _ledger(lb):
    return lb.shared_root / "docker-desktop"


def _record_of(lb, pid):
    (only,) = _ledger(lb).glob(f"claude-{pid}-*")
    return only


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
    # The ledger record moved to B; A's own state directory is its repo's, left to the stale sweep.
    assert [f.name.split("-")[1] for f in _ledger(lb).iterdir()] == ["4712"]

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
    record = _record_of(lb, "4711")
    repo_line, procs = record.read_text().split("\n", 1)
    assert repo_line == f"repo={REPO_NAME}"
    assert procs.startswith(f"{DESKTOP_PID}-")  # what docker-up recorded
    record.write_text(repo_line + "\n")  # the record a session killed while it booted leaves
    _kill(lb, "4711")
    began = as_session("4712")("begin")
    assert "ADOPTED" not in began.stdout
    assert "not adopted" in began.stdout
    assert _record_of(lb, "4711").is_file()


# ------------------------------------------------ a resume keeps its ledger (basecradle-noc#1715)


def _conversation_lab(lab):
    """Sessions of one repo keyed on their ``claude`` process, each in a conversation of its own:
    A (``claude`` 4711) pulls an image and runs a container, then may be killed."""
    lb = lab(images=[UBUNTU], lstart={"4711": 1792656776, "4712": 1792660000, "4713": 1792660100})

    def session(pid, conversation):
        env = lb.env | {"CLAUDE_PID": pid, "CLAUDE_CODE_SESSION_ID": conversation}
        return lambda *a: subprocess.run(
            [str(GUARD), *a], env=env, capture_output=True, text=True, cwd=lb.tmp, check=False
        )

    a = session("4711", "conversation-1")
    _ok(a("begin"))
    _ok(a("pull", "alpine:3.20"))
    _ok(a("run", "-d", "ubuntu:24.04"))
    return lb, session


def test_a_resume_adopts_the_ledger_its_crashed_predecessor_wrote(lab):
    """A resume is a new ``claude`` process in the same conversation, so it takes a new key, and the
    ledger the crashed session wrote sits under the old one. What it created, this conversation
    created: the resume adopts it out loud, and its sweep removes it."""
    lb, session = _conversation_lab(lab)
    (pulled,) = [i["id"] for i in lb.docker_state()["images"] if i["id"] != UBUNTU["id"]]
    (container,) = lb.docker_state()["containers"]
    _kill(lb, "4711")
    b = session("4712", "conversation-1")
    began = _ok(b("begin"))
    assert "ADOPTED: session claude-4711-" in began.stdout
    assert not list((_state_of(lb, "4711") / "records").iterdir())

    _ok(b("tabs-checked"))
    findings = _findings(b("check").stdout)
    assert container["id"] in findings and pulled in findings
    _ok(b("sweep"))
    st = lb.docker_state()
    assert [i["id"] for i in st["images"]] == [UBUNTU["id"]] and st["containers"] == []
    _ok(b("check"))


def test_an_ended_sessions_ledger_of_another_conversation_is_named_and_never_adopted(lab):
    """A stale ledger from a dead session is named on every ``begin`` and ``check``, never blocks a
    session it is not, and is never swept by one. Once what it records is gone from the engine, it
    clears itself."""
    lb, session = _conversation_lab(lab)
    _kill(lb, "4711")
    c = session("4713", "conversation-2")
    began = _ok(c("begin"))
    assert "ADOPTED" not in began.stdout
    assert "ended with a Docker ledger that still records 2 artefact(s)" in began.stdout
    _ok(c("tabs-checked"))
    got = _ok(c("check"))
    assert "not this session's: session claude-4711-" in got.stdout
    _ok(c("sweep"))
    assert len(lb.docker_state()["containers"]) == 1
    assert not any(call[:1] in (["rm"], ["rmi"]) for call in lb.calls_log())

    lb.mutate(containers=[], images=[UBUNTU])  # whoever clears them, by hand
    got = _ok(c("check"))
    assert "cleared ended session claude-4711-" in got.stdout
    assert not (_state_of(lb, "4711") / "records").exists()
    assert "ended with a Docker ledger" not in _ok(c("check")).stdout


def test_an_ended_sessions_unresolved_operation_is_dropped_by_name_so_its_ledger_clears(lab):
    lb, session = _conversation_lab(lab)
    _kill(lb, "4711")
    pending = _state_of(lb, "4711") / "pending"
    pending.mkdir(exist_ok=True)
    (pending / "1.2.3").write_text("busybox:1\npull\n4711\n")
    lb.mutate(containers=[], images=[UBUNTU])
    got = _ok(session("4713", "conversation-2")("begin"))
    assert "left a pull of busybox:1 unresolved" in got.stdout
    assert "cleared ended session claude-4711-" in got.stdout
    assert not list(pending.iterdir())


def test_a_live_sessions_ledger_is_neither_adopted_nor_named(lab):
    """A second process of the same conversation while the first still runs is not a resume."""
    lb, session = _conversation_lab(lab)
    began = _ok(session("4712", "conversation-1")("begin"))
    assert "ADOPTED" not in began.stdout
    assert "ended with a Docker ledger" not in began.stdout
    assert list((_state_of(lb, "4711") / "records").iterdir())


def test_a_stale_state_directory_whose_ledger_still_records_something_is_never_deleted(lab):
    """The stale-state sweep removes the guard's own leftovers past their window, but never a
    ledger that still records an artefact: deleted unread, the record would be lost."""
    lb, session = _conversation_lab(lab)
    _kill(lb, "4711")
    old = time.time() - 30 * 86400
    kept = _state_of(lb, "4711")
    os.utime(kept, (old, old))
    gone = lb.state_root / "claude-1-ThuOct010000002026"
    gone.mkdir()
    os.utime(gone, (old, old))
    _ok(session("4713", "conversation-2")("begin"))
    assert list((kept / "records").iterdir())
    assert not gone.exists()


# ---------------------------- overlapping builders share one Docker Desktop (basecradle-noc#1255)

OTHER_REPO = "basecradle-example"
OTHER_KEY = f"{OTHER_REPO}-builder"
UBUNTU = {"id": "sha256:" + "e" * 64, "tag": "ubuntu:24.04", "labelled": False}


def _overlap_lab(lab, tmp_path, **over):
    """Two builders of two carriers on one laptop: A (``claude`` 4711) runs this checkout's guard,
    B (4712) a copy under another repo's name, with its own label and state root. Only the
    laptop-wide Docker Desktop ledger is shared, as on the real laptop."""
    state = {
        "engine_up": False,
        "desktop_status": "stopped",
        "ps_lines": [],
        "ps_lines_after_start": [DESKTOP_LINE],
        "desktop_start_epoch": 1792656800,
        "lstart": {"4711": 1792656776, "4712": 1792656777},
    }
    state.update(over)
    lb = lab(**state)
    other = _carrier(tmp_path, f"SESSION_GUARD_REPO={OTHER_REPO}\n")

    def session(pid, guard, label, root):
        env = lb.env | {
            "CLAUDE_PID": pid,
            "DOCKER_STUB_LABEL": label,
            "SESSION_GUARD_STATE_ROOT": str(root),
        }
        return lambda *a: subprocess.run(
            [str(guard), *a], env=env, capture_output=True, text=True, cwd=lb.tmp, check=False
        )

    a = session("4711", GUARD, LABEL, lb.state_root)
    b = session("4712", other, f"{OTHER_KEY}=1", tmp_path / "other-sessions")
    return lb, a, b


def _ok(got):
    assert got.returncode == 0, got.stdout + got.stderr
    return got


def _clean_exit(run):
    """The check self-exit runs, then the state it clears: a session that can leave."""
    _ok(run("tabs-checked"))
    got = _ok(run("check"))
    assert "docker desktop start" not in got.stdout
    _ok(run("done"))
    return got


def _add(lb, kind, item):
    lb.mutate(**{kind: lb.docker_state()[kind] + [item]})


def _drop(lb, kind, ident):
    lb.mutate(**{kind: [x for x in lb.docker_state()[kind] if x["id"] != ident]})


@pytest.mark.parametrize("first_out", ["starter", "joiner"])
def test_overlapping_builders_leave_no_docker_desktop_and_no_image_in_either_exit_order(
    lab, tmp_path, first_out
):
    """The 2026-11-04 overlap: the harness builder started Docker Desktop, the router builder began
    beside it and read it as the laptop's own, and between them they left Docker Desktop running
    and an ``ubuntu:24.04`` on a laptop that had neither. A records starting it in the ledger, so B
    reads it as A's, and each answers for exactly what its own ledger records. In the starter-first
    order A quits Docker Desktop under B, and B's next ``docker-up`` starts its own, under which
    B's artefacts are still there for its sweep."""
    lb, a, b = _overlap_lab(lab, tmp_path)
    _ok(a("begin"))
    _ok(a("docker-up"))
    _ok(a("pull", "ubuntu:24.04"))
    _ok(a("run", "-d", "ubuntu:24.04", "sleep", "60"))

    began = _ok(b("begin"))
    assert "started by builder session claude-4711" in began.stdout
    assert "sibling session claude-4711" in _ok(b("docker-up")).stdout
    _ok(b("pull", "alpine:3.20"))
    _ok(b("run", "-d", "alpine:3.20", "sleep", "60"))
    a_container, b_container = (c["id"] for c in lb.docker_state()["containers"])

    if first_out == "starter":
        got = a("check")
        assert a_container in _findings(got.stdout)
        assert b_container not in _findings(got.stdout)
        _ok(a("sweep"))
        assert ["desktop", "stop"] in lb.calls_log()
        _clean_exit(a)

        assert "starting Docker Desktop" in _ok(b("docker-up")).stdout
        assert b_container in _findings(b("check").stdout)
        _ok(b("sweep"))
        _clean_exit(b)
    else:
        _ok(b("sweep"))
        assert ["desktop", "stop"] not in lb.calls_log()
        got = _clean_exit(b)
        assert "Docker Desktop is running for sibling session claude-4711" in got.stdout

        assert a_container in _findings(a("check").stdout)
        _ok(a("sweep"))
        _clean_exit(a)

    final = lb.docker_state()
    assert final["desktop_status"] == "stopped"
    assert final["images"] == []
    assert final["containers"] == []
    assert not list(_ledger(lb).iterdir())


def test_a_session_whose_own_leftovers_are_gone_exits_past_a_siblings_docker_desktop(lab, tmp_path):
    """The harness builder's refused exit at 22:56: its own Docker Desktop quit, its leftovers gone,
    and a Docker Desktop the router builder started since, which it correctly would not quit, held
    it open. A sibling's Docker Desktop, proved by the ledger, is named and never blocks."""
    lb, a, b = _overlap_lab(lab, tmp_path)
    _ok(a("begin"))
    _ok(a("docker-up"))
    _ok(a("sweep"))
    _ok(b("begin"))
    _ok(b("docker-up"))
    _add(lb, "containers", {**FOREIGN_CONTAINER, "labels": {OTHER_KEY: "1"}})

    got = _clean_exit(a)
    assert "Docker Desktop is running for sibling session claude-4712" in got.stdout
    assert lb.docker_state()["desktop_status"] == "running"
    assert lb.calls_log().count(["desktop", "stop"]) == 1  # A's own, never B's


def test_a_session_that_never_used_docker_exits_past_a_sibling_engine_either_way(lab, tmp_path):
    """No Docker use at all: a sibling's engine that came up after this session began needs no look
    here, and one that was up at ``begin`` going down is the sibling's sweep."""
    _, a, b = _overlap_lab(lab, tmp_path)
    _ok(a("begin"))
    _ok(b("begin"))
    _ok(b("docker-up"))
    got = _clean_exit(a)
    assert "Docker ledger: empty — nothing this session made is left on the engine" in got.stdout

    _ok(a("begin"))  # a fresh session of A, beside B's running engine
    _ok(b("sweep"))
    got = _clean_exit(a)
    assert "has been quit since" in got.stdout


def test_a_sibling_that_begins_while_docker_desktop_boots_reads_it_as_the_starters(lab, tmp_path):
    """The record is written before the start, with no processes yet: a sibling that begins while
    Docker Desktop boots must not read it as the laptop's own, the misreading #1255 began with."""
    lb, a, b = _overlap_lab(lab, tmp_path, desktop_boots_slowly=True)
    _ok(a("begin"))
    assert a("docker-up").returncode != 0  # the engine is still booting when the wait runs out
    assert lb.docker_state()["desktop_status"] == "running"
    assert "started by builder session claude-4711" in _ok(b("begin")).stdout


def test_a_docker_desktop_running_before_any_builder_is_never_quit_by_either(lab, tmp_path):
    """No record accounts for it, so it is the laptop's own: both use it, and neither quits it."""
    lb, a, b = _overlap_lab(
        lab,
        tmp_path,
        engine_up=True,
        desktop_status="running",
        ps_lines=[DESKTOP_LINE],
        lstart={"4711": 1792656776, "4712": 1792656777, DESKTOP_PID: 1792600000},
    )
    for run in (a, b):
        assert "the laptop's own, never quit" in _ok(run("begin")).stdout
        _ok(run("docker-up"))
    for run in (b, a):
        _ok(run("sweep"))
        _clean_exit(run)
    assert ["desktop", "stop"] not in lb.calls_log()
    assert lb.docker_state()["desktop_status"] == "running"


def test_a_sibling_that_ended_without_quitting_is_adopted_by_a_running_sessions_sweep(
    lab, tmp_path
):
    """B was already running when A was killed, so no ``begin`` of B's saw an orphan: B's sweep
    adopts the Docker Desktop A recorded, from another carrier's ledger record, and quits it."""
    lb, a, b = _overlap_lab(lab, tmp_path)
    _ok(a("begin"))
    _ok(a("docker-up"))
    _ok(b("begin"))
    _kill(lb, "4711")
    _ok(b("tabs-checked"))
    got = b("check")
    assert "which has ended without quitting it" in _findings(got.stdout)
    swept = _ok(b("sweep"))
    assert "ADOPTED" in swept.stdout
    assert ["desktop", "stop"] in lb.calls_log()
    _clean_exit(b)
    assert not list(_ledger(lb).iterdir())


def test_an_image_this_session_pulled_keeps_its_own_docker_desktop_up_until_removed(lab):
    """A pulled image carries no label. Quitting the engine over it would take it out of ``check``'s
    sight, and the session would exit clean with the image on the laptop."""
    lb = lab(engine_up=False, desktop_status="stopped", ps_lines=[], rmi_fails=True)
    _ok(lb.run("begin"))
    _ok(lb.run("docker-up"))
    lb.mutate(ps_lines=[DESKTOP_LINE])
    _ok(lb.run("pull", "ubuntu:24.04"))
    swept = lb.run("sweep")
    assert swept.returncode != 0
    assert ["desktop", "stop"] not in lb.calls_log()
    lb.mutate(rmi_fails=False)
    _ok(lb.run("sweep"))
    assert ["desktop", "stop"] in lb.calls_log()
    assert lb.docker_state()["images"] == []


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


def test_a_carriers_label_builder_and_state_root_come_from_its_config(lab, tmp_path):
    """The one per-repo value: the label every artefact carries, the name of the session's own
    builder and the default state root (where the ledger lives) are all derived from it, so the
    same bytes serve every carrier. The other carrier is a name no real one holds, and the builder
    it must leave alone is this checkout's own, so the copy's config, never the checkout it was
    copied from, is what decides."""
    other = "basecradle-example"
    guard = _carrier(tmp_path, f"# a comment\n\nSESSION_GUARD_REPO={other}\n")
    lb = lab(images=[UBUNTU], builders=["desktop-linux", f"{BUILDER_PREFIX}y"])
    env = lb.env | {"DOCKER_STUB_LABEL": f"{other}-builder=1", "HOME": str(tmp_path)}
    del env["SESSION_GUARD_STATE_ROOT"]

    def g(*a):
        return subprocess.run(
            [str(guard), *a], env=env, capture_output=True, text=True, cwd=lb.tmp, check=False
        )

    assert g("begin").returncode == 0
    state = tmp_path / f".cache/{other}/builder-sessions/test-session"
    assert (state / "began_at").is_file()
    _ok(g("run", "-d", "ubuntu:24.04"))
    _ok(g("build", "."))
    calls = lb.calls_log()
    (run,) = [c for c in calls if c[:1] == ["run"]]
    assert run[2].startswith(str(state / "containers"))
    assert run[5:7] == ["--label", f"{other}-builder=1"]
    (create,) = [c for c in calls if c[:2] == ["buildx", "create"]]
    assert create[create.index("--name") + 1] == f"{other}-builder-test-session"

    assert g("sweep").returncode == 0
    calls = lb.calls_log()
    assert ["buildx", "rm", f"{other}-builder-test-session"] in calls
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
    assert "listening-port census:" not in got.stdout  # no count from a census that failed
