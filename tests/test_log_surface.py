"""The deploy-side half of the log surface, pinned against the shipped bytes (#170).

Naming the program, exporting the delivery id across the privilege drop, and the
Vector presentation/scrub are all *config*, not Python — none of it is reachable from
a unit test of the daemon, and all of it is silently droppable by a refactor. The
router-AI never deploys, so a regression here would only be caught on the box, by the
NOC, after the fact. These tests are the offline gate instead: they assert the exact
properties the live behaviour depends on, against the files as they will ship.

No model, agent, or network — this reads three files off disk.
"""

from __future__ import annotations

import re
from pathlib import Path

_DEPLOY = Path(__file__).resolve().parents[1] / "deploy"
UNIT = _DEPLOY / "systemd" / "basecradle-router.service"
WAKE_RUNNER = _DEPLOY / "bin" / "wake-runner"
# The reference host's copy. Every other host's copy differs from it only by its sink
# (pinned in test_per_host_deploy.py), so the scrub proven here is the scrub on every box.
VECTOR = _DEPLOY / "hosts" / "ai.basecradle.com" / "vector.yaml"


# --- 1. name the program ---------------------------------------------------


def test_the_daemon_names_itself_in_the_journal() -> None:
    # The daemon logs to stdout under `uv run uvicorn`, so with no SyslogIdentifier
    # journald stamps every line SYSLOG_IDENTIFIER=uv — the router's own log was
    # attributed to its *launcher*, and no `journalctl -t` could address it. This is
    # also what vector.yaml prefixes each shipped line with, so losing it makes every
    # Live Tail line from this daemon read `[uv] …`.
    assert "SyslogIdentifier=basecradle-router" in UNIT.read_text()


# --- 2. the delivery id crosses the privilege drop -------------------------


def test_wake_runner_accepts_the_delivery_flag() -> None:
    script = WAKE_RUNNER.read_text()
    assert "--delivery)" in script, "wake-runner must parse --delivery <id>"
    assert 'delivery="$2"' in script


def test_wake_runner_exports_the_delivery_id_to_the_agent() -> None:
    # The router passes NO environment across the sudo boundary (that is the whole
    # security posture), so the only way the delivery id reaches the child is the
    # wrapper exporting it after the privilege drop. Without this the harness cannot
    # stamp its own lines with the id, and the two journals stay unjoinable.
    assert 'export BASECRADLE_DELIVERY_ID="$1"' in WAKE_RUNNER.read_text()


def test_the_delivery_id_is_exported_after_the_agent_env_loads() -> None:
    # Order is load-bearing: the router's value must win over a stale one left in the
    # agent's own agent.env, or a wake would report the id of some previous wake.
    script = WAKE_RUNNER.read_text()
    assert script.index('load_agent_env "$env_file"') < script.index(
        'export BASECRADLE_DELIVERY_ID="$1"'
    ), "BASECRADLE_DELIVERY_ID must be exported AFTER agent.env loads, so it cannot be overridden"


def test_wake_runner_shape_checks_the_delivery_id() -> None:
    # It crosses a sudo boundary as an argv element and lands in the journal. It is
    # never eval'd (the agent's bash assigns it literally), so this is defence in
    # depth — but the wrapper validates every input it is handed, without exception.
    assert re.search(r"\$delivery =~ \^\[A-Za-z0-9_\.:-\]\{1,128\}\$", WAKE_RUNNER.read_text()), (
        "wake-runner must pin --delivery to an inert charset"
    )


def test_the_delivery_id_is_passed_as_an_inert_positional_never_interpolated() -> None:
    # Same discipline as the trigger: root must never interpolate a caller-supplied
    # value into the shell it runs as the agent. It rides as a positional argument to
    # the single-quoted inner script, where it is only ever assigned, never evaluated.
    # The privilege drop is assembled once into `drop` and launched two ways (direct for
    # a probe, journald-wrapped for a real wake — basecradle-router#208), so this pins
    # the one assembly rather than each launch.
    assert (
        'drop=(runuser -u "$user" -- /bin/bash -c "$AGENT_SCRIPT" '
        'wake-runner "$real_cwd" "$delivery" "$launch_bin" "$@")'
    ) in WAKE_RUNNER.read_text()


# --- 3. Vector presentation + scrub ----------------------------------------


