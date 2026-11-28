r"""The shipped Vector config's VRL, run by `vector test` (#380, #398).

`ai_scrub` is VRL, and its output is the exact line Better Stack receives, so no Python reading of
the YAML can prove it. This runs the real thing: `vector test` executes the transform over the
fabricated events in every suite under `tests/vector/` and asserts the bytes. It runs every suite
against every host's shipped `vector.yaml`, on the Vector release the boxes run (deploy/README →
`VECTOR_VERSION`), so a VRL behaviour that changed between releases cannot pass here and fail on a
box.

The suites:

* `ai_scrub.yaml` — the redaction every shipped line passes: the literal rules before the prefix
  rules, and the harness's word rule (#398). Python's `re` cannot stand in for it: it has no
  `\p{L}` classes, and the word rule is a closure, so the real engine is the only test.
* `ai_credentials.yaml` — the credential-named assignment rule (#494): every credential-named
  assignment's value, in every field, for every writer, ships as the box's marker, and both the
  print site's marker and the box's own pass untouched. Beside it, below, a differential run holds
  the box's reading of a value to the print site's (`logfmt.redact_credentials`, the reference).
* `ai_writers.yaml` — the writer check (#484): a line under a fleet identifier is kept as it is
  only when its `_UID` is one the `fleet_writers` map lists for that identifier, and is otherwise
  marked `UNVERIFIED uid=<n>` under a renamed identifier.

Every suite runs against the host's shipped file with ONE change: the `fleet_writers` table reads
a map under `tmp_path` instead of `/etc/vector/fleet-writers.csv`, which exists only on a box. That
map is written by the real `deploy/bin/fleet-writers`, from a fabricated registry and account
database, so the generator and the check that reads its output are proven together.

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
`vector validate --no-environment` builds the topology without a component or health check. The
one running topology here (the no-warning start, #494) reads stdin and writes to a blackhole.
"""

from __future__ import annotations

import base64
import json
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

import credential_rule
from basecradle_router.logfmt import (
    _BREAK_CHARS,
    _LEAD_WORD,
    CANARY_VALUE_STOPS,
    REDACTED_VALUE,
    log_fields,
    redact_credentials,
    value_gap_class,
)
from conftest import BOX_REDACTED_VALUE, FORGED_REPLY, NOC_CREDENTIAL_ASSIGNMENT
from fleet_writers_box import ACCOUNTS, REGISTRY, fw

REPO = Path(__file__).resolve().parents[1]
README = REPO / "deploy" / "README.md"
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
HOSTS_DIR = REPO / "deploy" / "hosts"
SUITES = sorted((REPO / "tests" / "vector").glob("*.yaml"))
HOSTS = sorted(p.name for p in HOSTS_DIR.iterdir() if p.is_dir())
#: The table's path line, as every host's vector.yaml carries it exactly once.
MAP_LINE = f'      path: "{fw.MAP}"\n'


def _under_test(host: str, tmp_path: Path) -> Path:
    """The host's shipped vector.yaml, its `fleet_writers` table read from a map under `tmp_path`
    that the real writer built from the fabricated box. Nothing else is changed."""
    found, left_out = fw.rows(REGISTRY, ACCOUNTS.get)
    assert not left_out, left_out
    writers = tmp_path / "fleet-writers.csv"
    writers.write_text(fw.render(found))
    text = (HOSTS_DIR / host / "vector.yaml").read_text()
    assert text.count(MAP_LINE) == 1, f"{host} must read the map from {fw.MAP} exactly once"
    config = tmp_path / "vector.yaml"
    config.write_text(text.replace(MAP_LINE, f'      path: "{writers}"\n'))
    return config


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
    assert {suite.name for suite in SUITES} >= {
        "ai_credentials.yaml",
        "ai_scrub.yaml",
        "ai_writers.yaml",
    }


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
def test_the_shipped_transforms_pass_every_suite(host: str, suite: Path, tmp_path: Path) -> None:
    vector = _vector()
    got = subprocess.run(
        [vector, "test", str(_under_test(host, tmp_path)), str(suite)],
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
    """The `ai_scrub` remap's VRL, exactly as the host's vector.yaml ships it."""
    return scrub_vrl_of((HOSTS_DIR / host / "vector.yaml").read_text())


def scrub_vrl_of(text: str) -> str:
    """The `ai_scrub` remap's VRL in a vector.yaml's `text` (a YAML literal block, taken to the
    first line indented less than its own): the tree's, or an older commit's (the differential's
    regression bar, `tests/scrub_differential.py`)."""
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


#: The writer check's opening line in the dedented VRL, and its block's close.
_WRITER_CHECK_OPEN = 'if starts_with(ident, "basecradle-wake-")'


def _without_the_writer_check(source: str) -> str:
    """The scrub with the writer check's one block taken out, for `vector vrl`, which runs a
    program with no enrichment tables and so cannot compile the table lookup. The check is one
    table lookup per line, not a scan of it, so the timing below measures what it always did:
    the redaction. The check itself runs in ai_writers.yaml, on the whole shipped program."""
    start = source.index(_WRITER_CHECK_OPEN)
    assert source.count(_WRITER_CHECK_OPEN) == 1 and source[start - 1] == "\n"
    end = source.index("\n}\n", start) + len("\n}\n")
    cut = source[:start] + source[end:]
    assert '"fleet_writers"' not in cut and '"fleet_writers"' in source[start:end]
    return cut


def _prefix_body(host: str) -> str:
    source = _scrub_vrl(host)
    opener = r"replace_with(scrub_text, r'(?x)(?P<pre>[\s\S]*?)(?:"
    start = source.index(opener + "(?P<before>") + len(opener)
    return source[start : source.index(")') -> |key| {", start)]


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
    # The credential-named assignment rule (#494): names back to back, one name of 200,000
    # capitals (quadratic for the canary's own spelling in a backtracking engine), and a value
    # that keeps opening a quote, an escaped quote, a bearer word or a glued one.
    "credential names": _fill("A_TOKEN="),
    "credential capitals": _fill("A") + "_TOKEN=x",
    "credential single quotes": "A_TOKEN=" + _fill("'"),
    "credential escaped quotes": "A_TOKEN=" + _fill('\\"'),
    "credential bearer words": "A_TOKEN=" + _fill("Bearer\t"),
    "credential glued bearers": "A_TOKEN=" + _fill("xbearer"),
    # Values the every-field block's first reading leaves to the gap reading, each read for it
    # within a window (#561): a field of them costs a bounded time.
    "credential gaps after inner names": "A_TOKEN=" + _fill("xB_TOKEN= A_TOKEN="),
    "credential gaps before an open quote": _fill('A_TOKEN=Bearer\n"'),
    "credential runs of scheme words": "A_TOKEN=" + _fill("xBearer\tBearer "),
    # Lead words before a scheme word a tab glues to a value (#502), names behind tabs, and bare
    # names with the one tail (#504).
    "credential lead words": "A_TOKEN=" + _fill("x\t"),
    "credential quoted lead words": "A_TOKEN=" + _fill("'q'\t`a`\t"),
    "credential glued scheme words": "A_TOKEN=" + _fill("x\tBearer "),
    "credential names behind tabs": _fill("A_TOKEN=\t"),
    "credential bare names": _fill("KEY_BASE="),
    "credential names inside a value": "A_TOKEN=" + _fill("x'q'A_TOKEN="),
    "credential substitutions after tabs": "A_TOKEN=" + _fill("${x\t"),
    # The bearer header (#507): tokens that keep opening a quote or a substitution, and runs of
    # middle words, glued, quoted and escaped, in the bearer rule and the two rules that carry it.
    "bearer single quotes": _fill("Bearer '"),
    "bearer double quotes": _fill('Bearer "'),
    "bearer escaped quotes": _fill('Bearer \\"'),
    "bearer substitutions": _fill("Bearer $("),
    "bearer quoted middle words": "Bearer " + _fill('"q"bearer '),
    "bearer escaped middle words": "Bearer " + _fill('\\"Bearer '),
    "bearer open middle words": "Bearer " + _fill("'q'Bearer x\"bearer "),
    "bearer unclosed middle words": "Bearer " + _fill("'q'`Bearer $(\"\tBearer "),
    "x-api-key glued bearers": _fill("x-api-key: KBearer '"),
    "api-key glued bearers": _fill('api_key=KBearer "'),
    "heartbeat glued bearers": _fill("/heartbeat/K9xBearer "),
}


