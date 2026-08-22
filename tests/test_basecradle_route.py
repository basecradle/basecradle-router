"""The basecradle route's signature verification + normalization.

Offline only: a fabricated secret and a hand-computed signature, no network. The
verify boundary is the shared HMAC implementation (also exercised via github), so
these tests focus on this route's header names and its normalize contract.

Fabricated platform event: a new message lands on a timeline the fleet harness
persona @jt (``jt``, an AI user) views; the platform signs and POSTs it. @jt's
fabricated user uuid is a well-formed UUIDv7.
"""

import hashlib
import hmac
import json
import re

import pytest

from basecradle_router.config import ConfigError, route_secret_var
from basecradle_router.models import Agent, EventKind, Recipient, WakeKind
from basecradle_router.routes import (
    BasecradleRoute,
    DeliveryDecision,
    InboundRequest,
    PayloadError,
    RecipientKeyring,
    Route,
    SignatureError,
    load_recipient_keyring,
)
from basecradle_router.routes.basecradle import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    KEY_PATH_FALLBACK,
    KEY_PATH_RECIPIENT,
    RECIPIENT_SECRET_PREFIX,
    SHARED_FALLBACK_VAR,
    SIGNATURE_HEADER,
    _slug_suffix,
)

SECRET = "s3cret-fake-integration-secret"
JT_UUID = "019e916c-7f45-700e-afc0-f45557b237b7"  # @jt's BaseCradle user uuid
TIMELINE_UUID = "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
DELIVERY = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"  # the event_id / delivery id