def _component(component_id: str) -> str:
    """The YAML body of one vector.yaml component, bounded by the next key at its own indent.

    Bounding a component's body with a *named sibling* is a trap this file already fell into:
    `_host_metrics_source` used to slice to `transforms:`, and inserting `ai_internal_metrics`
    ahead of it silently widened the slice so a sibling component could satisfy an assertion
    meant for this one. Naming the new sibling instead would only move the bug one component
    along, and a slice whose end lands *before* its start collapses to `""` — where every
    `not in` assertion passes vacuously and the test dies silently. The next same-indent key
    is the real boundary, whatever it happens to be called.
    """
    text = VECTOR.read_text()
    key = re.search(rf"^(?P<indent> *){re.escape(component_id)}:[ \t]*$", text, re.MULTILINE)
    assert key, f"vector.yaml defines no component {component_id!r}"
    body = text[key.end() :]
    end = re.search(rf"^ {{0,{len(key.group('indent'))}}}\S", body, re.MULTILINE)
    return body[: end.start()] if end else body


def _scrub_source() -> str:
    """The body of the `ai_scrub` remap — the transform every shipped log passes."""
    return _component("ai_scrub")


def _statement_indent(source: str, statement: str) -> int:
    """The leading-whitespace width of the line `statement` starts, asserting it is there.

    Used to tell a top-level VRL statement from one nested inside a conditional, without
    hard-coding the block's own indentation (which is a YAML detail, not a security one).
    """
    match = re.search(rf"^(?P<indent> *){re.escape(statement)}", source, re.MULTILINE)
    assert match, f"ai_scrub has no statement {statement!r}"
    return len(match.group("indent"))


def test_every_shipped_line_is_prefixed_with_the_emitting_programs_identifier() -> None:
    # The presentation half: a Live Tail line reads `[basecradle-router] …` or
    # `[basecradle-wake-nova] …`, so a human can see which program on the box emitted it.
    # Done in Vector, once, so it covers EVERY program (sshd, Caddy, a wake) — never
    # hand-prefixed into the daemon's own log strings, which would cover only the daemon.
    source = _scrub_source()
    assert '.message = "[" + ident + "] " + msg' in source
    assert "ident = to_string(.SYSLOG_IDENTIFIER)" in source
    # A line with no identifier (a kernel message) must still get a prefix, never `[] `.
    assert 'ident = "unknown"' in source


def test_the_prefix_is_applied_after_redaction_not_before() -> None:
    # The prefix is not secret-bearing; redaction must run on the raw message and the
    # prefix must never be scanned or mangled by a later pattern.
    source = _scrub_source()
    assert source.index("[REDACTED_API_KEY]") < source.index('.message = "[" + ident + "] " + msg')


# What the redaction does to a line is proven by running it: `tests/vector/ai_scrub.yaml`, through
# `vector test` (tests/test_vector_unit.py). Python's `re` used to stand in for VRL here, and it can
# no longer: the word rule is a closure and reads `\p{…}` classes `re` does not have (#398). What
# stays here is what holds offline, with no Vector on the machine: which rules exist, where they
# sit, and in what order.

#: The statement that opens the prefix pass, the one `replace_with` in `ai_scrub`.
_PREFIX_PASS = "msg = replace_with(msg, r'(?x)"

#: The widest run class, the one a rule's run takes when it already takes a glued key whole.
_WIDE = "[A-Za-z0-9_-]"

#: The prefix pass, rule for rule, in pattern order: the prefix as written, the name its run is
#: captured under, the run's class and its minimum. These are the box's rules and minimums, a
#: subset of the harness's `_PREFIXED` (`sk-` and `xai-` are 16 here, where the harness's are 8).
#: Every run's class was diffed by hand against the harness's `_PREFIXED` at v0.187.0 and is the
#: harness's: a run narrower than the source's is a key the box ships whole while the harness
#: catches it, as `xai-`'s alphanumeric run shipped an xAI management key (basecradle-router#425).
#: Nothing here can read the harness, so a class the harness widens later is a hand diff again.
#: Spelled once, so the pattern pinned below is built from it and never retyped.
_PREFIX_RULES = (
    ("github_pat_", "pat", "[A-Za-z0-9_]", 20),
    ("gh[a-z]_", "gh", "[A-Za-z0-9]", 20),
    ("bc_(?P<bc_kind>[a-z]{3})_", "bc", _WIDE, 8),
    ("bc_uat_", "bc_uat", "[A-Za-z0-9]", 1),
    ("bc_isk_", "bc_isk", "[A-Za-z0-9]", 1),
    ("sk-", "sk", _WIDE, 16),
    ("xai-", "xai", _WIDE, 16),
    ("AIza", "aiza", _WIDE, 20),
    ("hf_", "hf", "[A-Za-z0-9]", 20),
    ("r8_", "r8", "[A-Za-z0-9]", 20),
    ("ste-", "ste", "[A-Za-z0-9]", 20),
    ("abl_", "abl", "[A-Za-z0-9]", 20),
    ("pplx-", "pplx", "[A-Za-z0-9]", 20),
    ("tk_", "tk", _WIDE, 20),
)