@pytest.mark.parametrize("shape", sorted(_LONG_TEXTS))
def test_the_scrub_is_linear_on_a_long_space_free_text(shape: str, tmp_path: Path) -> None:
    vector = _vector()
    program = tmp_path / "scrub.vrl"
    program.write_text(_without_the_writer_check(_scrub_vrl("ai.basecradle.com")))
    event = tmp_path / "event.json"
    # In the message, and in a field of its own, which the every-field block reads (#561).
    line = {"SYSLOG_IDENTIFIER": "nova", "message": _LONG_TEXTS[shape], "NOTE": _LONG_TEXTS[shape]}
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
    assert '"NOTE": ' in got.stdout, got.stdout[:400]
    assert elapsed < _TIME_BOUND_SECONDS, f"{shape}: {elapsed:.1f}s"


# --- the box reads a value as the print site does (#494) -----------------------------------------
#
# The print site's `logfmt.redact_credentials` is the reference (basecradle-router#491). The box's
# rule is VRL and reads a whole line, so it is held to the reference by running both on the same
# text: on every line below the box writes exactly what the print site writes, its own marker in
# place of the print site's. Where the two read a value differently, the difference is named,
# its box output pinned, and the canary is silent on it either way. The lists are the cases found,
# never a proof that no other exists: the seeded fuzz below is what holds the rule's two
# properties, that the canary reads nothing the box ships and that no credential-named assignment
# ships a value.

