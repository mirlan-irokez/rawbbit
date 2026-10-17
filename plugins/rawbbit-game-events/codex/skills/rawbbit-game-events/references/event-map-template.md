# Rawbbit event map template

Use this structure unless the target repository already has an analytics-document convention. Replace instructional text rather than retaining placeholder examples as if they were approved events.

```markdown
# Rawbbit event map

## Scope and provenance

- Product/game:
- Engine/version, backend, target, and verified-support status:
- Emitting surface(s), destination(s), and `app_id`:
- Environments:
- Analytics adapter:
- Rawbbit SDK and copied adapter-template provenance:
- Collector schema reference revision:
- Existing SDK state: new / compatible existing / deliberate upgrade / local fork:
- Last reviewed:
- Human approver and approval reference:

## Field ownership

- Game code owns approved event names and business parameters from authoritative state transitions.
- The analytics adapter/SDK owns envelope normalization, app/environment configuration, event identity/time, pseudonymous/session identity, confirmed consent, and transport.
- The Collector owns receive time, request ID, HTTP/IP metadata, and network-derived enrichment.
- NATS/raw storage own queue and storage lineage.
- The Collector schema accepts some server-owned fields; acceptance is not permission for a client to author them.
- Client code must not manufacture SDK session/consent, Collector, queue, or storage fields.

## Consent and identity

- Consent source and behavior:
- Human-approved `user_id` source (`none / account ID / approved alternative`):
- Rationale and risks for any player-entered identifier:
- Anonymous identity behavior:
- Authenticated identity behavior:
- Login/logout/account-switch behavior:
- Events allowed before consent, if any:
- Withdrawal denial/purge behavior and already-in-flight request limit:

## Configuration handoff

- Collector endpoint and environment (no key value):
- App ID and where the human saves it:
- App-scoped ingest-key variable, local ignored entry point, and deployment setting (no value):
- Direct Collector or game-owned proxy; CORS/CSP/rotation/abuse owner:

## Existing instrumentation and migration

- Current event/SDK inventory and downstream consumers:
- For each existing event: leave unchanged / dual-write / Rawbbit-only maintenance / replace:
- Other-analytics field/identity/consent mapping and independent adapter boundary:
- Comparison/cutover/rollback policy (including stored SDK queue/identity):

## Change classification

### New additive events

- None.

### Additive optional parameters

- None.

### Compatibility-sensitive contract changes — individually named approval required

- None.

### Deprecations — individually named approval required

- None.

## Event catalog

### `event_name`

- Status: `existing | proposed | approved | implemented | deprecated`
- Description:
- Analytical purpose / decision supported:
- Exact trigger and deduplication rule:
- Approved event set per committed transition and any conditional/session prefix:
- Destination(s) and other-tool mapping:
- Expected frequency / worst-case burst:
- Implementation location:
- Identity requirement:
- Consent requirement:
- Compatibility notes:

Parameters:

| Parameter | Required | JSON type | Unit / allowed values | Authoritative source | Description / fallback |
|---|---:|---|---|---|---|
| `example_id` | yes | string | stable bounded identifier | game state | Replace this example. |

## Deferred or rejected candidates

- Candidate, reason, and what would justify reconsideration.

## Verification ledger

- Event-map/code coverage:
- Payload-schema checks:
- Field-ownership negative checks, separate from schema acceptance:
- Trigger/deduplication checks:
- Second-run idempotency and independent adapter failure:
- Identity/session checks:
- Consent withdrawal/restart checks; already-started requests may still reach Collector:
- Missing-config and network-failure checks:
- Lint/test/typecheck/build:
- Live Collector traffic: not sent unless explicitly authorized.

## Deployment requirements and unresolved risks

- Runtime configuration names and locations, without values.
- Target Editor/device/browser-capability gates not yet run.
- Remaining compatibility or downstream-consumer risks.
```

Within one event, a Markdown parameter table is intentionally compact and reviewable. If the target platform does not render tables well, preserve the same fields as a bullet list.
