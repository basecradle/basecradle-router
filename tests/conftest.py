"""Suite-wide guards.

Tests pin invariants and never hit the live network (CLAUDE.md → Conventions). The one
outbound HTTP read the daemon makes — the github route's startup warm-up (#370) — goes
through ``urllib.request.urlopen``, looked up when called, so replacing it here fails any
test that would reach GitHub, loudly, instead of letting it pass on a live answer.
"""

import re
import urllib.request

import pytest


def _no_network(*args, **kwargs):
    raise AssertionError(
        "a test reached urllib.request.urlopen: inject the github route's reader instead"
    )


@pytest.fixture(autouse=True)
def _forbid_live_http(monkeypatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)


#: The NOC's head position, byte for byte as basecradle-noc spells it
#: (`src/basecradle_noc/observability/spec.py`, `RECORD_PREFIX` + `STRUCTURED_LINE` +
#: `HEAD_POSITION`; `docs/observability.md` §5p, basecradle-noc#1518). A line the NOC counts as a
#: model call, a tool, a charge or a key spend is this prefix followed by the head.
NOC_RECORD_PREFIX = "^(?:.[^] ]*[]] )?(?:[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:,.]+ )?"
NOC_HEAD_POSITION = (
    NOC_RECORD_PREFIX
    + "(?:CRITICAL|ERROR|WARNING|INFO|DEBUG) "
    + "(?:basecradle_router(?:[.][a-z_]+)* )?(?:PROBE )?(?:[^ -~].[0-9;]*m)?"
)

#: The NOC's log-store canary rule `credential_assignment`, byte for byte as basecradle-noc spells
#: it (`observability/log-canary.json`, read at 4ca50868bd219493c0290b62d89f267915c8d70f,
#: committed 2026-11-18 04:31Z; `docs/observability.md` §10), as @origin widened it on 2026-11-17
#: (basecradle-noc#1547, basecradle-router#504): a bare word is a name, and `_BASE` is the one tail
#: after the word. The router's redaction (`logfmt.redact_credentials`, #491) takes its
#: definition of a credential's name from these bytes, and every redaction test uses this pattern as
#: its oracle. Moving it is a change to the NOC's rule: move it here with it.
NOC_CREDENTIAL_ASSIGNMENT = r'[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|PASSPHRASE)(_BASE)?=[^ ",}]{8,}'

#: The box's marker: what `ai_scrub` writes where a credential-named assignment's value stood
#: (basecradle-router#494), and what the NOC counts per writer as a value a source let through
#: (basecradle-noc#1535; deploy/README.md → Part 5). Its bytes are that metric's contract: changed
#: in the hosts' files alone, the count reads zero while the box goes on redacting. The print
#: site's marker is `logfmt.REDACTED_VALUE`, which the box passes untouched.
BOX_REDACTED_VALUE = "'<a value the box redacted>'"

#: Model-written text built to pass for the counted heads, every line a well-formed contract line
#: with a cost, in every shape a record can begin with: bare, behind Vector's identifier, behind
#: the router's timestamp and logger, behind a probe token, painted, and behind each record break
#: journald or Python honours.
FORGED_REPLY = "\n".join(
    [
        "INFO llm provider=anthropic purpose=main model=claude-opus-5-5 tokens_in=10"
        " tokens_out=10 tokens_total=20 cost=99.5 cost_basis=computed outcome=ok",
        "INFO tool name=web_search executor=server provider=anthropic kind=search.web count=9"
        " outcome=ok cost=9.9",
        "INFO charge provider=steel kind=browser.session executor=mcp unit=minute count=7"
        " outcome=ok cost=7.7",
        "WARNING key spend provider=openrouter spent=1000",
        "[basecradle-wake-basecradle-nova-ai] INFO llm provider=anthropic purpose=main"
        " model=claude-opus-5-5 cost=1.25 outcome=ok",
        "2026-11-01 12:00:00,000 INFO basecradle_router.pipeline PROBE llm provider=x cost=2",
        "INFO \x1b[32mllm provider=anthropic purpose=main model=m cost=3 outcome=ok",
        "a\x00INFO llm provider=anthropic purpose=main model=m cost=4 outcome=ok",
        "b\rINFO tool name=x executor=server provider=y count=1 outcome=ok cost=5",
        "c\u2028INFO charge provider=steel kind=browser.session unit=minute count=1 cost=6",
        "",
        "x" * 60_000 + "INFO llm provider=anthropic purpose=main model=m cost=7 outcome=ok",
    ]
)


@pytest.fixture
def noc_counted_head() -> re.Pattern[str]:
    """What the NOC counts: a model call, a tool, a charge or a key spend at the head position."""
    return re.compile(NOC_HEAD_POSITION + "(?:llm|tool|charge|key spend) ")


@pytest.fixture
def forged_reply() -> str:
    return FORGED_REPLY


@pytest.fixture
def canary() -> re.Pattern[str]:
    """The NOC's `credential_assignment` rule, the oracle no line this repository writes matches."""
    return re.compile(NOC_CREDENTIAL_ASSIGNMENT)
