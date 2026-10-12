---
name: panel_notify
version: 1
permission: web
params:
  type: object
  properties:
    title:
      type: string
      description: One short line, at most 60 bytes — what the owner reads at a glance.
    body:
      type: string
      description: Optional detail under the title, at most 300 bytes, plain text.
    choices:
      type: array
      items:
        type: string
      description: Up to 4 short answers (16 bytes each) to make it a question, e.g. ["Yes", "No"]. Omit for a plain notice.
    await_answer:
      type: boolean
      description: Wait for the tap and return it (only with choices). Default true when there are choices.
    wait_s:
      type: integer
      description: How long to wait for the answer, 5-240 seconds (default 90). Past it you get the state so far; the panel keeps the question for 15 minutes.
    say:
      type: string
      description: Optional words to speak aloud as it appears, in the owner's read-aloud voice — eight seconds at most (about 20 words).
    image_id:
      type: string
      description: Optional picture to show — the id of an image in this chat (a generated, rendered, fetched or grabbed image). Without body or choices it fills the screen; with them it is a banner above the words.
    attachment_id:
      type: string
      description: Optional picture the owner attached to this chat, by its attachment id (instead of image_id).
    chime:
      type: boolean
      description: Chime when it arrives. Default false.
    hide_after_s:
      type: integer
      description: Hide it by itself this many seconds after it shows (3-600). Omit to keep it until tapped.
  required: [title]
---
Put a notice on the owner's desk panel — the small screen on his desk — and, with
choices, ask him a question there and get his tap back. Use it when he asks you to
("ping my panel when …", "ask me on the panel"), or for something a scheduled run must
put in front of him. It is not a chat reply: say what you did in your answer as well.

The panel may refuse, and the tool says why: switched off ("not accepting"), its night
hours ("quiet hours until 06:30"), or offline. A refusal is final for now — do not retry
it; tell him in your reply instead. Nothing is queued to show later.
