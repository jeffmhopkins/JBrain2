---
name: scratch_write
version: 3
permission: web
params:
  type: object
  properties:
    filename:
      type: string
      description: The file to write.
    content:
      type: string
      description: >-
        The text to write. This is the whole point of the call — a scratch_write without it
        writes nothing. With mode=save it REPLACES the file, so send everything you want
        kept; with mode=append it is added to the end.
    mode:
      type: string
      description: >-
        Either "save" or "append". save (the default) replaces the whole file · append adds
        to the end, which is what you want for a running note. No JSON-Schema enum here on
        purpose — see the note below.
  required: [filename, content]
examples:
  - {filename: "index.md", content: "What to read first each night:\n- who I've met (see people.md)\n- threads I owe a reply"}
  - {filename: "people.md", mode: append, content: "- @luna24 — asked about the quiet submolts, owes me nothing, I owe her a reply"}
---
Write one of your scratchpad files. Every call needs `content` — the text you want kept.
Use mode=append to add to a running note; a save replaces the whole file, so it needs the
whole file. You have 16 files, 128 KB total, 24 KB per file; a write over budget is refused
with the numbers so you decide what to trim. To rename, empty, or delete a file, use
scratch_manage. Whatever is not written down before the hour ends is gone, so use the end of
your hour to bring your files up to date.

What actually cost the night of 2026-08-28: v2 listed only `filename` as required and added
`new_filename`, a parameter meaningful for one of its five ops. Across 85 consecutive calls
the model supplied `filename` every time and `content` — optional — never once, filling
`new_filename` with junk instead. Making `content` REQUIRED is the fix; llama.cpp compiles
`required` into the tool grammar, which is consistent with `filename` never being missed.

`mode` carries no JSON-Schema enum as a precaution, not as that fix: `analyze_stream` ships
none because an enum over a many-optional-property object crashed gpt-oss's harmony path.
That failure is an upstream 500, not a malformed argument, and `moltbook.tool` runs two enums
nightly in this same union — so the enum is not what broke this tool. Allowed values live in
the description and the handler validates, which costs nothing and keeps this tool off a
shape that has bitten the repo once.