#: Every rule's whole key at exactly its own minimum, the harness's `_GLUED_KEY` built from the
#: box's rules (basecradle-router#423).
_GLUED_KEY = (
    "(?:"
    + "|".join(
        f"{prefix.replace('(?P<bc_kind>[a-z]{3})', '[a-z]{3}')}{run}{{{least}}}"
        for prefix, _, run, least in _PREFIX_RULES
    )
    + ")"
)


def _at_least(least: int, *, lazy: bool = False) -> str:
    return ("+" if least == 1 else f"{{{least},}}") + ("?" if lazy else "")


def _rule_pattern(prefix: str, name: str, run: str, least: int) -> str:
    """One rule as the pass must spell it: a wide run as is, a narrow one ending where a glued key
    begins — the harness's `_rule`, with the glued alternative named per rule (`<name>_glued`),
    because the regex allows no name twice."""
    if run == _WIDE:
        return f"{prefix}(?P<{name}>{run}{_at_least(least)})"
    glued = f"(?P<{name}_glued>{run}{_at_least(least, lazy=True)}{_GLUED_KEY}{_WIDE}*)"
    return f"{prefix}(?:{glued}|(?P<{name}>{run}{_at_least(least)}))"


def _prefix_pattern() -> str:
    """The shipped prefix pattern with the free-spacing whitespace taken out, which `(?x)` ignores
    (no class in it holds a space)."""
    source = _scrub_source()
    start = source.index(_PREFIX_PASS) + len(_PREFIX_PASS)
    return re.sub(r"\s+", "", source[start : source.index("') -> |key| {", start)])


def _has_rule(prefix: str) -> bool:
    (rule,) = [rule for rule in _PREFIX_RULES if rule[0] == prefix]
    return _rule_pattern(*rule) in _prefix_pattern()


def test_the_prefix_pass_is_the_rules_table_with_the_glued_key_in_every_narrow_rule() -> None:
    # A narrow run never swallows the head of a key glued after it (basecradle-router#423, the
    # harness's fix for basecradle-harness#858). The fix repeats the whole-key alternation once per
    # narrow rule, because VRL composes no pattern from parts: nine copies of one long string, where
    # one copy at a different minimum, or missing a rule, would leave that rule's glued key to
    # ship. So the pattern is pinned whole, built from the one table above.
    expected = r"(?P<before>^|[\s\S])(?:" + "|".join(_rule_pattern(*r) for r in _PREFIX_RULES) + ")"
    assert _prefix_pattern() == expected
    narrow = [name for _, name, run, _ in _PREFIX_RULES if run != _WIDE]
    assert narrow == ["pat", "gh", "bc_uat", "bc_isk", "hf", "r8", "ste", "abl", "pplx"]
    assert _prefix_pattern().count(_GLUED_KEY) == len(narrow)


def test_provider_api_keys_are_redacted() -> None:
    # Harness and builder agents run with provider keys in their env, and a wake's
    # stdout+stderr now flows into journald (#168) and therefore through here. Nothing
    # is known to print one; this is the belt for the braces.
    for prefix in ("sk-", "xai-", "AIza", "hf_", "r8_"):  # `sk-`: openai / anthropic / openrouter
        assert _has_rule(prefix), prefix


def test_the_harness_0_185_0_key_shapes_are_redacted() -> None:
    # refuseless, Perplexity and ntfy (basecradle-router#421): the harness's `_PREFIXED` rows
    # `abl_`, `pplx-` and `tk_`, at the harness's own minimums. Before harness 0.186.0 scrubbed a
    # prose line at its source (basecradle-harness#857), this pass was the only layer that redacted
    # one there, and it still is for any program that does not. `tk_`'s run takes `_` and `-`,
    # because ntfy's validator admits them.
    for prefix in ("abl_", "pplx-", "tk_"):
        assert _has_rule(prefix), prefix


