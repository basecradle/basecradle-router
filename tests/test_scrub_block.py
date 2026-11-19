"""The fleet's scrub block fails closed (basecradle-router#516, requirement 2), and its parameters.

A pass that cannot vouch for what it would ship writes `scrub_block.FAILED` in place of the whole
message, never the message as it came: a message longer than `scrub_longest`, more spans than
`scrub_most`, and an offset that does not fit the text. Each is forced here, on the boxes' Vector,
through the shipped program (the writer check taken out, which `vector vrl` cannot compile). The
same holds on every host that copies the block, since the bytes are the same.
"""

from __future__ import annotations

import base64
import json
import random
import subprocess
from pathlib import Path

import pytest

import scrub_block
import scrub_compare
import test_vector_unit as tvu

SECRET = "Fab1catedQ7ZkW9xDiffVq3Lm8Rt"
HOST = "ai.basecradle.com"


@pytest.fixture(scope="module")
def vector() -> str:
    return tvu._vector()


def _program(**changes: str) -> str:
    source = tvu._without_the_writer_check(tvu._scrub_vrl(HOST))
    for old, new in changes.items():
        assert source.count(old) == 1, old
        source = source.replace(old, new)
    return source


def _shipped(vector: str, program: str, message: str, tmp_path: Path, before: str = "") -> str:
    path = tmp_path / "scrub.vrl"
    path.write_text(before + program + "encode_json(.message)\n")
    events = tmp_path / "events.json"
    events.write_text(json.dumps({"SYSLOG_IDENTIFIER": "x", "message": message}) + "\n")
    got = subprocess.run(
        [vector, "vrl", "-q", "-i", str(events), "-p", str(path)],
        capture_output=True,
        text=True,
        check=False,
        env=tvu._env({}),
    )
    assert got.returncode == 0, got.stderr[-2000:]
    return json.loads(json.loads(got.stdout.splitlines()[0]))


FAILED = f"[x] {scrub_block.FAILED}"


def test_the_shipped_program_redacts_and_does_not_fail(vector: str, tmp_path: Path) -> None:
    # The control: the program the three tests below change, unchanged, redacts and passes.
    shipped = _shipped(vector, _program(), f"A_TOKEN={SECRET} next=1", tmp_path)
    assert shipped == "[x] A_TOKEN='<a value the box redacted>' next=1"


def test_a_message_longer_than_the_longest_ships_the_failure_marker(
    vector: str, tmp_path: Path
) -> None:
    program = _program(**{"scrub_longest = 1048576\n": "scrub_longest = 16\n"})
    assert _shipped(vector, program, f"plain words, then {SECRET}", tmp_path) == FAILED
    assert _shipped(vector, program, "short", tmp_path) == "[x] short"


def test_more_spans_than_the_most_ships_the_failure_marker(vector: str, tmp_path: Path) -> None:
    program = _program(**{"scrub_most = 1024\n": "scrub_most = 1\n"})
    two = f"A_TOKEN={SECRET} B_TOKEN={SECRET}"
    assert _shipped(vector, program, two, tmp_path) == FAILED
    assert _shipped(vector, program, f"A_TOKEN={SECRET}", tmp_path).endswith("redacted>'")


def test_an_offset_that_does_not_fit_ships_the_failure_marker(vector: str, tmp_path: Path) -> None:
    # A span ending past the text, as a wrong offset would record one: the rebuild refuses it, and
    # the message as it came never ships.
    program = _program(
        **{"scrub_count = 0\n": 'scrub_count = 0\nscrub_spans = {"000000000": [999999, "h"]}\n'}
    )
    assert _shipped(vector, program, f"A_TOKEN={SECRET} next=1", tmp_path) == FAILED


def test_a_message_that_is_not_utf8_is_read_and_rebuilt_on_the_same_offsets(
    vector: str, tmp_path: Path
) -> None:
    # Every rule reads the message through one lossy reading, each invalid byte one U+FFFD, and
    # the rebuild cuts that same text, so no offset moves (the review of #516: before, six invalid
    # bytes shifted every span by twelve, shipped part of the secret and redacted other text).
    raw = b"\xff" * 6 + f" A_TOKEN={SECRET} next=1 and then more text".encode()
    encoded = base64.b64encode(raw).decode()
    before = f'.message = get!({{"m": decode_base64!("{encoded}")}}, ["m"])\n'
    shipped = _shipped(vector, _program(), "", tmp_path, before=before)
    assert (
        shipped
        == "[x] " + "\ufffd" * 6 + " A_TOKEN='<a value the box redacted>' next=1 and then more text"
    )


