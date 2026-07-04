"""JPet — the family wall pet (docs/plans/JPET_PLAN.md).

A server-authoritative pet: one `app.pet_state` row holds the drives, mood, floor
position, and current utterance; a lightweight drives tick advances it on a clock,
and (later waves) `/pet/command` + an SSE fanout let a Wall display and a phone
Control screen drive it in sync. W0 is the backend safety spine: the table + RLS
firewall + the drive math + the tick, no LLM and no render.
"""