def test_every_prefix_rule_hands_its_run_to_the_word_rule() -> None:
    # The word rule reads a match's run, captured apart from its prefix, out of the one `run`
    # chain. A rule added to the pattern but not to the chain fails safe (an empty run is never
    # kept), yet over-redacts every word that holds its prefix, so it is no longer the harness's
    # verdict. Every run capture, in pattern order, must be in the chain, and nothing else:
    # `before` is the neighbour, `bc_kind` names a label, and a `_glued` capture is a match that
    # took a second key, which is read by the `glued` chain instead.
    source = _scrub_source()
    start = source.index(_PREFIX_PASS)
    end = source.index(") -> |key| {", start)
    captures = re.findall(r"\(\?P<(\w+)>", source[start:end])
    chain = re.search(r"^ *run = (?P<chain>.+)$", source[end:], re.MULTILINE)
    assert chain, "the prefix pass has no `run` chain"
    runs = re.findall(r"string\(key\.(\w+)\)", chain.group("chain"))
    not_runs = ("before", "bc_kind")
    assert runs == [n for n in captures if n not in not_runs and not n.endswith("_glued")]
    assert chain.group("chain").endswith('?? ""'), "an unmatched run must come back empty"


def test_a_match_that_took_a_glued_key_is_never_a_word_and_keeps_its_first_label() -> None:
    # The harness's `_is_word` (basecradle-harness#858): a glued tail can hold a later key that was
    # no part of any word, and read as one all-lowercase run it would be kept. Every `_glued`
    # capture is in the `glued` chain, the word test refuses any match whose chain is not empty,
    # and each glued capture takes the label of the rule it belongs to.
    source = _scrub_source()
    start = source.index(_PREFIX_PASS)
    end = source.index(") -> |key| {", start)
    glued = [name for name in re.findall(r"\(\?P<(\w+)>", source[start:end]) if "_glued" in name]
    chain = re.search(r"^ *glued = (?P<chain>.+)$", source[end:], re.MULTILINE)
    assert chain, "the prefix pass has no `glued` chain"
    assert re.findall(r"string\(key\.(\w+)\)", chain.group("chain")) == glued
    assert chain.group("chain").endswith('?? ""')
    assert re.search(r'^ *if glued == "" && run != "" && match\(before,', source[end:], re.M)
    # A rule whose plain capture names its own label carries its glued capture beside it; every
    # other rule's plain and glued captures alike fall to the default `[REDACTED_API_KEY]`.
    for name in glued:
        plain = name.removesuffix("_glued")
        if f"key.{plain} != null" in source:
            assert f"key.{plain} != null || key.{name} != null" in source, name
        else:
            assert f"key.{name} != null" not in source, name
    labelled = [n for n in glued if f"key.{n} != null" in source]
    assert labelled == ["pat_glued", "gh_glued", "bc_uat_glued", "bc_isk_glued"]


# One `msg = replace(msg, r'…', "…")` statement, the shape every fixed rule carries (the two
# closures, the heartbeat rule and the prefix pass, are pinned on their own): a raw
# string with no single quote inside it (one would close it early), and a fixed replacement
# with no dollar sign (a capture-group back-reference needs one, and Vector interpolates every
# dollar sign in the file before parsing it — basecradle-router#305).
_REPLACE = re.compile(
    r"""^ *msg = replace\(msg, r'(?P<pattern>[^']*)', "(?P<replacement>[^"$]*)"\)$"""
)


_HEARTBEAT_PASS = (
    r"msg = replace_with(msg, r'(?P<url>(?:https?://\S*)?/heartbeat/\S+)"
    r"(?P<after>[ \t]+(?:\x1b\[[0-9;]*m)?[A-Za-z_][A-Za-z0-9_]*=|[ \t]*(?:\n|\z))?') -> |hb| {"
)

# The heartbeat rule's whole closure (basecradle-router#402, #423), pinned offline because the
# `vector test` suite that asserts its bytes runs only on the boxes' Vector release. It returns
# fixed strings and nothing of the URL but the one captured bearer word and a kv value's closing
# quote (with the colour reset closing a coloured pair), so no part of the URL can ship; `after` is
# put back as matched, spaces, a colour code and the next field's name, and every later rule
# still reads it.
_HEARTBEAT_CLOSURE = [
    _HEARTBEAT_PASS,
    r'url = string(hb.url) ?? ""',
    r"""closing = parse_regex(url, r'\A(?P<body>[\s\S]*?)(?P<quote>"\x1b\[0m|")\z') ?? {}""",
    r'quote = string(closing.quote) ?? ""',
    r'if quote == "\"" && hb.after == null { quote = "" }',
    r'if quote != "" { url = string(closing.body) ?? url }',
    r"kept = parse_regex(url, r'(?i)(?:^|[^A-Za-z0-9])(?P<word>bearer)\z') ?? {}",
    r'"[REDACTED_HEARTBEAT_URL]" + (string(kept.word) ?? "") + quote + (string(hb.after) ?? "")',
    "}",
]

