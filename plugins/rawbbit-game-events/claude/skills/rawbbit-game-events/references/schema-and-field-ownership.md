# Collector schema and field ownership

The [generated Collector contract facts](collector-contract.json) contain the current endpoint, auth-header name, source-default limits, and accepted model fields with annotation types and required/optional status.
They are generated from the maintained Collector contract. The packaged [bundle manifest](../assets/sdk/BUNDLE-MANIFEST.json) records their hash so an installed plugin can detect accidental changes.
This document defines **field-ownership policy**, which schema acceptance alone cannot express.
The installed plugin is self-contained: use these references and bundled SDKs, not a private repository path.

## Transport and limits

Use the generated contract for the current POST path, auth-header name, accepted fields, and source-default request limits; deployments may configure lower limits.
The request uses `Content-Type: application/json` and body `{"events":[...]}`.
Each ingest key is app-scoped.
SDKs have their own batching and retry rules.
Local queue acceptance is not proof of Collector acceptance or downstream visibility.
Delivery is at least once.

## Accepted does not mean client-owned

The generated contract lists required and optional fields for `EventEnvelope` and its nested models.
A Pydantic-valid event is not necessarily a permissible *client-authored* event.
Enforce field ownership at the game adapter/SDK boundary and test the serialized wire request separately.

- **Game:** approved event name, committed gameplay parameters, approved user/account ID and user properties, supported app-owned device/traffic context, and current consent decision. Capture historical values at the transition, not from mutable later UI state. Avoid free text, secrets, unbounded values, and identifiers disguised as business parameters.
- **Adapter/SDK:** `app_id`, environment, envelope/ID/time defaults, pseudonymous installation and session IDs, platform, request/auth header, queue/retry, and confirmed `consent.analytics_storage`. Only documented SDK escape hatches with explicit event-map ownership may override defaults; the game must not forge SDK-owned session or consent values.
- **Collector:** top-level `received_at` and `ingest.*` request/receipt/network metadata. Game code must never send these, even though the Collector schema has fields for them.
- **NATS/raw storage:** queue sequence, storage path/partition, and writer lineage. Never author them in a client event.

The SDKs allow JSON-compatible business data under `event_params` and `user_properties`, but the event map must fix type, unit, allowed domain, source, and frequency.
`schema_version`, `product`, `surface`, and `source_type` are SDK/adapter event-parameter defaults, not arbitrary top-level Collector fields.
`user.user_id` is null unless an application-owned identity source is explicitly approved; neither email nor display name is an implicit ID.

## Negative ownership checks

In a game-level adapter fixture, attempt caller-supplied top-level `received_at` and `ingest`, plus overridden `user.session_id` and `consent.analytics_storage`.
Assert each is rejected or ignored according to the selected SDK and none is emitted as caller-controlled wire data.
Validate the remaining wire envelope against the Collector schema separately. Also assert no NATS/storage lineage.
Do not confuse accepted shape with authority to populate a field.
