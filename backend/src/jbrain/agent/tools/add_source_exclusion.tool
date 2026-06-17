---
name: add_source_exclusion
version: 1
permission: mutate
params:
  type: object
  properties:
    note_id:
      type: string
      description: The note to stop using as a source.
    domain:
      type: string
      description: The domain of the exclusion (matches the note/section domain).
    article_id:
      type: string
      description: Limit the exclusion to one article. Omit for a global exclusion.
    reason:
      type: string
      description: Why the owner is excluding this source. Optional.
  required: [note_id, domain]
---
Stop a note from being used as a source for a wiki article (or for every article, if no
article_id is given), then queue a rebuild so the article is re-derived without it. This is not
deletion and not retraction — the note stays in the knowledge base and the facts stay true; it is
only removed from what the builder draws on for the article. Use this when the owner wants a
source kept out of the wiki. Confirm what you excluded and that a rebuild is queued.
