"""The consent wire types: what a consumer asks, and what the store answers.

Domain-neutral value objects plus the vocabulary the decision is expressed in. They mirror the
shapes the consent and preference store serves at ``/v1/service/consent/...``, so a consumer
depends on this kit rather than on the store's internal domain module.

The parsing here is where this kit earns its keep. A consent answer is a legal position about
a person, and the one thing a client must never do is turn a non-allow into an allow. So
:meth:`ConsentDecision.from_payload` treats the wire as untrusted:

* ``allowed`` is True only when ``outcome`` is exactly ``"allowed"``. A missing field, an
  empty string, a value from a newer server, a typo: every one of them is not an allow.
* a reason string this kit does not recognise counts as DENYING, not as informational. A
  reason a newer store added is far more likely to be a new refusal than a new pleasantry, and
  reading an unknown token as harmless is the same absence-read-as-consent shape the store
  itself refuses.

Zero runtime dependencies (pure standard library).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Vocabulary (plain strings on the wire; the sets below are what this kit knows)
# --------------------------------------------------------------------------- #
#: The ONE outcome string that means contact is permitted. Everything else is a refusal.
OUTCOME_ALLOWED = "allowed"
OUTCOME_DENIED = "denied"

#: Contact channels the store scopes preferences, caps, suppressions and sends to.
CHANNELS: frozenset[str] = frozenset(
    {"email", "sms", "push", "voice", "chat", "post"},
)

#: The reasons this kit knows to be REFUSALS. Anything outside :data:`KNOWN_REASONS` is
#: treated as a refusal too (see :meth:`ConsentDecision.denying_reasons`), so this set does
#: not have to be exhaustive to stay safe; it only has to be correct about what it lists.
DENYING_REASONS: frozenset[str] = frozenset(
    {
        "tenant_unresolved",
        "subject_unresolved",
        "snapshot_tenant_mismatch",
        "purpose_unresolved",
        "consent_unknown",
        "consent_withdrawn",
        "consent_expired",
        "consent_not_yet_effective",
        "consent_pending_review",
        "suppressed",
        "channel_preference_unknown",
        "channel_opted_out",
        "frequency_cap_exceeded",
        "market_consent_rule_unsatisfied",
        # Raised by this kit, never by the store: the decision could not be obtained at all.
        "client_unavailable",
    }
)

#: The reasons this kit knows to be INFORMATIONAL: they appear on a permitted decision and
#: describe what was checked. The union with :data:`DENYING_REASONS` is what "known" means.
INFORMATIONAL_REASONS: frozenset[str] = frozenset(
    {
        "consent_granted",
        "channel_opted_in",
        "within_frequency_cap",
        "no_frequency_cap_configured",
        "market_consent_rules_satisfied",
        "no_market_consent_rules",
    }
)

KNOWN_REASONS: frozenset[str] = DENYING_REASONS | INFORMATIONAL_REASONS

#: This kit's own reason for a decision it had to synthesise because the store was unreachable.
REASON_CLIENT_UNAVAILABLE = "client_unavailable"


@dataclass(frozen=True, slots=True)
class Citation:
    """Provenance for the market consent rule a decision applied."""

    source_id: str
    title: str = ""
    snippet: str = ""


@dataclass(frozen=True, slots=True)
class ConsentQuery:
    """The question: may this tenant contact this subject, for this purpose, on this channel?

    ``tenant`` is asserted by the calling service and trusted because the caller is an
    authenticated S2S caller (the store verifies the caller, not an end user, on this path;
    per-hop OBO token exchange is the deferred next layer). It bounds what the store will read:
    an asserted tenant names WHO is calling, never grants a view of another tenant's subjects.

    ``as_of`` pins the instant the decision is made against. Leave it empty to decide against
    now; set it to reproduce a past decision exactly.
    """

    tenant: str
    subject_id: str
    purpose: str = "marketing"
    channel: str = "email"
    market: str = "SG"
    vertical: str = "banking"
    as_of: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "subject_id": self.subject_id,
            "purpose": self.purpose,
            "channel": self.channel,
            "market": self.market,
            "vertical": self.vertical,
            "as_of": self.as_of,
        }


@dataclass(frozen=True, slots=True)
class ConsentDecision:
    """The store's cited, replayable answer, parsed defensively.

    ``id`` is a content hash of the question and the answer: quote it on the send you make so
    the message reconciles against a replay of the store months later.
    """

    id: str
    tenant: str
    subject_id: str
    purpose: str
    channel: str
    outcome: str
    reasons: tuple[str, ...] = ()
    market: str = ""
    vertical: str = ""
    as_of: str = ""
    explanation: str = ""
    cap_limit: int | None = None
    sends_in_window: int = 0
    citations: tuple[Citation, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        """True ONLY for the exact allow outcome. Everything else is a refusal.

        Deliberately not ``outcome != "denied"``: that shape reads a value this kit has never
        heard of, a typo, or a truncated response as permission to contact a person.
        """
        return self.outcome == OUTCOME_ALLOWED

    @property
    def denying_reasons(self) -> tuple[str, ...]:
        """The reasons that refuse, INCLUDING any reason this kit does not recognise.

        An unknown token is counted as a refusal on purpose. A store newer than this kit is
        far more likely to have added a refusal than a pleasantry, and a consumer that logs
        "denied, but for no reason I can name" is safe, whereas one that quietly drops the
        unknown reason and shows a clean allow is not.
        """
        return tuple(r for r in self.reasons if r not in INFORMATIONAL_REASONS)

    @property
    def unknown_reasons(self) -> tuple[str, ...]:
        """Reason tokens outside this kit's vocabulary: the signal to upgrade the pin."""
        return tuple(r for r in self.reasons if r not in KNOWN_REASONS)

    @classmethod
    def from_payload(cls, data: Any, query: ConsentQuery | None = None) -> ConsentDecision:
        """Parse a decision from the wire, failing closed on anything unexpected.

        A payload that is not a mapping, or that carries no decision id, is not a decision, so
        it raises rather than being coerced into one. Everything else is read leniently but
        never permissively: an absent ``outcome`` becomes the empty string, which
        :attr:`allowed` refuses, rather than defaulting to the allow token.
        """
        if not isinstance(data, dict):
            raise ValueError(f"consent decision payload is not an object: {data!r}")
        decision_id = str(data.get("id", "") or "")
        if not decision_id:
            raise ValueError(f"consent decision payload carries no id: {data!r}")
        scope_fields = ("tenant", "subject_id", "purpose", "channel", "market", "vertical")
        scope = {name: str(data.get(name, "") or "") for name in scope_fields}
        missing_scope = tuple(name for name, value in scope.items() if not value)
        if missing_scope:
            raise ValueError(
                "consent decision payload does not bind the answer to "
                f"{', '.join(missing_scope)}: {data!r}"
            )
        if query is not None:
            mismatches = tuple(
                name for name, value in scope.items() if value != str(getattr(query, name))
            )
            if mismatches:
                raise ValueError(
                    "consent decision payload does not match the question for "
                    f"{', '.join(mismatches)}: {data!r}"
                )
            response_as_of = str(data.get("as_of", "") or "")
            if query.as_of and response_as_of != query.as_of:
                raise ValueError(
                    f"consent decision payload does not match the question for as_of: {data!r}"
                )
        reasons = tuple(str(r) for r in (data.get("reasons") or ()) if str(r))
        citations = tuple(
            Citation(
                source_id=str(c.get("source_id", "") or ""),
                title=str(c.get("title", "") or ""),
                snippet=str(c.get("snippet", "") or ""),
            )
            for c in (data.get("citations") or ())
            if isinstance(c, dict)
        )
        cap_limit = data.get("cap_limit")
        return cls(
            id=decision_id,
            tenant=scope["tenant"],
            subject_id=scope["subject_id"],
            purpose=scope["purpose"],
            channel=scope["channel"],
            # No default of OUTCOME_ALLOWED anywhere, at any level: the absent case must be a
            # refusal, and the only way to guarantee that is never to write the allow token
            # as a fallback.
            outcome=str(data.get("outcome", "") or ""),
            reasons=reasons,
            market=scope["market"],
            vertical=scope["vertical"],
            as_of=str(data.get("as_of", "") or ""),
            explanation=str(data.get("explanation", "") or ""),
            cap_limit=int(cap_limit) if isinstance(cap_limit, int | float) else None,
            sends_in_window=int(data.get("sends_in_window", 0) or 0),
            citations=citations,
        )

    @classmethod
    def unavailable(cls, query: ConsentQuery, detail: str) -> ConsentDecision:
        """The locally-synthesised refusal used when the store could not be reached.

        It is a real, inspectable decision object carrying an id that says plainly where it
        came from, so a consumer's audit trail records that contact was refused BECAUSE the
        consent store was unreachable, rather than recording nothing and moving on.
        """
        return cls(
            id="consent-client-unavailable",
            tenant=query.tenant,
            subject_id=query.subject_id,
            purpose=query.purpose,
            channel=query.channel,
            outcome=OUTCOME_DENIED,
            reasons=(REASON_CLIENT_UNAVAILABLE,),
            market=query.market,
            vertical=query.vertical,
            as_of=query.as_of,
            explanation=f"Contact refused: the consent store could not be reached ({detail}).",
        )


@dataclass(frozen=True, slots=True)
class SendRecord:
    """One contact actually made, quoting the decision that permitted it.

    Recording the send is what makes a frequency cap real: the store's caps count these rows,
    so a consumer that decides but never records will pass a cap forever.
    """

    id: str
    tenant: str
    subject_id: str
    channel: str = "email"
    purpose: str = "marketing"
    decision_id: str = ""
    sent_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tenant": self.tenant,
            "subject_id": self.subject_id,
            "channel": self.channel,
            "purpose": self.purpose,
            "decision_id": self.decision_id,
            "sent_at": self.sent_at,
        }