def _sign(body: bytes, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _payload(
    *,
    recipient_uuid: str | None = JT_UUID,
    timeline_uuid: str | None = TIMELINE_UUID,
    event: str = "message.created",
) -> dict:
    payload: dict = {
        "event": event,
        "event_id": DELIVERY,
        "occurred_at": "2026-06-09T00:00:00Z",
        "actor_uuid": None,
        "resource": {"type": "message", "uuid": TIMELINE_UUID, "url": "https://x/m/1"},
    }
    if recipient_uuid is not None:
        payload["recipient_uuid"] = recipient_uuid
    if timeline_uuid is not None:
        payload["timeline_uuid"] = timeline_uuid
    return payload


def _request(
    payload: dict | None = None,
    *,
    raw_body: bytes | None = None,
    event: str | None = "message.created",
    delivery: str | None = DELIVERY,
    signature: str | None = "auto",
) -> InboundRequest:
    body = raw_body if raw_body is not None else json.dumps(payload or _payload()).encode("utf-8")
    headers: dict[str, str] = {}
    if event is not None:
        headers[EVENT_HEADER] = event
    if delivery is not None:
        headers[DELIVERY_HEADER] = delivery
    if signature == "auto":
        headers[SIGNATURE_HEADER] = _sign(body)
    elif signature is not None:
        headers[SIGNATURE_HEADER] = signature
    return InboundRequest(headers=headers, body=body)


def _decision_line(caplog) -> str:
    """The one ``event=delivery_decision`` line the route emitted for this delivery."""
    return next(
        r.getMessage() for r in caplog.records if "event=delivery_decision" in r.getMessage()
    )


# --- contract + verify -----------------------------------------------------


def test_basecradle_route_satisfies_the_protocol() -> None:
    assert isinstance(BasecradleRoute(), Route)
    assert BasecradleRoute().name == "basecradle"


def test_verify_accepts_a_correct_signature() -> None:
    body = json.dumps(_payload()).encode("utf-8")
    BasecradleRoute().verify(_request(raw_body=body, signature=_sign(body)), SECRET)


def test_verify_accepts_regardless_of_header_case() -> None:
    body = b'{"event":"message.created"}'
    req = InboundRequest(headers={"x-basecradle-signature": _sign(body)}, body=body)
    BasecradleRoute().verify(req, SECRET)


def test_verify_rejects_a_tampered_body() -> None:
    body = json.dumps(_payload()).encode("utf-8")
    tampered = InboundRequest(headers={SIGNATURE_HEADER: _sign(body)}, body=body + b" ")
    with pytest.raises(SignatureError, match="does not match"):
        BasecradleRoute().verify(tampered, SECRET)


def test_verify_rejects_a_missing_header() -> None:
    body = b"{}"
    with pytest.raises(SignatureError, match="missing"):
        BasecradleRoute().verify(InboundRequest(headers={}, body=body), SECRET)


def test_verify_rejects_a_malformed_header() -> None:
    body = b"{}"
    bare = _sign(body).removeprefix("sha256=")
    req = InboundRequest(headers={SIGNATURE_HEADER: bare}, body=body)
    with pytest.raises(SignatureError, match="malformed"):
        BasecradleRoute().verify(req, SECRET)


# --- normalize -------------------------------------------------------------


def test_normalize_message_created_round_trips() -> None:
    event = BasecradleRoute().normalize(_request())
    assert event is not None
    assert event.source == "basecradle"
    assert event.kind is EventKind.PLATFORM_EVENT
    # Resolved by the recipient's BaseCradle user uuid, not a repo.
    assert event.recipient == Recipient(by="recipient_uuid", value=JT_UUID)
    # The wake hands the harness the timeline to process.
    assert event.wake_arg == TIMELINE_UUID
    assert event.delivery_id == DELIVERY
    # No GitHub-style issue to report on — the harness replies on the timeline itself.
    assert event.origin is None


@pytest.mark.parametrize("event", ["asset.created", "task.activated", "webhook_event.received"])
def test_normalize_wakes_on_the_other_actionable_events(event: str) -> None:
    # The founder's required wake set beyond message.created: a peer's asset (#95),
    # a scheduled task coming due, and an inbound webhook delivery (#90). All ride
    # the same firehose envelope — recipient_uuid + timeline_uuid — and must wake
    # the agent for that timeline, not fall on the floor.
    e = BasecradleRoute().normalize(_request(event=event))
    assert e is not None
    assert e.kind is EventKind.PLATFORM_EVENT
    assert e.recipient == Recipient(by="recipient_uuid", value=JT_UUID)
    assert e.wake_arg == TIMELINE_UUID
    assert e.delivery_id == DELIVERY


def test_normalize_still_defers_participant_added() -> None:
    # participant.added is self-authorable and NOT in the founder's required set, so
    # promoting asset.created (#95) must NOT also promote it — it stays a clean
    # ignore (deferred Tier 2) until the harness self-filter work covers it.
    assert BasecradleRoute().normalize(_request(event="participant.added")) is None


def test_normalize_ignores_non_actionable_event() -> None:
    # An event outside the actionable set is a clean ignore (None), not an error —
    # nothing for the harness to do, so no wake. (A deliberate, logged ignore.)
    assert BasecradleRoute().normalize(_request(event="participant.removed")) is None


def test_normalize_ignores_event_with_no_event_header() -> None:
    assert BasecradleRoute().normalize(_request(event=None)) is None


def test_normalize_rejects_malformed_json() -> None:
    with pytest.raises(PayloadError, match="not valid JSON"):
        BasecradleRoute().normalize(_request(raw_body=b"{not json"))


def test_normalize_rejects_non_object_body() -> None:
    with pytest.raises(PayloadError, match="must be a JSON object"):
        BasecradleRoute().normalize(_request(raw_body=b"[1, 2, 3]"))


def test_normalize_rejects_missing_delivery_header() -> None:
    with pytest.raises(PayloadError, match=DELIVERY_HEADER):
        BasecradleRoute().normalize(_request(delivery=None))


def test_normalize_rejects_missing_recipient_uuid() -> None:
    with pytest.raises(PayloadError, match="recipient_uuid"):
        BasecradleRoute().normalize(_request(_payload(recipient_uuid=None)))


def test_normalize_rejects_missing_timeline_uuid() -> None:
    with pytest.raises(PayloadError, match="timeline_uuid"):
        BasecradleRoute().normalize(_request(_payload(timeline_uuid=None)))


# --- observability: the ignore-vs-act decision is logged (#91) --------------


def test_normalize_logs_the_woke_decision_for_a_message(caplog) -> None:
    # An acted message records decision=woke with the event type and the recipient
    # it resolved to — the signal a silent task.activated drop lacked.
    with caplog.at_level("INFO", logger="basecradle_router.routes"):
        BasecradleRoute().normalize(_request())
    line = _decision_line(caplog)
    assert "source=basecradle" in line
    assert "event_type=message.created" in line
    assert f"decision={DeliveryDecision.WOKE.value}" in line
    assert f"recipient={JT_UUID}" in line
    assert f"delivery={DELIVERY}" in line


def test_normalize_logs_the_ignored_decision_with_the_event_type(caplog) -> None:
    # A non-actionable delivery is a *visible* deliberate ignore: the event type is
    # named so an unexpectedly-ignored class is discoverable from observability.
    with caplog.at_level("INFO", logger="basecradle_router.routes"):
        BasecradleRoute().normalize(_request(event="reaction.created"))
    line = _decision_line(caplog)
    assert "source=basecradle" in line
    assert "event_type=reaction.created" in line
    assert f"decision={DeliveryDecision.IGNORED.value}" in line
    # The delivery id rides a HEADER, so it is known on the ignore path too — which
    # is what lets `delivery=<id>` select even the deliveries that woke nobody (#170).
    assert f"delivery={DELIVERY}" in line


def test_normalize_logs_ignored_event_type_none_when_header_absent(caplog) -> None:
    # A delivery with no event header is still a visible ignore (event_type=<none>),
    # not a silent drop.
    with caplog.at_level("INFO", logger="basecradle_router.routes"):
        BasecradleRoute().normalize(_request(event=None))
    line = _decision_line(caplog)
    assert "event_type=<none>" in line
    assert f"decision={DeliveryDecision.IGNORED.value}" in line


# --- per-recipient verification keys (basecradle/basecradle#497) -------------
#
# One `integration_secret` per persona, not one per route. The route picks the key by
# the delivery's `recipient_uuid`, falls back to the route-wide secret while the
# cutover runs, and — once that fallback is retired — rejects a recipient it holds no
# key for. Fabricated cast: @jt and @nova, two harness personas with well-formed
# UUIDv7 user uuids and correctly-shaped fake integration secrets.

NOVA_UUID = "019e916c-7f45-700e-afc0-f45557b2aaaa"  # @nova's BaseCradle user uuid
JT_KEY = "bc_isk_fakejtintegrationsigningkey0001"
NOVA_KEY = "bc_isk_fakenovaintegrationsigningkey02"

JT_SECRET_VAR = f"{RECIPIENT_SECRET_PREFIX}JT"
NOVA_SECRET_VAR = f"{RECIPIENT_SECRET_PREFIX}NOVA"


def _persona(key: str, recipient_uuid: str) -> Agent:
    return Agent(
        key=key,
        os_user=key,
        clone_path=f"/home/{key}/harness",
        wake_kind=WakeKind.HARNESS,
        recipient_uuid=recipient_uuid,
        wake_bin=f"/home/{key}/venv/bin/basecradle-harness-wake",
    )


#: The registry's by-``recipient_uuid`` index, as ``load_recipient_keyring`` reads it.
PERSONAS = {
    JT_UUID: _persona("jt", JT_UUID),
    NOVA_UUID: _persona("nova", NOVA_UUID),
}


def _keyed_request(secret: str, *, recipient_uuid: str | None = JT_UUID) -> InboundRequest:
    """A delivery for ``recipient_uuid``, signed with ``secret``."""
    body = json.dumps(_payload(recipient_uuid=recipient_uuid)).encode("utf-8")
    return _request(raw_body=body, signature=_sign(body, secret))


def _verify_key_line(caplog) -> str:
    """The one ``event=verify_key`` line the route emitted for this delivery."""
    return next(r.getMessage() for r in caplog.records if "event=verify_key" in r.getMessage())


def _verify_key_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "event=verify_key" in r.getMessage()]


