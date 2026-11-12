r"""The shipped Vector config's VRL, run by `vector test` (#380, #398).

`ai_otel_llm` and `ai_scrub` are VRL, and their output is the exact line Better Stack receives,
so no Python reading of the YAML can prove it. This runs the real thing: `vector test` executes
the transforms over the fabricated events in every suite under `tests/vector/` and asserts the
bytes. It runs every suite against every host's shipped `vector.yaml`, on the Vector release the
boxes run (deploy/README → `VECTOR_VERSION`), so a VRL behaviour that changed between releases
cannot pass here and fail on a box.

The suites:

* `ai_otel_llm.yaml` — a Claude Code model call rendered as the fleet's `llm` line (#380).
* `ai_scrub.yaml` — the redaction every shipped line passes: the literal rules before the prefix
  rules, and the harness's word rule (#398). Python's `re` cannot stand in for it: it has no
  `\p{L}` classes, and the word rule is a closure, so the real engine is the only test.

CI installs that release (ci.yml → "Install Vector"), so there the binary is required. A
laptop without it, or with a different release, skips: point `VECTOR_BIN` at the pinned
release to run it locally.

Every run carries the environment the boxes' `vector.service` runs under (#441): from Vector 0.57
a config's placeholders are filled only when `VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION` is
on, and the boxes switch it on with a fixed drop-in. Off, a placeholder loads as its own literal
text and every check here would still pass, so a run without it would prove a load the unit
never performs. Each host's file is also validated as the unit loads it, and a run with a sink
token withheld must fail, so the switch is shown to be live, never assumed.

No network, model or agent: `vector test` runs transforms only, never a source or a sink, and
`vector validate --no-environment` builds the topology without a component or health check.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
README = REPO / "deploy" / "README.md"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
HOSTS_DIR = REPO / "deploy" / "hosts"
SUITES = sorted((REPO / "tests" / "vector").glob("*.yaml"))
HOSTS = sorted(p.name for p in HOSTS_DIR.iterdir() if p.is_dir())

#: The switch the boxes' unit sets, in its drop-in
#: `/etc/systemd/system/vector.service.d/20-env-interpolation.conf`.
INTERPOLATION = "VECTOR_DANGEROUSLY_ALLOW_ENV_VAR_INTERPOLATION"

#: A fabricated value for every variable a shipped vector.yaml names: every host's Better Stack
#: token, and the Axiom token of ubuntu-1's owner destination (#442). None leaves the process:
#: neither `vector test` nor `vector validate --no-environment` builds a sink.
SINK_TOKENS = {
    "AXIOM_INGEST_TOKEN": "fabricated-axiom-token",
    "BETTERSTACK_AI_SOURCE_TOKEN": "fabricated-test-token",
}


#: The variables each host's unit supplies, declared here rather than read off the file under
#: test: a host's file that starts reading a variable its unit does not carry fails to load on the
#: box, so it must fail here until this contract (and the box) says the unit carries it. Every
#: host's unit carries its Better Stack token; ubuntu-1's also carries its owner's Axiom token,
#: which the NOC places on that box before #442's config may reach it.
UNIT_VARIABLES = {
    "ai.basecradle.com": ["BETTERSTACK_AI_SOURCE_TOKEN"],
    "red.basecradle.com": ["BETTERSTACK_AI_SOURCE_TOKEN"],
    "ubuntu-1.landfill.click": ["AXIOM_INGEST_TOKEN", "BETTERSTACK_AI_SOURCE_TOKEN"],
}


def _named(host: str) -> list[str]:
    """The variables a host's vector.yaml reads."""
    text = (HOSTS_DIR / host / "vector.yaml").read_text()
    return sorted(set(re.findall(r"\$\{(\w+)\}", text)))


def _tokens(host: str) -> dict[str, str]:
    """The variables the host's unit carries, each with its fabricated value."""
    return {name: SINK_TOKENS[name] for name in UNIT_VARIABLES[host]}


def _env(variables: dict[str, str]) -> dict[str, str]:
    """The unit's environment: interpolation on, plus exactly the variables given."""
    return {"PATH": os.environ.get("PATH", ""), "NO_COLOR": "1", INTERPOLATION: "true", **variables}


