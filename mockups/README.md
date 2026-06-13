# Reviews page redesign — interactive mockups

Three self-contained, interactive HTML mockups exploring a redesigned Reviews
page. Each is a single file — open it in a browser (no server, no build). Drag
works with a mouse; keyboard shortcuts are listed via the `?` button.

## The two problems these fix

1. **You can't move between reviews.** Today the queue only lets you skip or
   resolve — there's no next/previous, no jumping to a specific item, no going
   back to one you just decided.
2. **Reject-only dead ends.** Many proposals only offer *reject* — if a change
   is 90% right you must throw it away. There's no way to edit/modify it.

Every mockup solves both: real navigation + progress + undo, and a
GitHub-style "approve-with-edits" path so *reject is never the only exit*.

## The three concepts

| File | Concept | Best for | How you navigate | How you avoid dead ends |
|------|---------|----------|------------------|-------------------------|
| `reviews-A-flow.html` | **A · Flow** | Keyboard power users | `j`/`k` + a jump-rail to hit any item; auto-advance | Proposed text is inline-editable; candidates + "none/create" for ambiguous kinds |
| `reviews-B-inbox.html` | **B · Split Inbox** | Browsing & batching | Filter (Pending/Deferred/Decided), list→detail with prev/next, bulk actions | Before→after diff with an editable proposed side; reopen from Decided |
| `reviews-C-deck.html` | **C · Deck** | One-thumb, tactile | Swipe (→ approve, ← reject, ↑ defer) + scrubber to jump | Flip-to-edit back side; left-swipe asks a reason so reject is deliberate, never careless |

## Shared design

All three use JBrain2's design tokens (dark default, domain colors at 13%
tint, 1px borders, 12/16 radii, system font, 150ms motion), respect
`prefers-reduced-motion`, surface provenance + confidence + rationale per item,
show an undo snackbar after every decision, and end on a completion/empty
state. Seeded with 8 realistic reviews across all 7 review kinds — including the
`ambiguous_mention` case that is the classic reject-only dead end today.

These are throwaway prototypes for design review, not production code.
