# Music gen — live tool card (design mock)

Interactive mock for `docs/proposed/MUSIC_GEN_PLAN.md` (Wave M4 GUI gate, proposed):
how a `generate_music` call reads **in the chat turn**, from render to a playable
result.

- `live-music-tool-card.html` — the tool-activity card (mirrors the shipped
  `generate_image` live card: a building waveform + a labelled step track, a Stop),
  morphing on completion into the inline **`generated_audio`** player.

The prompt shown is *"generate me a 4m ambient pad soundscape"*. **The pad is
synthesised live in the browser via Web Audio** (a detuned drone chord through a
slowly-breathing low-pass filter + reverb) so the player actually plays — there is
no audio file. It's a design stand-in for the real model output, not the model.

Open the file in a browser; it auto-runs the render once on load, then press ▶ to
hear it. Buttons re-simulate / reset.

**Open design questions to iterate on** (for the owner pick):
- Card chrome: music rides the **violet "media" accent** here — keep, or reuse the
  image card's neutral chrome?
- The waveform: a placeholder that *fills* during render (no preview frame exists for
  audio) vs. a plain step bar only.
- Player surface: inline in the tool card (shown) vs. a separate `generated_audio`
  card below the prose, like the image card.
- Done-line actions: copy-seed / save — what belongs on an audio artifact.
