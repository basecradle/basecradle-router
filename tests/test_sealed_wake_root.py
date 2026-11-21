"""The whole real ``wake-runner`` and ``set-wake-bin``, as root, against a sealed box (#535).

``tests/test_wake_runner_sealed.py`` proves the gate's functions offline. What only root can
show is proven here: the script as it runs on the box, from its own constant paths
(``/etc/basecradle-router/agents.json``, ``/opt/basecradle-harness``), against trees root built
for the agent's group out of real ``uv`` environments, through the real privilege drop. Each
test builds a fresh box in a private mount namespace (``tests/sealed_wake_box.py``), with
overlays whose upper layers are a tmpfs, so nothing it makes reaches the real ``/etc``,
``/opt`` or ``/home``, and it is all gone when the namespace's one process exits.

The hostile cases are the issue's: an agent, or a misbuilt seal, that offers a symlinked
parent, a group-writable parent, a parent root does not own, another agent's tree, a ``..``
escape, a planted link, a set-id binary or interpreter, or a home path once sealed, gets a
refusal that names the check, and nothing is run. A probe proves the boundary with no model
(and no token); where the journal is reachable, a real wake proves the binary actually ran,
as the agent, from the sealed tree.

It needs Linux, root, ``unshare``, overlayfs, ``runuser``, ``useradd``, ``jq``, ``flock``,
``uv`` and ``/usr/bin/python3``, so it skips on the laptop. CI runs it as root with
``SEALED_WAKE_ROOT_REQUIRED=1``, which turns any skip into a failure: a proof that silently
did not run is not a proof. Fabricated cast only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).resolve().parent / "sealed_wake_box.py"
REQUIRED = os.environ.get("SEALED_WAKE_ROOT_REQUIRED") == "1"

BASE = "/opt/basecradle-harness"
TREE = f"{BASE}/venv/nova"
SEALED_BIN = f"{TREE}/bin/basecradle-harness-wake"
HOME_BIN = "/home/nova/venv/bin/basecradle-harness-wake"
JT_BIN = f"{BASE}/venv/jt/bin/basecradle-harness-wake"
WAKE_RUNNER = "/opt/basecradle-router/bin/wake-runner"
SET_WAKE_BIN = "/opt/basecradle-router/bin/set-wake-bin"
CLONE = "/home/nova/harness"
TIMELINE = "0192f3a4-5b6c-7d8e-9f01-0000000000cd"
PROBE = [WAKE_RUNNER, "--user", "nova", "--cwd", CLONE, "--probe", "{marker}"]


def real_wake(wake_bin: str = SEALED_BIN) -> list[str]:
    return [WAKE_RUNNER, "--user", "nova", "--cwd", CLONE, "--", wake_bin, "--timeline", TIMELINE]


def _missing() -> str | None:
    if not sys.platform.startswith("linux"):
        return "Linux only: mount namespaces, overlayfs and runuser are under test"
    if os.geteuid() != 0:
        return "needs root: the privilege drop and root-owned trees are under test"
    for tool in ("unshare", "runuser", "useradd", "groupadd", "jq", "flock", "getent", "uv"):
        if shutil.which(tool) is None:
            return f"{tool} is not installed"
    if not Path("/usr/bin/python3").exists():
        return "/usr/bin/python3 is the interpreter the box's venvs link to"
    if not Path("/proc/filesystems").read_text().count("overlay"):
        return "overlayfs is not available"
    return None


@pytest.fixture(autouse=True)
def _require() -> None:
    reason = _missing()
    if reason:
        if REQUIRED:
            pytest.fail(f"SEALED_WAKE_ROOT_REQUIRED=1 but the proof cannot run: {reason}")
        pytest.skip(reason)


def box(
    tmp_path: Path,
    runs: list[list[str] | dict],
    *,
    nova_sealed: bool = True,
    nova_tree: bool = True,
    mutations: tuple[str, ...] = (),
    check_paths: tuple[str, ...] = (),
) -> dict:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    spec = {
        "repo": str(REPO),
        "scratch": str(scratch),
        "uv": shutil.which("uv"),
        "python": "/usr/bin/python3",
        "nova_sealed": nova_sealed,
        "nova_tree": nova_tree,
        "mutations": list(mutations),
        "check_paths": list(check_paths),
        "runs": [run if isinstance(run, dict) else {"argv": run} for run in runs],
    }
    done = subprocess.run(
        ["unshare", "--mount", "--propagation", "private", "--",
         sys.executable, "-I", str(DRIVER), json.dumps(spec)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )  # fmt: skip
    assert done.returncode == 0, f"the box did not build:\n{done.stderr}"
    return json.loads(done.stdout)


def refused(result: dict, needle: str) -> None:
    assert result["rc"] == 1, result
    assert needle in result["stderr"], result["stderr"]


# --------------------------------------------------------------------------- #
# It launches from the sealed tree, and from the home only while unsealed.      #
# --------------------------------------------------------------------------- #


def test_the_box_builds_a_real_uv_environment_the_way_the_noc_does(tmp_path: Path) -> None:
    shape = (
        f'test -L {TREE}/bin/python && head -n1 {SEALED_BIN} >"$SCRATCH/shape" '
        f"&& stat -c '%U:%G %a' {TREE} {TREE}/bin {SEALED_BIN} >>\"$SCRATCH/shape\""
    )
    out = box(tmp_path, [["sh", "-c", 'cat "$SCRATCH/shape"']], mutations=(shape,))
    lines = out["runs"][0]["stdout"].splitlines()
    # A box path is short, so uv names the interpreter link directly in the `#!` line.
    assert lines[0] == f"#!{TREE}/bin/python"
    assert lines[1:] == ["harness-up-nova:nova 2750", "harness-up-nova:nova 2750",
                         "harness-up-nova:nova 755"]  # fmt: skip


def test_a_sealed_probe_proves_the_boundary_at_the_sealed_binary(tmp_path: Path) -> None:
    (probe,) = box(tmp_path, [PROBE])["runs"]
    assert probe["rc"] == 0, probe
    assert probe["stdout"].startswith("BCNOC1-ACK ")
    stated = f"acked as nova in {CLONE}; a real wake would have exec'd {SEALED_BIN}"
    assert stated in probe["stdout"]


def test_a_sealed_real_wake_runs_the_sealed_binary_as_the_agent(tmp_path: Path) -> None:
    out = box(tmp_path, [real_wake()])
    if not out["journal"]:
        if REQUIRED:
            pytest.fail("the journal is not reachable, so a real wake cannot run here")
        pytest.skip("the journal is not reachable (a container): the probe proves the gate")
    assert out["runs"][0]["rc"] == 0, out["runs"][0]
    assert out["woke"] == f"nova\n{SEALED_BIN}\n--timeline\n{TIMELINE}\n"


def test_an_unsealed_agent_still_wakes_from_its_home(tmp_path: Path) -> None:
    out = box(tmp_path, [PROBE], nova_sealed=False, nova_tree=False)
    assert out["runs"][0]["rc"] == 0, out["runs"][0]
    assert f"would have exec'd {HOME_BIN}" in out["runs"][0]["stdout"]


# --------------------------------------------------------------------------- #
# Refused, by name, and nothing runs.                                           #
# --------------------------------------------------------------------------- #


def _refused_both_ways(tmp_path: Path, needle: str, wake_bin: str = SEALED_BIN, **kwargs) -> None:
    out = box(tmp_path, [PROBE, real_wake(wake_bin)], **kwargs)
    for result in out["runs"]:
        refused(result, needle)
    assert out["woke"] is None, "a refused wake ran something"


def test_the_home_path_is_refused_once_sealed(tmp_path: Path) -> None:
    _refused_both_ways(tmp_path, "nova is sealed", wake_bin=HOME_BIN, nova_sealed=False)


def test_a_sealed_registry_with_no_tree_is_refused_never_sent_home(tmp_path: Path) -> None:
    _refused_both_ways(tmp_path, "is not a real directory", nova_tree=False)


def test_a_symlinked_parent_is_refused(tmp_path: Path) -> None:
    _refused_both_ways(
        tmp_path,
        f"sealed parent {BASE}/venv is not a real directory",
        mutations=(f"mv {BASE}/venv /opt/elsewhere && ln -s /opt/elsewhere {BASE}/venv",),
    )


def test_a_group_writable_parent_is_refused(tmp_path: Path) -> None:
    _refused_both_ways(
        tmp_path,
        f"{BASE} is writable by its group or by others",
        mutations=(f"chmod 0775 {BASE}",),
    )


def test_a_parent_root_does_not_own_is_refused(tmp_path: Path) -> None:
    _refused_both_ways(
        tmp_path,
        f"sealed parent {BASE}/venv is not owned by root",
        mutations=(f"chown nova {BASE}/venv",),
    )


def test_another_agents_tree_is_refused(tmp_path: Path) -> None:
    flip = f'.nova.wake_bin = "{JT_BIN}"'
    _refused_both_ways(
        tmp_path,
        f"is not under {TREE}",
        wake_bin=JT_BIN,
        mutations=(_registry_edit(flip),),
    )


def test_an_escape_with_dot_dot_is_refused(tmp_path: Path) -> None:
    escape = f"{TREE}/../jt/bin/basecradle-harness-wake"
    _refused_both_ways(
        tmp_path,
        "has a .. segment",
        wake_bin=escape,
        mutations=(_registry_edit(f'.nova.wake_bin = "{escape}"'),),
    )


def test_an_unsealed_agents_planted_link_to_another_tree_is_refused(tmp_path: Path) -> None:
    # The agent writes its own home, so it can swap its venv for a link to jt's sealed tree.
    plant = (
        "runuser -u nova -- sh -c "
        f"'mv /home/nova/venv /home/nova/v0 && ln -s {BASE}/venv/jt /home/nova/venv'"
    )
    _refused_both_ways(
        tmp_path, "symlink", wake_bin=HOME_BIN, nova_sealed=False, nova_tree=False,
        mutations=(plant,),
    )  # fmt: skip


def test_the_agent_cannot_plant_anything_in_the_sealed_tree(tmp_path: Path) -> None:
    attempts = [
        {"argv": ["mv", TREE, f"{BASE}/venv/nova-old"], "as": "nova"},
        {"argv": ["ln", "-sf", HOME_BIN, SEALED_BIN], "as": "nova"},
        {"argv": ["chmod", "0777", f"{TREE}/bin"], "as": "nova"},
        {"argv": ["touch", f"{TREE}/bin/planted"], "as": "nova"},
        PROBE,
    ]  # fmt: skip
    out = box(tmp_path, attempts)
    for attempt in out["runs"][:-1]:
        assert attempt["rc"] != 0, attempt
    assert out["runs"][-1]["rc"] == 0, out["runs"][-1]


def test_a_tree_the_agent_owns_is_refused(tmp_path: Path) -> None:
    _refused_both_ways(
        tmp_path,
        "is owned by nova; a sealed tree is never the agent's own",
        mutations=(f"chown -R --no-dereference nova {TREE}",),
    )


def test_a_setuid_binary_is_refused(tmp_path: Path) -> None:
    _refused_both_ways(tmp_path, "is setuid or setgid", mutations=(f"chmod u+s {SEALED_BIN}",))


def test_a_setgid_interpreter_behind_the_link_is_refused(tmp_path: Path) -> None:
    repoint = (
        "install -o root -g root -m 2755 /usr/bin/python3 /opt/python-setgid "
        f"&& ln -sfn /opt/python-setgid {TREE}/bin/python"
    )
    _refused_both_ways(tmp_path, "is setuid or setgid", mutations=(repoint,))


def test_an_interpreter_the_agent_owns_behind_the_link_is_refused(tmp_path: Path) -> None:
    repoint = (
        "install -o nova -g nova -m 0755 /usr/bin/python3 /opt/python-novas "
        f"&& ln -sfn /opt/python-novas {TREE}/bin/python"
    )
    _refused_both_ways(tmp_path, "is owned by nova or router", mutations=(repoint,))


def test_a_router_owned_tree_is_refused(tmp_path: Path) -> None:
    # The daemon is the account this wrapper fences: a tree it owned, it could rewrite.
    _refused_both_ways(
        tmp_path,
        "is owned by router; a sealed tree is never the router's",
        mutations=(f"chown -R --no-dereference router {TREE}",),
    )


def test_a_tree_entry_with_an_access_control_list_is_refused(tmp_path: Path) -> None:
    if shutil.which("setfacl") is None:
        if REQUIRED:
            pytest.fail("setfacl is not installed, so the ACL case cannot run")
        pytest.skip("setfacl is not installed")
    # The mask cut to r-x, so the mode bits show no write and only the list itself is left.
    grant = f'setfacl -m u:nova:rwx,m::rx {TREE}/bin 2>/dev/null || touch "$SCRATCH/no-acl"'
    supported = ["sh", "-c", 'test ! -e "$SCRATCH/no-acl"']
    out = box(tmp_path, [supported, PROBE, real_wake()], mutations=(grant,))
    if out["runs"][0]["rc"] != 0:
        reason = "this box's tmpfs refuses an access control list (a container's kernel)"
        if REQUIRED:
            pytest.fail(f"the ACL case cannot run: {reason}")
        pytest.skip(reason)
    for result in out["runs"][1:]:
        refused(result, f"{TREE}/bin carries an access control list")
    assert out["woke"] is None, "a refused wake ran something"


INJECT = (
    "install -d -o nova -g nova /home/nova/inject && printf '%s\\n' "
    '\'open("/home/nova/harness/INJECTED", "w").close()\' >/home/nova/inject/sitecustomize.py '
    "&& printf 'PYTHONPATH=/home/nova/inject\\n' >>/home/nova/.config/basecradle/agent.env"
)


def test_the_injection_is_live_on_this_box(tmp_path: Path) -> None:
    # The positive control for the two tests below: the mutation lands its line in agent.env,
    # and the planted sitecustomize.py does run under that PYTHONPATH as nova, so its absence
    # there is the scrub's doing and not a sitecustomize that never loads.
    env_line = [
        "grep",
        "-qx",
        "PYTHONPATH=/home/nova/inject",
        "/home/nova/.config/basecradle/agent.env",
    ]
    loads = {
        "argv": ["env", "PYTHONPATH=/home/nova/inject", "/usr/bin/python3", "-c", "pass"],
        "as": "nova",
    }
    out = box(tmp_path, [env_line, loads], mutations=(INJECT,), check_paths=(f"{CLONE}/INJECTED",))
    assert [run["rc"] for run in out["runs"]] == [0, 0], out["runs"]
    assert out["exists"] == {f"{CLONE}/INJECTED": True}


def test_a_sealed_agents_env_file_cannot_load_its_own_code(tmp_path: Path) -> None:
    # The seal's point: a PYTHONPATH in the agent's own env file to a sitecustomize.py in its
    # home would run the agent's code inside the sealed interpreter, before the harness.
    out = box(tmp_path, [PROBE], mutations=(INJECT,), check_paths=(f"{CLONE}/INJECTED",))
    assert out["runs"][0]["rc"] == 0, out["runs"][0]
    assert out["exists"] == {f"{CLONE}/INJECTED": False}, "agent code ran in a sealed wake"


def test_no_agents_probe_runs_its_injected_code(tmp_path: Path) -> None:
    # Sealed or not, a probe runs only our verifier (#538): an agent that could load code into it
    # could ack a marker its secret does not verify.
    out = box(
        tmp_path, [PROBE], nova_sealed=False, nova_tree=False,
        mutations=(INJECT,), check_paths=(f"{CLONE}/INJECTED",),
    )  # fmt: skip
    assert out["runs"][0]["rc"] == 0, out["runs"][0]
    assert out["exists"] == {f"{CLONE}/INJECTED": False}, "agent code ran in the probe verifier"


#: A usercustomize.py in nova's own user site, which a Python not run with -I imports at start.
USER_SITE = (
    "site=$(runuser -u nova -- /usr/bin/python3 -c "
    "'import site; print(site.getusersitepackages())')"
    " && runuser -u nova -- mkdir -p \"$site\" && printf '%s\\n' "
    '\'open("/home/nova/harness/INJECTED", "w").close()\' '
    '| runuser -u nova -- tee "$site/usercustomize.py" >/dev/null'
)

#: An LD_LIBRARY_PATH in nova's agent.env to a directory of empty files named for every library
#: python3 links: should the loader see it, python3 does not start.
LOADER = (
    "install -d -o nova -g nova /home/nova/lib && for lib in "
    "$(ldd /usr/bin/python3 | awk '/=>/ {print $1}'); do "
    "runuser -u nova -- touch /home/nova/lib/$lib; done "
    "&& printf 'LD_LIBRARY_PATH=/home/nova/lib\\n' >>/home/nova/.config/basecradle/agent.env"
)


def test_no_agents_probe_imports_its_user_site(tmp_path: Path) -> None:
    # What `-I` alone keeps out of the verifier: the agent's own user site directory (#538).
    out = box(
        tmp_path, [PROBE], nova_sealed=False, nova_tree=False,
        mutations=(USER_SITE,), check_paths=(f"{CLONE}/INJECTED",),
    )  # fmt: skip
    assert out["runs"][0]["rc"] == 0, out["runs"][0]
    assert out["exists"] == {f"{CLONE}/INJECTED": False}, "the verifier imported the agent's site"


def test_no_agents_loader_variable_reaches_the_probe(tmp_path: Path) -> None:
    # What the scrub alone keeps out: the loader's variables, which `-I` does not touch (#538).
    # The control is in the same box: the same env, run as nova without the scrub, fails to start.
    control = {
        "argv": ["env", "LD_LIBRARY_PATH=/home/nova/lib", "/usr/bin/python3", "-c", "pass"],
        "as": "nova",
    }
    out = box(
        tmp_path, [control, PROBE], nova_sealed=False, nova_tree=False, mutations=(LOADER,),
    )  # fmt: skip
    assert out["runs"][0]["rc"] != 0, "the planted libraries do not break python3 here"
    assert out["runs"][1]["rc"] == 0, out["runs"][1]


def test_a_sealed_real_wake_runs_none_of_the_agents_injected_code(tmp_path: Path) -> None:
    out = box(tmp_path, [real_wake()], mutations=(INJECT,), check_paths=(f"{CLONE}/INJECTED",))
    if not out["journal"]:
        if REQUIRED:
            pytest.fail("the journal is not reachable, so a real wake cannot run here")
        pytest.skip("the journal is not reachable (a container): the probe proves the scrub")
    assert out["runs"][0]["rc"] == 0, out["runs"][0]
    assert out["woke"] == f"nova\n{SEALED_BIN}\n--timeline\n{TIMELINE}\n"
    assert out["exists"] == {f"{CLONE}/INJECTED": False}, "agent code ran in a sealed wake"


def test_a_router_owned_interpreter_behind_the_link_is_refused(tmp_path: Path) -> None:
    repoint = (
        "install -o router -g router -m 0755 /usr/bin/python3 /opt/python-routers "
        f"&& ln -sfn /opt/python-routers {TREE}/bin/python"
    )
    _refused_both_ways(tmp_path, "is owned by nova or router", mutations=(repoint,))


def test_a_daemon_still_holding_the_old_value_is_refused(tmp_path: Path) -> None:
    # Registry flipped, daemon not restarted: it still passes the home path as argv[1].
    out = box(tmp_path, [real_wake(HOME_BIN)])
    refused(out["runs"][0], "must be the registered wake_bin")
    assert out["woke"] is None


# --------------------------------------------------------------------------- #
# set-wake-bin: one field, judged by the same gate, written atomically.         #
# --------------------------------------------------------------------------- #


def _registry_edit(expression: str) -> str:
    registry = "/etc/basecradle-router/agents.json"
    return f'jq \'{expression}\' {registry} >"$SCRATCH/r" && cat "$SCRATCH/r" >{registry}'


def _without_novas_wake_bin(text: str) -> dict:
    document = json.loads(text)
    del document["nova"]["wake_bin"]
    return document


def test_set_wake_bin_seals_an_agent_and_changes_nothing_else(tmp_path: Path) -> None:
    # The settled order: tree built at its final name inside the hold, then the flip.
    out = box(
        tmp_path,
        [PROBE, [SET_WAKE_BIN, "nova", SEALED_BIN], PROBE],
        nova_sealed=False,
    )
    before_flip, flip, after_flip = out["runs"]
    refused(before_flip, "nova is sealed")
    assert flip["rc"] == 0, flip
    assert f"nova wake_bin {HOME_BIN} -> {SEALED_BIN}" in flip["stdout"]
    assert json.loads(out["registry_after"])["nova"]["wake_bin"] == SEALED_BIN
    assert _without_novas_wake_bin(out["registry_after"]) == _without_novas_wake_bin(
        out["registry_before"]
    )
    assert out["registry_owner"] == [0, out["router_gid"], "0o640"]
    assert out["staging_left"] is False
    assert after_flip["rc"] == 0, after_flip
    assert f"would have exec'd {SEALED_BIN}" in after_flip["stdout"]


def _setter_refused(tmp_path: Path, args: list[str], needle: str, **kwargs) -> None:
    out = box(tmp_path, [[SET_WAKE_BIN, *args]], **kwargs)
    refused(out["runs"][0], needle)
    assert out["registry_after"] == out["registry_before"], "a refusal wrote the registry"
    assert out["staging_left"] is False


def test_set_wake_bin_refuses_a_sealed_path_with_no_tree(tmp_path: Path) -> None:
    _setter_refused(
        tmp_path, ["nova", SEALED_BIN], "is not a real directory",
        nova_sealed=False, nova_tree=False,
    )  # fmt: skip


def test_set_wake_bin_refuses_the_home_while_the_tree_exists(tmp_path: Path) -> None:
    _setter_refused(tmp_path, ["nova", HOME_BIN], "nova is sealed")


def test_set_wake_bin_unseals_once_the_tree_is_gone(tmp_path: Path) -> None:
    out = box(tmp_path, [[SET_WAKE_BIN, "nova", HOME_BIN], PROBE], mutations=(f"rm -rf {TREE}",))
    assert out["runs"][0]["rc"] == 0, out["runs"][0]
    assert f"would have exec'd {HOME_BIN}" in out["runs"][1]["stdout"]


def test_set_wake_bin_refuses_another_agents_tree(tmp_path: Path) -> None:
    _setter_refused(tmp_path, ["nova", JT_BIN], f"is not under {TREE}")


def test_set_wake_bin_refuses_a_builder_entry(tmp_path: Path) -> None:
    _setter_refused(tmp_path, ["basecradle/basecradle-ruby", JT_BIN], "only a harness agent")


def test_set_wake_bin_refuses_an_unknown_agent(tmp_path: Path) -> None:
    _setter_refused(tmp_path, ["rowan", SEALED_BIN], "no agent rowan in the registry")


def test_set_wake_bin_refuses_a_setuid_binary(tmp_path: Path) -> None:
    _setter_refused(
        tmp_path, ["nova", SEALED_BIN], "is setuid or setgid",
        nova_sealed=False, mutations=(f"chmod u+s {SEALED_BIN}",),
    )  # fmt: skip


def test_set_wake_bin_refuses_a_directory(tmp_path: Path) -> None:
    _setter_refused(tmp_path, ["nova", f"{TREE}/bin"], "is not a regular file", nova_sealed=False)


def test_set_wake_bin_names_an_empty_os_user_rather_than_shifting_fields(tmp_path: Path) -> None:
    _setter_refused(
        tmp_path, ["nova", SEALED_BIN], "agent nova has no os_user",
        mutations=(_registry_edit('.nova.os_user = ""'),),
    )  # fmt: skip


def test_set_wake_bin_writes_nothing_for_the_value_already_there(tmp_path: Path) -> None:
    out = box(tmp_path, [[SET_WAKE_BIN, "nova", SEALED_BIN]])
    assert out["runs"][0]["rc"] == 0
    assert "nothing written" in out["runs"][0]["stdout"]
    assert out["registry_after"] == out["registry_before"]


def test_set_wake_bin_refuses_to_run_as_anyone_but_root(tmp_path: Path) -> None:
    out = box(tmp_path, [{"argv": [SET_WAKE_BIN, "nova", SEALED_BIN], "as": "nova"}])
    refused(out["runs"][0], "must run as root")
    assert out["registry_after"] == out["registry_before"]