#: Lines on which the two agree: every reading the box shares with the print site, fabricated.
_AGREED = [
    "+ export DEPLOY_TOKEN=fabricatedDeployValue0001 && ./run",
    "A_KEY=k1 B_TOKEN=t1 C_SECRET=s1 D_PASSWORD=p1 E_PASSPHRASE=q1 S3_BUCKET_2_KEY=v myAPI_KEY=w",
    "A_TOKEN=abc\tB_TOKEN=fabricatedValue0002 next=1",
    "A_TOKEN=fabricatedValue0003\ntokens_in=5 cost=0.1",
    "A_PASSWORD='fabricated pass phrase' next=1",
    'B_PASSWORD="fabricated pass phrase" next=2',
    'C_PASSWORD="fabricated end"',
    "LAST_TOKEN=",
    "x_token=abcdefghij TOKEN_FILE=/run/fabricated KEY=abcdefghij PASSWORD=abcdefghij",
    "GH_TOKEN=ghp_Fab1catedGithubToken000000000000 OPENAI_API_KEY=sk-proj-Fab1catedKey00000000",
    "INFO llm provider=openrouter purpose=main endpoint=StreamLake model=z-ai/glm-5.2"
    " duration=42.96s tokens_in=764942 tokens_out=236 tokens_total=765178 cached_tokens=238277"
    " cache_write_tokens=1024 cost=0.0445 cost_basis=computed outcome=ok agent=nova"
    " generation_id=gen-1791030923-8abbrsSEsIk06gI224Fi",
    "INFO charge provider=steel kind=browser.session executor=mcp unit=minute count=7"
    " duration=402.18s outcome=ok cost=0.07 | INFO key spend provider=openrouter spent=1.04",
    *FORGED_REPLY.split("\n")[:-1],
    # The review's cases (#494). A value glued after a marker, or punctuation, reads as a value
    # with it.
    "X_TOKEN='<a redacted value>'fabricatedGlued0004 next=1",
    f"MY_API_KEY={BOX_REDACTED_VALUE}.",
    # A double-quoted value opened outside every quote goes whole, closed or not, with whatever is
    # glued after it: Python's repr of an argv, a shell line, prose, and a line cut short.
    "Command '['sh', '-c', 'export X_TOKEN=\"fabricatedSecret0001\"']' failed",
    'Y_TOKEN="fabricatedSecret0002"; ./run',
    'Set X_TOKEN="fabricatedSecret0003". done | X_TOKEN="fabricatedSecret0004":x',
    'export X_TOKEN="fabricated secret value',
    "X_PASSPHRASE='correct horse battery",
    'X_API_KEY=\\"fabricatedSecret0005\\"abc next=1',
    # A shell word holding a quote, read by parity: a quote inside the value, an escaped backslash
    # before one, one name's value holding another's, and two quoted pieces.
    'E_TOKEN=abc"def ghi" next=1',
    'X_TOKEN=\\\\"fabricatedSecret0001" next=1',
    'X_TOKEN=Y_TOKEN="fabricatedSecret0002" next=1',
    'X_TOKEN="fabricated""Secret0003" next=1',
    # Every credential word under a quoted value, so each copy of the name is run.
    'A_KEY="k 1" B_TOKEN="t 1" C_SECRET="s 1" D_PASSWORD="p 1" E_PASSPHRASE="q 1" next=1',
    # The one shape the parity-independent quoted reading misreads, as the print site does: an
    # empty assignment closing a quoted value, and a next field whose own value begins with a
    # blank. The field goes; a stray quote earlier never lets a quoted value through, which is what
    # the reading buys.
    'a="X_TOKEN=" b=" y" c=1',
    # Every piece of a shell word, anywhere in it.
    "F_TOKEN=(cat /run/fabricated) next=1",
    "L_TOKEN=abc'def ghi' next=1 | M_TOKEN=abc\\ def next=2 | N_TOKEN=$'abc def' next=3",
    "O_TOKEN=`cat /run/fabricated` next=1 | P_TOKEN=$(cat /run/fabricated) next=2",
    # A bearer value goes with its token (#497): a scheme word in any case, one glued to a value
    # as HTTP spells it, and any run of the two, in any order. A scheme word with no word after it
    # on its line is the value.
    "A_TOKEN=Bearer eyJhbGciOiJIUzI1NiJ9.e30.sig next=1",
    "X_API_KEY=KBearer abcdefgh:fabricatedTail0003 next=1",
    "A_TOKEN=Bearer Bearer eyJabc next=1 | B_TOKEN=bearer\tBEARER fabricated next=2",
    "C_TOKEN=Bearer KBearer fabricated next=1 | D_TOKEN=KBearer Bearer fabricated next=2",
    "F_TOKEN=x.Bearer fabricated next=2",
    # A word merely ending in the letters is a scheme word too (#510), and the box reads it now as
    # the print site does (#516, the bearer rule reads every one).
    "E_TOKEN=abc-bearer fabricatedTok next=1",
    "Q_TOKEN=a\\ Bearer fabricated next=2",
    # A double-quoted token, which the parity pass takes where the name is outside every quote,
    # after a space or a tab; and a scheme word inside a quoted piece or an escape, which is none.
    'L_TOKEN=Bearer "fabricated quoted" next=1 | M_TOKEN=Bearer KBearer "fabricated" next=2',
    "N_TOKEN=Bearer\t\"fabricated quoted\" next=1 | O_TOKEN=Bearer\t'fabricated quoted' next=2",
    "P_TOKEN='a Bearer' fabricated next=1",
    "X_TOKEN=Bearer fabricatedSecret=x next=1",
    "X_TOKEN=Bearer abc123=xyz789fabricated next=1",
    # The token is the whole run, an `=` included, so a scheme word with a bare field after it
    # takes the field: a fleet line quotes any value holding an `=`, so it is never a fleet
    # line's, and redaction comes first.
    "stage=wake X_TOKEN=Bearer cost=0.1 outcome=ok",
    # A scheme word a tab glues to a value goes with its token (#502): the issue's three lines, any
    # case, several lead words, every lead piece, after a token, and a quoted token by parity.
    "A_TOKEN=x\tBearer eyJhbGciOiJIUzI1NiJ9.e30.fabricatedSig next=1",
    "A_TOKEN=\tBearer eyJhbGciOiJIUzI1NiJ9.e30.fabricatedSig next=1",
    "A_TOKEN='q'\tKBearer eyJhbGciOiJIUzI1NiJ9.e30.fabricatedSig next=1",
    "B_TOKEN=x\tbearer\t\tBEARER fabricated next=1 | C_TOKEN=a\tb\tBearer fabricated next=2",
    "D_TOKEN=a\\'b\tBearer fabricated next=1 | E_TOKEN=`a\tb`\tBearer fabricated next=2",
    "F_TOKEN='a\tb'\tBearer fabricated next=1 | G_TOKEN=Bearer x\tBearer fabricated next=2",
    'H_TOKEN=x\tBearer "fabricated quoted" next=1',
    # The shape #497 backed out: a quoted value holding a tab and a scheme word is one word, and
    # every field after it stays.
    "I_TOKEN='a\tBearer b' next=1 outcome=ok",
    # A word after a lead word is read whole, a quoted one too.
    "J_TOKEN=a\t'b c' next=1",
    # A backslash before a tab is its word's last character, so the scheme word after the tab is the
    # value's (the review of #502); and a scheme word's token is a word on its own record.
    "K_TOKEN=a\\\tBearer fabricated next=1 | L_TOKEN=Bearer ,next=1",
    # Whatever stands before the tab, a value never ends between a scheme word and its token (the
    # third review of #502): a parenthesis and a substitution, the box's closed pieces.
    "N_TOKEN=(a)\tBearer fabricated next=1 | O_TOKEN=$(cat\tf)\tBearer fabricated next=2",
    "P_TOKEN=f(x)\tKBearer fabricated next=1",
    # A bare word names a credential, and `_BASE` is the one tail (#504).
    "PASSWORD=fabricated1 KEY=fabricated2 SECRET_KEY_BASE=fabricated3 _TOKEN=fabricated4",
    "GH_TOKEN_FILE=/run/fabricated API_KEY_ID=abcdefghij MAX_TOKENS=1000000 x_token=abcdefghij",
    # A double-quoted value after a tab goes with the word, as the box's parity reads it (#502's
    # review).
    'L_TOKEN=a\t"b c" next=1 | M_TOKEN=\t"fabricated secret value" next=2',
    # A credential name inside a value takes its own value with it (#504's review, from
    # basecradle-harness#928), read as any value is; a name inside a quote is the quote's text.
    "A_TOKEN='a b'B_TOKEN=\tfabricatedSecret1 next=1",
    "C_TOKEN=\t'q'DB_PASSWORD=Bearer fabricatedSecret2 next=1",
    "D_TOKEN=x'q'E=x'q'F_TOKEN=Bearer fabricatedSecret3 next=1",
    "G_TOKEN=Bearer ;H_KEY=:KBearer :\tBearer fabricatedSecret4 next=1",
    "I_TOKEN='q'J_TOKEN=x\tBearer fabricatedSecret5 next=1",
    "K_TOKEN=x'q'L_TOKEN=\tnext=1 | M_TOKEN='N_TOKEN=x' next=2 other=3",
]

#: Lines the router renders itself: the print site has already redacted them, so the box must pass
#: them exactly as they came (the capital, 2026-11-16, on basecradle-router#494).
_RENDERED = [
    log_fields(stage="wake", outcome="error", agent="nova", detail=text)
    for text in (
        "refused: export DEPLOY_TOKEN=fabricatedDeployValue0001 && ./run",
        "ANTHROPIC_API_KEY='fabricated key value' then GH_TOKEN=ghp_Fab1catedGithubToken0000",
        'said "STEEL_API_KEY=fabricatedSteelValue01" and A_TOKEN=Bearer',
    )
] + [
    "ANTHROPIC_API_KEY='<a redacted value>' | api_key='<a redacted value>'",
    # A marker inside a quoted value: kept too, though the print site never writes one so.
    "X_TOKEN=\"'<a redacted value>'\" next=1",
]