# The `x-api-key` header rule (basecradle-router#423, basecradle-harness#861) and the assignment
# rule whose value may open with a quote and takes a glued bearer (basecradle-harness#866), as the
# harness spells them; a single quote is `\x27`, which a raw string cannot hold.
_X_API_KEY = (
    r"""(?i:x[-_]api[-_]?key)(?:\\?["\x27]\s*(?:=>|[:=,])|\s*(?::|=>))\s*(?:b?\\?["\x27])?"""
    r"""(?:(?i:bearer)\s+)*(?:[^\s"\x27&\\]*?(?i:bearer)\s+[A-Za-z0-9._~+/-]{8,}|[^\s"\x27&\\]+)"""
)
_API_KEY = (
    r"""(?i)api[-_]?key="?(?:bearer\s+)*"""
    r"""(?:[^&\s"]*?bearer\s+[A-Za-z0-9._~+/\-]{8,}|[^&\s"]+)"""
)


def _statements(source: str) -> list[str]:
    """Every non-comment line of `source` that mentions `replace`, in file order."""
    return [
        line.strip()
        for line in source.splitlines()
        if "replace" in line and not line.lstrip().startswith("#")
    ]


def test_the_scrub_is_literal_rules_then_one_prefix_pass() -> None:
    # The order is the fix (basecradle-router#398): a prefix rule's run is made of the characters
    # a literal rule's key name is, so a prefix rule that runs first swallows `api-key` or
    # `bearer` and leaves the secret after it for no rule to match. Every literal rule is a fixed
    # `replace` in the one safe shape, and the prefix rules are ONE `replace_with` after all of
    # them, never a rule per pass, so a word kept by one prefix rule is never cut by another.
    statements = _statements(_scrub_source())
    prefix_passes = [i for i, line in enumerate(statements) if line.startswith(_PREFIX_PASS)]
    assert len(prefix_passes) == 1, f"ai_scrub must carry one prefix pass: {statements!r}"
    literal = statements[: prefix_passes[0]]
    assert statements[prefix_passes[0] + 1 :] == [], "no rule may run after the prefix pass"
    # Nor anything else that rewrites `msg`: the prefix pass is the last assignment to it, so
    # nothing after it can undo or bypass the order, whatever function it calls.
    assignments = [
        line.strip() for line in _scrub_source().splitlines() if re.match(r"^ *msg *=", line)
    ]
    assert assignments[-1].startswith(_PREFIX_PASS), assignments
    # The heartbeat URL, then the `x-api-key` header, then the assignment, then bearer: the bearer
    # rule's token class takes a URL's `https` and an assignment's `api_key=`. After it, a
    # heartbeat URL would keep its userinfo out of the redaction, and an assignment would ship its
    # secret. The assignment takes every bearer scheme in its value, so `AUTH_API_KEY=Bearer eyJ…`
    # is redacted whole.
    #
    # The heartbeat rule is the one closure in the pass (basecradle-router#402, #423): it puts back
    # a bearer word ending its run, so the bearer rule still takes the word and its token, and a kv
    # value's closing quote with what follows it. Its whole closure is pinned here; what it returns
    # is pinned by the `vector test` suite, which asserts the shipped bytes. The other three are
    # fixed `replace`s in the one safe shape.
    assert literal[0] == _HEARTBEAT_PASS, f"pass 1 must open with the heartbeat rule: {literal!r}"
    lines = [line.strip() for line in _scrub_source().splitlines()]
    start = lines.index(_HEARTBEAT_PASS)
    assert lines[start : start + len(_HEARTBEAT_CLOSURE)] == _HEARTBEAT_CLOSURE
    for line in literal[1:]:
        assert _REPLACE.match(line), f"not a closed raw string + fixed replacement: {line!r}"
    # The `x-api-key` rule goes before the assignment rule, so the header's name goes with its
    # value: the assignment rule reads `x-api-key=>V` as `key=` and takes it under its own label.
    patterns = [_REPLACE.match(line).group("pattern") for line in literal[1:]]
    assert patterns == [
        _X_API_KEY,
        _API_KEY,
        r"(?i)bearer\s+(?:bearer\s+)*[A-Za-z0-9._~+/\-]+=*",
    ]


def test_steel_api_keys_and_the_api_key_query_shape_are_redacted() -> None:
    # Steel (steel.dev), the browser backend behind Playwright MCP, takes its key two ways:
    # the `steel-api-key` header, and `apiKey=` on the wss://connect.steel.dev URL
    # (basecradle-noc#797). The `ste-` rule catches the key wherever it lands; the query
    # rule catches `apiKey=` whatever its value, so a future key format in a URL is covered.
    source = _scrub_source()
    assert _has_rule("ste-")
    assert _API_KEY in source
    top_level = _statement_indent(source, 'if .SYSLOG_IDENTIFIER == "sudo"')
    for rule in (
        _PREFIX_PASS,
        "msg = replace(msg, r'(?i:x[-_]api[-_]?key)",
        "msg = replace(msg, r'(?i)api[-_]?key=",
    ):
        assert _statement_indent(source, rule) == top_level, f"{rule!r} must be unconditional"


