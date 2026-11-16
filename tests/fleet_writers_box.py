"""The one fabricated box every writer-map test runs on (#484), and the loader for the script.

``deploy/bin/fleet-writers`` is a root-owned helper with no ``.py`` name, so it is loaded as a
module from its path, once, here. The cast is the one ``tests/vector/ai_writers.yaml`` asserts
against, so its UIDs live in exactly one place:

  nova                 a harness agent                          uid 1001
  basecradle-nova-ai   a builder (``basecradle/basecradle-nova``)   uid 1002
  router               the daemon's account                     uid 999
  john                 John Doe's login session, in no registry  uid 1000
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "fleet-writers"


def _load():
    if "fleet_writers" in sys.modules:
        return sys.modules["fleet_writers"]
    loader = importlib.machinery.SourceFileLoader("fleet_writers", str(SCRIPT))
    spec = importlib.util.spec_from_loader("fleet_writers", loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fleet_writers"] = module
    loader.exec_module(module)
    return module


fw = _load()

ACCOUNTS = {"router": 999, "john": 1000, "nova": 1001, "basecradle-nova-ai": 1002}
REGISTRY = {
    "nova": {"kind": "harness", "os_user": "nova", "clone_path": "/home/nova/harness"},
    "basecradle/basecradle-nova": {"os_user": "basecradle-nova-ai"},
}
