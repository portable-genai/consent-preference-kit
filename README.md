# consent-preference-kit

The shared client for the catalog's **consent and preference store**: the consent wire types
plus a fail-closed service-to-service client, so every consumer asks whether a data subject may
be contacted the same way instead of copy-pasting an HTTP call into each repo.

The store itself lives inside the marketing compliance and brand governance system
(`marketing-compliance-gate`), which already models consent as a rule with a
`CONSENT_REQUIRED` check and already owns the deterministic rule engine and the rule citations
a consent denial cites. This kit is only its client half.

Zero runtime dependencies (pure standard library), exactly like `pii-kit` and
`review-kit`: a leaf commons that consumers pin cannot itself pull another `git+https`
commons without the nested tag-vs-SHA reference conflicting with the consumer's own lockfile,
so the small S2S client helpers (a stdlib `urllib` POST, the https-only base-URL guard, and the
bearer / HMAC-signed-actor headers) are inlined and kept wire-compatible with
`hex-service-kit`'s server verifier. The transport is pluggable, so the client is unit-testable
with no live store.

## Use

```python
from consent_preference_kit import ConsentClient, ConsentQuery, SendRecord

client = ConsentClient("https://mkt6-compliance.internal")  # https-only off loopback

query = ConsentQuery(
    tenant="demo-bank",          # the calling service asserts this; the store trusts the S2S caller
    subject_id="subj-000101",    # a stable key, never a name
    purpose="marketing",
    channel="email",
    market="SG",
    vertical="banking",
    as_of="",                    # empty decides against now; pin it to replay a past decision
)

decision = client.decide(query, actor="e5-proactive-outreach")
if decision.allowed:
    ...  # send the message, then record it so the frequency cap counts it
    client.record_send(
        SendRecord(
            id="se-0001",
            tenant="demo-bank",
            subject_id="subj-000101",
            channel="email",
            purpose="marketing",
            decision_id=decision.id,   # ties the message to the exact state that allowed it
        )
    )
else:
    log.info("refused: %s", ", ".join(decision.denying_reasons))
```

Three ways to handle the answer, in increasing strictness:

| Call | On a refusal | On an unreachable store |
|---|---|---|
| `decide(query)` | returns a decision with `allowed == False` | **raises** `ConsentClientError` |
| `decide_or_deny(query)` | same | returns a DENIED decision naming `client_unavailable` |
| `require_allowed(decision)` | **raises** `ConsentDeniedError`, carrying the decision | n/a |

`decide` raises by default because a caller that has not thought about the failure path should
stop rather than proceed on a decision it never received. `decide_or_deny` is for callers who
have thought about it and want to keep running: it returns a real, inspectable decision object,
so the refusal lands in the caller's audit trail with a reason rather than disappearing.

## Nothing here invents an allow

The store fails closed on unknown **consent** state. This kit fails closed on unknown **wire**
state, which is the failure mode the store cannot see.

- `decision.allowed` is true only when `outcome` is exactly `"allowed"`. Not
  `outcome != "denied"`: that shape reads a truncated response, a typo, or a token from a newer
  store as permission to contact a person.
- A payload that is not a decision (no id, not an object) raises rather than being coerced into
  one.
- Every decision must carry tenant, subject, purpose, channel, market and vertical, and each
  must exactly match the question. The client never fills absent security scope from its own
  request, so an old, malformed or cached allow cannot acquire authority during parsing.
- A reason string this kit does not recognise counts as **denying**, not informational. A
  reason a newer store added is far more likely to be a new refusal than a new pleasantry, and
  `decision.unknown_reasons` tells you to upgrade the pin.
- No source line anywhere in the package can produce the allow token from an absent or falsy
  value. That is enforced by a test that greps the package, so it fails on a shape the
  behaviour tests did not enumerate.

## Credentials fail closed

Both variables are resolved in three states (unset, set-and-blank, set-and-valid) and unset is
not a member of the valid set:

| Store | `CONSENT_S2S_TOKEN` | Result |
|---|---|---|
| `https://...` (remote) | unset | **Refused at construction**, naming the variable. An absent credential is not consent to ask unauthenticated. |
| `https://...` (remote) | set, blank | **Refused.** A blank value is never read as configured, so no empty bearer is ever sent. |
| `https://...` (remote) | set | Sent stripped as `Authorization: Bearer ...`. |
| `http://localhost...` | unset | Allowed: the zero-secret offline posture, the same loopback carve-out the https-only base-URL guard makes. |

`CONSENT_S2S_SIGNING_KEY` follows the same rule for blank values (a blank HMAC key is a key
everyone knows). When it is unset the signed-actor pair is **omitted** rather than sent
unsigned: an unsigned assertion of who is asking is worth less than no assertion, and the store
reads the authenticated S2S caller, not that header, as the trust anchor today. Credentials are
re-read on every call, so a variable cleared after start-up cannot leave a long-lived client
silently downgraded.

Both environment variable names are constructor arguments (`token_env`, `signing_key_env`), so
a consumer that already carries a platform-wide S2S secret can point the client at it.

Managed consumers may instead pass `token_provider=...`. The provider is invoked lazily for
every request and must return a nonblank bearer. This is the Workload Identity seam: a GCP
adapter can mint an audience-bound Google ID token without adding a cloud SDK dependency to this
stdlib-only wire kit, while local loopback retains its zero-secret posture.

## What the store answers

`ConsentDecision` mirrors the store's own shape:

- `id` is a content hash of the question and the answer. Quote it on the send you make, and the
  message reconciles against a replay of the store months later.
- `reasons` carries every reason, denying and informational, so an audit record says all of why
  rather than the first why.
- `citations` are the market consent rules the decision applied, the same rules and the same
  citations the asset-review path produces.
- `cap_limit` and `sends_in_window` explain a frequency-cap refusal without a second call.

## Record the sends

A frequency cap counts recorded sends and nothing else, so a consumer that decides but never
calls `record_send` will pass a cap forever. Record after the message actually goes out,
quoting the decision id.

## The wire

```
POST /v1/service/consent/decision   -> ConsentDecision
POST /v1/service/consent/sends      -> {"send_id": "..."}
```

Both are the store's **service** intake: they authenticate the calling SERVICE rather than an
end user, which is the only way a proactive outreach system with no user in the loop can ask at
all, and they therefore take the tenant in the body. An asserted tenant names who is calling; it
never grants a view of another tenant's subjects, because the store reads only that tenant's
rows. Per-hop OAuth2 token exchange (on-behalf-of) is the deferred next layer.

## Develop

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests
.venv/bin/mypy src
.venv/bin/pytest -q
```

## License

Apache-2.0. See [`LICENSE`](LICENSE).