# The only dollar signs in a host's vector.yaml are its sink tokens: pinned for every host,
# owner destinations included, by tests/test_per_host_deploy.py.


def test_the_process_command_line_is_never_shipped() -> None:
    # journald attaches the emitting process's full argv as the `_CMDLINE` metadata FIELD,
    # and Vector ships the whole journal record — so every redaction above, which operates
    # on `msg`, is blind to it. That gap put all seven harness agents' credentials into the
    # Better Stack store: the NOC's fleet-deploy-runner hands each agent's whole agent.env
    # to `runuser -u <agent> -- env -i KEY=VALUE …` on argv (basecradle-noc#443).
    #
    # The router's own launch paths are clean — wake-runner passes NO environment across the
    # sudo boundary — but this box's scrub guards every program on it, not only ours.
    source = _scrub_source()
    assert 'del(."_CMDLINE")' in source, (
        "ai_scrub must delete the `_CMDLINE` journald field before the logs sink — "
        "any secret on any program's argv ships in it, unredacted (basecradle-noc#443)"
    )


def test_the_command_line_drop_is_unconditional_not_an_identifier_allow_list() -> None:
    # The pre-existing `sudo` rule is an identifier allow-list, and `runuser` is not `sudo` —
    # which is precisely how the leak above got past it. Guarding the field holds for programs
    # this repo has never heard of; guarding a list of names holds only until the next one.
    # Pinned because the tempting "fix" is to narrow this to the identifier we caught, which
    # would restore the exact hole. The router-AI never deploys, so a regression here is only
    # visible on the box, in the log store, after the secrets are already in it.
    #
    # Read structurally, off indentation: a VRL statement nested in a conditional is indented
    # deeper than the block's own statements. Compare against the `sudo` drop, which is known
    # to sit at the top level, so this keeps holding if the whole block is ever reindented.
    source = _scrub_source()
    top_level = _statement_indent(source, 'if .SYSLOG_IDENTIFIER == "sudo"')
    assert _statement_indent(source, 'del(."_CMDLINE")') == top_level, (
        "the `_CMDLINE` deletion must not sit inside a conditional — it applies to every "
        "event, whatever program emitted it (an identifier allow-list is what leaked)"
    )


def test_basecradle_integration_signing_keys_are_redacted() -> None:
    # THIS box is where `bc_isk_…` values live: the router verifies each platform
    # delivery's HMAC with the recipient agent's own Integration Signing Key, so all of
    # them sit in router.env and are read into the daemon's environment
    # (basecradle/basecradle#497). The box that holds the secrets is the one that must carry
    # the rule: the harness's wide `bc_???_` rule takes a key of eight or more, and the short
    # `bc_isk_` rule takes a shorter one, under its own label.
    assert _has_rule("bc_isk_")
    # The harness's wide rule, ahead of the two short ones, so a glued second key is taken whole.
    pattern = _prefix_pattern()
    assert _has_rule("bc_(?P<bc_kind>[a-z]{3})_") and _has_rule("bc_uat_")
    wide = _rule_pattern(*_PREFIX_RULES[2])
    assert pattern.index(wide) < pattern.index("bc_uat_(?:(?P<bc_uat_glued>")


def test_the_integration_signing_key_redaction_is_unconditional() -> None:
    # Same rule as the `_CMDLINE` deletion: a redaction nested inside a conditional
    # holds only for the programs that conditional happens to name. Read structurally,
    # off indentation, against the `sudo` drop which is known to sit at the top level.
    source = _scrub_source()
    top_level = _statement_indent(source, 'if .SYSLOG_IDENTIFIER == "sudo"')
    assert _statement_indent(source, _PREFIX_PASS) == top_level


def test_the_pre_existing_scrub_rules_survive() -> None:
    # The redaction set only ever grows: #33/#338 added these after a live journald
    # secret leak, and a refactor of this transform must not quietly drop one.
    source = _scrub_source()
    for pattern in ("REDACTED_GH_TOKEN", "REDACTED_HEARTBEAT_URL", "REDACTED_BC_TOKEN"):
        assert pattern in source
    # GitHub fine-grained personal access tokens (#398), a rule of the prefix pass.
    assert _has_rule("github_pat_")
    assert 'SYSLOG_IDENTIFIER == "sudo"' in source  # the whole-event drop for argv leaks


