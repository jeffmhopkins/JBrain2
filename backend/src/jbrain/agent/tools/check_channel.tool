---
name: check_channel
version: 1
permission: web
params:
  type: object
  properties:
    channel_id:
      type: string
      description: The YouTube channel id (a UC… id) or @handle to check.
    title_include:
      type: string
      description: Optional — only return uploads whose title contains this text (case-insensitive).
    limit:
      type: integer
      description: How many recent uploads to inspect (default 10, max 25).
  required: [channel_id]
---
List a YouTube channel's most recent uploads and return the ones NOT already in the
owner's analysed-video library — the new videos worth analysing. Optionally filter by
a substring of the title (e.g. only "Starship" videos on a space channel). Use this to
answer "any new videos from channel X?" and to find fresh videos to add: for each result
you want in the library, call analyze_stream on its URL in full mode. Pass a channel id
(a UC… id) or an @handle, never a full URL. Results already in the library are omitted,
so analysing a returned video never repeats work.
