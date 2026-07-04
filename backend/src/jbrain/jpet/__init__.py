"""JPet — the family wall pet (docs/archive/JPET_PLAN.md; v2: docs/proposed/JPET_V2_PLAN.md).

A server-authoritative play companion for young children: one `app.pet_state` row holds
the positive happy-meters, mood, floor position, room objects, and the bounded action
*script* the pet plays out; `/pet/command` + an SSE fanout keep the on-box Wall display
and the phone Control screen in sync. A lightweight tick advances it on a clock (pure
arithmetic, second seat). v2 replaced the Tamagotchi decay model with command-and-response
play: the pet is always happy, and the kids make it do things — dance, chase the ball, or
"pick up the ball and put it in the corner" — by voice or big buttons.
"""
