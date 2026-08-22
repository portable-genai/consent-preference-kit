"""consent-preference-kit: the shared client for the catalog's consent and preference store.

A thin, versioned kit carrying the consent wire types and the service-to-service client, so a
consumer that needs to know whether a data subject may be contacted depends on this rather
than on the store's internal domain module or on a hand-rolled HTTP call.

The store itself lives inside the marketing compliance and brand governance system, which
already models consent as a rule with a ``CONSENT_REQUIRED`` check and already owns the
deterministic rule engine and the rule citations that a consent denial cites. This kit is only
its client half.

Zero runtime dependencies (pure standard library), exactly like ``pii-kit`` and
``review-kit``: a leaf commons that consumers pin cannot itself pull another git+https
commons without the nested tag-vs-SHA reference conflicting with the consumer's own lockfile.
The S2S transport hardening is therefore inlined and kept wire-compatible with
``hex-service-kit``'s server verifier.

Fail closed on both sides of the wire: the store refuses on unknown consent state, and this
kit refuses on unknown transport state. Nothing here produces an allow that the store did not
say.
"""

from __future__ import annotations

from .client import (
    BearerTokenProvider,
    ConsentClient,
    ConsentClientError,
    ConsentDeniedError,
    Transport,
)
from .models import (
    CHANNELS,
    DENYING_REASONS,
    INFORMATIONAL_REASONS,
    KNOWN_REASONS,
    OUTCOME_ALLOWED,
    OUTCOME_DENIED,
    REASON_CLIENT_UNAVAILABLE,
    Citation,
    ConsentDecision,
    ConsentQuery,
    SendRecord,
)

__version__ = "0.0.1"

__all__ = [
    "CHANNELS",
    "DENYING_REASONS",
    "INFORMATIONAL_REASONS",
    "KNOWN_REASONS",
    "OUTCOME_ALLOWED",
    "OUTCOME_DENIED",
    "REASON_CLIENT_UNAVAILABLE",
    "Citation",
    "BearerTokenProvider",
    "ConsentClient",
    "ConsentClientError",
    "ConsentDecision",
    "ConsentDeniedError",
    "ConsentQuery",
    "SendRecord",
    "Transport",
    "__version__",
]