#: Where the box reads a value differently, and why: the box output, which the canary reads
#: nothing in. The print site reads one value, the box a whole line, so the box never reads past
#: the line's own structure: the close of the quoted value an assignment sits in, a `"` a
#: single-quoted value would cross, a `,` or `}`, a record break, a shell substitution.
_DIFFERENT = {
    # The bearer rule's gap after its scheme word is whitespace or control characters (#561),
    # wider than the harness's whitespace, so a scheme word ending a line takes the first word of
    # the next; the print site's value ends at the break. The box takes more, never less (#516).
    "G_TOKEN=Bearer \nfabricated next=1 | H_TOKEN=Bearer": (
        f"G_TOKEN={BOX_REDACTED_VALUE} next=1 | H_TOKEN={BOX_REDACTED_VALUE}"
    ),
    "M_TOKEN=Bearer \rImportant next=1": f"M_TOKEN={BOX_REDACTED_VALUE} next=1",
    # The canary stops at a `,` or a `}`, and so does the box, in either pass: in a JSON line they
    # are structure.
    'Q_TOKEN=v1,next="x y" end=1': f'Q_TOKEN={BOX_REDACTED_VALUE},next="x y" end=1',
    "C_TOKEN=v1,next=2 D_TOKEN=v2}": (
        f"C_TOKEN={BOX_REDACTED_VALUE},next=2 D_TOKEN={BOX_REDACTED_VALUE}}}"
    ),
    # The box reads one level of a substitution, and no quote inside it; the print site nests. A
    # `)` inside a quote ends the box's substitution, and the quote then opens a piece that runs
    # to the record's end: more is taken, never less.
    "F_TOKEN=$(cat $(ls /run) /x) next=1": f"F_TOKEN={BOX_REDACTED_VALUE} /x) next=1",
    "G_TOKEN=$(echo ')' fabricated) next=1": f"G_TOKEN={BOX_REDACTED_VALUE}",
    # A bare `(` left open is an ordinary character: prose opens far more of them than a shell.
    "H_TOKEN=a(b fabricated next=1": f"H_TOKEN={BOX_REDACTED_VALUE} fabricated next=1",
    # A single-quoted piece never crosses a bare `"`, where the shell would read it as literal.
    "I_TOKEN='a\"b c' next=1": f"I_TOKEN={BOX_REDACTED_VALUE}\"b c' next=1",
    # `\"` opens a JSON-escaped piece, as it does inside a JSON string, where the shell reads a
    # literal `"` and goes on to a `'` that opens a quote.
    "J_TOKEN=\\\"a'b\\\" c' next=1": f"J_TOKEN={BOX_REDACTED_VALUE} c' next=1",
    # A tab is part of a word on the box, as it is of the canary's run, whatever came before it;
    # the print site crosses one only after a lead word, and a quoted value holding a blank is none
    # (#502). Neither ships a value the canary reads.
    "K_TOKEN='a b'\tc next=1": f"K_TOKEN={BOX_REDACTED_VALUE} next=1",
    # The box reaches a scheme word after any prefix of its closed pieces, a quoted value holding a
    # blank included, where the canary's run, and so the print site's value, stops inside the quote;
    # and a double-quoted value goes on, by parity, past a tab only to a scheme word and its token.
    # The print site leaves the scheme word in sight for the box's bearer rule either way.
    "X_TOKEN=x\t'a b'\tBearer fabricatedTok next=1": f"X_TOKEN={BOX_REDACTED_VALUE} next=1",
    'Y_TOKEN="a b"\tBearer fabricatedTok next=1': f"Y_TOKEN={BOX_REDACTED_VALUE} next=1",
    # A credential name after a `,` inside a value is the box's own assignment, since a `,` ends the
    # box's value: its value goes either way, and on the box its name stays.
    "AUTH_TOKEN=x,DB_PASSWORD=\tfabricatedSecret6 next=1": (
        f"AUTH_TOKEN={BOX_REDACTED_VALUE},DB_PASSWORD={BOX_REDACTED_VALUE} next=1"
    ),
    # A single-quoted value never crosses a `"`: a request's `'` keeps an access line's JSON whole.
    '{"request":{"uri":"/hook?X_TOKEN=\'abc&y=1"},"status":202}': (
        f'{{"request":{{"uri":"/hook?X_TOKEN={BOX_REDACTED_VALUE}"}},"status":202}}'
    ),
    # A `"` after the `=` inside a quoted value is that value's close, not an opening, whatever
    # follows it (the next field's value may be empty).
    'a="set FOO_TOKEN=" b="x y" c=1': 'a="set FOO_TOKEN=" b="x y" c=1',
    'a="X_TOKEN=" b="" c=1': 'a="X_TOKEN=" b="" c=1',
    # Every record break ends the box's value, as it ends the canary's run. The print site's shell
    # reading of an unquoted value ends only at a space, a tab or a newline, so it runs on through
    # a `\r`, a NUL or a U+2028 into what the box ships as the next record.
    "B_TOKEN=fabricatedValue0004\u2028next=1\rC_TOKEN=fabricatedValue0005\x00next=2": (
        f"B_TOKEN={BOX_REDACTED_VALUE}\u2028next=1\rC_TOKEN={BOX_REDACTED_VALUE}\x00next=2"
    ),
    # A gap before a value is the value's (#561), and the marker stands directly after the name:
    # a word of prose after `NAME= ` is the accepted cost. The box reads it first, as the fleet's
    # last layer; the print site's reading of the gap follows in its own change (#561), and these
    # move to `_AGREED` with it.
    "EMPTY_TOKEN= next=1 | LAST_TOKEN=": f"EMPTY_TOKEN={BOX_REDACTED_VALUE} | LAST_TOKEN=",
    "X_TOKEN=\nfabricatedAfterBreak0005 next=1": f"X_TOKEN={BOX_REDACTED_VALUE} next=1",
    "A_TOKEN=\r\nfabricatedAfterBreak0006 next=1 | B_TOKEN=\t fabricatedGap0007 next=2": (
        f"A_TOKEN={BOX_REDACTED_VALUE} next=1 | B_TOKEN={BOX_REDACTED_VALUE} next=2"
    ),
    "C_TOKEN=\n  fabricatedIndent0008 next=1 | D_TOKEN=  fabricatedGap0009 next=2": (
        f"C_TOKEN={BOX_REDACTED_VALUE} next=1 | D_TOKEN={BOX_REDACTED_VALUE} next=2"
    ),
    # An inner name's value after its gap goes with the value that holds it (#561).
    "K_TOKEN=x'q'L_TOKEN= next=1 | M_TOKEN='N_TOKEN=x' next=2 other=3": (
        f"K_TOKEN={BOX_REDACTED_VALUE} | M_TOKEN={BOX_REDACTED_VALUE} next=2 other=3"
    ),
    # A record break right after the `=` with no value after it: the canary's run crosses the
    # break, so the box writes its marker before it, and takes nothing.
    "X_TOKEN=\n,next=1": f"X_TOKEN={BOX_REDACTED_VALUE}\n,next=1",
    # A canary stop ends a bearer token, as it ends every value: in a JSON line it is structure.
    "X_TOKEN=Bearer abc,fabricatedTail next=1": (
        f"X_TOKEN={BOX_REDACTED_VALUE},fabricatedTail next=1"
    ),
    # An unclosed JSON-escaped value ends at its JSON string's own close, where the print site's
    # shell reading takes the rest of the line as the open quote's.
    '{"cmd":"export X_TOKEN=\\"fabricatedUnclosed0006","n":1}': (
        f'{{"cmd":"export X_TOKEN={BOX_REDACTED_VALUE}","n":1}}'
    ),
}


