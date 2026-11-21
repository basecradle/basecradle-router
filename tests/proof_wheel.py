"""A real venv, built the way the NOC builds a sealed tree, without the network (#535).

The question the sealed gate has to answer for a REAL environment: ``uv venv`` makes
``bin/python`` a link to the box's interpreter, and the console script uv writes for a
package's entry point names that link in its ``#!`` line. So the proof builds exactly that:
``uv venv`` at the tree's final name (a venv cannot be renamed after it is built), then
``uv pip install`` of a wheel that declares a ``basecradle-harness-wake`` console script, so the
script and its ``#!`` line are uv's own work and not a hand-written stand-in.

The wheel is assembled here from a few literal files, so nothing is fetched: ``--no-index``
with a local wheel and an explicit interpreter is uv working entirely offline. Its entry point
writes, into the working directory, the account it ran as and the argv it was given, which is
what a test reads back to know which binary a wake ran.
"""

from __future__ import annotations

import base64
import hashlib
import subprocess
import zipfile
from pathlib import Path

NAME = "basecradle_wake_proof"
VERSION = "0.0.1"
SCRIPT = "basecradle-harness-wake"

_MODULE = """\
import os
import pwd
import sys


def main():
    with open(os.path.join(os.getcwd(), "woke"), "w") as out:
        out.write("\\n".join([pwd.getpwuid(os.getuid()).pw_name, *sys.argv]) + "\\n")
"""


def _record_line(name: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{name},sha256={digest},{len(data)}"


def build_wheel(directory: Path) -> Path:
    """Write the proof wheel into ``directory`` and return its path."""
    dist_info = f"{NAME}-{VERSION}.dist-info"
    files = {
        f"{NAME}/__init__.py": _MODULE.encode(),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {VERSION}\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: proof_wheel\n"
            b"Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/entry_points.txt": (f"[console_scripts]\n{SCRIPT} = {NAME}:main\n").encode(),
    }
    record = [_record_line(name, data) for name, data in files.items()]
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()
    wheel = directory / f"{NAME}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return wheel


def build_venv(uv: str, python: str, tree: Path, wheel: Path, cache: Path) -> Path:
    """``uv venv`` at ``tree`` and install the wheel; return the console script's path."""
    env = {"PATH": "/usr/bin:/bin", "UV_CACHE_DIR": str(cache), "UV_NO_CONFIG": "1"}
    subprocess.run(
        [uv, "venv", "--quiet", "--python", python, "--no-python-downloads", str(tree)],
        check=True,
        env=env,
    )
    subprocess.run(
        [
            uv, "pip", "install", "--quiet", "--no-index", "--offline",
            "--python", str(tree / "bin" / "python"), str(wheel),
        ],
        check=True,
        env=env,
    )  # fmt: skip
    return tree / "bin" / SCRIPT
