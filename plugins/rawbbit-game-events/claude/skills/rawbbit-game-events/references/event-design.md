# Event design rules

## Start with a decision

Every event must answer a named question or support a named operational check. Examples:

- Where do players abandon the first-session funnel?
- Which level, mode, or difficulty produces unexpected failure?
- Does a rewarded action change progression or retention?
- Did a recoverable game-system failure affect a player-visible outcome?

Do not collect data merely because it is available. If no decision depends on a field, omit it.

## Event names

- Use stable lowercase `snake_case` names describing completed facts: `game_started`, `round_completed`, `level_failed`.
- Use one name for one durable meaning. Do not overload an event based on parameter combinations.
- Prefer outcome events over low-level UI interactions. `level_completed` is more durable than `continue_button_clicked` when both represent the same fact.
- Use a new versioned name, such as `level_completed_v2`, when a necessary semantic change would make historical rows incomparable.
- Do not encode user IDs, level IDs, timestamps, experiment variants, or other unbounded values in the event name.

## Parameters

For every parameter document:

- key and plain-language meaning;
- JSON type: `string`, `number`, `boolean`, `null`, object, or array;
- unit for numeric values;
- allowed values or expected range;
- authoritative source in application state;
- required or optional status;
- fallback behavior when unavailable.

Use lowercase `snake_case` keys. Keep types and units stable. Prefer seconds or milliseconds only when the key states the unit, for example `duration_ms`.
Distinguish identifiers from display labels. Avoid formatted values when the raw typed value is available.

Required parameters must exist at every trigger. A new optional parameter is normally additive; making it required or changing its type, unit, domain, or meaning is a contract change.

## Cardinality and volume

- Bound enum-like values and document their allowed set.
- Use stable identifiers for levels, modes, items, and experiments.
- Keep free text, full URLs, stack traces, arbitrary error strings, and dynamically generated object keys out of normal event parameters.
- Estimate events per player session and the worst plausible burst.
- Do not emit per frame, animation tick, pointer move, movement step, repeated collision, timer tick, or indiscriminate click.
- Coalesce or summarize high-frequency mechanics at a meaningful boundary such as round completion.

Read the current source-default count and byte limits from [generated Collector contract facts](collector-contract.json), and confirm the target deployment's effective settings. These are transport ceilings, not a telemetry-volume target.

## Identity and sessions

- Use the SDK's pseudonymous identity and session adapter unless the application already has an approved identity design.
- Ask the human what should populate `user.user_id`: no value (SDK pseudonymous identity only), a stable application-owned account ID, or another explicitly approved source.
- Unless the human directs otherwise, supply `user.user_id` only from a stable application-owned authenticated identity. Never silently derive it from email, display name, player-entered name, or device fingerprinting.
- A simple game may deliberately use a player-entered value only after explicit human approval. Document that it may be non-unique, mutable, shared, personal, or abusive free text; prefer an application-generated pseudonymous identifier when stable person-level identity is not required.
- Document login, logout, account switching, and anonymous-to-known transitions. Changing identity behavior on an existing event requires separate permission.
- Do not put identifiers redundantly into `event_params`.
- An SDK session ID does not itself imply a session-start event. The bundled Unity and browser adapters can prepend an optional game-owned `session_started` event through a configured session-start factory; without that factory, the SDK emits no such prefix. Propose it only when the game needs and approves that event, and document its trigger, consent gate, fields, and effect on the approved event set.
- Some existing analytics catalogs call this event `session_start`. Keep the game's approved name or explicitly map it to `session_started`; do not emit both names merely because the catalogs differ.

## Privacy and consent

- Follow the application's existing consent decision and wording. Do not infer consent from mere use of the game.
- Do not send secrets, ingest keys, auth tokens, raw email addresses, chat text, clipboard or page content, full sensitive URLs, precise location, or device fingerprints. Do not send names unless the human explicitly approves a documented player-entered identifier under the identity rules above.
- Prefer coarse, application-owned categories over free text.
- Record whether each event is allowed before analytics consent, after consent, or not at all. When consent is denied, the adapter must follow the approved product behavior rather than silently sending an event with `analytics_storage: false`.

## Common game-event candidates

These are prompts, not a mandatory event catalog:

- session or game lifecycle: optional game-owned `session_started` prefix, `game_loaded`, `game_started`, `game_ended`;
- round or level outcomes: `level_started`, `level_completed`, `level_failed`, `level_abandoned`;
- onboarding: `tutorial_started`, `tutorial_step_completed`, `tutorial_completed`;
- progression/economy: `progression_unlocked`, `currency_earned`, `currency_spent`, `item_acquired`;
- purchases, only if the game sells them: `in_app_purchase` after confirmed fulfilment, not a store view or checkout attempt;
- meaningful choices: mode, difficulty, character, or upgrade selected;
- actionable failures: asset load, save, purchase, or state recovery failure.

Include only mechanics confirmed in code or description and only when their analytical purpose is explicit.
For `in_app_purchase`, define the authoritative purchase-confirmation state, duplicate-callback guard, product identifier, price unit/currency, and refund treatment in the event map. Never include receipts, payment credentials, or other payment details.