def _credential_rule() -> str:
    """The credential-named assignment rule alone, as the shipped scrub carries it: from its value
    pass to its field-name check's close."""
    source = _scrub_vrl("ai.basecradle.com")
    start = source.index("scrubbed = map_values(., recursive: true) -> |value| {")
    end = source.index("\n}\n", source.index("for_each(scrubbed) -> |field, value| {")) + 3
    return source[start:end]


def _parity_pass() -> str:
    """The credential rule with its run passes taken out: quote parity alone, the first reading's
    and the gap reading's after it (#561)."""
    rule = _credential_rule()
    for head, stands in (("replace_with(parity, ", "parity"), ("replace_with(gapped, ", "gapped")):
        assert rule.count(head) == 1, head
        start = rule.index(head)
        end = rule.index("\n        }\n", start) + len("\n        }\n")
        rule = rule[:start] + stands + "\n" + rule[end:]
    return rule


def _scrub_lines(vector: str, tmp_path: Path, lines: list[str], *, alone: str = "") -> list[str]:
    """Each line's message after the shipped scrub, its `[identifier] ` prefix taken off; or, with
    `alone`, after the credential rule alone (`"rule"`) or its parity pass alone (`"parity"`)."""
    program = tmp_path / "scrub.vrl"
    if alone:
        rule = {"rule": _credential_rule, "parity": _parity_pass}[alone]()
        body = rule + '.message = "[x] " + string!(.message)\n'
    else:
        body = _without_the_writer_check(_scrub_vrl("ai.basecradle.com"))
    program.write_text(body + "encode_base64(.message)\n")
    events = tmp_path / "events.json"
    events.write_text(
        "".join(json.dumps({"SYSLOG_IDENTIFIER": "x", "message": line}) + "\n" for line in lines)
    )
    got = subprocess.run(
        [vector, "vrl", "-q", "-i", str(events), "-p", str(program)],
        capture_output=True,
        text=True,
        check=False,
        env=_env({}),
    )
    assert got.returncode == 0, got.stdout + got.stderr
    out = [base64.b64decode(json.loads(row)).decode() for row in got.stdout.splitlines() if row]
    assert len(out) == len(lines), got.stdout
    assert all(row.startswith("[x] ") for row in out), out
    return [row[len("[x] ") :] for row in out]


def test_the_box_reads_a_value_as_the_print_site_does(tmp_path: Path) -> None:
    vector = _vector()
    box = _scrub_lines(vector, tmp_path, _AGREED)
    for line, got in zip(_AGREED, box, strict=True):
        assert got == redact_credentials(line).replace(REDACTED_VALUE, BOX_REDACTED_VALUE), line


def test_a_line_the_print_site_redacted_passes_the_box_untouched(tmp_path: Path) -> None:
    assert all(REDACTED_VALUE in line for line in _RENDERED)
    assert _scrub_lines(_vector(), tmp_path, _RENDERED) == _RENDERED


def test_every_difference_from_the_print_site_is_named_and_the_canary_reads_nothing(
    tmp_path: Path,
) -> None:
    canary = re.compile(NOC_CREDENTIAL_ASSIGNMENT)
    lines = list(_DIFFERENT)
    box = _scrub_lines(_vector(), tmp_path, lines)
    for line, got in zip(lines, box, strict=True):
        assert got == _DIFFERENT[line], line
        # Named because it IS a difference: a case the two agree on belongs in _AGREED.
        reference = redact_credentials(line).replace(REDACTED_VALUE, BOX_REDACTED_VALUE)
        assert got != reference, line
    for got in box + _scrub_lines(_vector(), tmp_path, _AGREED + _RENDERED):
        assert not canary.search(got), got


#: The pieces a fuzzed line is made of: credential names, every quote, escape, stop, blank and
#: record break the rule reads, bearer words, both markers, and plain text.
_FUZZ_PIECES = [
    "A_TOKEN=",
    "MY_API_KEY=",
    "x_token=",
    "api_key=",
    "'",
    '"',
    "\\",
    "\\\\",
    '\\"',
    ",",
    "}",
    ")",
    ";",
    " ",
    "\t",
    "\n",
    "\r",
    "\x00",
    "\u2028",
    "Bearer ",
    "KBearer ",
    "\tBearer ",
    "x\t",
    "PASSWORD=",
    "SECRET_KEY_BASE=",
    "DB_PASSWORD=",
    "x'q'",
    "(a)\t",
    "abc-bearer ",
    "(a)Bearer ",
    'x"q"bearer ',
    "\\Bearer ",
    '\\"a b\\"bearer ',
    "=",
    "==",
    REDACTED_VALUE,
    BOX_REDACTED_VALUE,
    "fabricated",
    "Secret0001",
    "abc",
    ".",
    ":",
    ">",
    '""',
    "=x",
    "cost=0.1",
]

#: The gaps a value may open after (#561), beyond the fuzz pieces' blanks and breaks: a non-breaking
#: space, an escape, a CRLF, a break and an indent, and a gap before a quoted value. Kept apart from
#: `_FUZZ_PIECES`, which `tests/scrub_differential.py` seeds its corpus from: a piece added there
#: reshuffles that corpus and loses the shapes its own checks are proven on.
_GAP_PIECES = ["\xa0", "\x1b", "\r\n", "\n  ", ' "', "\t "]

#: The characters an empty value may end at, after its gap (#561): a canary stop, which ends the
#: box's value. Anything else after a credential name's `=` and its gap is a value the box should
#: have taken, and a marker it writes stands directly after the `=`.
_EMPTY_VALUE_ENDS = set(CANARY_VALUE_STOPS) - {" "}

#: The gap a value may open after (`logfmt.VALUE_GAP_RANGES`, #561).
_GAP_CLASS = value_gap_class(lambda point: re.escape(chr(point)))
_GAP = re.compile(_GAP_CLASS + "*")


#: The canary's name with its `=`, as tests/conftest.py holds it.
_CREDENTIAL_NAME = re.compile(NOC_CREDENTIAL_ASSIGNMENT[: NOC_CREDENTIAL_ASSIGNMENT.index("=") + 1])


def _quote_open_at(record: str, at: int) -> bool:
    """Whether a `"` is open at `at`, read as the box reads parity: a backslash escapes the next
    character, and a record is one record, so a quote may span its lines."""
    open_, i = False, 0
    while i < at:
        c = record[i]
        if c == "\\" and i + 1 < at:
            i += 1
        elif c == '"':
            open_ = not open_
        i += 1
    return open_


#: What a value's run may hold: anything but a blank, a canary stop or a record break.
_VALUE_CHAR = re.compile("[^" + re.escape(" \t" + CANARY_VALUE_STOPS + _BREAK_CHARS) + "]")
_MARKERS = (REDACTED_VALUE, BOX_REDACTED_VALUE)