def test_a_provisioned_recipient_is_verified_with_its_own_key() -> None:
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    route.verify(_keyed_request(JT_KEY), SECRET)


def test_a_provisioned_recipient_no_longer_accepts_the_shared_secret() -> None:
    # The whole point of the change: once @jt is rotated, the value the other six
    # personas still share stops being able to speak for @jt. Without this assertion the
    # feature could be entirely inert and every other test here would still pass.
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    with pytest.raises(SignatureError, match="does not match"):
        route.verify(_keyed_request(SECRET), SECRET)


def test_one_personas_key_cannot_sign_for_another() -> None:
    # The forgery the shared secret allowed: @nova signing a delivery addressed to @jt.
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY, NOVA_UUID: NOVA_KEY}))
    with pytest.raises(SignatureError, match="does not match"):
        route.verify(_keyed_request(NOVA_KEY, recipient_uuid=JT_UUID), SECRET)


def test_an_unprovisioned_recipient_still_verifies_against_the_shared_secret() -> None:
    # The backward-compatible half: @nova has not been rotated yet, so its deliveries
    # keep verifying while @jt's already use @jt's own key. This is what lets the
    # platform rotate one persona at a time.
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    route.verify(_keyed_request(SECRET, recipient_uuid=NOVA_UUID), SECRET)


