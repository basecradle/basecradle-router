"""The harness's own scrub, read out of its published wheel, for the differential (#516).

Run by an interpreter the harness supports (it needs Python 3.11 or later; the router runs on
3.10), never imported by the router's tests:

    python tests/harness_scrub.py <wheel> <sha256>  < lines.json  > readings.json

It reads a JSON list of texts on stdin and writes one JSON object: `scrub`, every text through
the harness's `scrub` (all its rules, as a harness record carries them), and `credentials`, every
text through its `redact_credentials` (the credential-named rule alone, the print site's peer).
With `--spans` after the two arguments it writes, for each text, what each of the harness's rules
takes on its own, which the differential's table reads to say which rule a difference is in.

The wheel's bytes must hash to `sha256` before anything in it runs. Only two modules of it run,
`_observability` and the one sibling it imports, served from the verified zip; the package's
`__init__` (which imports every adapter and the platform SDK) is never executed, and nothing is
installed. So the harness is a pinned, verified input to the suite and never a dependency of the
router: it is not in `uv.lock`, and nothing here reaches the box (`uv sync --locked` installs the
development group there).

Standard library only, so it runs under any interpreter with no environment of its own.
"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import io
import json
import sys
import types
import zipfile

PACKAGE = "basecradle_harness"


class _WheelModules(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves `basecradle_harness` as an empty package and its top-level modules from one verified
    wheel, nothing else (the NOC's loader for the same wheel, `harness_price._WheelModules`)."""

    def __init__(self, wheel: zipfile.ZipFile) -> None:
        self._wheel = wheel
        self._names = set(wheel.namelist())

    def _member(self, fullname: str) -> str | None:
        if fullname == PACKAGE:
            return ""
        head, _, tail = fullname.partition(".")
        if head != PACKAGE or not tail or "." in tail:
            return None
        member = f"{PACKAGE}/{tail}.py"
        return member if member in self._names else None

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        member = self._member(fullname)
        if member is None:
            return None
        return importlib.util.spec_from_loader(fullname, self, is_package=member == "")

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> None:
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        member = self._member(module.__name__)
        if not member:
            module.__path__ = []
            return
        module.__file__ = f"<{member} from the verified wheel>"
        source = self._wheel.read(member).decode("utf-8")
        exec(compile(source, module.__file__, "exec"), module.__dict__)  # noqa: S102


def load(wheel_path: str, sha256: str) -> types.ModuleType:
    """The harness's `_observability`, from the wheel at `wheel_path` once its bytes hash to
    `sha256`."""
    with open(wheel_path, "rb") as handle:
        body = handle.read()
    got = hashlib.sha256(body).hexdigest()
    if got != sha256:
        raise SystemExit(f"{wheel_path} hashes to {got}, not the pinned {sha256}")
    wheel = zipfile.ZipFile(io.BytesIO(body))  # the bytes just verified, never the path again
    sys.meta_path.insert(0, _WheelModules(wheel))
    return importlib.import_module(f"{PACKAGE}._observability")


def rule_spans(observability: types.ModuleType, text: str) -> dict[str, list[list[int]]]:
    """What each of the harness's rules takes of `text` on its own: the spans `scrub` merges
    (`_observability.scrub`, which names its five rules and reads them so). These are the module's
    private names at the pinned release, so a harness roll that renames one fails here, loudly."""
    o = observability
    values = {start for start, _ in o._credential_spans(text)}
    spans = {
        "credential": [s for s in o._credential_spans(text) if s[1] > s[0]],
        "heartbeat": [o._heartbeat_span(m) for m in o._HEARTBEAT_RUN.finditer(text)],
        "x-api-key": list(o._literal_spans(o._X_API_KEY, text, values)),
        "query": list(o._literal_spans(o._QUERY, text, values)),
        "api_key": list(o._literal_spans(o._API_KEY, text, values)),
        "bearer": list(o._bearer_spans(text)),
        "prefix": [m.span() for m in o._PREFIX_RULES.finditer(text) if not o._is_word(m)],
        "member": list(o._member_spans(text)),
    }
    return {name: [list(span) for span in found] for name, found in spans.items()}


def main() -> None:
    if len(sys.argv) not in (3, 4) or sys.argv[3:] not in ([], ["--spans"]):
        raise SystemExit("usage: harness_scrub.py <wheel> <sha256> [--spans]")
    wheel_path, sha256 = sys.argv[1], sys.argv[2]
    observability = load(wheel_path, sha256)
    texts = json.load(sys.stdin)
    if sys.argv[3:] == ["--spans"]:
        json.dump([rule_spans(observability, text) for text in texts], sys.stdout)
        return
    json.dump(
        {
            "scrub": [observability.scrub(text) for text in texts],
            "credentials": [observability.redact_credentials(text) for text in texts],
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
