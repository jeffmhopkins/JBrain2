---
name: scratch_write
version: 1
permission: web
params:
  type: object
  properties:
    filename:
      type: string
      description: The file to write (created if new, replaced if it exists).
    content:
      type: string
      description: The full new contents of the file. A write REPLACES the whole file, so include everything you want kept.
    mode:
      type: string
      enum: [save, delete]
      description: "save (default) writes content; delete removes the file."
  required: [filename]
examples:
  - {filename: "index.md", content: "What to read first each night:\n- who I've met (see people.md)\n- threads I owe a reply"}
  - {filename: "old-notes.md", mode: delete}
---
Write one of your scratchpad files. A write REPLACES the whole file — read it first if
you want to keep what's there and add to it. You have 16 files, 128 KB total, 24 KB per
file; a write that would exceed the budget is refused with the numbers, so you decide
what to trim. Organize your files however you like — the names and structure are yours.
Set mode=delete to remove a file. Whatever is not written down before the hour ends is
gone, so use the end of your hour to bring your files up to date.
