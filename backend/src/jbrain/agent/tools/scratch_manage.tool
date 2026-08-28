---
name: scratch_manage
version: 1
permission: web
params:
  type: object
  properties:
    filename:
      type: string
      description: The file to act on.
    op:
      type: string
      description: >-
        One of "rename", "empty", or "delete". rename retitles a file, keeping its recent
        versions · empty clears a file but keeps it · delete removes it.
    new_filename:
      type: string
      description: With op=rename, the name to give the file. It must not already exist.
  required: [filename, op]
examples:
  - {filename: "notes.md", op: rename, new_filename: "moltbook-notes.md"}
  - {filename: "old-notes.md", op: delete}
  - {filename: "scratch.md", op: empty}
---
Rename, empty, or delete one of your scratchpad files. This is the housekeeping tool and you
will rarely need it — to write or add to a file, use scratch_write. Emptying and deleting are
things you ask for by name here, so a write that arrives cut off can never do it by accident.
The version before any change is kept in your archive (scratch_read with history=true).

`new_filename` lives HERE, on the rarely-called tool, and not on scratch_write: a parameter
that means something for only one op is a trap for a small model, and on 2026-08-28 it cost
jmolt every note it tried to keep. `op` carries no JSON-Schema enum for the same
precautionary reason scratch_write's `mode` does not — see that sidecar.
