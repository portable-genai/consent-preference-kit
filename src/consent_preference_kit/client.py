"""The consent client: ask a consent and preference store whether contact is permitted, S2S-authed.

Wire-compatible with ``hex-service-kit``'s server verifier and with ``review-kit``'s
client, deliberately: the https-only base-URL guard, the three-state credential resolution and
the bearer / HMAC-signed-actor headers are the same rules, inlined for the same reason. A leaf
commons that consumers pin cannot itself pull another git+https commons, because the nested
tag-vs-SHA direct-URL reference conflicts at install time with the consumer's own lockfile pin
of that commons. So this kit has ZERO runtime dependencies, exactly like ``pii-kit`` and
``review-kit``.

The HTTP transport is pluggable, so the client is unit-testable offline with no live store.

Fail closed, on this side of the wire too
-----------------------------------------
The store fails closed on unknown consent state. This client fails closed on unknown
TRANSPORT state, which is the failure mode the store cannot see:

* :meth:`ConsentClient.decide` raises on an unreachable or non-2xx store, so a caller that
  forgot to handle the error cannot proceed on a decision it never received;
* :meth:`ConsentClient.decide_or_deny` is the explicit alternative for callers that want to
  keep running: it returns a real, locally-synthesised DENIED decision naming
  ``client_unavailable``, never an allow; and
* :meth:`ConsentClient.require_allowed` raises :class:`ConsentDeniedError` unless the decision
  permits contact, so a caller cannot forget to check a boolean and have the omission read as
  permission.

There is no code path in this module that produces ``outcome == "allowed"`` from anything
other than the store saying so.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlparse

from .models import ConsentDecision, ConsentQuery, SendRecord

#: A transport is (url, body, headers, timeout) -> parsed JSON dict. Injectable for testing.
Transport = Callable[[str, bytes, Mapping[str, str], float], dict[str, Any]]
BearerTokenProvider = Callable[[], str]

_DECISION_PATH = "/v1/service/consent/decision"
_SEND_PATH = "/v1/service/consent/sends"
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# The S2S actor headers, kept byte-for-byte compatible with hex-service-kit's server verifier
# (``hex_service_kit.web.make_require_service_caller``) so the store accepts what this sends.
_ACTOR_HEADER = "X-S2S-Actor"
_ACTOR_SIG_HEADER = "X-S2S-Actor-Sig"


def _is_loopback(url: str) -> bool:
    """Is this store on the local machine, and therefore the zero-secret dev posture?"""
    return (urlparse(url).hostname or "") in _LOOPBACK_HOSTS


def _validate_base_url(url: str, *, service: str) -> str:
    """Return ``url`` without a trailing slash; refuse plaintext outside loopback (https-only).

    Consent answers say what may be done to a named person and the questions name that person's
    subject id, so this one is not merely hygiene: plaintext would put both on the wire.
    """
    stripped = url.rstrip("/")
    parsed = urlparse(stripped)
    host = parsed.hostname or ""
    if parsed.scheme == "https":
        return stripped
    if parsed.scheme == "http" and host in _LOOPBACK_HOSTS:
        return stripped
    raise ValueError(f"{service} base URL must be https outside loopback (got {url!r})")


def _resolve_secret(env_name: str, *, required: bool, purpose: str) -> str:
    """Resolve one credential in THREE states, where unset is not a member of the valid set.

    * unset: absent from the environment. Refused when ``required``; otherwise "" (the feature
      it enables is simply not used), because an absent variable is a state of its own and
      never a stand-in for a configured value.
    * set and blank (``""`` or whitespace): ALWAYS refused. A blank credential is a value
      someone believes they configured, and treating it as configured is how an empty bearer
      gets sent as ``Authorization: Bearer`` or a blank, publicly guessable key gets used to
      HMAC an actor assertion that then looks authenticated.
    * set and non-blank: returned stripped, exactly as ``hex_service_kit.s2s.client_headers``
      does, so the two stay wire-identical.

    This is the ONLY place in the package that reads the environment
    (``tests/test_env_single_source.py`` fails the build if a second reader appears).
    """
    raw = os.environ.get(env_name)
    if raw is None:
        if required:
            raise ValueError(
                f"{env_name} is not set, so {purpose} is unconfigured. An absent credential is "
                "not consent to ask unauthenticated: set it, or point the client at a loopback "
                "store for the offline zero-secret posture."
            )
        return ""
    value = raw.strip()
    if not value:
        raise ValueError(
            f"{env_name} is set but blank, which is refused rather than read as a configured "
            f"value for {purpose}. Unset it, or give it the real credential."
        )
    return value


def _client_headers(actor: str, *, token: str, signing_key: str) -> dict[str, str]:
    """Auth headers for one outbound S2S request: a bearer token, and an HMAC-signed actor.

    Both credentials arrive already resolved by :func:`_resolve_secret`, so a blank one cannot
    reach here. With no signing key the actor pair is OMITTED rather than sent unsigned: an
    unsigned assertion of who is asking is worth less than no assertion, and the receiving
    store reads the S2S caller, not this header, as the trust anchor today.
    """
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if actor and signing_key:
        signature = hmac.new(
            signing_key.encode("utf-8"), actor.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        headers[_ACTOR_HEADER] = actor
        headers[_ACTOR_SIG_HEADER] = signature
    return headers


class ConsentClientError(RuntimeError):
    """Raised when a consent call fails (non-2xx response, unreachable store, bad payload)."""


class ConsentDeniedError(RuntimeError):
    """Raised by :meth:`ConsentClient.require_allowed` when contact is not permitted.

    Carries the decision, so the caller's audit record can name the id and the reasons rather
    than logging "denied" with nothing to reconcile against later.
    """

    def __init__(self, decision: ConsentDecision) -> None:
        self.decision = decision
        reasons = ", ".join(decision.denying_reasons) or decision.outcome or "no outcome"
        super().__init__(f"consent refused for {decision.subject_id} ({reasons})")


def _urllib_transport(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> dict[str, Any]:  # pragma: no cover - exercised against a live server, not the offline gate
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return payload
    except urllib.error.HTTPError as exc:
        raise ConsentClientError(f"consent store returned {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise ConsentClientError(f"consent store unreachable: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ConsentClientError(f"consent store returned a non-JSON body: {exc}") from exc


class ConsentClient:
    """Ask a consent and preference store at ``base_url`` whether contact is permitted."""

    def __init__(
        self,
        base_url: str,
        *,
        service: str = "consent-preference-store",
        token_env: str = "CONSENT_S2S_TOKEN",
        signing_key_env: str = "CONSENT_S2S_SIGNING_KEY",
        token_provider: BearerTokenProvider | None = None,
        timeout: float = 10.0,
        transport: Transport | None = None,
    ) -> None:
        # https-only outside loopback; a plaintext non-loopback URL is refused at construction.
        self._base = _validate_base_url(base_url, service=service)
        self._token_env = token_env
        self._signing_key_env = signing_key_env
        self._token_provider = token_provider
        # A store anywhere but this machine is reachable by something other than this process,
        # so it needs a bearer. Refusing HERE, beside the transport guard, turns a
        # misconfigured consumer into a construction error rather than a consent question that
        # silently leaves unauthenticated and is rejected (or, on a misconfigured store,
        # answered) at the far end.
        self._token_required = not _is_loopback(self._base)
        if token_provider is None:
            self._resolve_credentials()
        else:
            # Validate the optional actor-signing posture without invoking a managed token
            # provider at construction. Workload-identity SDK imports and metadata access stay
            # lazy until an actual request is made.
            _resolve_secret(
                self._signing_key_env,
                required=False,
                purpose="signed-actor propagation",
            )
        self._timeout = timeout
        self._transport: Transport = transport or _urllib_transport

    def _resolve_credentials(self) -> tuple[str, str]:
        """Resolve the bearer and signing key, refusing an unset or blank one where it matters.

        Re-read on every call rather than cached at construction, so a credential cleared or
        blanked after start-up cannot leave a long-lived client silently downgraded.
        """
        if self._token_provider is None:
            token = _resolve_secret(
                self._token_env,
                required=self._token_required,
                purpose="the consent store service bearer",
            )
        else:
            try:
                token = self._token_provider().strip()
            except Exception as exc:
                raise ConsentClientError(
                    f"the consent store bearer provider failed: {exc}"
                ) from exc
            if not token:
                raise ConsentClientError(
                    "the consent store bearer provider returned an empty token; refusing "
                    "rather than asking the store unauthenticated"
                )
        signing_key = _resolve_secret(
            self._signing_key_env,
            required=False,
            purpose="signed-actor propagation",
        )
        return token, signing_key

    def _post(self, path: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        token, signing_key = self._resolve_credentials()
        headers = {
            "Content-Type": "application/json",
            **_client_headers(actor, token=token, signing_key=signing_key),
        }
        body = json.dumps(payload).encode("utf-8")
        return self._transport(self._base + path, body, headers, self._timeout)

    # ------------------------------------------------------------------ #
    # The decision
    # ------------------------------------------------------------------ #
    def decide(self, query: ConsentQuery, *, actor: str = "") -> ConsentDecision:
        """Ask one consent question. Raises on a store that cannot answer.

        Raising rather than returning a denial is the right default here: a caller that has not
        thought about the failure path should stop, not proceed on a decision it never got. Use
        :meth:`decide_or_deny` when the caller has thought about it and wants to keep running.
        """
        data = self._post(_DECISION_PATH, query.to_payload(), actor)
        try:
            return ConsentDecision.from_payload(data, query)
        except ValueError as exc:
            raise ConsentClientError(f"malformed consent decision: {exc}") from exc

    def decide_or_deny(self, query: ConsentQuery, *, actor: str = "") -> ConsentDecision:
        """Ask one consent question, answering DENIED when the store cannot be reached.

        The synthesised decision names ``client_unavailable`` and is a real decision object, so
        the refusal lands in the caller's audit trail with a reason rather than disappearing.
        It can never be an allow: :meth:`ConsentDecision.unavailable` is the only constructor
        used on this path and it writes the denied outcome unconditionally.
        """
        try:
            return self.decide(query, actor=actor)
        except ConsentClientError as exc:
            return ConsentDecision.unavailable(query, str(exc))

    @staticmethod
    def require_allowed(decision: ConsentDecision) -> ConsentDecision:
        """Return the decision if it permits contact, else raise :class:`ConsentDeniedError`.

        For callers who would rather not carry a boolean they can forget to check. The reasons
        travel on the exception, so the refusal is still fully explainable at the catch site.
        """
        if not decision.allowed:
            raise ConsentDeniedError(decision)
        return decision

    # ------------------------------------------------------------------ #
    # The send record (what a frequency cap counts)
    # ------------------------------------------------------------------ #
    def record_send(self, send: SendRecord, *, actor: str = "") -> str:
        """Record one contact so the store's frequency caps count it. Returns the send id.

        Call this AFTER the message actually goes out, quoting the decision id that permitted
        it. A consumer that decides but never records will pass a cap forever, because a cap
        counts recorded sends and nothing else.
        """
        data = self._post(_SEND_PATH, send.to_payload(), actor)
        send_id = str((data or {}).get("send_id", "") or "")
        if not send_id:
            raise ConsentClientError(f"malformed send-record response: {data!r}")
        return send_id