def test_an_empty_keyring_verifies_exactly_as_before() -> None:
    # The default construction — every caller that predates per-recipient keys.
    BasecradleRoute(RecipientKeyring()).verify(_keyed_request(SECRET), SECRET)
    BasecradleRoute().verify(_keyed_request(SECRET), SECRET)


def test_a_retired_fallback_rejects_a_recipient_with_no_key_of_its_own() -> None:
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}, shared_fallback=False))
    with pytest.raises(SignatureError, match="shared fallback is retired"):
        route.verify(_keyed_request(SECRET, recipient_uuid=NOVA_UUID), SECRET)


def test_a_retired_fallback_still_verifies_a_provisioned_recipient() -> None:
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}, shared_fallback=False))
    route.verify(_keyed_request(JT_KEY), SECRET)


def test_the_no_key_rejection_never_names_the_untrusted_recipient() -> None:
    # The message reaches the journal AND the evidence document, and at this point the
    # uuid is an unauthenticated claim. Naming the key path is the diagnosis; echoing
    # attacker-supplied bytes is not.
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}, shared_fallback=False))
    with pytest.raises(SignatureError) as caught:
        route.verify(_keyed_request(SECRET, recipient_uuid=NOVA_UUID), SECRET)
    assert NOVA_UUID not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        b"{not json",  # unparseable
        b"[1, 2, 3]",  # not an object
        b'{"event":"message.created"}',  # no recipient_uuid at all
        b'{"recipient_uuid": 42}',  # not a string
        b'{"recipient_uuid": "not-a-uuid"}',  # not uuid-shaped
        b'{"recipient_uuid": "019e916c-7f45-700e-afc0-f45557b237b7\\ninjected=1"}',
    ],
)
def test_a_body_that_names_no_usable_recipient_falls_back_to_the_shared_secret(
    body: bytes,
) -> None:
    # The pre-verification parse must never be able to fail *open* or crash: whatever
    # the body is, it selects no per-recipient key and the delivery is verified — and
    # for a malformed body, rejected — exactly as it is today.
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    route.verify(InboundRequest(headers={SIGNATURE_HEADER: _sign(body)}, body=body), SECRET)


def test_a_malformed_body_is_still_rejected_at_the_signature_not_as_a_bad_payload() -> None:
    # `verify` reads the body before verifying it, so it must not start answering an
    # unauthenticated caller "your JSON is bad" — that is normalize's job, after trust.
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    body = b"{not json"
    with pytest.raises(SignatureError):
        route.verify(
            InboundRequest(headers={SIGNATURE_HEADER: "sha256=deadbeef"}, body=body), SECRET
        )


