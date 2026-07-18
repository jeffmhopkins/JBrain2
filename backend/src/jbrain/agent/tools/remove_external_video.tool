---
name: remove_external_video
version: 1
permission: web
params:
  type: object
  properties:
    url:
      type: string
      description: The URL (or id) of a library video to remove — e.g. one a search_external_video result linked to.
  required: [url]
---
Remove one video from the owner's library. This does NOT delete anything itself — it stages the
removal for the owner to approve inline, and only their approval deletes it (the deletion is
permanent, and the video's transcript, passages, thumbnails, and summary all go). Use it when the
owner asks to remove, delete, or forget a video from their library; find the right video with
search_external_video first, then pass its URL.

Report that you've staged the removal and that nothing is deleted until they approve — don't claim
the video is gone. One tool call stages one video's removal.
