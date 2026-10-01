"""No representation of a credential-holding object emits the credential (#317).

The class (basecradle/basecradle#612): a generic serializer or representation of an
object that holds a credential prints the credential. Measured on this repo before the
fix, ``repr``/``str``/``pprint``/``%r`` logging of ``Config``, ``RecipientKeyring`` and
``WakeProbe`` (and of the ``Pipeline`` holding the config) printed live signing secrets,
``dataclasses.asdict(WakeProbe)`` emitted the probe secret, and every holder's
``__reduce_ex__`` state carried them.

These tests pin both halves. The value type refuses every surface on its own, and every
holder the daemon actually builds is clean on every surface the issue names. The holders
are built through the production composition root (``create_app``) and loaders, not by
hand, so a loader that went back to storing a bare ``str`` would fail here.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import io
import json
import logging
import pickle
import pprint
import sys
from collections.abc import Callable
from types import FrameType

import pytest

from basecradle_router.app import create_app
from basecradle_router.probe import WakeProbe
from basecradle_router.routes.base import InboundRequest, SignatureError, verify_hmac_sha256
from basecradle_router.routes.basecradle import RECIPIENT_SECRET_PREFIX
from basecradle_router.secret import REDACTED, Secret

# Fabricated, correctly-shaped fakes — one per credential the daemon holds.
GITHUB_SECRET = "fake-github-webhook-secret-" + "7" * 32
BASECRADLE_SECRET = "bc_isk_fakeroutewidebasecradlesecret0001"
PROBE_SECRET = "fake-probe-route-secret-" + "3" * 32
NOVA_KEY = "bc_isk_fakenovaintegrationsigningkey9001"
PLAINTEXTS = (GITHUB_SECRET, BASECRADLE_SECRET, PROBE_SECRET, NOVA_KEY)

NOVA_UUID = "019e916c-7f45-700e-afc0-f45557b237b7"


def _assert_clean(text: str) -> None:
    for plaintext in PLAINTEXTS:
        assert plaintext not in text, f"credential emitted: {text[:200]!r}"


# --- the value type ------------------------------------------------------------------


def test_reveal_is_the_one_way_to_the_plaintext() -> None:
    assert Secret(NOVA_KEY).reveal() == NOVA_KEY


@pytest.mark.parametrize(
    "render",
    [
        repr,
        str,
        lambda s: f"{s}",
        lambda s: f"{s!r}",
        lambda s: format(s),
        pprint.pformat,
        lambda s: "%s" % s,  # noqa: UP031 — the %-operator is the surface under test
        lambda s: "%r" % s,  # noqa: UP031
        lambda s: json.dumps(s, default=str),
        lambda s: json.dumps({"key": s}, default=repr),
    ],
    ids=[
        "repr",
        "str",
        "fstring",
        "fstring-r",
        "format",
        "pprint",
        "%s",
        "%r",
        "json-str",
        "json-repr",
    ],
)
def test_every_text_rendering_is_the_fixed_redaction(render: Callable[[Secret], str]) -> None:
    rendered = render(Secret(NOVA_KEY))
    _assert_clean(rendered)
    assert "**********" in rendered


def test_the_redaction_carries_no_length_or_prefix() -> None:
    # A length or a `bc_isk_` prefix would tell a log reader something about the key.
    assert repr(Secret("a")) == repr(Secret(NOVA_KEY)) == REDACTED


def test_logging_with_r_and_s_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="basecradle_router.test_secret"):
        log = logging.getLogger("basecradle_router.test_secret")
        log.debug("key=%r", Secret(NOVA_KEY))
        log.debug("key=%s", Secret(NOVA_KEY))
    assert len(caplog.records) == 2
    for record in caplog.records:
        _assert_clean(record.getMessage())


def test_there_is_no_instance_dict_to_walk() -> None:
    secret = Secret(NOVA_KEY)
    with pytest.raises(TypeError):
        vars(secret)
    with pytest.raises(AttributeError):
        secret.__dict__  # noqa: B018


def test_json_dump_to_a_stream_writes_nothing_before_it_raises() -> None:
    # `json.dump` flushes partial output before a `default` raises; `dumps` hides that.
    stream = io.StringIO()
    with pytest.raises(TypeError):
        json.dump({"key": Secret(NOVA_KEY)}, stream, default=vars)
    _assert_clean(stream.getvalue())


@pytest.mark.parametrize("protocol", range(pickle.HIGHEST_PROTOCOL + 1))
def test_pickle_is_refused_at_every_protocol(protocol: int) -> None:
    with pytest.raises(TypeError, match="refusing to serialize or copy a Secret") as caught:
        pickle.dumps(Secret(NOVA_KEY), protocol=protocol)
    _assert_clean(str(caught.value))


@pytest.mark.parametrize(
    "duplicate",
    [
        copy.copy,
        copy.deepcopy,
        lambda s: s.__reduce__(),
        lambda s: s.__reduce_ex__(2),
        lambda s: s.__reduce_ex__(4),
    ],
    ids=["copy", "deepcopy", "reduce", "reduce_ex-2", "reduce_ex-4"],
)
def test_copy_and_reduce_are_refused(duplicate: Callable[[Secret], object]) -> None:
    with pytest.raises(TypeError, match="refusing to serialize or copy a Secret"):
        duplicate(Secret(NOVA_KEY))


def test_dataclasses_asdict_refuses_rather_than_emitting() -> None:
    @dataclasses.dataclass
    class Holder:
        key: Secret

    holder = Holder(Secret(NOVA_KEY))
    for flatten in (dataclasses.asdict, dataclasses.astuple):
        with pytest.raises(TypeError, match="refusing to serialize or copy a Secret"):
            flatten(holder)


def test_it_is_immutable() -> None:
    secret = Secret(NOVA_KEY)
    with pytest.raises(AttributeError):
        secret._value = "swapped"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del secret._value
    assert secret.reveal() == NOVA_KEY


def test_it_wraps_only_a_str() -> None:
    with pytest.raises(TypeError):
        Secret(NOVA_KEY.encode())  # type: ignore[arg-type]


def test_equality_is_by_value_between_secrets_only() -> None:
    assert Secret(NOVA_KEY) == Secret(NOVA_KEY)
    assert Secret(NOVA_KEY) != Secret(GITHUB_SECRET)
    # A bare str is never equal, so a holder that regressed to storing one is caught
    # by any test comparing against a Secret.
    assert Secret(NOVA_KEY) != NOVA_KEY


def test_it_is_unhashable() -> None:
    # A hash of the plaintext is a fingerprint of it.
    with pytest.raises(TypeError):
        hash(Secret(NOVA_KEY))


# --- every holder the daemon builds --------------------------------------------------


def _build_holders(tmp_path) -> dict[str, object]:
    """Every object holding a credential, built the way production builds it."""
    registry_file = tmp_path / "agents.json"
    registry_file.write_text(
        json.dumps(
            {
                "nova": {
                    "kind": "harness",
                    "os_user": "nova",
                    "clone_path": "/home/nova/harness",
                    "recipient_uuid": NOVA_UUID,
                    "wake_bin": "/home/nova/venv/bin/basecradle-harness-wake",
                },
                "basecradle/basecradle-python": {
                    "os_user": "basecradle-python-ai",
                    "clone_path": "/home/basecradle-python-ai/basecradle-python",
                    "bot_slug": "basecradle-python-ai",
                },
            }
        )
    )
    env = {
        "BASECRADLE_ROUTER_AGENTS": str(registry_file),
        "BASECRADLE_ROUTER_ENABLED_ROUTES": "github,basecradle,probe",
        "BASECRADLE_ROUTER_GITHUB_WEBHOOK_SECRET": GITHUB_SECRET,
        "BASECRADLE_ROUTER_GITHUB_TRUSTED_ACTORS": "john",
        "BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET": BASECRADLE_SECRET,
        "BASECRADLE_ROUTER_PROBE_WEBHOOK_SECRET": PROBE_SECRET,
        f"{RECIPIENT_SECRET_PREFIX}NOVA": NOVA_KEY,
        "BASECRADLE_ROUTER_EVIDENCE_FILE": str(tmp_path / "evidence.json"),
        "BASECRADLE_ROUTER_WAKE_LOCK_DIR": str(tmp_path / "wake-locks"),
    }
    server = create_app(env)
    pipeline = server.pipeline
    config = pipeline.config
    route = pipeline.registry.get("basecradle")
    # Built exactly as `python -m basecradle_router probe wake` builds it.
    probe = WakeProbe(
        secret=config.webhook_secret("probe"),
        evidence_path=str(tmp_path / "evidence.json"),
        self_url="http://127.0.0.1:8000",
    )

    # The holders really hold the credentials. Without this the whole sweep could pass
    # against objects that never carried a secret at all.
    assert config.webhook_secret("github").reveal() == GITHUB_SECRET
    assert config.webhook_secret("basecradle").reveal() == BASECRADLE_SECRET
    assert route.keyring.by_recipient[NOVA_UUID].reveal() == NOVA_KEY
    assert probe.secret.reveal() == PROBE_SECRET

    return {
        "Config": config,
        "RecipientKeyring": route.keyring,
        "BasecradleRoute": route,
        "WakeProbe": probe,
        "RouteRegistry": pipeline.registry,
        "Pipeline": pipeline,
        "WebhookServer": server,
    }


HOLDERS = [
    "Config",
    "RecipientKeyring",
    "BasecradleRoute",
    "WakeProbe",
    "RouteRegistry",
    "Pipeline",
    "WebhookServer",
]


def _vars_or_none(holder: object) -> object:
    try:
        return vars(holder)
    except TypeError:
        return None  # a slots object: nothing to walk


@pytest.mark.parametrize("name", HOLDERS)
def test_no_text_rendering_of_a_holder_emits_a_credential(tmp_path, name: str) -> None:
    holder = _build_holders(tmp_path)[name]
    for rendered in (
        repr(holder),
        str(holder),
        f"{holder}",
        pprint.pformat(holder),
        json.dumps(holder, default=str),
        repr(_vars_or_none(holder)),
        pprint.pformat(_vars_or_none(holder)),
    ):
        _assert_clean(rendered)


@pytest.mark.parametrize("name", HOLDERS)
def test_logging_a_holder_emits_no_credential(
    tmp_path, name: str, caplog: pytest.LogCaptureFixture
) -> None:
    holder = _build_holders(tmp_path)[name]
    with caplog.at_level(logging.DEBUG, logger="basecradle_router.test_secret"):
        log = logging.getLogger("basecradle_router.test_secret")
        log.debug("holder=%r", holder)
        log.debug("holder=%s", holder)
    for record in caplog.records:
        _assert_clean(record.getMessage())


@pytest.mark.parametrize("name", HOLDERS)
def test_json_dump_of_a_holder_to_a_stream_emits_nothing(tmp_path, name: str) -> None:
    holder = _build_holders(tmp_path)[name]
    stream = io.StringIO()
    # Refusing is fine; what was already flushed before the refusal is what matters.
    with contextlib.suppress(TypeError, ValueError):
        json.dump(holder, stream, default=vars)
    _assert_clean(stream.getvalue())


@pytest.mark.parametrize("name", HOLDERS)
def test_flattening_a_holder_refuses_or_emits_nothing(tmp_path, name: str) -> None:
    holder = _build_holders(tmp_path)[name]
    if not dataclasses.is_dataclass(holder):
        pytest.skip(f"{name} is not a dataclass")
    for flatten in (dataclasses.asdict, dataclasses.astuple):
        try:
            flattened = flatten(holder)
        except TypeError as exc:
            _assert_clean(str(exc))
        else:
            _assert_clean(repr(flattened))


@pytest.mark.parametrize("name", HOLDERS)
def test_pickling_a_holder_refuses_or_emits_nothing(tmp_path, name: str) -> None:
    holder = _build_holders(tmp_path)[name]
    for protocol in range(pickle.HIGHEST_PROTOCOL + 1):
        try:
            payload = pickle.dumps(holder, protocol=protocol)
        except (TypeError, AttributeError, pickle.PicklingError) as exc:
            _assert_clean(str(exc))
        else:  # pragma: no cover — no holder pickles today; if one ever does, it must be clean
            for plaintext in PLAINTEXTS:
                assert plaintext.encode() not in payload


@pytest.mark.parametrize("name", HOLDERS)
def test_reduce_state_and_copies_of_a_holder_emit_nothing(tmp_path, name: str) -> None:
    holder = _build_holders(tmp_path)[name]
    _assert_clean(repr(holder.__reduce_ex__(4)))
    _assert_clean(repr(copy.copy(holder)))
    try:
        duplicate = copy.deepcopy(holder)
    except (TypeError, AttributeError, pickle.PicklingError) as exc:
        _assert_clean(str(exc))
    else:  # pragma: no cover — a deep copy must not have duplicated a plaintext either
        _assert_clean(repr(duplicate))


def test_a_holder_with_a_secret_is_refused_by_deepcopy_at_the_secret(tmp_path) -> None:
    # WakeProbe is the one holder whose other fields all deep-copy, so it shows the
    # refusal is the Secret's own and not an accident of a mappingproxy or a lambda.
    probe = _build_holders(tmp_path)["WakeProbe"]
    with pytest.raises(TypeError, match="refusing to serialize or copy a Secret"):
        dataclasses.asdict(probe)


# --- the verify path ---------------------------------------------------------------


def test_no_router_frame_on_the_verify_path_binds_the_plaintext() -> None:
    """The plaintext is revealed inside the HMAC expression and bound to no name.

    So a crash reporter that renders frame locals (Sentry, ``rich``, ``cgitb``) has no
    router frame to find it in. The stdlib's own ``hmac`` frames necessarily hold the
    key; ours do not.
    """
    seen: list[str] = []

    def profile(frame: FrameType, event: str, _arg: object) -> None:
        if event == "return" and "basecradle_router" in frame.f_code.co_filename:
            seen.append(repr(frame.f_locals))

    request = InboundRequest(headers={"X-Hub-Signature-256": "sha256=00"}, body=b"{}")
    # Wrapped before profiling starts: construction necessarily sees the plaintext, and
    # the claim is about the verify path, which receives the Secret already wrapped.
    secret = Secret(GITHUB_SECRET)
    sys.setprofile(profile)
    try:
        with pytest.raises(SignatureError):
            verify_hmac_sha256(request, secret, header="X-Hub-Signature-256")
    finally:
        sys.setprofile(None)

    assert seen, "the profiler saw no router frame — the test is not measuring anything"
    for frame_locals in seen:
        _assert_clean(frame_locals)