# --- observability: which key path verified the delivery --------------------


def test_a_verified_delivery_logs_the_key_path_and_who_it_was_for(caplog) -> None:
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    with caplog.at_level("INFO", logger="basecradle_router.routes"):
        route.verify(_keyed_request(JT_KEY), SECRET)
    line = _verify_key_line(caplog)
    assert "source=basecradle" in line
    assert f"key_path={KEY_PATH_RECIPIENT}" in line
    assert f"recipient={JT_UUID}" in line
    # The same join key every other line of this delivery's trip carries (#170).
    assert f"delivery={DELIVERY}" in line


def test_a_fallback_verified_delivery_says_so(caplog) -> None:
    # What makes the cutover watchable: `key=fallback` is the count that must fall to
    # zero before the shared secret can be retired.
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    with caplog.at_level("INFO", logger="basecradle_router.routes"):
        route.verify(_keyed_request(SECRET, recipient_uuid=NOVA_UUID), SECRET)
    assert f"key_path={KEY_PATH_FALLBACK}" in _verify_key_line(caplog)


def test_a_rejected_delivery_logs_no_verify_key_line_but_names_the_path(caplog) -> None:
    # Before the signature checks out the recipient is a claim, not a fact — so the
    # line that asserts one is emitted only after verification. The key path still
    # reaches the operator, through the rejection reason the core logs and stores.
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    with (
        caplog.at_level("INFO", logger="basecradle_router.routes"),
        pytest.raises(SignatureError, match=f"key_path={KEY_PATH_RECIPIENT}") as caught,
    ):
        route.verify(_keyed_request(SECRET), SECRET)
    assert _verify_key_lines(caplog) == []
    assert "does not match" in str(caught.value)


def test_the_rejection_names_the_fallback_path_when_that_is_what_was_tried(caplog) -> None:
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    with pytest.raises(SignatureError, match=f"key_path={KEY_PATH_FALLBACK}"):
        route.verify(_keyed_request(JT_KEY, recipient_uuid=NOVA_UUID), SECRET)


# --- loading the keyring from the environment --------------------------------


def test_the_keyring_defaults_to_empty_with_the_fallback_on() -> None:
    # No per-recipient vars set: today's deployment, unchanged.
    keyring = load_recipient_keyring(PERSONAS, {})
    assert dict(keyring.by_recipient) == {}
    assert keyring.shared_fallback is True


def test_a_key_is_loaded_under_the_personas_uuid_not_its_slug() -> None:
    # The registry is what maps @jt's readable slug to the uuid the platform signs
    # for, so an operator never transcribes a uuid into router.env.
    keyring = load_recipient_keyring(PERSONAS, {JT_SECRET_VAR: JT_KEY})
    assert dict(keyring.by_recipient) == {JT_UUID: JT_KEY}


def test_a_slugs_hyphens_become_underscores_in_the_variable_name() -> None:
    personas = {JT_UUID: _persona("basecradle-harness", JT_UUID)}
    keyring = load_recipient_keyring(
        personas, {f"{RECIPIENT_SECRET_PREFIX}BASECRADLE_HARNESS": JT_KEY}
    )
    assert dict(keyring.by_recipient) == {JT_UUID: JT_KEY}


def test_a_slug_with_a_dot_still_yields_a_usable_variable_name() -> None:
    # The registry key `glm-5.2` is not a bare [a-z0-9-] slug, and a hyphens-only rule
    # left the dot in place: `…_WEBHOOK_SECRET_GLM_5.2` is not a name systemd passes
    # through, so the value never reached the daemon and that persona stayed on the
    # shared secret after the platform had rotated it — silent, and unreachable by
    # every guard below, because the variable simply never arrives.
    personas = {JT_UUID: _persona("glm-5.2", JT_UUID)}
    keyring = load_recipient_keyring(personas, {f"{RECIPIENT_SECRET_PREFIX}GLM_5_2": JT_KEY})
    assert dict(keyring.by_recipient) == {JT_UUID: JT_KEY}