# --- 4. host_metrics does not scrape what it cannot read -------------------


def _host_metrics_source() -> str:
    """The body of the `ai_host_metrics` source — the box's metrics collector."""
    return _component("ai_host_metrics")


def test_host_metrics_excludes_the_pseudo_filesystems_it_cannot_read() -> None:
    # The `filesystem` collector stats every mount in /proc/mounts, and the unprivileged
    # `vector` user cannot reach /sys/kernel/debug/tracing (tracefs, under a 0700 debugfs):
    # every 30s scrape logged a `statvfs` permission-denied ERROR — ~2,880/day into the
    # crown-jewels box's journal, which is exactly how a human learns to skim past ERROR
    # (basecradle#414). Vector checks these excludes BEFORE the statvfs, so the syscall is
    # never attempted. Dropping them re-arms the error carpet, and the router-AI never
    # deploys — nobody would notice until the NOC read the box.
    source = _host_metrics_source()
    for fs in ("tracefs", "debugfs"):
        assert re.search(rf"""excludes:.*["']{fs}["']""", source), (
            f"host_metrics must exclude the unreadable pseudo-filesystem {fs!r}"
        )


def test_host_metrics_still_collects_real_filesystems() -> None:
    # The other way to silence the ERROR was to drop the `filesystem` collector entirely —
    # rejected, because disk-usage metrics are the point (a full disk is how this box dies).
    # The fix must stay surgical: exclude the pseudo-filesystems, keep the collector. An
    # excludes-only list includes everything else, so every real filesystem is still scraped.
    source = _host_metrics_source()
    assert "filesystem," in source or "filesystem]" in source, (
        "the `filesystem` collector must remain enabled — the fix is to exclude "
        "unreadable pseudo-filesystems, not to stop collecting disk usage"
    )


# --- 5. Vector's own health is visible to the NOC (basecradle#419) ----------
#
# Vector is the transport that ships this box's entire log stream, and it is the one
# component nothing watches: its own logs are deliberately DROPPED from the stream
# (they echo the sink token — basecradle#338), and `introspect-vector` reads only
# "installed and running", which a running-but-BROKEN Vector satisfies perfectly. A VRL
# aborting 100% of events logs nothing, passes `vector validate`, and shows `active`
# while the sink receives zero. These counters are the only thing that sees that.


def test_the_health_component_ids_are_a_contract_with_the_noc() -> None:
    # NOT cosmetic, and the reason `_component` anchors on the YAML key rather than searching
    # the file for the bare string: the NOC's wrapper carries a reviewed allow-list and will
    # never name an id that is not on it — because on a Better Stack *generated* config the
    # component id IS the ingest token (that is how it leaked ~40x). Renaming either id here
    # does not fail loudly; it surfaces on the box as an `unknown_components` finding and
    # needs a change in a repo we do not own to clear. A comment that merely mentions the id
    # must not be able to satisfy this.
    for component_id in ("ai_internal_metrics", "ai_vector_health_exporter"):
        assert _component(component_id), (
            f"{component_id!r} is allow-listed by the NOC's introspect-vector-health op — "
            "renaming it silently strands the guard (basecradle-noc#215)"
        )


def test_vector_exports_its_own_health_counters() -> None:
    # The source of truth for received/sent/discarded. Without it the exporter below has
    # nothing to serve and the NOC's guard reads `endpoint_reachable: false` forever.
    assert re.search(r"""type:\s*["']internal_metrics["']""", _component("ai_internal_metrics")), (
        "`ai_internal_metrics` must be an `internal_metrics` source"
    )


def test_the_health_exporter_is_bound_to_localhost_only() -> None:
    # This is not a network service. The only reader is a root-owned wrapper op on this same
    # machine, pulling out-of-band. Binding 0.0.0.0 would publish the box's internals to the
    # internet — the box holds the fleet's crown jewels.
    exporter = _component("ai_vector_health_exporter")
    assert re.search(r"""address:\s*["']127\.0\.0\.1:9598["']""", exporter), (
        "the health exporter must bind 127.0.0.1 — never 0.0.0.0"
    )