#: The bearer scheme words the box's credential value reads through (#497): alone in any case, or
#: glued to token characters as HTTP spells it. Any other word ending in one ends the value: the
#: credential rule's own limit (#510), which holds in every field but the message, where the
#: union closes it (#516, `_LIMIT`).
_BOX_SCHEME_WORD = f"(?i:bearer)|[{credential_rule.SCHEME_GLUE}]*Bearer"

#: The bearer scheme words a value may open with, each with its blanks (#497), and the lead words a
#: tab glues one to (#502): a quoted value after them is still the name's, and the parity pass takes
#: it with them.
_SCHEME_RUN = re.compile(
    "(?:\t*(?:" + _LEAD_WORD.pattern + ")*(?:" + _BOX_SCHEME_WORD + ")" + _GAP_CLASS + "+)*"
)

#: After a marker, only scheme words glued to it: the marker is no lead word.
_GLUED_SCHEME_RUN = re.compile("(?:(?:" + _BOX_SCHEME_WORD + ")" + _GAP_CLASS + "+)*")

#: What may follow a quoted marker the parity pass keeps whole: anything that ends its word, the
#: line's end included.
_QUOTED_MARKER_END = {"", *" ,}" + _BREAK_CHARS}


def _after_marker(rest: str) -> str:
    """`rest` with a marker at its head taken off."""
    for marker in _MARKERS:
        if rest.startswith(marker):
            return rest[len(marker) :]
    return rest


def _opens_a_quoted_value(line: str) -> str | None:
    """The first credential name in `line` whose shell word holds a `"` that opens a value, or
    None: what the parity pass must leave none of. A marker at the word's head is skipped, so a
    quoted value glued after one is caught, and so are bearer scheme words before it; a `"` that
    closes the quoted value the name sits in is the end of an empty value."""
    for name in _CREDENTIAL_NAME.finditer(line):
        rest = line[name.end() :]
        rest = rest[_GAP.match(rest).end() :]  # a gap before a value is the value's (#561)
        if any(
            rest.startswith(f'"{marker}"')
            and rest[len(marker) + 2 : len(marker) + 3] in _QUOTED_MARKER_END
            for marker in _MARKERS
        ):
            continue  # a quoted marker, which the parity pass keeps as it is
        after = _after_marker(rest)
        at = len(line) - len(after)
        at = (_GLUED_SCHEME_RUN if after is not rest else _SCHEME_RUN).match(line, at).end()
        while at < len(line) and line[at] != '"' and _VALUE_CHAR.match(line, at):
            if line[at] == "'":
                # A single-quoted piece is skipped to its close. One that would cross a `"` is
                # the named difference (`_DIFFERENT`): no piece crosses a bare `"`, so the word
                # is not read past it.
                close = re.compile("['\"" + re.escape(_BREAK_CHARS) + "]").search(line, at + 1)
                if close is None or line[close.start()] != "'":
                    break
                at = close.end()
                continue
            # Inside a word a backslash escapes the next character, a record break aside, as the
            # shell reads one (`\\'` is no quote).
            escaped = line[at] == "\\" and at + 1 < len(line) and line[at + 1] not in _BREAK_CHARS
            at += 2 if escaped else 1
        if line[at : at + 1] == '"' and not _quote_open_at(line, at):
            return line[name.start() : name.end() + 24]
    return None


def _ships_a_value(line: str) -> str | None:
    """The first credential name in `line` followed by a value, or by a marker with a value glued
    to it, a quoted one aside (the parity pass's, held by its own property and by the stray-quote
    test below), or None: what the whole rule must leave none of."""
    for name in _CREDENTIAL_NAME.finditer(line):
        rest = line[name.end() :]
        value = rest[_GAP.match(rest).end() :]  # a gap before a value is the value's (#561)
        after = _after_marker(value)
        if after is not value:
            if after and _VALUE_CHAR.match(after) and after[0] != '"':
                return line[name.start() : name.end() + 40]
            continue
        if not value or value[0] in _EMPTY_VALUE_ENDS:
            continue
        return line[name.start() : name.end() + 24]
    return None


def test_the_canary_reads_nothing_the_box_ships(tmp_path: Path) -> None:
    # The property the whole rule exists for (basecradle/basecradle#877): a canary match on a line
    # the box shipped means the scrub failed. A hand-picked list cannot show it holds, so this is
    # seeded fuzz over the pieces the rule reads: 3,000 lines of 1 to 24 pieces each.
    rng = random.Random(494)
    lines = [
        "".join(rng.choice(_FUZZ_PIECES + _GAP_PIECES) for _ in range(rng.randint(1, 24)))
        for _ in range(3000)
    ]
    canary = re.compile(NOC_CREDENTIAL_ASSIGNMENT)
    box = _scrub_lines(_vector(), tmp_path, lines)
    leaked = [(line, got) for line, got in zip(lines, box, strict=True) if canary.search(got)]
    assert not leaked, leaked[:5]
    # And the rule redacts, not only blinds the canary, which never reads a quoted value: every
    # credential-named assignment it leaves is followed by a marker or by an empty value. Read in
    # its two halves, each where its parity is the line's: the parity pass leaves no credential
    # name opening a quoted value, and the whole rule no other value. (A later rule, or the run
    # pass itself, may take a `"` or half of an escaped one with a value, and parity read on what
    # it leaves is not the parity the line had.)
    for alone, check in (("parity", _opens_a_quoted_value), ("rule", _ships_a_value)):
        out = _scrub_lines(_vector(), tmp_path, lines, alone=alone)
        shipped = [(line, got, check(got)) for line, got in zip(lines, out, strict=True)]
        assert not [c for c in shipped if c[2]], (alone, [c for c in shipped if c[2]][:5])


#: The oracle's pieces: every construct the box and the print site read alike, and nothing that is
#: a named difference (`_DIFFERENT`): no line structure (`"`, `,`, `}`, a record break), no
#: substitution or parenthesis, no escaped `\"`. A tab is one (#502): the box reads more past one
#: than the print site, never less. A marker appears only as both redactors write one, before a
#: blank.
_ORACLE_SECRET = "Fab1catedSecret"
_ORACLE_PIECES = [
    "A_TOKEN=",
    "MY_API_KEY=",
    "x_token=",
    "'",
    "\\",
    "\\\\",
    " ",
    "=",
    "$'",
    "`",
    "abc",
    _ORACLE_SECRET,
    REDACTED_VALUE + " ",
    BOX_REDACTED_VALUE + " ",
    ".",
    ":",
    ";",
    "Bearer ",
    "KBearer ",
    "\t",
    "\tBearer ",
    "x\t",
    "PASSWORD=",
    "DB_PASSWORD=",
    "x'q'",
    "(a)\t",
]


