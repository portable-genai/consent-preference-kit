"""The one property this kit exists to guarantee: nothing here invents an allow.

The store fails closed on unknown CONSENT state. This kit fails closed on unknown WIRE state,
which is the failure mode the store cannot see: a truncated response, an outcome token from a
newer server, a store that is simply down. Each test below is a shape that a naive client would
turn into permission to contact a person.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from consent_preference_kit import (
    ConsentClient,
    ConsentClientError,
    ConsentDecision,
    ConsentDeniedError,
    ConsentQuery,
)

LOOPBACK = "http://localhost:8105"

QUERY = ConsentQuery(tenant="demo-brand", subject_id="subj-000101")


def _decision_payload(**overrides: Any) -> dict[str, Any]:
    payload = {**QUERY.to_payload(), "id": "consent-1", "outcome": "denied"}
    payload.update(overrides)
    return payload


def _client(payload: Any) -> ConsentClient:
    def transport(
        url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> dict[str, Any]:
        if isinstance(payload, Exception):
            raise payload
        return payload  # type: ignore[no-any-return]

    return ConsentClient(LOOPBACK, transport=transport)


# --------------------------------------------------------------------------- #
# Only the exact allow token is an allow
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param("denied", id="the-ordinary-refusal"),
        pytest.param("", id="empty-string"),
        pytest.param("Allowed", id="wrong-case"),
        pytest.param("allow", id="near-miss"),
        pytest.param("allowed_with_conditions", id="token-from-a-newer-store"),
        pytest.param("pending", id="a-state-this-kit-has-never-heard-of"),
    ],
)
def test_only_the_exact_allow_token_permits_contact(outcome: str) -> None:
    decision = _client(_decision_payload(outcome=outcome)).decide(QUERY)
    assert not decision.allowed


def test_a_missing_outcome_is_not_an_allow() -> None:
    """A truncated response must never be read as permission."""
    payload = _decision_payload()
    payload.pop("outcome")
    decision = _client(payload).decide(QUERY)
    assert decision.outcome == ""
    assert not decision.allowed


def test_a_payload_that_is_not_a_decision_raises_rather_than_being_coerced() -> None:
    with pytest.raises(ConsentClientError, match="malformed"):
        _client({"outcome": "allowed"}).decide(QUERY)  # no id: not a decision
    with pytest.raises(ConsentClientError, match="malformed"):
        _client(["allowed"]).decide(QUERY)


@pytest.mark.parametrize(
    "missing", ["tenant", "subject_id", "purpose", "channel", "market", "vertical"]
)
def test_a_decision_must_bind_every_security_scope_field(missing: str) -> None:
    payload = _decision_payload(outcome="allowed", reasons=["consent_granted"])
    payload.pop(missing)

    with pytest.raises(ConsentClientError, match=missing):
        _client(payload).decide(QUERY)


def test_a_decision_bound_to_a_different_question_is_malformed() -> None:
    payload = _decision_payload(outcome="allowed", tenant="different-tenant")

    with pytest.raises(ConsentClientError, match="does not match.*tenant"):
        _client(payload).decide(QUERY)


# --------------------------------------------------------------------------- #
# Unknown reasons count as refusals
# --------------------------------------------------------------------------- #
def test_a_reason_this_kit_does_not_know_counts_as_a_denial() -> None:
    """A reason a newer store added is far more likely a new refusal than a pleasantry."""
    decision = _client(
        _decision_payload(
            outcome="denied",
            reasons=["quiet_hours_active", "consent_granted"],
        )
    ).decide(QUERY)
    assert decision.denying_reasons == ("quiet_hours_active",)
    assert decision.unknown_reasons == ("quiet_hours_active",)


def test_a_known_informational_reason_is_not_counted_as_a_denial() -> None:
    """The guard above is only meaningful if the vocabulary it knows is actually used."""
    decision = _client(
        _decision_payload(
            outcome="allowed",
            reasons=["consent_granted", "no_frequency_cap_configured"],
        )
    ).decide(QUERY)
    assert decision.allowed
    assert decision.denying_reasons == ()


# --------------------------------------------------------------------------- #
# A store that cannot answer
# --------------------------------------------------------------------------- #
def test_decide_raises_when_the_store_cannot_be_reached() -> None:
    """The default stops the caller rather than letting it proceed on nothing."""
    with pytest.raises(ConsentClientError):
        _client(ConsentClientError("consent store unreachable: timed out")).decide(QUERY)


def test_decide_or_deny_answers_denied_rather_than_allowed() -> None:
    decision = _client(ConsentClientError("consent store unreachable: timed out")).decide_or_deny(
        QUERY
    )
    assert not decision.allowed
    assert decision.outcome == "denied"
    assert decision.denying_reasons == ("client_unavailable",)
    assert decision.subject_id == "subj-000101"
    assert "could not be reached" in decision.explanation


def test_the_unavailable_decision_can_never_be_an_allow() -> None:
    """Walked directly, because this is the one decision this kit constructs itself."""
    synthesised = ConsentDecision.unavailable(QUERY, "connection refused")
    assert not synthesised.allowed
    assert synthesised.id == "consent-client-unavailable"


def test_require_allowed_raises_and_carries_the_reasons() -> None:
    decision = _client(_decision_payload(outcome="denied", reasons=["consent_withdrawn"])).decide(
        QUERY
    )
    with pytest.raises(ConsentDeniedError) as excinfo:
        ConsentClient.require_allowed(decision)
    assert excinfo.value.decision is decision
    assert "consent_withdrawn" in str(excinfo.value)


def test_a_malformed_send_response_raises_rather_than_reporting_success() -> None:
    """A send the store did not record is a cap that will never fire; do not report it saved."""
    from consent_preference_kit import SendRecord

    with pytest.raises(ConsentClientError, match="malformed"):
        _client({}).record_send(
            SendRecord(id="se-1", tenant="demo-brand", subject_id="subj-000101")
        )


# --------------------------------------------------------------------------- #
# The guard on the guard
# --------------------------------------------------------------------------- #
def test_no_source_line_defaults_an_outcome_to_the_allow_token() -> None:
    """A literal ``"allowed"`` fallback is the exact shape that would break every test above.

    Written as a source check rather than a behaviour check on purpose: the behaviour tests
    above enumerate the shapes we thought of, and this one fails on a shape we did not.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "consent_preference_kit"
    # A default that yields the allow token: ``get("outcome", "allowed")``, ``or "allowed"``,
    # ``or OUTCOME_ALLOWED`` and friends.
    pattern = re.compile(r"(,\s*|or\s+)(\"allowed\"|'allowed'|OUTCOME_ALLOWED)")
    offenders = [
        f"{path.name}:{number}: {line.strip()}"
        for path in sorted(src.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if pattern.search(line) and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "these lines can produce the allow token from an absent or falsy value, which is how "
        "a truncated response becomes permission to contact a person:\n" + "\n".join(offenders)
    )