def test_the_health_counters_are_pulled_out_of_band_not_shipped() -> None:
    # A Vector whose SINK is broken cannot ship the metric that says its sink is broken — a
    # self-report through the failing channel is not a monitor. So the counters are exposed
    # on localhost and PULLED by the NOC over a channel independent of the pipe under test.
    # Routing them into a Better Stack sink would make the guard blind in the exact case it
    # exists to catch.
    exporter = _component("ai_vector_health_exporter")
    assert re.search(r"""inputs:\s*\[["']ai_internal_metrics["']\]""", exporter), (
        "the exporter must serve the `ai_internal_metrics` counters"
    )
    for sink in ("better_stack_logs", "better_stack_metrics"):
        assert "ai_internal_metrics" not in _component(sink), (
            f"the health counters must not be shipped through {sink} — a broken transport "
            "cannot report its own breakage through itself (basecradle-noc#215)"
        )


def test_the_health_counters_never_enter_the_log_scrub() -> None:
    # `ai_scrub` is log-shaped: it rewrites `.timestamp` -> `.dt` and reads log-only fields.
    # These are METRIC events; routing them through it would corrupt them. Same reasoning
    # already applies to `ai_host_metrics`, which is likewise kept out of the scrub.
    assert "ai_internal_metrics" not in _scrub_source(), (
        "`ai_internal_metrics` is metric events — the log scrub would corrupt them"
    )


# --- 4. the fleet colours survive the shipping path (#228) ------------------


def test_the_scrub_never_strips_the_ansi_colour_the_journal_lines_carry() -> None:
    # The router paints its verdict tokens (@origin's fleet palette, 2026-08-17) and
    # Better Stack Live Tail renders them — but only because every hop between the two
    # passes the escape bytes through. `ai_scrub` is the one hop this repo owns, and VRL
    # ships a `strip_ansi_escape_codes` function that a future sanitising edit could
    # reach for in good faith. That edit would delete a decided fleet convention with
    # nothing failing: the daemon's own unit tests cannot see this file, and the loss
    # would surface only as colourless lines nobody happens to look at. Redaction is
    # unaffected — every rule here rewrites secret-SHAPED text, and an escape is not one.
    assert "strip_ansi_escape_codes" not in _scrub_source(), (
        "ai_scrub must pass ANSI through untouched — the palette is presentation the "
        "journal carries end to end (basecradle-router#228)"
    )


# --- 6. the Claude Code builders' OpenTelemetry path (#380) ----------------
#
# What the transform renders is proven by running it (tests/test_vector_unit.py). These pin
# the plumbing around it, which `vector test` never exercises: where the listener binds and which
# sink the rendered line reaches.


def test_the_otel_listeners_are_bound_to_localhost_only() -> None:
    # The senders are the builders' own Claude Code processes on this box. Binding 0.0.0.0 would
    # let anyone on the internet write lines into the fleet's log store under any agent's name.
    source = _component("ai_otel")
    assert re.search(r"""type:\s*["']opentelemetry["']""", source)
    addresses = re.findall(r"""address:\s*["']([^"']+)["']""", source)
    assert addresses == ["127.0.0.1:4317", "127.0.0.1:4318"], addresses


def test_only_the_rendered_llm_line_reaches_the_log_sink_and_through_the_scrub() -> None:
    # The raw OTel event carries session, user and account identifiers. It reaches Better Stack
    # only as the line `ai_otel_llm` rebuilds, and that line passes `ai_scrub` like every journald
    # line, so its redaction and prefix come from the one place that applies them.
    assert re.search(r"""inputs:\s*\[["']ai_scrub["']\]""", _component("better_stack_logs"))
    scrub_inputs = (
        r"""inputs:\s*\n\s*- ["']ai_journald["']\s*\n(\s*#.*\n)*\s*- ["']ai_otel_llm["']"""
    )
    assert re.search(scrub_inputs, _scrub_source())
    transform = _component("ai_otel_llm")
    assert re.search(r"""inputs:\s*\n\s*- ["']ai_otel\.logs["']\s*\n""", transform)
    for component_id in ("better_stack_logs", "better_stack_metrics", "ai_scrub"):
        assert "ai_otel." not in _component(component_id), component_id


def test_a_failing_render_drops_the_event_never_forwards_it() -> None:
    # A remap forwards the ORIGINAL event on a runtime error unless told otherwise, and the
    # original is the raw OTel event, identifiers and all. Both drops are load-bearing.
    transform = _component("ai_otel_llm")
    assert re.search(r"^ *drop_on_abort: true$", transform, re.MULTILINE)
    assert re.search(r"^ *drop_on_error: true$", transform, re.MULTILINE)


def test_the_rendered_event_is_rebuilt_from_scratch() -> None:
    # `. = {` replaces the whole event, so no field of the input survives by omission. A version
    # that deleted fields one by one would ship every field Claude Code adds in a later release.
    transform = _component("ai_otel_llm")
    assert re.search(r"^ *\. = \{$", transform, re.MULTILINE)
