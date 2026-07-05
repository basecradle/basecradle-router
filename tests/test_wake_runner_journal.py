"""The wake-runner routes each wake's output into journald, attributable per agent.

Before basecradle-router#168 the caller (the router) captured the wake's
stdout+stderr and dropped it, so the harness's per-step ledger (`step N/M …`,
`wake used X/N steps`) reached journald *nowhere*. The wrapper now `exec`s the wake
through `systemd-cat --identifier=basecradle-wake-<os_user>`, so one persona's
wake output is greppable via `journalctl -t basecradle-wake-<slug>`.

A full behavioural exec of wake-runner is not offline-testable — it gates on
``EUID==0``, a real UID≥1000 login user, ``runuser``, and the root-owned registry
— which is why the sibling env-loader test extracts only the pure function. So this
pins the plumbing **structurally**, against the shipped bytes, so a future refactor
of the exec line cannot silently drop the capture, misattribute it, or break the
`exec`-through that preserves exit-code propagation. No model/agent/network touched.
"""

from __future__ import annotations

import re
from pathlib import Path

WAKE_RUNNER = Path(__file__).resolve().parents[1] / "deploy" / "bin" / "wake-runner"


def _script() -> str:
    return WAKE_RUNNER.read_text()


def test_the_wake_is_wrapped_in_systemd_cat_execing_through_to_runuser() -> None:
    # `exec "$systemd_cat" … -- runuser …`: the wrap must `exec` (not fork) so
    # runuser's exit — i.e. the wake's — flows straight back to the caller, and it
    # must sit immediately in front of runuser so the whole wake subtree inherits
    # the journal-connected fds. Output plumbing only; nothing else changes.
    assert re.search(
        r'exec\s+"\$systemd_cat"\s+--identifier="\$journal_tag"\s+--\s+runuser\s+-u\s+"\$user"',
        _script(),
    ), "the wake must be exec'd through systemd-cat wrapping runuser (basecradle-router#168)"


def test_the_journal_identifier_is_the_per_agent_os_user_slug() -> None:
    # Attribution rides the identifier: it must be the agent's OS-user slug (its
    # universal identity — never the registry key, which for a builder is
    # `owner/name` and carries a slash). `journalctl -t basecradle-wake-<slug>`.
    assert 'journal_tag="basecradle-wake-$user"' in _script(), (
        "the journald identifier must be basecradle-wake-<os_user> for per-agent attribution"
    )


def test_systemd_cat_is_resolved_off_a_trusted_path_not_the_callers() -> None:
    # Same discipline as `claude`: resolve the binary off a root-owned trusted PATH
    # so a compromised router cannot plant a `systemd-cat`. systemd-cat runs as root
    # here (before the privilege drop), so its resolution must not trust caller state.
    assert re.search(
        r'systemd_cat="\$\(PATH="\$TRUSTED_PATH"\s+command -v systemd-cat\)"\s+\|\|\s+die',
        _script(),
    ), "systemd-cat must be resolved off the trusted PATH, dying if absent"