def _box_leaves_a_scheme_word(line: str) -> bool:
    """Whether `line` holds a scheme word the box's credential rule does not read through (#510): a
    word ending in `bearer` that is not one alone or glued as HTTP spells it. The print site reads
    the token after it and the credential rule alone leaves it (its own limit, closed for the
    message by the union, `_LIMIT`), so the two do not read that token alike."""
    return any(
        not re.fullmatch(_BOX_SCHEME_WORD, word)
        for word in re.findall(r"\S*?(?i:bearer)(?=[ \t])", line)
    )


def test_the_box_never_leaves_more_of_a_value_than_the_print_site(tmp_path: Path) -> None:
    # The reference holds the box to itself, not only to the lines picked above: on every
    # construct the two read alike, the box's credential rule alone takes at least what the print
    # site takes. Seeded fuzz, 3,000 lines; a fabricated secret the print site redacts must not
    # survive the rule. A scheme word the rule leaves is no construct the two read alike: it is the
    # rule's own limit, closed for the message by the union (`_LIMIT`).
    rng = random.Random(877)
    lines = [
        "".join(rng.choice(_ORACLE_PIECES) for _ in range(rng.randint(1, 30))) for _ in range(3000)
    ]
    lines = [line for line in lines if not _box_leaves_a_scheme_word(line)]
    assert len(lines) > 1500
    box = _scrub_lines(_vector(), tmp_path, lines, alone="rule")
    leaked = [
        (line, got)
        for line, got in zip(lines, box, strict=True)
        if got.count(_ORACLE_SECRET) > redact_credentials(line).count(_ORACLE_SECRET)
    ]
    assert not leaked, leaked[:5]


def test_a_closed_quoted_value_goes_whatever_came_before_it(tmp_path: Path) -> None:
    # Quote parity is the fleet's reading of a well-formed line, and a foreign line need not be one:
    # a stray `"` earlier (an inch mark, a `'"'` in a traceback, a quote opened on a line above)
    # flips it, and a quoted value then reads as a close. So a double-quoted value closed on its
    # line before a word's end goes whatever the parity, and this holds it independently of the
    # parity model the property above shares with the rule: seeded junk of every kind before it.
    rng = random.Random(1877)
    junk = ['"', "'", '\\"', "\n", '5" wide', "KeyError: '\"'", "a=", " ", "x", "\\", "`"]
    secret = "Fab1catedQuotedSecret"
    lines = [
        "".join(rng.choice(junk) for _ in range(rng.randint(0, 12)))
        + f' X_TOKEN="{secret} value"'
        + rng.choice(["", " next=1", "\n", ",", "}", "\tz"])
        for _ in range(3000)
    ]
    box = _scrub_lines(_vector(), tmp_path, lines)
    leaked = [(line, got) for line, got in zip(lines, box, strict=True) if secret in got]
    assert not leaked, leaked[:5]


# --- a bearer header's token is taken whole, quoted or not (#507) --------------------------------
#
# The fleet's definition (basecradle-harness docs/telemetry.md → Credential Scrubbing, #929): the
# scheme word is `bearer` in any case glued to anything before it; the token is a run of token
# characters, or the whole shell word a quote or substitution opens, or an escaped quote and the
# rest of its word; while the word read as the token ends in `bearer`, the next word is the token.
# Every line below is a bearer header built by that definition, so where its token stands is known
# by construction, and the token is the fabricated secret: it must never ship. Seeded fuzz over
# every context a header reaches the box in, outside a credential-named assignment's value. In one,
# the print site reads the fleet's scheme word (any word of closed pieces ending in `bearer`) and
# holds the same by its own fuzz (#510); the box's credential rule reads it only as #497 did, the
# stated limit below, where the canary reads nothing the box ships.

_BEARER_SECRET = "eyJFab1catedSig"

#: Words ending in a scheme word: bare in every case, glued to token characters, to a closed
#: quote, a substitution, a bracket, or an escaped quote; and a word whose quoted piece is left
#: open, or closed and followed by one left open, glued before it (#507: the NOC's differential).
_GLUED_SCHEME_WORDS = [
    "Bearer",
    "bearer",
    "BEARER",
    "KBearer",
    "x.Bearer",
    "a=Bearer",
    "abc-bearer",
    '"q"bearer',
    "'q'Bearer",
    "`q`Bearer",
    '\\"Bearer',
    "\\'Bearer",
    "(a)Bearer",
    "$(x)Bearer",
    "[x]Bearer",
]
_OPEN_PIECES = ["'", '"', "`", "$(", "${", "$'", '\\"', "'q'", '"q"', "`q`", '""K)', '$("\t)']
_SCHEME_WORDS = _GLUED_SCHEME_WORDS + [
    first + second + "Bearer" for first in _OPEN_PIECES for second in ["", *_OPEN_PIECES[:3]]
]
_SCHEME_BLANKS = [" ", "\t", "  ", " \t"]

#: The token, holding the secret: bare, in every quote and substitution, escaped, left open,
#: glued after a closing quote, with escapes inside, and with text glued after it.
_TOKENS = [
    _BEARER_SECRET,
    f"{_BEARER_SECRET}==",
    f"'{_BEARER_SECRET}'",
    f'"{_BEARER_SECRET}"',
    f"`{_BEARER_SECRET}`",
    f"$'{_BEARER_SECRET}'",
    f"$({_BEARER_SECRET})",
    "${" + _BEARER_SECRET + "}",
    f'\\"{_BEARER_SECRET}\\"',
    f"\\'{_BEARER_SECRET}",
    f'\\\\"{_BEARER_SECRET}\\\\"',
    f"'a {_BEARER_SECRET} b'",
    f'"a {_BEARER_SECRET} b"',
    f"'{_BEARER_SECRET}",
    f'"{_BEARER_SECRET}',
    f'"x"{_BEARER_SECRET}',
    f"'x'{_BEARER_SECRET}",
    f'"a\\"{_BEARER_SECRET}\\""',
    f"$'a\\'{_BEARER_SECRET}'",
    f'"{_BEARER_SECRET}"next',
]

#: Where a header stands: prose, a header dump, compact JSON, the two literal rules that carry it,
#: and after a heartbeat run, with the scheme word put back or written in its place.
_HEADER_CONTEXTS = [
    "",
    "x",
    "auth: ",
    "Authorization: ",
    '{"auth":"',
    "x-api-key: ",
    '"x-api-key": "',
    "api_key=",
    "?api_key=",
    "https://h.example.com/heartbeat/K9&auth=",
    "https://h.example.com/heartbeat/K9x",
    "uptime.example.com/heartbeat/K9 ",
]

#: A credential-named assignment's value, which the print site and the box's credential rule read
#: before any header rule (#510): bare, exported, glued after a quote or a `,`, opening with a `,`,
#: and after a lead word a tab glues to it.
_CREDENTIAL_CONTEXTS = [
    "A_TOKEN=",
    "X_API_KEY=",
    "export GH_TOKEN=",
    "SECRET_KEY_BASE=",
    "x,DB_PASSWORD=",
    "'q'DB_PASSWORD=",
    "A_TOKEN=,",
    "A_TOKEN=x\t",
]
_HEADER_ENDS = ["", " next=1", '"}', "\nnext", " | other", "'", '"']


