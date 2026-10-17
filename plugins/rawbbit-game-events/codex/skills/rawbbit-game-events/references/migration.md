# Existing events, migrations, and maintenance

Inventory before editing. 
For each current event in Rawbbit or another tool, record semantic meaning, exact committed trigger, call sites, properties with types/units, consent, identity, frequency, downstream consumers, and owner. 
Unknown dynamic names remain unresolved until bounded. Name equality is not semantic equality.

## Classify the requested work

- `new_event`: additive event with a new purpose/trigger.
- `optional_parameter`: additive field whose absence preserves old rows and consumers.
- `contract_change`: rename, removal, trigger or meaning change, required parameter, type/unit/domain shift, or identity/consent change.
- `deprecation`: old contract stays documented while producers/consumers move; not immediate deletion.

For each existing event choose `leave unchanged`, `keep other tool + add Rawbbit`, `replace other tool with Rawbbit`, or `Rawbbit-only maintenance`. 
Show the mapping and identity/consent differences in the event map. 
Compatibility-sensitive changes must be individually named and approved; one concrete review response can approve several named items. 
Preserve existing other-tool emissions unless removal was approved.

## Independent dual-write

The game's authoritative transition constructs application-owned business inputs once, then calls two independent adapters. 
Rawbbit must not consume another SDK's generated identity, session, consent cache, autocapture payload, or storage. 
Do not make gameplay or one destination contingent on the other's success. 
A shared app-authored event ID is allowed only if both contracts deliberately support the same semantics; each destination's delivery retry retains its own stable ID. 
Do not equate one gameplay trigger with one HTTP request: a transition may produce several approved events and an SDK session prefix.

## Replacement and deprecation

Require per-event field mapping, consent and identity policy, consumer inventory, rollout/comparison window, cutover and rollback. 
A new versioned event is safer than changing the meaning of historic rows. Deprecation retains old catalog semantics; remove calls only with explicit authorization after checking consumers. 
Existing Rawbbit event maintenance should normally reuse the installed SDK. 
A new SDK version is a separate upgrade with persisted queue/identity/endpoint/adapter review, not a side effect of adding an event. 
For a locally changed SDK, preserve and diff the fork rather than replacing it.

For each later change update the same event map, including status (`existing`, `proposed`, `approved`, `implemented`, `deprecated`), approval reference, implementation location, and SDK/adapter provenance. 
A second run must not duplicate event entries, SDK copies, hosts, or listeners.
