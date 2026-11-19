r"""The standing differential: the box's scrub and the print site against the harness's (#516).

Three rounds of #510 each found, in a second seat's differential, a regression this repository's
own fuzz could not see: the box's literal rules run one after another, so one rule's value or
written-back word becomes another rule's boundary, and a generator that lacks the shape never
reaches it. This holds every reading the fleet has to one generator, so a shape is found here, in
CI, before a server runs it.

**One generator** (`texts`), seeded, over the union of every piece set the suite holds
(`tests/test_vector_unit.py`'s fuzz, oracle, scheme-word, token, header and credential pieces), the
pieces of this repository's #510 prototype, and the NOC's differential (basecradle-noc#1573: its
scheme words, names, openers, glue and closes, and its contexts), plus the three shapes the rounds
found: every rule's label after a credential value, an opener and a scheme word; a bearer header
with an opened piece before a credential assignment; `api_key=`-heavy contexts. Every text carries
one fabricated secret (`SECRET`, in a bearer token, a quoted value, a vendor-prefixed key, …), and
a reading *takes* it when no eight characters of it survive (the NOC's measure). The rounds'
reproductions are read on every run too (`REPRODUCTIONS`).

**Three readings of every text:** the box's `ai_scrub`, run by the boxes' Vector; the print site,
`logfmt.redact_credentials`; and the harness's own scrub at the fleet's pinned release
(`HARNESS_VERSION`), read out of its published wheel by `tests/harness_scrub.py`. The box answers
to the harness's `scrub` (every rule); the print site, which reads credential-named assignments
alone, to the harness's `redact_credentials` (the same rule alone).

**Three failures.** A secret the harness takes and the box or the print site leaves, unless a row of
`STATED` names the text; against the previous commit's `ai_scrub`, any regression at all, a secret
a host's old config took and its new one leaves, with no table (the bar #510's rounds were held
to); and against the previous commit's print site, the same, unless a `print-history` row states
the shape (the capital, 2026-11-19 on #516: #514's print-site change had no second seat's
differential, and this is the check that would have named its one shape before the merge word).

**The table** (`STATED`): each row a name, a predicate, its reason, where the difference is written
down (a phrase `deploy/README.md` holds, or the issue a finding was reported on), and a fabricated
example that must still differ. Most predicates read the text's shape; two read a mechanism instead,
computed from the text: `pass order` (the box's own rules, each run alone on the text, take the
secret between them, so only their sequence loses it: piece 2 of #516 empties it) and `a marker read
again` (no one rule of the harness's takes it alone). A mechanism row explains any shape of its
mechanism, which is why the regression bar below carries no table: a change that makes the box
leave a secret it took fails there, whatever row the text would fall under. A text no row
explains fails the run with the text.

Run it deeper by hand: `uv run python tests/scrub_differential.py --lines 400000 --seed 1` prints
what each row explains and every text no row does (`--help`).
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import types
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "tests") not in sys.path:  # run as a script, the suite's helpers are siblings
    sys.path.insert(0, str(REPO / "tests"))

import scrub_block  # noqa: E402
import scrub_compare  # noqa: E402
import test_vector_unit as tvu  # noqa: E402
from basecradle_router import logfmt  # noqa: E402
from basecradle_router.logfmt import redact_credentials  # noqa: E402

# --- the harness, pinned -------------------------------------------------------------------------

#: The harness release the fleet runs (the NOC's inventory pin), and its wheel's sha256 as PyPI
#: publishes it. Move both with the fleet's harness roll; CI downloads exactly this file
#: (ci.yml → "Fetch the Harness's Scrub"), and `tests/harness_scrub.py` refuses any other bytes.
HARNESS_VERSION = "0.198.2"
HARNESS_WHEEL_SHA256 = "3bd085826bbc6f78a3ad7a02e129b68769839d878f83b1620e61095d2baedf61"

# --- the secret ----------------------------------------------------------------------------------

#: The fabricated secret every text carries, exactly once, so a row reads the one copy there is.
#: `Fab1cated` is the fleet's mark for fabricated test data; the rest is mixed case and digits, so
#: every rule's run reads it, and no piece below shares eight characters with it (asserted at
#: import).
SECRET = "Fab1catedQ7ZkW9xDiffVq3Lm8Rt"
_WINDOWS = tuple(SECRET[i : i + 8] for i in range(len(SECRET) - 7))


def leaks(text: str) -> bool:
    """Whether any eight characters of `SECRET` survive in `text` (the NOC's measure)."""
    return any(window in text for window in _WINDOWS)


# --- the pieces ----------------------------------------------------------------------------------

#: Credential-named assignments: every name shape the canary reads, and where one stands.
NAMES = sorted(
    {
        *tvu._CREDENTIAL_CONTEXTS,
        "A_TOKEN=",
        "API_KEY=",
        "X_API_KEY=",
        "PASSWORD=",
        "DB_SECRET=",
        "SECRET_KEY_BASE=",
        "MY_API_KEY=",
        "x,DB_PASSWORD=",
        "'q'DB_PASSWORD=",
        "export GH_TOKEN=",
        "X-API-KEY=",
        "API-KEY=",
    }
)

#: What a value may open with: nothing, a canary stop, a quote, an escape, a substitution.
OPENERS = ["", "", "", ",", "}", ",}", "},", ",x}", "x", "'q'", '"', "'", '\\"', "\\'", "`"]
OPENERS += ["$(", "${", "$'", "("]

#: Every other literal rule's label, as the box's rules read them, and the separators they take.
LABELS = sorted(
    {
        "x-api-key: ",
        "x-api-key:",
        "X_API_KEY: ",
        "X-API-KEY: ",
        "x-api-key=>",
        '"x-api-key": "',
        '"x-api-key": ',
        "('x-api-key', '",
        'x-api-key: \\"',
        "api_key=",
        "apiKey=",
        "api-key=",
        "API-KEY=",
        "X-API-KEY=",
        "Kapi_key=",
        "api_key=\\",
        'api_key=\\"',
        'api_key="',
        "?api_key=",
        "&api_key=",
        "?apiKey=",
        "?token=",
        "&key=",
        "x-api-key: api_key=",
        "x-api-key: Bearer api_key=",
        "Bearer ",
        "KBearer ",
        "abc-bearer ",
        "Authorization: Bearer ",
        "https://h.example.com/heartbeat/K9",
        "uptime.example.com/heartbeat/K9 ",
    }
)

#: Scheme words: every one the fleet's definition reads (closed pieces and open), and the NOC's.
SCHEME_WORDS = sorted(
    {
        *tvu._SCHEME_WORDS,
        "KBearer",
        "x.Bearer",
        "abc-bearer",
        "(a)Bearer",
        'x"q"bearer',
        "\\Bearer",
        '\\"a b\\"bearer',
        "'q'Bearer",
        "`q`bearer",
        "a=Bearer",
        "$(a)Bearer",
        "}Bearer",
        ",bearer",
    }
)
BLANKS = tvu._SCHEME_BLANKS

#: What glues one piece to the next.
GLUE = ["", "", "", " ", "\t", "x", "K", "=", ":", ".", "-", "q", ")", "}", ",", '"', "'", "\\"]
GLUE += ['\\"', "(a)", "next=1 ", "&a=1 "]

#: Where a text stands: prose, a log line, compact JSON, a header dump, a URL, and the rounds'
#: mechanism A, a bearer header with an opened piece before what follows.
CONTEXTS = (
    sorted(
        {
            *tvu._HEADER_CONTEXTS,
            "msg ",
            "stage=wake ",
            "err: ",
            '{"h":"',
            'msg="',
            "x ",
            'Bearer "',
            "Bearer '",
            "Bearer `",
            "Bearer $(",
            "Bearer ${",
            'Bearer \\"',
            'Bearer "x ',
            "Bearer 'q ",
            'x-api-key: Bearer "',
            'api_key=\\"',
            'api_key="',
            'x-api-key: api_key=\\"',
            "api_key=x,",
            "x-api-key: Bearer api_key=\\",
            'apiKey=\\"Bearer ',
            'api-key=Bearer "',
            'Authorization: Bearer "',
            '{"h":"Bearer \'',
        }
    )
    + [""] * 12
)

#: The pieces the suite's own fuzz is made of, a secret of its own left out.
PIECES = sorted(
    {
        piece
        for piece in (*tvu._FUZZ_PIECES, *tvu._ORACLE_PIECES, *tvu._OPEN_PIECES)
        if "Fab1cated" not in piece
    }
)

#: How the secret is written: a bare run, every quote and substitution, escaped, left open, glued
#: after a closing quote or a stop, and as a vendor-prefixed key the prefix rules read.
TOKENS = sorted(
    {
        token.replace(tvu._BEARER_SECRET, "{s}")
        for token in tvu._TOKENS
        if tvu._BEARER_SECRET in token
    }
    | {
        "{s}",
        "eyJ{s}",
        "eyJ{s}.e30.sig",
        "{s}==",
        "'{s}'",
        '"{s}"',
        "'\"{s}",
        '\\"{s}\\"',
        ",{s}",
        "}{s}",
        "x}{s}",
        "{s},tail",
        "$({s})",
        "`{s}`",
        "(eyJ{s}",
        "q(eyJ{s}",
        "={s}",
        "=,{s}",
        "ghp_{s}AbCd",
        "github_pat_11ABCDEFG0{s}_klmnopqrstuv",
        "sk-ant-api03-{s}",
        "sk-proj-{s}",
        "xai-{s}",
        "AIza{s}abc",
        "hf_{s}",
        "r8_{s}",
        "bc_uat_{s}",
        "bc_isk_{s}",
        "abl_{s}Kq7Zt2Wm9Rx4Lp6Vn3Hs",
        "pplx-{s}",
        "tk_{s}",
        "ste-{s}Kq7Zt2Wm9Rx4Lp6Vn3Hs8Dj5Fb1Gc0Ye7Ua2Ti9Ow4",
    }
)

#: What follows the secret.
ENDS = sorted(
    {
        *tvu._HEADER_ENDS,
        "",
        " next=1",
        "&a=1 next=1",
        '"}',
        "\n next",
        "'",
        " | other",
        '"',
        '\\"',
        "`",
        ")",
        "}",
        ",",
        "&a=1",
        "\\",
        ' b" next=1',
    }
)

_ALL_PIECES = [
    *NAMES,
    *OPENERS,
    *LABELS,
    *SCHEME_WORDS,
    *GLUE,
    *CONTEXTS,
    *PIECES,
    *(t.replace("{s}", "") for t in TOKENS),
    *ENDS,
]
assert not [p for p in _ALL_PIECES if leaks(p * 3)], "a piece shares eight characters with SECRET"


_HEADER_HEADS = CONTEXTS + NAMES
_SECRET_HEADS = [*LABELS, *NAMES, ""]


def _scheme(rng: random.Random) -> str:
    return rng.choice(SCHEME_WORDS) + rng.choice(BLANKS)


def _segment(rng: random.Random) -> str:
    """One piece of what stands before the secret: a credential name and its opener, another
    rule's label, a scheme word with its blanks, an open piece, or a piece of the suite's fuzz,
    each with some glue after it."""
    kind = rng.random()
    if kind < 0.25:
        piece = rng.choice(NAMES) + rng.choice(OPENERS)
    elif kind < 0.50:
        piece = rng.choice(LABELS)
    elif kind < 0.72:
        piece = _scheme(rng)
    elif kind < 0.82:
        piece = rng.choice(OPENERS)
    else:
        piece = rng.choice(PIECES)
    return piece + rng.choice(GLUE)


def _text(rng: random.Random) -> str:
    shape = rng.random()
    secret = rng.choice(TOKENS).replace("{s}", SECRET)
    if shape < 0.15:
        # The suite's own fuzz, with the secret dropped somewhere in it.
        pieces = [rng.choice(PIECES) for _ in range(rng.randint(1, 16))]
        pieces.insert(rng.randint(0, len(pieces)), secret)
        text = "".join(pieces)
    elif shape < 0.30:
        # A bearer header the fleet's definition builds (#507), in every context.
        words = [rng.choice(SCHEME_WORDS) for _ in range(rng.choice([1, 1, 2, 2, 3]))]
        text = rng.choice(_HEADER_HEADS) + words[0]
        for word in words[1:]:
            text += rng.choice(BLANKS) + word
        text += rng.choice(BLANKS) + secret + rng.choice(ENDS)
    else:
        # The rounds' shapes: a context, a run of names, labels, scheme words and openers, and
        # the secret, so every rule's label stands after every other rule's value.
        text = rng.choice(CONTEXTS)
        for _ in range(rng.choice([0, 1, 1, 2, 2, 3, 4])):
            text += _segment(rng)
        text += rng.choice(_SECRET_HEADS) + rng.choice(GLUE) + secret + rng.choice(ENDS)
    if rng.random() < 0.2:
        # A JSON line: the text as a string value, with structure after it.
        text = json.dumps({"h": text, "n": 1})
    return text


#: What CI reads: one seed and 40,000 texts (about five seconds). A deeper run is by hand.
SEED = 516
LINES = 40_000


def texts(seed: int, count: int) -> list[str]:
    """`count` fabricated texts from `seed`, each holding `SECRET` once."""
    rng = random.Random(seed)
    return [_text(rng) for _ in range(count)]


#: The rounds' reproductions, read on every run: #510's and #515's lines, the box's stated limits,
#: and the NOC's minimal cases (basecradle-noc#1573, c1 to c8), with its secret replaced by ours.
REPRODUCTIONS = [
    line.replace("eyJfabricatedSecretValue", "eyJ" + SECRET).replace("K9fabricated", SECRET)
    for line in (
        # #510 and #515
        "A_TOKEN=abc-bearer eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=Bearer x\"q\"bearer 'eyJfabricatedSecretValue01' next=1",
        "X_API_KEY=(a)Bearer eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=,K9fabricatedSecret01 next=1",
        "x-api-key: api_key=\"K9fabricatedSecret03' next=1",
        'api_key=\\"K9fabricatedSecret07\\" n',
        '{"h":"api_key=\\"K9fabricatedSecret03\\""}',
        "SECRET_KEY_BASE='q''Bearer\t'a eyJfabricatedSecretValue01 b'",
        'X_API_KEY="Bearer\t"a eyJfabricatedSecretValue01 b" next=1',
        'api_key=x-api-key: "K9fabricatedSecret04" next=1',
        "x-api-key: x-api-key=> eyJfabricatedSecretValue02 next=1",
        # c1
        "A_TOKEN=,x-api-key: eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=}x-api-key: eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=,api_key= eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=,x,api_key=eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=}Bearer 'eyJfabricatedSecretValue01 next=1",
        'A_TOKEN=}Bearer "eyJfabricatedSecretValue01 next=1',
        "A_TOKEN=,Bearer `eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=,x}eyJfabricatedSecretValue01 next=1",
        "API_KEY=},x}eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=Bearer 'eyJfabricatedSecretValue01 next=1",
        # c2
        "A_TOKEN=}Bearer '\"eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=,Bearer eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=,Bearer\teyJfabricatedSecretValue01 next=1",
        'A_TOKEN=,x"q"Bearer eyJfabricatedSecretValue01 next=1',
        'PASSWORD=,"x-api-key": "eyJfabricatedSecretValue01" next=1',
        "A_TOKEN=,apiKey=eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=,a,api_key=eyJfabricatedSecretValue01 next=1",
        '{"h":"A_TOKEN=,x-api-key: eyJfabricatedSecretValue01"}',
        'A_TOKEN=,"x-api-key: eyJfabricatedSecretValue01" next=1',
        "A_TOKEN=abc-bearer 'eyJfabricatedSecretValue01 next=1",
        'A_TOKEN=x"q"bearer eyJfabricatedSecretValue01 next=1',
        # c3
        "A_TOKEN=abc-bearer x-api-key: eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=(a)Bearer x-api-key: eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=Bearer x-api-key: eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=abc-bearer apiKey=,eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=abc-bearer ,eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=Bearer ,eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=abc-bearer }eyJfabricatedSecretValue01 next=1",
        "A_TOKEN='q'Bearer x-api-key:}eyJfabricatedSecretValue01 next=1",
        "A_TOKEN=abc-bearer 'api_key=\"eyJfabricatedSecretValue01 next=1",
        # c4, c5, c6
        "A_TOKEN=(a)Bearer eyJfabricatedSecretValue01 next=1",
        'api_key=\\",api_key="eyJfabricatedSecretValue01&a=1 next=1',
        'api_key=x,api_key="eyJfabricatedSecretValue04" next=1',
        'api-key="Bearer api_key="eyJfabricatedSecretValue46&a=1 next=3',
        "api_key=abc api_key=eyJfabricatedSecretValue05 next=4",
        "api_key=Bearer x api_key=eyJfabricatedSecretValue06 next=5",
        # c7, c8
        'Bearer "A_TOKEN=abc-bearer q(eyJfabricatedSecretValue01 next=1',
        "Bearer $(PASSWORD=abc-bearer K(eyJfabricatedSecretValue02 next=1",
        "Bearer `API_KEY=x\tBearer q,eyJfabricatedSecretValue03 next=1",
        'Bearer "A_TOKEN=abc-bearer eyJfabricatedSecretValue09 next=1',
        "A_TOKEN=abc-bearer q(eyJfabricatedSecretValue10 next=1",
        "x-api-key: Bearer api_key=\\Bearer X-API-KEY=,eyJfabricatedSecretValue04 next=1",
        "api_key=Bearer X-API-KEY=}eyJfabricatedSecretValue05 next=1",
        "api_key=Bearer API_KEY=,eyJfabricatedSecretValue06 next=1",
        "api_key=Bearer X-API-KEY=eyJfabricatedSecretValue07 next=1",
        "x-api-key: api_key=Bearer X-API-KEY=eyJfabricatedSecretValue08 next=1",
        'x-api-key: Bearer api_key=\\Bearer X-API-KEY="eyJfabricatedSecretValue11" next=1',
        "x-api-key: Bearer api_key=\\Bearer X-API-KEY=(eyJfabricatedSecretValue12 next=1",
        "x-api-key: Bearer api_key='q'Bearer Kapi_key=$(eyJfabricatedSecretValue13 next=1",
        'api_key=\\Bearer X-API-KEY="eyJfabricatedSecretValue14" next=1',
        'x-api-key: Bearer api_key=\\Bearer X-API-KEY="K9fabricatedSecretValue15" next=1',
    )
]
assert all(leaks(line) for line in REPRODUCTIONS)

# --- the readings --------------------------------------------------------------------------------


def harness_python() -> str | None:
    """The interpreter that runs the harness's scrub (it needs Python 3.11 or later)."""
    return os.environ.get("HARNESS_PYTHON")


def harness_wheel() -> str | None:
    """The pinned harness wheel, downloaded by CI (or by hand) beside the run."""
    return os.environ.get("HARNESS_WHEEL")


def _harness(lines: list[str], *flags: str) -> object:
    python, wheel = harness_python(), harness_wheel()
    assert python and wheel, "HARNESS_PYTHON and HARNESS_WHEEL must both be set"
    got = subprocess.run(
        [python, "-I", str(REPO / "tests" / "harness_scrub.py"), wheel, HARNESS_WHEEL_SHA256]
        + list(flags),
        input=json.dumps(lines),
        capture_output=True,
        text=True,
        check=False,
    )
    assert got.returncode == 0, got.stderr
    return json.loads(got.stdout)


def harness_readings(lines: list[str]) -> tuple[list[str], list[str]]:
    """Every line through the harness's `scrub`, and through its `redact_credentials`."""
    out = _harness(lines)
    assert len(out["scrub"]) == len(out["credentials"]) == len(lines)
    return out["scrub"], out["credentials"]


def harness_rules(lines: list[str]) -> list[frozenset[str]]:
    """For each line, the harness's rules that take the secret on their own."""
    found = _harness(lines, "--spans") if lines else []
    assert len(found) == len(lines)
    taken = []
    for text, rules in zip(lines, found, strict=True):
        names = set()
        for name, spans in rules.items():
            out, pos = [], 0
            for start, end in sorted(spans):
                if start >= pos:
                    out += [text[pos:start], "[redacted]"]
                    pos = end
                else:
                    pos = max(pos, end)
            if not leaks("".join(out) + text[pos:]):
                names.add(name)
        taken.append(frozenset(names))
    return taken


def host_sources(ref: str | None = None) -> dict[str, str]:
    """Each host's `ai_scrub` at `ref` (a commit), or as the tree holds it, the writer check taken
    out (it reads the record's identifier and `_UID`, never a value). A host the commit did not
    have is left out; a commit with none of them is an error, never an empty comparison."""
    sources: dict[str, str] = {}
    for host in tvu.HOSTS:
        path = f"deploy/hosts/{host}/vector.yaml"
        if ref is None:
            text = (REPO / path).read_text()
        else:
            got = subprocess.run(
                ["git", "show", f"{ref}:{path}"], cwd=REPO, capture_output=True, text=True
            )
            if got.returncode != 0:
                continue
            text = got.stdout
        sources[host] = tvu._without_the_writer_check(tvu.scrub_vrl_of(text))
    if not sources:
        raise ValueError(f"{ref} holds no host's vector.yaml")
    return sources


def scrub_sources(ref: str | None = None) -> list[str]:
    """Every distinct `ai_scrub` the hosts ship (`host_sources`), in host order."""
    return list(dict.fromkeys(host_sources(ref).values()))


def box_readings(vector: str, source: str, lines: list[str]) -> list[str]:
    """Every line's message after the `ai_scrub` VRL `source`, its `[identifier] ` prefix taken
    off, run by the boxes' Vector."""
    out = _run_vrl(vector, source, lines)
    assert all(row.startswith("[x] ") for row in out)
    return [row[len("[x] ") :] for row in out]


def rules_alone(source: str) -> dict[str, str]:
    """Each rule of the fleet's scrub (`tests/scrub_block.py`) as a program of its own over
    `.message`, reading the text as it came: the block with that rule's statements alone.

    Only for a source that carries the block the tree's generator writes, or this fails: a rule
    added to the box is a rule the differential must learn to read alone."""
    framed = "\n".join(
        line[6:] for line in (scrub_block.PARAMETERS + scrub_block.BLOCK).split("\n")
    )
    assert framed in source, "not the tree's scrub block"
    return {rule: scrub_block.alone(rule) + ".message = msg\n" for rule in scrub_block.RULES}


def _run_vrl(vector: str, program: str, lines: list[str]) -> list[str]:
    """`.message` after `program` for each line, raw (no identifier prefix to take off)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rule.vrl"
        path.write_text(program + "encode_base64(string!(.message))\n")
        events = Path(tmp) / "events.json"
        events.write_text(
            "".join(
                json.dumps({"SYSLOG_IDENTIFIER": "x", "message": line}) + "\n" for line in lines
            )
        )
        got = subprocess.run(
            [vector, "vrl", "-q", "-i", str(events), "-p", str(path)],
            capture_output=True,
            text=True,
            check=False,
            env=tvu._env({}),
        )
    assert got.returncode == 0, got.stdout[-2000:] + got.stderr[-2000:]
    out = [base64.b64decode(json.loads(row)).decode() for row in got.stdout.splitlines() if row]
    assert len(out) == len(lines)
    return out


def taken_apart(vector: str, source: str, lines: list[str]) -> list[bool]:
    """For each line, whether the box's rules, each reading the line as it came, take the secret
    between them: every eight characters of it are gone from at least one rule's output. That is
    the union the harness takes (its `scrub`), and what the box takes once its pass reads so."""
    if not lines:
        return []
    outputs = [_run_vrl(vector, program, lines) for program in rules_alone(source).values()]
    return [
        all(any(window not in out[i] for out in outputs) for window in _WINDOWS)
        for i in range(len(lines))
    ]


# --- the table -----------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Facts:
    """What a row may read about a text besides the text itself."""

    taken_apart: bool = False
    harness_rules: frozenset[str] = frozenset()


@dataclasses.dataclass(frozen=True)
class Row:
    """A stated difference: which reading it excuses (`"box"` or `"print"` against the harness,
    `"print-history"` against the previous commit's print site), a name, a predicate
    on the input text, its reason, where it is written down, and a fabricated text it explains,
    which must still differ (so a row whose difference is closed fails until it is removed)."""

    reading: str
    name: str
    applies: Callable[[str, Facts], bool]
    reason: str
    anchor: str
    example: str
    since: str = ""  #: for a `print-history` row: the commit whose print site took the example


def _credential_value_to_secret(text: str) -> str | None:
    """What a credential-named value holds from its `=` to the secret, read as the print site (and
    the harness, byte for byte) reads a value, when the secret is in that value; else None."""
    at, pos = text.find(SECRET), 0
    while (name := logfmt._CREDENTIAL_NAME.search(text, pos)) is not None:
        end = logfmt._value_end(text, name.end())
        if name.end() <= at < end:
            return text[name.end() : at]
        pos = max(end, name.end())
    return None


_SCHEME_AND_BLANKS = re.compile(r"(?i:bearer)\s+")


@dataclasses.dataclass
class _Word:
    """A shell word's pieces, read as the print site and the harness read one
    (`logfmt._shell_word_end`): how many quoted pieces, substitutions and parentheses open in
    it, whether one opens inside another, whether a bearer scheme word and its blanks stand inside
    a quote, and whether a bare `(` is left open."""

    pieces: int = 0
    nested: bool = False
    scheme_inside: bool = False
    open_paren: bool = False
    open_piece: bool = False


def _word(text: str) -> _Word:
    word = _Word()
    stack: list[tuple[str, bool]] = []  # what closes each open piece, and whether it is a bare `(`
    i = 0

    def push(close: str, width: int, bare: bool = False) -> None:
        nonlocal i
        word.pieces += 1
        word.nested = word.nested or bool(stack)
        stack.append((close, bare))
        i += width

    def scheme_at(at: int) -> None:
        word.scheme_inside = word.scheme_inside or bool(_SCHEME_AND_BLANKS.match(text, at))

    while i < len(text):
        top = stack[-1][0] if stack else ""
        if stack and not stack[-1][1]:  # inside a quote or a substitution, not a bare `(`
            scheme_at(i)
        c = text[i]
        if top == "'":  # a backslash is literal inside '…'
            if c == "'":
                stack.pop()
            i += 1
        elif c == "\\":
            if stack and not stack[-1][1]:
                scheme_at(i + 1)
            i += 2
        elif top == "$'":
            if c == "'":
                stack.pop()
            i += 1
        elif top == "`":
            if c == "`":
                stack.pop()
            i += 1
        elif top == '"':
            if c == '"':
                stack.pop()
                i += 1
            elif text.startswith(("$(", "${"), i):
                push(")" if text[i + 1] == "(" else "}", 2)
            elif c == "`":
                push("`", 1)
            else:
                i += 1
        elif top and c == top:
            stack.pop()
            i += 1
        elif text.startswith("$'", i):
            push("$'", 2)
        elif text.startswith(("$(", "${"), i):
            push(")" if text[i + 1] == "(" else "}", 2)
        elif c in "'\"`":
            push(c, 1)
        elif c == "(" and top != "}":
            push(")", 1, bare=True)
        else:
            i += 1
    word.open_paren = any(bare for _, bare in stack)
    word.open_piece = any(not bare for _, bare in stack)
    return word


def _bearer_token_to_secret(text: str) -> str | None:
    """The bearer token's word from its start to the secret, when the secret is in the shell word
    a scheme word's blanks lead to (the fleet's definition, read as the harness reads it)."""
    at = text.find(SECRET)
    for scheme in _SCHEME_AND_BLANKS.finditer(text, 0, at + 1):
        start = scheme.end()
        if start <= at < logfmt._shell_word_end(text, start)[0]:
            return text[start:at]
    return None


def _one_piece_deep(text: str, facts: Facts) -> bool:
    token = _bearer_token_to_secret(text)
    if token is None:
        return False
    word = _word(token)
    return word.pieces >= 2 or word.nested


def _scheme_word_inside_a_piece(text: str, facts: Facts) -> bool:
    token = _bearer_token_to_secret(text)
    return token is not None and _word(token).scheme_inside


_X_API_LABEL = re.compile(r"(?i:x[-_]api[-_]?key)")
_API_KEY_LABEL = re.compile(r"(?i:api[-_]?key=)")
_QUERY_LABEL = re.compile(r"[?&](?i:api[-_]?key|access[-_]?token|token|key)=")
_LEADING_SCHEMES = re.compile(r"(?:(?i:bearer)\s+)*")


def _inner_header(text: str, facts: Facts) -> bool:
    return len(_X_API_LABEL.findall(text, 0, text.find(SECRET))) >= 2


def _second_api_key_label(text: str, facts: Facts) -> bool:
    return len(_API_KEY_LABEL.findall(text, 0, text.find(SECRET))) >= 2


def _in_a_query_value(text: str, facts: Facts) -> bool:
    at = text.find(SECRET)
    return any(
        not re.search(r"[&\s]", text[_LEADING_SCHEMES.match(text, label.end()).end() : at])
        for label in _QUERY_LABEL.finditer(text, 0, at)
    )


def _credential(test: Callable[[str], bool]) -> Callable[[str, Facts], bool]:
    """A credential-named value the harness's own rule takes, whose stretch from its `=` to the
    secret passes `test`."""

    def applies(text: str, facts: Facts) -> bool:
        value = _credential_value_to_secret(text)
        return "credential" in facts.harness_rules and value is not None and test(value)

    return applies


def _in(rule: str, test: Callable[[str, Facts], bool]) -> Callable[[str, Facts], bool]:
    return lambda text, facts: rule in facts.harness_rules and test(text, facts)


def _one_level_deep(value: str) -> bool:
    """A `$(…)` or `(…)` inside another, or a bare `(` left open: the box reads a substitution one
    level deep and an open bare parenthesis as a plain character."""
    word = _word(value)
    return word.nested or word.open_paren


def _another_reading_of_a_value(text: str, facts: Facts) -> bool:
    """Only the harness's credential rule takes the secret, and the router's present reading puts
    it in no value: the box, held to that reading, has no value to take it from."""
    return facts.harness_rules == {"credential"} and _credential_value_to_secret(text) is None


def _past_a_scheme_word_only_510_reads(text: str, facts: Facts) -> bool:
    """A credential value, ending before the secret, that reaches past a word ending in `bearer`
    which only #510's one scheme word reads (one glued to anything, `abc-bearer`, `x"q"bearer`)
    to a word holding a credential name: the reading since #514 takes that word as the bearer
    token (a name inside it is its text), where the reading before ended the value at the scheme
    word and then read the name, and its value, from inside the word."""
    at, pos = text.find(SECRET), 0
    while (name := logfmt._CREDENTIAL_NAME.search(text, pos)) is not None:
        end = logfmt._value_end(text, name.end())
        if end <= at:
            value = text[name.end() : end]
            for word in re.finditer(r"\S*?(?i:bearer)(?=[ \t])", value):
                if not re.fullmatch(tvu._BOX_SCHEME_WORD, word.group()) and (
                    logfmt._CREDENTIAL_NAME.search(value, word.end())
                ):
                    return True
        pos = max(end, name.end())
    return False


#: Stated differences and findings, in the order they are tried. A finding's anchor is this issue:
#: it is reported there before anything is built for it.
STATED: list[Row] = [
    Row(
        "box",
        "a marker read again",
        lambda text, facts: not facts.harness_rules,
        "No one rule of the harness's takes the secret: it merges the spans its rules take and"
        " reads on past a marker as a shell word, again while it can (`_past_marker`). The box"
        " reads a marker again once, at a merged span's end and at each credential span's own.",
        "deploy/README.md “reads on once more, as a value whose first piece is the marker”",
        'MY_API_KEY=\\"\\X-API-KEY: .\'"Fab1catedQ7ZkW9xDiffVq3Lm8Rt\\',
    ),
    Row(
        "box",
        "a canary stop inside a value",
        _credential(lambda value: bool(set(value) & set(",}"))),
        "The box's value ends at a `,` or a `}`, structure on a JSON line, as the canary's run"
        " does (one opening the value is read past, #516); the harness reads the shell word.",
        "deploy/README.md “Where the box reads less than the print site, by design”",
        "A_TOKEN=x,Fab1catedQ7ZkW9xDiffVq3Lm8Rt next=1",
    ),
    Row(
        "box",
        "a record break inside a value",
        _credential(lambda value: bool(set(value) & set(logfmt._BREAK_CHARS))),
        "Every record break ends the box's value, as it ends the canary's run; the harness's shell"
        " word runs on through all but a newline.",
        "deploy/README.md “Where the box reads less than the print site, by design”",
        "A_TOKEN=x\rFab1catedQ7ZkW9xDiffVq3Lm8Rt next=1",
    ),
    Row(
        "box",
        "a record break inside a bearer token",
        _in(
            "bearer",
            lambda text, facts: bool(
                set(_bearer_token_to_secret(text) or "") & set(logfmt._BREAK_CHARS)
            ),
        ),
        "Every record break ends the box's bearer token, as it ends a value; the harness's shell"
        " word runs on through all but a newline outside a quote.",
        "deploy/README.md “Every record break ends a bearer token on the box”",
        "Bearer `q`\rFab1catedQ7ZkW9xDiffVq3Lm8Rt",
    ),
    Row(
        "box",
        "a bare double quote inside a value",
        _credential(lambda value: '"' in value),
        'The box reads a whole line, so no piece of a value crosses a bare `"`, which on a JSON or'
        " logfmt line is the line's own; the harness reads one value as a shell word.",
        "deploy/README.md “no piece ever crosses a bare”",
        "A_TOKEN='a\"Fab1catedQ7ZkW9xDiffVq3Lm8Rt' next=1",
    ),
    Row(
        "box",
        "a piece left open in a value",
        _credential(lambda value: _word(value).open_piece and bool(re.search(r"[ \t]", value))),
        "The box's first pass reads only pieces that close, so a substitution or quote left open"
        " is plain text there and the value ends at its first blank (a name inside it is read as a"
        " name); the harness reads the open piece to its line's end.",
        "deploy/README.md “Every other piece here must close”",
        "A_TOKEN=$(B_TOKEN=x Fab1catedQ7ZkW9xDiffVq3Lm8Rt next=1",
    ),
    Row(
        "box",
        "a substitution read one level deep",
        _credential(lambda value: _one_level_deep(value) or "${" in value),
        "The box reads a substitution or a `${…}` one level deep, with no quote or piece tracked"
        " inside it, and a bare `(` left open as a plain character; the harness nests.",
        "deploy/README.md “a substitution is read one level deep”",
        "A_TOKEN=a(b Fab1catedQ7ZkW9xDiffVq3Lm8Rt next=1",
    ),
    Row(
        "box",
        "a bearer token read to a bare quote or one level deep",
        _in("bearer", _one_piece_deep),
        "The bearer rule's pieces stop at a bare `\"`, the line's own on a JSON line, and read a"
        " substitution one level deep, in both its readings; the harness's shell word crosses the"
        " quote and nests, so its token runs on where the box's ends.",
        "deploy/README.md “no piece but a double-quoted one crosses one”",
        "Bearer $(Bearer q$(Fab1catedQ7ZkW9xDiffVq3Lm8Rt))",
    ),
    Row(
        "box",
        "a scheme word past a bare quote in the token",
        _in("bearer", _scheme_word_inside_a_piece),
        "A scheme word the harness reads inside the token's own piece lies past a bare `\"`, where"
        " the box's piece stops, so the box reads it as a scheme word and ends the token there.",
        "deploy/README.md “no piece but a double-quoted one crosses one”",
        'Bearer `\\"a b\\"bearer  q(eyJFab1catedQ7ZkW9xDiffVq3Lm8Rt`',
    ),
    Row(
        "box",
        "a double quote in a query value",
        _in("query", _in_a_query_value),
        "A query value stops at a `\"`, as the web hosts' rule does, so a URL inside a JSON string"
        " keeps its line JSON; the harness's `_QUERY` reads quotes into the value.",
        'deploy/README.md “loses its value to the next `&`, blank or `"`”',
        '&key=\\""Fab1catedQ7ZkW9xDiffVq3Lm8Rt"\nnext',
    ),
    Row(
        "print",
        "the harness's reading before #514",
        _past_a_scheme_word_only_510_reads,
        "The harness carries the router's reading of a value as it was before #514 until it"
        " re-pins: a word ending in `bearer` such as `abc-bearer` was no scheme word, so the"
        " value ended there and a credential name in the quoted word after it was read as a"
        " name. The present reading takes that word as the bearer token (a name inside a quote"
        " is the quote's text) and leaves the word after it, which no reading calls a value.",
        "basecradle-router#516 (finding); the harness's re-pin of #514",
        "A_TOKEN=abc-bearer 'B_TOKEN=x' Fab1catedQ7ZkW9xDiffVq3Lm8Rt next=1",
    ),
    Row(
        "box",
        "the harness's reading before #514",
        _another_reading_of_a_value,
        "The box's credential rule is held to the router's reading of a value (the print site),"
        " and the harness's credential rule, still on the reading before #514, takes a value that"
        " reading does not: the row above, seen from the box.",
        "basecradle-router#516 (finding); the harness's re-pin of #514",
        'A_TOKEN=}abc-bearer "DB_SECRET=-"a Fab1catedQ7ZkW9xDiffVq3Lm8Rt b"',
    ),
    Row(
        "print-history",
        "a word holding a name, after a scheme word only #510 reads",
        _past_a_scheme_word_only_510_reads,
        "Since #514 the value goes on past a scheme word glued to anything to the word after it,"
        " the bearer token, and a credential name inside that word is its text. The reading"
        " before ended the value at the scheme word and read the name as an assignment from"
        " inside the word, taking the word after it. That word is no credential's value under"
        " the fleet's reading (decided on #516, 2026-11-19).",
        "deploy/README.md “a word that holds a credential name after a scheme word only #510"
        " reads”",
        "A_TOKEN=abc-bearer 'B_TOKEN=x' Fab1catedQ7ZkW9xDiffVq3Lm8Rt next=1",
        since="ac3fe8a",
    ),
]


def explained(reading: str, text: str, facts: Facts) -> Row | None:
    """The first row of `STATED` that names `text`'s difference in `reading`, or None."""
    return next(
        (row for row in STATED if row.reading == reading and row.applies(text, facts)), None
    )


# --- the comparison ------------------------------------------------------------------------------

#: One reading's differences from the harness, by the name of the row that explains each (`None`
#: for none): the text, what the reading left, and what the harness left.
Sorted = dict[str | None, list[tuple[str, str, str]]]


def _sort(reading: str, differ: list[tuple[str, str, str]], apart: list[bool]) -> Sorted:
    groups: Sorted = {}
    rules = harness_rules([text for text, _, _ in differ])
    for (text, got, ref), whole, by in zip(differ, apart, rules, strict=True):
        row = explained(reading, text, Facts(taken_apart=whole, harness_rules=by))
        groups.setdefault(row and row.name, []).append((text, got, ref))
    return groups


def differences(vector: str, lines: list[str]) -> tuple[Sorted, Sorted]:
    """What the box and the print site leave of `lines` that the harness takes, each sorted by
    the row of `STATED` that explains it."""
    harness, harness_credentials = harness_readings(lines)
    box: Sorted = {}
    for source in scrub_sources():
        differ = [
            (text, got, ref)
            for text, got, ref in zip(
                lines, box_readings(vector, source, lines), harness, strict=True
            )
            if leaks(got) and not leaks(ref)
        ]
        apart = taken_apart(vector, source, [text for text, _, _ in differ])
        for name, rows in _sort("box", differ, apart).items():
            box.setdefault(name, []).extend(rows)
    differ = [
        (text, got, ref)
        for text, ref in zip(lines, harness_credentials, strict=True)
        if leaks(got := redact_credentials(text)) and not leaks(ref)
    ]
    print_site = _sort("print", differ, [False] * len(differ))
    return box, print_site


def regressions(
    vector: str, before: dict[str, str], after: dict[str, str], lines: list[str]
) -> list[tuple[str, str, str, str]]:
    """Every line whose secret a host's `ai_scrub` in `before` took and the same host's in
    `after` leaves: the host, the text, what the old one left and what the new one left. Each host
    is held to its own old config; one new to `after` is held to nothing. Nothing is read twice:
    a config is run once whatever hosts share it, and a host whose bytes did not move is skipped."""
    readings: dict[str, list[str]] = {}

    def read(source: str) -> list[str]:
        if source not in readings:
            readings[source] = box_readings(vector, source, lines)
        return readings[source]

    found = []
    for host, new in after.items():
        old = before.get(host)
        if old is None or old == new:
            continue
        for text, was, now in zip(lines, read(old), read(new), strict=True):
            if not leaks(was) and leaks(now):
                found.append((host, text, was, now))
    return found


def by_order(
    vector: str,
    before: dict[str, str],
    found: list[tuple[str, str, str, str]],
    workdir: Path | None = None,
) -> tuple[list[tuple[str, str, str, str]], list[tuple[str, str, str, str]]]:
    """`found` (from `regressions`) split into the real and the one row the bar excuses (decided
    on #516, 2026-11-19): a line whose secret the host's previous `ai_scrub` took only by the
    order of its rules, its rules each reading the text as it came leaving it
    (`scrub_compare.taken_apart_sequential`). A union reads no rule into such a secret. Against
    a previous config that carries the fleet's scrub block nothing is excused: its rules' union
    is its own output."""
    real, excused = [], []
    for host in sorted({row[0] for row in found}):
        rows = [row for row in found if row[0] == host]
        if scrub_compare.BLOCK_START in before[host]:
            real += rows
            continue
        taken = scrub_compare.taken_apart_sequential(
            vector, before[host], [(row[1], SECRET) for row in rows], workdir
        )
        for row, whole in zip(rows, taken, strict=True):
            (real if whole else excused).append(row)
    return real, excused


def print_site_at(ref: str) -> str:
    """The print site's source (`logfmt.py`, standard library only) at `ref`."""
    return subprocess.run(
        ["git", "show", f"{ref}:src/basecradle_router/logfmt.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _redactor(source: str) -> Callable[[str], str]:
    module = types.ModuleType("logfmt_before")
    exec(compile(source, "<an earlier logfmt.py>", "exec"), module.__dict__)  # noqa: S102
    return module.redact_credentials


def print_regressions(before: str, lines: list[str]) -> Sorted:
    """Every line whose secret the print site in `before` (a `logfmt.py` source) took and the
    tree's leaves, sorted by the `print-history` row that states it (`None` for none). Nothing
    to read when the two are the same bytes."""
    if before == (REPO / "src" / "basecradle_router" / "logfmt.py").read_text():
        return {}
    old = _redactor(before)
    found: Sorted = {}
    for text in lines:
        was, now = old(text), redact_credentials(text)
        if not leaks(was) and leaks(now):
            row = explained("print-history", text, Facts())
            found.setdefault(row and row.name, []).append((text, was, now))
    return found


def base_ref() -> str:
    """The commit whose `ai_scrub` the tree's is held to: `SCRUB_DIFFERENTIAL_BASE`, or the first
    parent of what is checked out. In CI that is the previous commit on `main`, and on a pull
    request the base branch's tip (the merge commit's first parent)."""
    return os.environ.get("SCRUB_DIFFERENTIAL_BASE") or "HEAD^"


def _vector_or_exit() -> str:
    """The boxes' Vector, as `tests/test_vector_unit.py` requires it, or a refusal: a run on any
    other release reads a scrub no box runs."""
    binary = os.environ.get("VECTOR_BIN") or shutil.which("vector")
    if binary is None:
        raise SystemExit("set VECTOR_BIN to the boxes' Vector release")
    got = subprocess.run([binary, "--version"], capture_output=True, text=True, check=False)
    pinned = tvu._pinned_version()
    if not got.stdout.startswith(f"vector {pinned} "):
        raise SystemExit(f"{got.stdout.strip()!r} is not the boxes' Vector {pinned}")
    return binary


def _show(title: str, rows: list[tuple[str, str, str]], limit: int) -> None:
    rows = sorted(rows, key=lambda row: len(row[0]))
    print(f"  {title}: {len(rows)}")
    for text, got, ref in rows[:limit]:
        print(f"    in:      {text!r}")
        print(f"    reading: {got!r}")
        print(f"    harness: {ref!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seed", type=int, action="append", help=f"repeatable; default {SEED}")
    parser.add_argument("--lines", type=int, default=LINES, help="per seed")
    parser.add_argument("--base", help="a commit to hold the tree's ai_scrub to (no regressions)")
    parser.add_argument("--show", type=int, default=8, help="unexplained texts printed per group")
    parser.add_argument("--dump", help="write every difference as JSON to this file")
    args = parser.parse_args()
    vector = _vector_or_exit()
    dump: dict[str, object] = {}
    for seed in args.seed or [SEED]:
        lines = texts(seed, args.lines) + REPRODUCTIONS
        box, print_site = differences(vector, lines)
        print(f"seed {seed}: {len(lines)} texts")
        for reading, groups in (("box", box), ("print site", print_site)):
            print(f" {reading} leaves what the harness takes:")
            for name, rows in sorted(groups.items(), key=lambda kv: (kv[0] is None, str(kv[0]))):
                _show(name or "UNEXPLAINED", rows, args.show if name is None else 0)
        found = []
        if args.base:
            before = host_sources(args.base)
            found, excused = by_order(
                vector, before, regressions(vector, before, host_sources(), lines)
            )
            _show(f"regressions against {args.base}", [row[1:] for row in found], args.show)
            _show(f"excused by order against {args.base}", [row[1:] for row in excused], 0)
            for name, rows in print_regressions(print_site_at(args.base), lines).items():
                _show(f"print site against {args.base}: {name or 'UNEXPLAINED'}", rows, args.show)
        dump[str(seed)] = {
            "box": {str(k): v for k, v in box.items()},
            "print": {str(k): v for k, v in print_site.items()},
            "regressions": found,
        }
    if args.dump:
        Path(args.dump).write_text(json.dumps(dump))


if __name__ == "__main__":
    main()