def _pinned_version() -> str:
    match = re.search(r"^VECTOR_VERSION=(\S+)$", README.read_text(), re.MULTILINE)
    assert match, "deploy/README.md must pin VECTOR_VERSION — the release the boxes install"
    return match.group(1)


def _vector() -> str:
    """The pinned Vector binary; required in CI, a skip anywhere else."""
    binary = os.environ.get("VECTOR_BIN") or shutil.which("vector")
    if binary is None:
        assert not os.environ.get("CI"), "Vector is missing on the CI runner"
        pytest.skip("vector is not installed (set VECTOR_BIN to the pinned release)")
    got = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False)
    pinned = _pinned_version()
    if not got.stdout.startswith(f"vector {pinned} "):
        assert not os.environ.get("CI"), f"CI runs {got.stdout.strip()!r}, not Vector {pinned}"
        pytest.skip(f"{got.stdout.strip()!r} is not the boxes' Vector {pinned}")
    return binary


def test_ci_installs_the_release_the_boxes_run() -> None:
    # The proof below is only as good as the Vector it runs on. A CI pin that drifted from the
    # boxes' would green VRL the box's release rejects, or renders differently.
    pinned = _pinned_version()
    assert f'VECTOR_VERSION: "{pinned}"' in CI_WORKFLOW.read_text()


def test_every_suite_is_run() -> None:
    # The glob below is the whole list: a suite named here and missing from disk, or the scrub's
    # suite renamed out of the glob, would otherwise leave the redaction untested and green.
    assert {suite.name for suite in SUITES} >= {"ai_otel_llm.yaml", "ai_scrub.yaml"}


def test_the_readme_recipe_switches_interpolation_on_as_the_unit_does() -> None:
    # The recipe is how a box gets the drop-in; one that drifted from these bytes would install
    # a unit that loads its placeholders as text, and a validate run without the switch would pass
    # it. The NOC's guard reads the drop-in back by these same bytes.
    recipe = README.read_text()
    dropin = "/etc/systemd/system/vector.service.d/20-env-interpolation.conf"
    dropin_step = (
        f"printf '[Service]\\nEnvironment={INTERPOLATION}=true\\n' \\\n  | sudo tee {dropin}"
    )
    assert dropin_step in recipe
    # Every command line in a bash block that runs `vector validate`, however it is indented or
    # wrapped; a comment line naming the command is prose, not a run.
    commands = [
        line.strip()
        for block in re.findall(r"^```bash\n(.*?)^```$", recipe, re.MULTILINE | re.DOTALL)
        for line in block.splitlines()
        if not line.strip().startswith("#")
    ]
    validates = [line for line in commands if "vector validate" in line]
    assert validates, "deploy/README.md must validate the config before starting Vector"
    for line in validates:
        assert f"{INTERPOLATION}=true vector validate" in line, line


def test_the_readme_recipe_verifies_the_package_and_lets_vector_read_the_journal() -> None:
    # Two first-install steps that fail silently when missing (provision-server §5): an
    # unverified .deb installs whatever the download returned, and a `vector` user outside
    # `systemd-journal` reads an empty journal while validate passes and health stays green.
    # (The group is Vector's, never the router daemon's.)
    recipe = README.read_text()
    assert "| sha256sum -c -" in recipe
    assert recipe.index("sha256sum -c -") < recipe.index("sudo apt-get install -y")
    assert "\nsudo usermod -aG systemd-journal vector\n" in recipe


def _validate(
    vector: str, host: str, variables: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [vector, "validate", "--no-environment", str(HOSTS_DIR / host / "vector.yaml")],
        capture_output=True,
        text=True,
        check=False,
        env=_env(variables),
    )


@pytest.mark.parametrize("host", HOSTS)
def test_a_hosts_config_loads_as_the_unit_loads_it(host: str) -> None:
    got = _validate(_vector(), host, _tokens(host))
    assert got.returncode == 0, got.stdout + got.stderr
    assert "Validated" in got.stdout, got.stdout + got.stderr


def test_every_host_declares_its_units_variables() -> None:
    assert set(UNIT_VARIABLES) == set(HOSTS)
    declared = {name for names in UNIT_VARIABLES.values() for name in names}
    assert declared == set(SINK_TOKENS), "SINK_TOKENS must fabricate exactly the declared variables"


