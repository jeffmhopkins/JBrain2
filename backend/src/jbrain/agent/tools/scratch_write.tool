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

`mode` carries NO JSON-Schema enum, for the reason `analyze_stream` carries none: gpt-oss's
harmony tool path builds a GBNF grammar over the tool union, and an enum on a property of an
object with optional fields is the shape that has bitten this repo before. v2 shipped a
five-value enum here alongside a conditional `new_filename`, and the model answered by
filling `new_filename` with junk and omitting `content` on 85 consecutive calls — a whole
night of notes refused. Allowed values live in the description; the handler validates.