#: systemd's own rule for an environment variable name (`env_name_is_valid`): ASCII
#: letters, digits and underscore, never leading with a digit. An assignment whose
#: name fails it is not passed from an EnvironmentFile to the service.
_SYSTEMD_ENV_NAME = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")


@pytest.mark.parametrize(
    "key",
    [
        "jt",
        "nova",
        "glm-5.2",
        "basecradle-harness",
        "5-alive",
        "a.b c",
        "persona!",
        "caf\u00e9",
        "\u00df-one",
        "x/y",
    ],
)
def test_a_derived_variable_name_is_always_one_systemd_will_pass_through(key: str) -> None:
    # The invariant the dot defect broke, pinned over the whole character space rather
    # than over the one character that happened to bite: whatever a registry key
    # contains, the name it derives is a name the daemon can actually be handed. A key
    # this refuses is a key whose secret is silently never consulted.
    var = RECIPIENT_SECRET_PREFIX + _slug_suffix(key)
    assert _SYSTEMD_ENV_NAME.fullmatch(var), var
    # And that legal name is the one the loader actually provisions the persona from.
    personas = {JT_UUID: _persona(key, JT_UUID)}
    assert dict(load_recipient_keyring(personas, {var: JT_KEY}).by_recipient) == {JT_UUID: JT_KEY}


def test_the_unknown_slug_error_names_the_normalised_variable_not_the_raw_slug() -> None:
    # An operator following the old hyphens-only rule writes `…_GLM_5.2`. On the box
    # systemd drops that name before the daemon sees it; anywhere it does arrive it is
    # loud, and the error names the spelling that works.
    personas = {JT_UUID: _persona("glm-5.2", JT_UUID)}
    with pytest.raises(ConfigError) as caught:
        load_recipient_keyring(personas, {f"{RECIPIENT_SECRET_PREFIX}GLM_5.2": JT_KEY})
    assert f"{RECIPIENT_SECRET_PREFIX}GLM_5_2" in str(caught.value)


def test_the_per_recipient_prefix_is_the_route_wide_variable_plus_a_slug() -> None:
    # Pinned so a rename of the route-wide secret cannot orphan the per-recipient keys
    # while leaving the daemon booting perfectly.
    assert RECIPIENT_SECRET_PREFIX == "BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET_"
    assert RECIPIENT_SECRET_PREFIX.startswith(route_secret_var("basecradle"))


def test_a_key_for_an_unknown_slug_is_a_loud_error() -> None:
    # The cutover's worst silent failure: a typo'd slug leaves that persona on the
    # shared secret after the platform has already rotated it, and five failed
    # deliveries auto-disable its integration.
    with pytest.raises(ConfigError, match="names no registered agent"):
        load_recipient_keyring(PERSONAS, {f"{RECIPIENT_SECRET_PREFIX}JTT": JT_KEY})


def test_the_unknown_slug_error_lists_the_slugs_that_do_exist() -> None:
    with pytest.raises(ConfigError) as caught:
        load_recipient_keyring(PERSONAS, {f"{RECIPIENT_SECRET_PREFIX}JTT": JT_KEY})
    assert JT_SECRET_VAR in str(caught.value)
    assert NOVA_SECRET_VAR in str(caught.value)


def test_an_empty_key_is_a_loud_error_never_an_empty_hmac_key() -> None:
    # "" is a perfectly usable HMAC key — one an attacker can also compute with.
    with pytest.raises(ConfigError, match="set but empty"):
        load_recipient_keyring(PERSONAS, {JT_SECRET_VAR: "   "})