@pytest.mark.parametrize("host", HOSTS)
def test_a_hosts_file_reads_exactly_what_its_unit_carries(host: str) -> None:
    # Offline, before Vector: one the file reads and the unit lacks is a load failure on the box;
    # one the unit carries and the file never reads is a stale contract.
    assert _named(host) == UNIT_VARIABLES[host]


@pytest.mark.parametrize(
    ("host", "withheld"), [(host, name) for host in HOSTS for name in UNIT_VARIABLES[host]]
)
def test_interpolation_is_live_in_every_run(host: str, withheld: str) -> None:
    # With interpolation off, a withheld variable loads as its placeholder's text and validates
    # (measured on 0.61.0, #441); so only a refusal here proves the switch above is really on,
    # and that the host's file reads the variable the unit supplies.
    variables = {name: value for name, value in _tokens(host).items() if name != withheld}
    got = _validate(_vector(), host, variables)
    assert got.returncode != 0, got.stdout + got.stderr
    assert f'Missing environment variable in config. name = "{withheld}"' in got.stdout + got.stderr


@pytest.mark.parametrize("suite", SUITES, ids=lambda suite: suite.stem)
@pytest.mark.parametrize("host", HOSTS)
def test_the_shipped_transforms_pass_every_suite(host: str, suite: Path) -> None:
    vector = _vector()
    got = subprocess.run(
        [vector, "test", str(HOSTS_DIR / host / "vector.yaml"), str(suite)],
        capture_output=True,
        text=True,
        check=False,
        env=_env(_tokens(host)),
    )
    report = got.stdout + got.stderr
    assert got.returncode == 0, report
    # Every declared test ran and passed: a test Vector silently skipped would pass vacuously.
    declared = re.findall(r'^  - name: "(.+)"$', suite.read_text(), re.MULTILINE)
    assert declared, f"tests/vector/{suite.name} declares no tests"
    for name in declared:
        assert f"test {name} ... passed" in report, f"{name!r} did not pass:\n{report}"


# --- the prefix pass's cost: its compiled size and its time (#423) -------------------------------
#
# The fix for a key glued after a narrow run (#423, the harness's fix for basecradle-harness#858)
# repeats the whole-key alternation once per narrow rule, so the prefix pattern grew about twelve
# times over. Two costs follow, and both are measured on the real engine, never estimated:
#
# * SIZE. VRL compiles a regex literal when the program loads, under the regex engine's default
#   size limit, and a pattern over it is a load error: the transform never runs, and the NOC's
#   pre-install validation refuses the apply. Measured at #423 on Vector 0.56.0: the limit is
#   10,485,760 bytes, 38 copies of the then prefix pattern (ten whole-key alternations) compile
#   and 40 do not, so it was about 1/39 of the limit. #425 took one alternation out (`xai-`'s run
#   became wide), so the shipped pattern is smaller and the headroom only grew. The check
#   asserts that 16 copies still compile, so the shipped pattern stays under 1/16 of it, and
#   that 128 copies fail with the engine's size error, so the check can tell the two apart and
#   never passes vacuously.
# * TIME. The glued alternative's lazy run is what keeps the pass linear; a backtracking engine
#   would be quadratic on a long space-free run. Rust's regex, under VRL, is not, and the
#   check proves it on the shipped program: every adversarial 200,000-character space-free text
#   (the harness's length) is scrubbed in well under the bound, where a quadratic pass would take
#   minutes.

_SIZE_LIMIT_ERROR = "Compiled regex exceeds size limit"
_HEADROOM = 16
_FAR_OVER = 128
_LONG = 200_000
_TIME_BOUND_SECONDS = 10.0


def _scrub_vrl(host: str) -> str:
    """The `ai_scrub` remap's VRL, exactly as the host's vector.yaml ships it (a YAML literal block,
    taken to the first line indented less than its own)."""
    text = (HOSTS_DIR / host / "vector.yaml").read_text()
    component = text.index("\n  ai_scrub:\n")
    start = text.index("    source: |\n", component) + len("    source: |\n")
    lines = []
    for line in text[start:].splitlines():
        if line.strip() and not line.startswith(" " * 6):
            break
        lines.append(line[6:])
    source = "\n".join(lines) + "\n"
    # Never a vacuous slice: the remap's first rule and its last statement are both inside it.
    assert 'del(."_CMDLINE")' in source and source.rstrip().endswith(".dt = del(.timestamp)")
    return source


