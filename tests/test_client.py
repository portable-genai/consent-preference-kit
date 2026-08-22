"""The client's happy path: what goes on the wire, and what comes back off it."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from consent_preference_kit import ConsentClient, ConsentQuery, SendRecord

LOOPBACK = "http://localhost:8105"

_ALLOWED_PAYLOAD: dict[str, Any] = {
    "id": "consent-abc123",
    "tenant": "demo-brand",
    "subject_id": "subj-000101",
    "purpose": "marketing",
    "channel": "email",
    "market": "SG",
    "vertical": "banking",
    "as_of": "2026-08-08T09:00:00+00:00",
    "outcome": "allowed",
    "reasons": ["consent_granted", "channel_opted_in", "within_frequency_cap"],
    "explanation": "Contact permitted for marketing on email (SG).",
    "cap_limit": 3,
    "sends_in_window": 1,
    "citations": [
        {
            "source_id": "SG-BANK-CONSENT-PDPA",
            "title": "Synthetic marketing-consent rule (FICTIONAL)",
            "snippet": "",
        }
    ],
}


class _RecordingTransport:
    """A fake transport that records the call and replays a canned payload."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def __call__(
        self, url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> dict[str, Any]:
        self.calls.append((url, json.loads(body.decode("utf-8")), dict(headers)))
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload  # type: ignore[no-any-return]


def _query(**overrides: Any) -> ConsentQuery:
    base = {
        "tenant": "demo-brand",
        "subject_id": "subj-000101",
        "purpose": "marketing",
        "channel": "email",
        "market": "SG",
        "vertical": "banking",
        "as_of": "2026-08-08T09:00:00+00:00",
    }
    base.update(overrides)
    return ConsentQuery(**base)  # type: ignore[arg-type]


def test_a_permitted_decision_is_parsed_in_full() -> None:
    transport = _RecordingTransport(_ALLOWED_PAYLOAD)
    client = ConsentClient(LOOPBACK, transport=transport)

    decision = client.decide(_query())

    assert decision.allowed
    assert decision.id == "consent-abc123"
    assert decision.denying_reasons == ()
    assert decision.unknown_reasons == ()
    assert decision.cap_limit == 3
    assert decision.sends_in_window == 1
    assert [c.source_id for c in decision.citations] == ["SG-BANK-CONSENT-PDPA"]


def test_the_question_goes_to_the_service_intake_with_the_asserted_tenant() -> None:
    transport = _RecordingTransport(_ALLOWED_PAYLOAD)
    ConsentClient(LOOPBACK, transport=transport).decide(_query())

    url, body, headers = transport.calls[0]
    assert url == f"{LOOPBACK}/v1/service/consent/decision"
    assert body["tenant"] == "demo-brand"
    assert body["subject_id"] == "subj-000101"
    assert body["as_of"] == "2026-08-08T09:00:00+00:00"
    assert headers["Content-Type"] == "application/json"


def test_require_allowed_returns_the_decision_when_contact_is_permitted() -> None:
    client = ConsentClient(LOOPBACK, transport=_RecordingTransport(_ALLOWED_PAYLOAD))
    decision = client.decide(_query())
    assert client.require_allowed(decision) is decision


def test_a_send_is_recorded_against_the_decision_that_permitted_it() -> None:
    transport = _RecordingTransport({"send_id": "se-0001"})
    client = ConsentClient(LOOPBACK, transport=transport)

    send_id = client.record_send(
        SendRecord(
            id="se-0001",
            tenant="demo-brand",
            subject_id="subj-000101",
            channel="email",
            purpose="marketing",
            decision_id="consent-abc123",
        )
    )

    url, body, _ = transport.calls[0]
    assert send_id == "se-0001"
    assert url == f"{LOOPBACK}/v1/service/consent/sends"
    assert body["decision_id"] == "consent-abc123"


def test_a_signed_actor_is_sent_only_when_a_signing_key_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsigned assertion of who is asking is worth less than no assertion at all."""
    transport = _RecordingTransport(_ALLOWED_PAYLOAD)
    client = ConsentClient(LOOPBACK, transport=transport)

    client.decide(_query(), actor="e5-outreach")
    assert "X-S2S-Actor" not in transport.calls[-1][2]

    monkeypatch.setenv("CONSENT_S2S_SIGNING_KEY", "fictional-signing-key")
    client.decide(_query(), actor="e5-outreach")
    headers = transport.calls[-1][2]
    assert headers["X-S2S-Actor"] == "e5-outreach"
    assert headers["X-S2S-Actor-Sig"]


def test_a_plaintext_store_off_loopback_is_refused_at_construction() -> None:
    """Consent questions name a person's subject id; plaintext would put it on the wire."""
    with pytest.raises(ValueError, match="https"):
        ConsentClient("http://consent.example.test")
