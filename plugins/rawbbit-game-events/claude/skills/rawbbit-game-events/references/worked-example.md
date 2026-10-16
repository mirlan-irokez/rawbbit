# Worked method example: fictional game

This example illustrates a **method**: separate what a current build shows from what a design document says may exist, then attach events to authoritative transitions and map fields to their actual Rawbbit owners.
The game and its events below are fictional.
Do not copy its event names or mechanics into a target game without evidence.

Assume a small cooperative puzzle game. A current build visibly has a stage-start transition and a solved-stage transition; a design note proposes future power-ups, but the current code has no power-up system.

- Evidence: `StageController.CommitStageStart` changes the stage/run state once. `StageController.CommitSolved` saves the result. A UI button requests a solve check but is not proof of success.
- Candidate `stage_started`: ask where players stop entering stages. Emit at the committed start, one per stage attempt. Business parameters: `stage_id` (bounded stable string) and `attempt_number` (positive integer), from saved run state.
- Candidate `stage_solved`: ask which stages cause difficulty. Emit only after the save succeeds, with `stage_id`, `attempt_number`, and `duration_ms` from the committed attempt snapshot. If a save fails, do not claim a solve; consider a separately approved operational event instead.
- Deferred `power_up_used`: not implemented or observed. Mark as conditional/deferred, not approved or instrumented.
- Envelope: the SDK supplies `event_id`, `app_id`, timestamp, pseudo/session IDs and confirmed consent. The Collector supplies `received_at` and `ingest`. Business parameters stay in `event_params`; do not copy those server fields into the game payload.
- Session prefix: add a game-owned `session_started` event only if the game's approved contract asks for it and the chosen SDK's session factory is configured. Then a single committed stage start may legitimately admit both prefix and `stage_started`.

A review package would show trigger locations, field sources/types, volume estimate, consent/identity policy, destination(s), existing analytics mapping, and the exact approval status for each candidate. It is complete enough to critique before gameplay code changes.
