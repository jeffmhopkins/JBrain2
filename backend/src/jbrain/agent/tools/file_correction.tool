---
name: file_correction
version: 1
permission: sensitive
params:
  type: object
  properties:
    body:
      type: string
      description: The owner's correction, stated as a note (what is actually true).
    domain:
      type: string
      description: The domain the corrected fact belongs to (e.g. general, health, finance).
    article_id:
      type: string
      description: The wiki article being corrected (for provenance). Optional.
    revision_id:
      type: string
      description: The specific revision the correction disputes (the anchor). Optional.
  required: [body, domain]
---
File an owner correction to the machine-written wiki. The wiki is never edited directly; instead
this records the owner's note as authoritative — it out-argues the conflicting fact in the graph
(force-supersedes + pins it) and the affected article is rebuilt from the corrected facts. Use
this when the owner says a wiki claim is wrong and tells you what is actually true. State `body`
as the correct fact in the owner's voice, and pass the `domain` it belongs to. Confirm what you
filed; do not claim the article changed instantly — the rebuild runs in the background.
