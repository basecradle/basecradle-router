"""The fleet's scrub block fails closed (basecradle-router#516, requirement 2).

A pass that cannot vouch for what it would ship writes `scrub_block.FAILED` in place of the whole
message, never the message as it came: a message longer than `scrub_longest`, more spans than
`scrub_most`, and an offset that does not fit the text. Each is forced here, on the boxes' Vector,
through the shipped program (the writer check taken out, which `vector vrl` cannot compile). The
same holds on every host that copies the block, since the bytes are the same.
"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path

import pytest

import scrub_block
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
