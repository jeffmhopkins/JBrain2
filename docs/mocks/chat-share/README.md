# Chat share links — GUI gate (pending owner choice)

Mock for sharing an agent chat from the Chats list's swipe rail. `options-a-b-c.html` is the
interactive mock: switch Variant × Screen (swipe rail · share sheet · public view · revoked)
× theme, and pick the rail glyph at the bottom. No direction is chosen yet.

- **Rail.** A fifth button, **share**, joins rename · scope · archive · delete. It sits
  between rename and scope so its steel tint never touches archive's. Each button stays
  48px, so the rail grows from 192 to 240px. A shared chat shows the share glyph beside
  its turn count. Glyph candidates: chain link (recommended; matches report sharing),
  share arrow, share nodes.
- **A · Snapshot.** Tapping share mints a link to the chat as it is now; later turns stay
  private until "Update snapshot".
- **B · Live.** The link follows the chat; a "Keep updating" switch pauses it.
- **C · Pick turns.** Choose turns before the link exists; the public page marks a gap
  where a turn was left out.

All three: a link box with Copy and "Send with…", a "Show tool steps" switch (off by
default), opens count + last opened, **Revoke**, and a violet warning when the chat read a
firewalled domain (here Financial). The public page is shell-less: a steel "shared chat ·
read-only" band, the transcript, sources, and a footer saying the reader can't reply.

Tokens only, steel = links, amber = research mode (`docs/reference/DESIGN.md`). Prior art:
`docs/mocks/research-share/`.
