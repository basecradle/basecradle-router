"""What a correct edge sends, read from a Caddyfile's text: one parser for every test.

`deploy/smoke-test.sh` case 10 reads the installed Caddyfile with its own bash reader. The
tests that check that reader, and the tests that fake a correct edge around the rest of the
smoke test, need the same answer from a parser that shares nothing with it. This is that
parser, written once, so a Caddyfile shape it learns is learned by every test at once.
"""

from __future__ import annotations

import re

_MAP = re.compile(r"(?ms)^\s*map \{path\} \{(\w+)\} \{\n(.*?)^\s*\}")
_ENTRY = re.compile(r'(?m)^\s*(\S+)\s+"([^"]*)"\s*$')
_HEADER = re.compile(r'(?m)^\s*header\s+(\S+)\s+"([^"]*)"\s*$')


def edge_headers(caddyfile: str, path: str) -> list[tuple[str, str]]:
    """Every `header` directive in ``caddyfile`` as Caddy sends it on ``path``, in order.

    A ``"{var}"`` value is the entry for ``path`` in the `map {path}` writing ``var``, else
    that map's default: Caddy compares a map key to the whole path.
    """
    maps = {}
    for var, body in _MAP.findall(caddyfile):
        entries = dict(_ENTRY.findall(body))
        maps[var] = entries.get(path, entries.get("default"))
    return [
        (name, maps[value[1:-1]] if value.startswith("{") else value)
        for name, value in _HEADER.findall(caddyfile)
    ]
