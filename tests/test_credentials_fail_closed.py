"""Credentials resolve in THREE states, and unset is not a member of the valid set.

The same rule ``review-kit`` carries, for the same reason: a bare
``os.environ.get(name, "")`` sees only two states and answers both with the literal, so a
variable an operator SET to nothing reads as configured and an empty bearer goes on the wire.
Here the stakes are a question about a named person, asked unauthenticated.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from consent_preference_kit import ConsentClient, ConsentClientError, ConsentQuery

LOOPBACK = "http://localhost:8105"
REMOTE = "https://consent.example.test"

TOKEN_ENV = "CONSENT_S2S_TOKEN"
KEY_ENV = "CONSENT_S2S_SIGNING_KEY"


def _transport(payload: dict[str, Any]) -> Any:
    def transport(
        url: str, body: bytes, headers: Mapping[str, str], timeout: float
    ) -> dict[str, Any]:
        transport.headers = dict(headers)  # type: ignore[attr-defined]
        return payload

    return transport


def _denial() -> dict[str, Any]:
    return {
        "id": "consent-1",
        "tenant": "demo-brand",
        "subject_id": "subj-000101",
        "purpose": "marketing",
        "channel": "email",
        "market": "SG",
        "vertical": "banking",
        "outcome": "denied",
    }


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    monkeypatch.delenv(KEY_ENV, raising=False)


def test_a_remote_store_with_no_token_is_refused_at_construction() -> None:
    """An absent credential is not consent to ask unauthenticated."""
    with pytest.raises(ValueError, match=TOKEN_ENV):
        ConsentClient(REMOTE)


def test_a_blank_token_is_refused_rather_than_read_as_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TOKEN_ENV, "   ")
    with pytest.raises(ValueError, match="blank"):
        ConsentClient(REMOTE)


def test_a_configured_token_is_sent_as_a_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TOKEN_ENV, "  fictional-token  ")
    transport = _transport(_denial())
    client = ConsentClient(REMOTE, transport=transport)
    client.decide(ConsentQuery(tenant="demo-brand", subject_id="subj-000101"))
    assert transport.headers["Authorization"] == "Bearer fictional-token"  # type: ignore[attr-defined]


def test_an_injected_managed_token_provider_is_lazy_and_re_read_per_call() -> None:
    issued = iter(("managed-id-token-1", "managed-id-token-2"))
    calls = 0

    def provide() -> str:
        nonlocal calls
        calls += 1
        return next(issued)

    transport = _transport(_denial())
    client = ConsentClient(REMOTE, token_provider=provide, transport=transport)
    assert calls == 0

    client.decide(ConsentQuery(tenant="demo-brand", subject_id="subj-000101"))
    assert transport.headers["Authorization"] == "Bearer managed-id-token-1"  # type: ignore[attr-defined]
    client.decide(ConsentQuery(tenant="demo-brand", subject_id="subj-000101"))
    assert transport.headers["Authorization"] == "Bearer managed-id-token-2"  # type: ignore[attr-defined]
    assert calls == 2


@pytest.mark.parametrize("provided", ["", "   "])
def test_an_injected_token_provider_must_return_a_nonblank_bearer(provided: str) -> None:
    client = ConsentClient(REMOTE, token_provider=lambda: provided, transport=_transport(_denial()))

    with pytest.raises(ConsentClientError, match="provider returned an empty token"):
        client.decide(ConsentQuery(tenant="demo-brand", subject_id="subj-000101"))


@pytest.mark.parametrize(
    "provider",
    [lambda: "", lambda: (_ for _ in ()).throw(RuntimeError("metadata unavailable"))],
)
def test_decide_or_deny_normalizes_managed_provider_failure(
    provider: Callable[[], str],
) -> None:
    client = ConsentClient(REMOTE, token_provider=provider, transport=_transport(_denial()))

    decision = client.decide_or_deny(ConsentQuery(tenant="demo-brand", subject_id="subj-000101"))

    assert not decision.allowed
    assert decision.denying_reasons == ("client_unavailable",)


def test_a_loopback_store_runs_with_no_secret_at_all() -> None:
    """The zero-secret offline posture, the same carve-out the base-URL guard already makes."""
    transport = _transport(_denial())
    client = ConsentClient(LOOPBACK, transport=transport)
    client.decide(ConsentQuery(tenant="demo-brand", subject_id="subj-000101"))
    assert "Authorization" not in transport.headers  # type: ignore[attr-defined]


def test_a_blank_signing_key_is_refused_even_though_the_key_is_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank HMAC key is a key everyone knows, so it is never read as configured."""
    monkeypatch.setenv(KEY_ENV, "")
    with pytest.raises(ValueError, match="blank"):
        ConsentClient(LOOPBACK)


def test_a_token_cleared_after_construction_is_noticed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials are re-read per call, so a long-lived client cannot be silently downgraded."""
    monkeypatch.setenv(TOKEN_ENV, "fictional-token")
    client = ConsentClient(REMOTE, transport=_transport({"id": "consent-1", "outcome": "denied"}))
    monkeypatch.delenv(TOKEN_ENV)
    with pytest.raises(ValueError, match=TOKEN_ENV):
        client.decide(ConsentQuery(tenant="demo-brand", subject_id="subj-000101"))