def test_a_query_key_after_a_kept_marker_loses_its_value(vector: str, tmp_path: Path) -> None:
    # A marker kept where a value ends never takes the `&` after it, so the next query key is read
    # (the review of #516).
    shipped = _shipped(
        vector, _program(), f"GET /cb?token='<a redacted value>'&token={SECRET}", tmp_path
    )
    assert SECRET[:8] not in shipped and shipped.endswith("&token=[REDACTED]")


#: A web host's request line, as the capital's web read of #516 fabricated it: a path an outside
#: caller chose, holding a heartbeat URL, then the request's own fields.
REQUEST = (
    '{"method":"GET","path":"/heartbeat/K9fabricatedHeartbeat01","status":404,'
    '"remote_ip":"203.0.113.9","request_id":"0190f3a2-7c4e-7b1a-9d2e-5f6a7b8c9d0e"}'
)


#: The parameter flipped to `false`, for a host with no request lines.
_RUN_PAST_A_QUOTE = {"scrub_heartbeat_to_quote = true\n": "scrub_heartbeat_to_quote = false\n"}


def test_a_heartbeat_url_read_to_a_quote_keeps_a_json_line_and_its_fields(
    vector: str, tmp_path: Path
) -> None:
    # `scrub_heartbeat_to_quote` (this repository's hosts' value since #516's last round, and the
    # web hosts'): the URL ends at the `"` closing the path, so the line stays JSON and every
    # field after the path, the caller's address included, stays.
    program = _program()
    shipped = _shipped(vector, program, REQUEST, tmp_path).removeprefix("[x] ")
    assert json.loads(shipped) == {
        **json.loads(REQUEST),
        "path": "[REDACTED_HEARTBEAT_URL]",
    }
    kv = 'ping url="https://hc.example.com/heartbeat/K9fabricatedHeartbeat01" next=1'
    assert _shipped(vector, program, kv, tmp_path) == (
        '[x] ping url="[REDACTED_HEARTBEAT_URL]" next=1'
    )
    # A quote escaped inside a JSON string ends the URL before its backslash (the review of the
    # parameter: the run took the backslash and left the quote bare).
    nested = json.dumps({"msg": 'ping url="https://hc.example.com/heartbeat/K9fab01" next=1'})
    shipped = _shipped(vector, program, nested, tmp_path).removeprefix("[x] ")
    assert json.loads(shipped) == {"msg": 'ping url="[REDACTED_HEARTBEAT_URL]" next=1'}


#: What a caller may put in a request's path around a heartbeat URL: every character the JSON
#: encoder escapes or a run could end at.
_PATH_PIECES = ('"', "\\", "\\\\", "'", " ", "\t", "/", "?", "&", "=", "%22", "x", "\u00e9", "\n")

#: The member names the property test puts beside a path.
_MEMBERS = ("password", "Authorization", "api-key", "ACCESS_TOKEN", "clientSecret")


def test_a_caller_chosen_heartbeat_path_never_breaks_a_request_line(
    vector: str, tmp_path: Path
) -> None:
    # The property the parameter's `true` exists for, over 2,000 seeded paths: the request line
    # stays JSON, every field but the path is as it came, and no eight characters of the
    # heartbeat token ship. Half the lines carry a credential-named member too (#516's last round):
    # its value goes and every other field stays.
    rng = random.Random(516)
    token = "K9fabricatedHeartbeat01"
    program = _program()
    lines = []
    for _ in range(2000):
        around = ["".join(rng.choices(_PATH_PIECES, k=rng.randint(0, 4))) for _ in range(3)]
        path = f"{around[0]}/heartbeat/{token}{around[1]}"
        if rng.random() < 0.5:
            path = f"https://hc.example.com{path}"
        request = {"method": "GET", "path": around[2] + path, "status": 404}
        request["remote_ip"] = "203.0.113.9"
        if rng.random() < 0.5:
            # A credential-named member beside the path, its value as unruly as the path.
            request[rng.choice(_MEMBERS)] = rng.choice(around) + SECRET + rng.choice(around)
        lines.append(request)
    events = [{"SYSLOG_IDENTIFIER": "x", "message": json.dumps(r)} for r in lines]
    shipped = scrub_compare.run(vector, program, events, tmp_path)
    for request, out in zip(lines, shipped, strict=True):
        assert out is not None and out.startswith("[x] "), (request, out)
        got = json.loads(out.removeprefix("[x] "))
        members = [k for k in request if k in _MEMBERS]
        assert {k: v for k, v in got.items() if k != "path" and k not in members} == {
            k: v for k, v in request.items() if k != "path" and k not in members
        }, (request, out)
        assert all(got[k] == MARK for k in members), (request, out)
        assert "[REDACTED_HEARTBEAT_URL]" in got["path"], (request, out)
        for secret in (token, SECRET):
            assert not any(secret[i : i + 8] in out for i in range(len(secret) - 7)), (request, out)


