---
name: check_channel
version: 3
permission: web
params:
  type: object
  properties:
    channel_id:
      type: string
      description: The YouTube channel id (a UC… id) or @handle to check.
    title_include:
      type: array
      items:
        type: string
      description: >-
        Optional title pre-filter. Keep only uploads whose title contains ANY of these
        phrases (case-insensitive substring), e.g. ["Starship", "Starbase"]. Leave it off
        to see every recent upload and judge from the returned metadata instead.
    published_within_days:
      type: integer
      description: >-
        Optional — only return uploads published within this many days (e.g. 7 for the last
        week). Uploads whose publish date can't be resolved are shown anyway (date unknown).
    limit:
      type: integer
      description: How many recent uploads to inspect (default 10, max 25).
  required: [channel_id]
---
List a YouTube channel's most recent uploads and return the ones NOT already in the owner's
analysed-video library, each with its title, length, publish date, a one-line description teaser,
and format tags — enough for you to judge which are worth adding. Two tags flag uploads you'll
usually skip: "was live" (a finished-livestream re-upload, often multi-hour) and "Short?" (a
vertical short-form clip). For each new video worth keeping (e.g. news or update-style episodes,
not Shorts, livestream re-uploads, or off-topic clips), call analyze_stream on its URL in full
mode; the ones you skip are simply not analysed. Optionally narrow the listing with title_include
(keep titles containing ANY of several phrases) and/or published_within_days (a recency window
like the last 7 days). Pass a channel id (a UC… id) or an @handle, never a full URL. Results
already in the library are omitted, so analysing a returned video never repeats work.