@pytest.mark.parametrize("other", ["jt_one", "jt.one", "jt one"])
def test_two_slugs_colliding_on_one_variable_are_a_loud_error(other: str) -> None:
    # Scrubbing the whole character class is lossy, so more keys collapse together than
    # the hyphen rule collapsed. Each collision is boot-fatal rather than resolved: two
    # personas sharing one variable means one persona's secret verifies the other's
    # deliveries, which is the property per-recipient keys exist to remove.
    personas = {
        JT_UUID: _persona("jt-one", JT_UUID),
        NOVA_UUID: _persona(other, NOVA_UUID),
    }
    with pytest.raises(ConfigError, match="both map to"):
        load_recipient_keyring(personas, {})


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", "no", "off"])
def test_the_fallback_can_be_retired_by_flag(raw: str) -> None:
    keyring = load_recipient_keyring(
        PERSONAS, {SHARED_FALLBACK_VAR: raw, JT_SECRET_VAR: JT_KEY, NOVA_SECRET_VAR: NOVA_KEY}
    )
    assert keyring.shared_fallback is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", "", "   "])
def test_the_fallback_stays_on_for_every_truthy_or_absent_value(raw: str) -> None:
    assert load_recipient_keyring(PERSONAS, {SHARED_FALLBACK_VAR: raw}).shared_fallback is True


def test_an_unparseable_fallback_flag_is_a_loud_error() -> None:
    # A security switch must never read "flase" as "leave the fallback on".
    with pytest.raises(ConfigError, match=SHARED_FALLBACK_VAR):
        load_recipient_keyring(PERSONAS, {SHARED_FALLBACK_VAR: "flase"})


def test_retiring_the_fallback_with_an_unprovisioned_persona_refuses_to_boot() -> None:
    # The combination that would make a persona permanently unreachable while the box
    # looked perfectly healthy — the green-while-absent shape this repo instruments
    # everywhere else. It is caught at boot, by name.
    with pytest.raises(ConfigError, match="nova") as caught:
        load_recipient_keyring(PERSONAS, {SHARED_FALLBACK_VAR: "0", JT_SECRET_VAR: JT_KEY})
    assert SHARED_FALLBACK_VAR in str(caught.value)


def test_retiring_the_fallback_is_accepted_once_every_persona_is_provisioned() -> None:
    keyring = load_recipient_keyring(
        PERSONAS, {SHARED_FALLBACK_VAR: "0", JT_SECRET_VAR: JT_KEY, NOVA_SECRET_VAR: NOVA_KEY}
    )
    assert keyring.shared_fallback is False
    assert dict(keyring.by_recipient) == {JT_UUID: JT_KEY, NOVA_UUID: NOVA_KEY}


def test_the_loader_ignores_environment_it_does_not_own() -> None:
    keyring = load_recipient_keyring(
        PERSONAS, {"PATH": "/usr/bin", "BASECRADLE_ROUTER_BASECRADLE_WEBHOOK_SECRET": SECRET}
    )
    assert dict(keyring.by_recipient) == {}


# --- the daemon states its key config at boot --------------------------------


def test_the_route_states_its_keyring_at_boot() -> None:
    # Once every persona is keyed, an armed fallback and a retired one produce
    # identical traffic — every delivery reads key=recipient either way. Nothing but
    # a boot statement can tell a finished cutover from a forgotten last step.
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}, shared_fallback=False))
    summary = route.boot_summary()
    assert "recipient_keys=1" in summary
    assert "shared_fallback=false" in summary


def test_the_boot_statement_names_an_armed_fallback() -> None:
    summary = BasecradleRoute().boot_summary()
    assert "recipient_keys=0" in summary
    # Rendered lowercase, never Python's `True` — a log value a query filters on
    # literally (the log_fields contract).
    assert "shared_fallback=true" in summary


def test_a_hostile_body_that_breaks_the_decoder_falls_back_rather_than_escaping() -> None:
    # Deeply-nested JSON raises RecursionError, not a JSONDecodeError — and this parse
    # runs on unauthenticated input, ahead of the signature check. It must degrade to
    # "no per-recipient key", never propagate out of the verify boundary.
    body = b"[" * 60_000 + b"]" * 60_000
    route = BasecradleRoute(RecipientKeyring({JT_UUID: JT_KEY}))
    with pytest.raises(SignatureError):
        route.verify(InboundRequest(headers={SIGNATURE_HEADER: "sha256=00"}, body=body), SECRET)
