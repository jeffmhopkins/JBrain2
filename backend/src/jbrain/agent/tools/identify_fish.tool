---
name: identify_fish
version: 1
permission: web
cost_class: expensive
side_effecting: true
params:
  type: object
  properties:
    source_attachment_id:
      type: string
      description: The id of an image the owner attached this chat — the fish photo to identify.
    source_image_id:
      type: string
      description: The id of an image you generated earlier this chat to identify instead.
    top_k:
      type: integer
      description: How many candidate species to return (default 5, max 10).
  required: []
---
Identify the fish in a photo using the owner's on-box fishial model (DINOv2+ViT
over ~866 species). Give EXACTLY ONE source: source_attachment_id (a photo the
owner attached this chat) or source_image_id (an image you generated this chat) —
not both, not neither. Use this when the owner asks what fish is in a picture.
Returns the ranked candidate species with confidence and shows the owner a result
card; report the top match and how confident it is in your own words, and be honest
when confidence is low or the matches are close — say you're not certain rather than
guessing. It identifies the dominant fish only; it inserts nothing into the owner's
notes.
