"""Two scrubs over the same lines, read as counts (basecradle-router#516).

Standard library only, so the NOC runs it on a server with the system Python and the server's
own Vector, reading that server's journal where it lies; nothing it reads is written anywhere.
It runs a remap transform's VRL from two Vector configs (`main`'s and a candidate's) through
`vector vrl`, which compiles a program with no enrichment tables, so the router's writer check
(one table lookup, never a scan of the text) is taken out first.

    journalctl -o json --since -7d | python3 -I tests/scrub_compare.py counts OLD.yaml NEW.yaml

prints four counts as one JSON object: `read`, the records read; `differ`, those whose message
the two write differently; `unredacted`, those where the candidate's message holds a run of
eight or more characters that `main`'s redacted; and `json_broken`, those whose message (after
its `[identifier] ` prefix) `main` leaves valid JSON and the candidate does not. With `--where`
it also prints the 1-based position of each `unredacted` and each `json_broken` record in the
stream, so the NOC reads that record on the server
and names its mechanism; no text is ever printed.

    python3 -I tests/scrub_compare.py excused OLD.yaml  < regressions.json

reads a JSON list of `[text, secret]` pairs, lines a candidate leaves the secret of where `OLD`
took it, and prints how many `OLD`'s own rules, each reading the text as it came, leave the
secret: a secret only `OLD`'s sequence took, which the union reads no rule into. That is the one
row the regression bar excuses while `main` is the sequential pass (decided on #516,
2026-11-19), and the differential reads it through `taken_apart_sequential` here. It refuses a
config that already carries the fleet's scrub block: against a union, no row excuses anything.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import itertools
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path

#: The fleet's scrub block's first line (`tests/scrub_block.py` `START`), dedented.
BLOCK_START = "# THE FLEET'S SCRUB, PASS 1 (basecradle-router#516)."
WRITER_CHECK_OPEN = 'if starts_with(ident, "basecradle-wake-")'
#: The least run of a secret read as that secret (the NOC's bar).
RUN = 8


def transform_vrl(config: str, transform: str) -> str:
    """The remap `transform`'s VRL in a Vector config's text (a YAML literal block, dedented), its
    writer check taken out."""
    component = config.index(f"\n  {transform}:\n")
    head = re.compile(r"\n( +)source: \|\n")
    found = head.search(config, component)
    if found is None:
        raise SystemExit(f"{transform} has no source block")
    indent = len(found.group(1)) + 2
    lines = []
    for line in config[found.end() :].splitlines():
        if line.strip() and not line.startswith(" " * indent):
            break
        lines.append(line[indent:])
    source = "\n".join(lines) + "\n"
    if WRITER_CHECK_OPEN in source:
        start = source.index(WRITER_CHECK_OPEN)
        end = source.index("\n}\n", start) + len("\n}\n")
        source = source[:start] + source[end:]
    if "find_enrichment_table_records" in source:
        raise SystemExit(f"{transform} reads an enrichment table, which `vector vrl` cannot")
    return source


def run(
    vector: str, program: str, events: list[dict[str, object]], workdir: Path | None = None
) -> list[str | None]:
    """`.message` after `program` for each event, or None where the program dropped it. The
    program is written to `workdir` (a test's `tmp_path`), or to a temporary directory removed
    when the run ends; the events go through standard input and are written nowhere."""
    if not events:
        return []
    marker = "SCRUB-COMPARE-"
    body = (
        program + f'\n"{marker}" + encode_base64(encode_json(.message)) + "-" + encode_json(.__n)\n'
    )
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        path = Path(tmp) / "program.vrl"
        path.write_text(body)
        stream = "".join(json.dumps({**e, "__n": str(i)}) + "\n" for i, e in enumerate(events))
        got = subprocess.run(
            [vector, "vrl", "-q", "-i", "/dev/stdin", "-p", str(path)],
            input=stream,
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": os.environ.get("PATH", ""), "NO_COLOR": "1"},
        )
    out: list[str | None] = [None] * len(events)
    for row in got.stdout.splitlines():
        value = json.loads(row) if row.startswith('"') else None
        if isinstance(value, str) and value.startswith(marker):
            encoded, _, index = value[len(marker) :].rpartition("-")
            message = json.loads(base64.b64decode(encoded))
            out[int(json.loads(index))] = message if isinstance(message, str) else None
    if got.returncode != 0 and all(row is None for row in out):
        raise SystemExit(got.stderr[-2000:])
    return out


def redacted_runs(original: str, scrubbed: str) -> Iterator[str]:
    """Every run of `RUN` or more characters of `original` that `scrubbed` replaced."""
    matcher = difflib.SequenceMatcher(None, original, scrubbed, autojunk=False)
    for tag, i1, i2, _, _ in matcher.get_opcodes():
        if tag in ("replace", "delete") and i2 - i1 >= RUN:
            yield original[i1:i2]


def unredacted(original: str, old: str, new: str) -> bool:
    """Whether `new` holds `RUN` characters of a run `old` redacted, and `old` does not."""
    for run_ in redacted_runs(original, old):
        for i in range(len(run_) - RUN + 1):
            window = run_[i : i + RUN]
            if window in new and window not in old:
                return True
    return False


def _json(message: str) -> bool:
    """Whether a shipped message, its `[identifier] ` prefix taken off, is valid JSON."""
    body = message.split("] ", 1)[1] if message.startswith("[") and "] " in message else message
    try:
        json.loads(body)
    except ValueError:
        return False
    return True


def _records(stream: Iterable[str]) -> Iterator[dict[str, object]]:
    """Journal records (`journalctl -o json`), each as the journald source makes it: `MESSAGE`
    becomes `message`, and a field journald wrote as bytes is decoded."""
    for line in stream:
        if not line.strip():
            continue
        record = json.loads(line)
        event = {}
        for key, value in record.items():
            if isinstance(value, list) and all(isinstance(b, int) for b in value):
                value = bytes(value).decode("utf-8", "replace")
            event["message" if key == "MESSAGE" else key] = value
        yield event


def counts(vector: str, old: str, new: str, stream: Iterable[str], batch: int = 5000) -> dict:
    """The four counts over a stream of journal records, and where each counted one is. The
    stream is read `batch` records at a time, so a week of a busy journal is never held whole."""
    result = {"read": 0, "differ": 0, "unredacted": 0, "json_broken": 0, "where": {}}
    where: dict[str, list[int]] = {"unredacted": [], "json_broken": []}
    records = _records(stream)
    start = 0
    while chunk := list(itertools.islice(records, batch)):
        before, after = run(vector, old, chunk), run(vector, new, chunk)
        for i, (event, was, now) in enumerate(zip(chunk, before, after, strict=True)):
            result["read"] += 1
            if was != now:
                result["differ"] += 1
            text = str(event.get("message", ""))
            if was is None or now is None:
                continue
            for count, hit in (
                ("unredacted", unredacted(text, was, now)),
                ("json_broken", _json(was) and not _json(now)),
            ):
                if hit:
                    result[count] += 1
                    where[count].append(start + i + 1)
        start += len(chunk)
    result["where"] = where
    return result


# --- the one excused row -------------------------------------------------------------------------

_CREDENTIAL_OPEN = "scrubbed = map_values(., recursive: true) -> |value| {"
_FIELD_CHECK = "for_each(scrubbed) -> |field, value| {"


def sequential_rules(source: str) -> list[str]:
    """Each rule of a sequential `ai_scrub` (the pass before #516) as a program of its own over
    `.message`, reading the text as it came: the credential rule, then every `msg = ` statement
    of the message passes."""
    if BLOCK_START in source:
        raise SystemExit("this config carries the fleet's scrub block: no row excuses anything")
    start = source.index(_CREDENTIAL_OPEN)
    end = source.index("\n}\n", source.index(_FIELD_CHECK, start)) + 3
    programs = [source[start:end]]
    body = source[source.index("msg = to_string(.message)") : source.index('\nident = ""\n')]
    statements = re.split(r"\n(?=msg = )", body)[1:]
    assert statements, "no message pass"
    programs += ['msg = to_string(.message) ?? ""\n' + s + "\n.message = msg\n" for s in statements]
    return programs


def taken_apart_sequential(
    vector: str, source: str, pairs: list[tuple[str, str]], workdir: Path | None = None
) -> list[bool]:
    """For each `(text, secret)`, whether the sequential `source`'s rules, each alone on the text
    as it came, take the secret between them: every `RUN` characters of it gone from at least one
    rule's output. False is the excused row."""
    if not pairs:
        return []
    events = [{"SYSLOG_IDENTIFIER": "x", "message": text} for text, _ in pairs]
    outputs = [run(vector, program, events, workdir) for program in sequential_rules(source)]
    taken = []
    for i, (_, secret) in enumerate(pairs):
        windows = [secret[j : j + RUN] for j in range(len(secret) - RUN + 1)]
        taken.append(all(any(w not in (out[i] or "") for out in outputs) for w in windows))
    return taken


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--vector", default="vector", help="the server's Vector (default: on PATH)")
    parser.add_argument("--transform", default="ai_scrub", help="the remap to read")
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("counts", help="journal records on stdin; four counts")
    one.add_argument("old")
    one.add_argument("new")
    one.add_argument("--where", action="store_true", help="also print where each counted one is")
    two = sub.add_parser("excused", help="[text, secret] pairs on stdin; the excused row's count")
    two.add_argument("old")
    args = parser.parse_args()
    old = transform_vrl(Path(args.old).read_text(), args.transform)
    if args.command == "counts":
        new = transform_vrl(Path(args.new).read_text(), args.transform)
        result = counts(args.vector, old, new, sys.stdin)
        if not args.where:
            del result["where"]
        print(json.dumps(result))
        return
    pairs = [(text, secret) for text, secret in json.load(sys.stdin)]
    taken = taken_apart_sequential(args.vector, old, pairs)
    left = [i + 1 for i, t in enumerate(taken) if t]
    print(json.dumps({"regressions": len(pairs), "excused": len(pairs) - len(left), "left": left}))


if __name__ == "__main__":
    main()