def test_a_heartbeat_url_read_past_a_quote_where_the_parameter_is_false(
    vector: str, tmp_path: Path
) -> None:
    # `false`, kept for a host with no request lines, reads as the sequential pass did: the run
    # takes the `"` and what is glued to it, and a kv value's closing quote is put back. On a JSON
    # line that takes the rest of the line.
    program = _program(**_RUN_PAST_A_QUOTE)
    shipped = _shipped(vector, program, REQUEST, tmp_path)
    assert shipped == '[x] {"method":"GET","path":"[REDACTED_HEARTBEAT_URL]'
    kv = 'ping url="https://hc.example.com/heartbeat/K9fabricatedHeartbeat01" next=1'
    assert _shipped(vector, program, kv, tmp_path) == (
        '[x] ping url="[REDACTED_HEARTBEAT_URL]" next=1'
    )


MARK = "'<a value the box redacted>'"


@pytest.mark.parametrize(
    ("member", "kept"),
    [
        ({"password": SECRET}, None),
        ({"API_KEY": SECRET}, None),
        ({"apikey": SECRET}, None),
        ({"Api-Key": SECRET}, None),
        ({"access_token": SECRET}, None),
        ({"accessToken": SECRET}, None),
        ({"refresh-token": SECRET}, None),
        ({"CLIENT_SECRET": SECRET}, None),
        ({"secret": SECRET}, None),
        ({"Authorization": f"Basic {SECRET}"}, None),
        ({"password": f'a"b\\c{SECRET}'}, None),
        # Never a bare `key` or `token`, and never a name that only holds one of the words.
        ({"key": SECRET}, SECRET),
        ({"token": SECRET}, SECRET),
        ({"my_password": SECRET}, SECRET),
        ({"password_hint": SECRET}, SECRET),
        ({"access token": SECRET}, SECRET),
    ],
)
def test_a_credential_named_json_member_loses_its_value_and_keeps_its_name(
    vector: str, tmp_path: Path, member: dict[str, str], kept: str | None
) -> None:
    # @origin's rule (2026-11-19, #516): the value goes, the name and every other field stay, and
    # the line stays JSON.
    line = json.dumps({**member, "status": 200, "remote_ip": "203.0.113.9"})
    got = json.loads(_shipped(vector, _program(), line, tmp_path).removeprefix("[x] "))
    ((name, value),) = member.items()
    assert got == {name: kept or MARK, "status": 200, "remote_ip": "203.0.113.9"}
    if kept is None:
        assert SECRET[:8] not in json.dumps(got) and value != MARK


def test_a_credential_named_member_escaped_at_any_depth_keeps_every_line_json(
    vector: str, tmp_path: Path
) -> None:
    # JSON inside one, two, three and four JSON strings: each level's value goes, its close stays
    # as deep as it came, so every level still parses, a quote inside the value included.
    inner: object = {"password": f'x"{SECRET}', "user": "john"}
    nested = [inner]
    for _ in range(4):
        inner = {"msg": json.dumps(inner), "n": 1}
        nested.append(inner)
    for depth, line in enumerate(nested):
        out = _shipped(vector, _program(), json.dumps(line), tmp_path).removeprefix("[x] ")
        assert SECRET[:8] not in out, (depth, out)
        got: object = json.loads(out)
        for _ in range(depth):
            got = json.loads(got["msg"])  # type: ignore[index]
        assert got == {"password": MARK, "user": "john"}, (depth, out)


def test_a_member_value_already_redacted_is_left_as_it_is(vector: str, tmp_path: Path) -> None:
    for marker in (MARK, "'<a redacted value>'", "[redacted]", "[REDACTED]", ""):
        line = json.dumps({"password": marker, "n": 1})
        assert _shipped(vector, _program(), line, tmp_path) == f"[x] {line}"