def _bearer_header(rng: random.Random, contexts: list[str], scheme_words: list[str]) -> str:
    words = [rng.choice(scheme_words) for _ in range(rng.choice([1, 1, 2, 2, 3, 4]))]
    header = rng.choice(contexts) + words[0]
    for word in words[1:]:
        header += rng.choice(_SCHEME_BLANKS) + word
    return header + rng.choice(_SCHEME_BLANKS) + rng.choice(_TOKENS) + rng.choice(_HEADER_ENDS)


def test_no_bearer_header_ships_its_token(tmp_path: Path) -> None:
    rng = random.Random(507)
    lines = [_bearer_header(rng, _HEADER_CONTEXTS, _SCHEME_WORDS) for _ in range(4000)]
    box = _scrub_lines(_vector(), tmp_path, lines)
    leaked = [(line, got) for line, got in zip(lines, box, strict=True) if "Fab1cated" in got]
    assert not leaked, leaked[:5]


def test_no_credential_value_ends_between_a_scheme_word_and_its_token_at_the_print_site() -> None:
    # The print site reads the same definition (#510): every header in a credential-named value
    # loses its token there too, before any line reaches the box.
    rng = random.Random(510)
    lines = [_bearer_header(rng, _CREDENTIAL_CONTEXTS, _GLUED_SCHEME_WORDS) for _ in range(6000)]
    leaked = [line for line in lines if "Fab1cated" in redact_credentials(line)]
    assert not leaked, leaked[:5]


#: What the box did not take while its literal rules ran one after another (basecradle-router#510),
#: each closed by the union (#516), with what the box ships now. The print site closes the first
#: four at the source and leaves the rest; the box takes them all, every rule reading the line as
#: it came.
#:
#: 1. The issue's three reproductions: a credential value ending in a word that the fleet's
#:    definition reads as a scheme word (any word ending in `bearer`). The bearer rule reads every
#:    such word, and the credential value and the header after it are one merged span.
#: 2. A credential value opening with a `,` or a `}`: the wide run reads it.
#: 3. An `x-api-key` header whose value is an `api_key=` assignment with a quoted token: the
#:    assignment rule reads the line as it came, so the header rule no longer takes its label.
#: 4. An `api_key=` value opening with an escaped quote (#515): the value opens at a quote
#:    escaped to any depth.
#: 5. A scheme word glued after a piece the word leaves open: the credential rule pairs that quote
#:    with a later one, the bearer rule opens the token's quote, and the union takes both.
_LIMIT = {
    "A_TOKEN=abc-bearer eyJFab1catedSig.e30.sig next=1": f"A_TOKEN={BOX_REDACTED_VALUE} next=1",
    "A_TOKEN=Bearer x\"q\"bearer 'eyJFab1catedSig' next=1": f"A_TOKEN={BOX_REDACTED_VALUE} next=1",
    "X_API_KEY=(a)Bearer eyJFab1catedSig next=1": f"X_API_KEY={BOX_REDACTED_VALUE} next=1",
    "A_TOKEN=,K9Fab1catedSecret01 next=1": f"A_TOKEN={BOX_REDACTED_VALUE} next=1",
    "x-api-key: api_key=\"K9Fab1catedSecret03' next=1": "x-api-key: [REDACTED] next=1",
    'api_key=\\"K9Fab1catedSecret07\\" next=1': 'apiKey=[REDACTED]\\" next=1',
    "SECRET_KEY_BASE='q''Bearer\t'a eyJFab1catedSig b'": f"SECRET_KEY_BASE={BOX_REDACTED_VALUE}",
    'X_API_KEY="Bearer\t"a eyJFab1catedSig b" next=1': f"X_API_KEY={BOX_REDACTED_VALUE} next=1",
}

#: The lines the print site closes at the source: the first four.
_LIMIT_CLOSED_AT_THE_SOURCE = list(_LIMIT)[:4]


def test_the_boxs_former_limits_are_closed_and_ship_nothing_the_canary_reads(
    tmp_path: Path,
) -> None:
    # Pinned, so a change to either reading is seen: the box takes every former limit, the print
    # site takes the ones it closes, and the canary reads nothing in any line the box ships. The
    # fuzz holds the same over every scheme word in every credential-named context.
    lines = list(_LIMIT)
    assert _scrub_lines(_vector(), tmp_path, lines) == list(_LIMIT.values())
    for line in lines:
        closed = "Fab1cated" not in redact_credentials(line)
        assert closed == (line in _LIMIT_CLOSED_AT_THE_SOURCE), line
    rng = random.Random(1510)
    lines = [_bearer_header(rng, _CREDENTIAL_CONTEXTS, _SCHEME_WORDS) for _ in range(3000)]
    canary = re.compile(NOC_CREDENTIAL_ASSIGNMENT)
    shipped = [got for got in _scrub_lines(_vector(), tmp_path, lines) if canary.search(got)]
    assert not shipped, shipped[:5]


@pytest.mark.parametrize("host", HOSTS)
def test_the_shipped_scrub_compiles_without_a_warning(host: str, tmp_path: Path) -> None:
    # A VRL compilation warning fails no check (`vector validate` and `vector test` print none),
    # yet a running Vector logs it on every start, into the box's own journal: noise an operator
    # learns to skim past (basecradle#414). The first deploy of #494 did exactly that with a dead
    # `!` on an infallible call. So every shipped transform runs here as the box runs it, a stdin
    # source in place of journald and a blackhole for the sinks, over one fabricated record, and
    # must start with no warning of any kind. No network: neither component reaches one.
    vector = _vector()
    text = _under_test(host, tmp_path).read_text()
    transforms = text[
        text.index("\ntransforms:\n") + len("\ntransforms:") : text.index("\nsinks:\n")
    ]
    journald_input = 'inputs:\n      - "ai_journald"'
    assert transforms.count(journald_input) == 1
    config = tmp_path / "running.yaml"
    config.write_text(
        f'data_dir: "{tmp_path}"\n'
        + text[text.index("enrichment_tables:") : text.index("\nsources:\n")]
        + '\nsources:\n  stdin_in:\n    type: "stdin"\n    decoding:\n      codec: "json"\n'
        + "transforms:"
        + transforms.replace(journald_input, 'inputs:\n      - "stdin_in"')
        + '\nsinks:\n  out:\n    type: "blackhole"\n    inputs: ["ai_scrub"]\n'
        + "    print_interval_secs: 0\n"
    )
    record = {"SYSLOG_IDENTIFIER": "basecradle-router", "_UID": "999", "message": "started"}
    got = subprocess.run(
        [vector, "--config", str(config)],
        input=json.dumps(record) + "\n",
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
        env=_env({}),
    )
    report = got.stdout + got.stderr
    assert got.returncode == 0, report
    assert "Vector has started" in report, report
    assert "warning[" not in report and " WARN " not in report, report