def _prefix_body(host: str) -> str:
    source = _scrub_vrl(host)
    start = source.index("msg = replace_with(msg, r'(?x)") + len("msg = replace_with(msg, r'(?x)")
    return source[start : source.index("') -> |key| {", start)]


def _compile_copies(
    vector: str, tmp_path: Path, body: str, copies: int
) -> subprocess.CompletedProcess[str]:
    # Each copy's captures are renamed, because the regex allows no name twice.
    alternation = "|".join(
        "(?:" + re.sub(r"\(\?P<(\w+)>", rf"(?P<\g<1>_{i}>", body) + ")" for i in range(copies)
    )
    program = tmp_path / f"copies-{copies}.vrl"
    program.write_text(f"replace_with(\"\", r'(?x){alternation}') -> |m| {{ m.string }}\n")
    event = tmp_path / "event.json"
    event.write_text("{}\n")
    return subprocess.run(
        [vector, "vrl", "-q", "-i", str(event), "-p", str(program)],
        capture_output=True,
        text=True,
        check=False,
        env=_env({}),
    )


@pytest.mark.parametrize("host", HOSTS)
def test_the_prefix_pattern_stays_far_under_the_regex_size_limit(host: str, tmp_path: Path) -> None:
    vector = _vector()
    body = _prefix_body(host)
    fits = _compile_copies(vector, tmp_path, body, _HEADROOM)
    assert fits.returncode == 0, fits.stdout + fits.stderr
    over = _compile_copies(vector, tmp_path, body, _FAR_OVER)
    assert over.returncode != 0
    assert _SIZE_LIMIT_ERROR in over.stdout + over.stderr, over.stdout + over.stderr


def _fill(unit: str) -> str:
    return (unit * (_LONG // len(unit) + 1))[:_LONG]


#: Long space-free texts aimed at each rule's run: a narrow run with no glued key, one that keeps
#: nearly starting one, a chain of glued keys, a text of nothing but prefixes, a lowercase word,
#: and the literal rules' own anchors repeated.
_LONG_TEXTS = {
    "narrow run": "hf_" + _fill("Ab3"),
    "near-glued run": "hf_" + _fill("Ab3ghp"),
    "glued chain": _fill("hf_" + "Ab3" * 7 + "ghp_" + "Cd4" * 7),
    "prefixes": _fill("hf_"),
    "lowercase word": "x" + _fill("hf_abcdefghijklmnopqrstughp"),
    "schemes": _fill("http://") + "/heartbeat/",
    "heartbeats": _fill("/heartbeat/"),
    "quotes": _fill('"'),
    "x-api-key": _fill("x-api-key:"),
    "assignments": _fill("api_key=Bearer"),
    "bearers": _fill("Bearer"),
}


@pytest.mark.parametrize("shape", sorted(_LONG_TEXTS))
def test_the_scrub_is_linear_on_a_long_space_free_text(shape: str, tmp_path: Path) -> None:
    vector = _vector()
    program = tmp_path / "scrub.vrl"
    program.write_text(_scrub_vrl("ai.basecradle.com"))
    event = tmp_path / "event.json"
    line = {"SYSLOG_IDENTIFIER": "nova", "message": _LONG_TEXTS[shape]}
    event.write_text(json.dumps(line) + "\n")
    started = time.monotonic()
    got = subprocess.run(
        [vector, "vrl", "-q", "-o", "-i", str(event), "-p", str(program)],
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIME_BOUND_SECONDS * 6,
        env=_env({}),
    )
    elapsed = time.monotonic() - started
    assert got.returncode == 0, got.stdout + got.stderr
    # `vector vrl -o` prints VRL's display of the event, not JSON, so read it as text.
    assert '"message": "[nova] ' in got.stdout, got.stdout[:400]
    assert elapsed < _TIME_BOUND_SECONDS, f"{shape}: {elapsed:.1f}s"
