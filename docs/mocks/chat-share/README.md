# Chat share links — GUI gate

Owner chose **B · Live** (2026-10-12), with two changes: the public page shows the chat
exactly as the app does, and "live" means current as of each load (no push, no pause).
`options-a-b-c.html` is the original three-way mock. The binding mock is published at
https://claude.ai/artifact/PCwfnyFjqRunoT7FPsej3p and built from `source/`.

**Owner side.**
- **Rail.** A fifth button, **share** (chain-link glyph, steel), between rename and scope,
  so its tint never touches archive's. 48px each; the rail grows from 192 to 240px. A
  shared chat shows the link glyph beside its turn count.
- **Sheet.** Tapping share mints the link and opens a sheet: link box + Copy, "Send with…"
  (Web Share), opens count + last opened, and **Revoke**. A violet warning appears when the
  chat's scope includes a firewalled domain (Financial / Medical / Location). There are no
  toggles: tool steps are always included.

**Public page** (shell-less, like the research share page).
- A steel "shared chat · read-only" band: title, "As of <time> · <mode> · N turns", and
  "Reload for newer turns".
- The transcript is the shipped `AgentTranscript` inside `.fb-shell`, so Thought, Worked,
  step rows, inline tool marks, citations and copy behave exactly as in the app. There is
  no top bar and no omnibox; the page scrolls as one document.
- Each load serves the whole conversation as it is at that moment. A revoked link shows
  "This link no longer works".
- A footer says the reader can't reply, open its notes, or see anything else.

**Rebuilding the mock.** Copy `source/` to `frontend/_mock_chatshare/`, then run
`npx vite build --config _mock_chatshare/vite.config.ts` from `frontend/` and inline
`dist/assets/*.{css,js}` into one page. Don't commit the build folder.
