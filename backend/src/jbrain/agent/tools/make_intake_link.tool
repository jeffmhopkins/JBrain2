---
name: make_intake_link
version: 2
permission: sensitive
params:
  type: object
  properties:
    subject_id:
      type: string
      description: >-
        The id of the subject this link collects information ABOUT (the person the
        captured facts will be attributed to). Resolve it from the owner's own
        subjects before calling — never invent one.
    domain:
      type: string
      description: Which domain the captured facts belong to — general, health, finance, or location.
    fields_brief:
      type: string
      description: >-
        What the interviewer should collect from the recipient, in plain prose — the
        specific, bounded set of information (e.g. "their current mailing address and a
        phone number"). This is the interview's goal, templated in as data; it can never
        grant the interviewer tools or access.
    persona_brief:
      type: string
      description: >-
        Extra framing for the interviewer's tone and manner (e.g. "warm and patient;
        reassure them their answers stay private and are reviewed before anything is
        kept"). Draft a short one that fits the subject and audience — the owner edits it.
        The interviewer's fixed identity and security rules always apply; this only tunes
        the voice, it can never grant a tool or widen scope. Not secret — a visitor could
        read it back.
    opening_blurb:
      type: string
      description: A short, friendly welcome message the recipient sees first. You draft it; the owner edits it.
    max_runs:
      type: integer
      description: How many SUBMISSIONS the link may accept before it dies (the submission ceiling).
    bind_on_first:
      type: boolean
      description: >-
        true = bind to ONE person (the first to open it); false = open to multiple people
        (up to max_opens). Ask the owner which they want.
    max_opens:
      type: integer
      description: >-
        Optional. How many times the link may be OPENED (the higher, redeem-time ceiling).
        Defaults to 4x max_runs when omitted.
    ttl_hours:
      type: number
      description: Optional. How many hours the link stays live. Defaults to 24.
    capture_enterer_name:
      type: boolean
      description: Optional. Whether to ask the recipient for their name (default true).
    disclose_owner_identity:
      type: boolean
      description: Optional. Whether the recipient is told who the owner is (default false — generic).
  required: [subject_id, domain, fields_brief, max_runs, bind_on_first]
---
Stage a guided-intake share link for the owner to review and approve. This NEVER mints a
link directly — you have no privileged path to create one. It stages an EDITABLE Proposal
showing the proposed configuration; the owner tweaks it (the blurb, the limits, the
toggles) and approves, and only then is the secret link minted and shown to them once.

Use it when the owner wants to collect a specific, bounded set of information from someone
else (a relative's medical history, a contractor's quote, a new contact's details). Fill
the brief by interviewing the owner: which subject the information is about, which domain,
exactly what to collect, and whether it's for one person or several. Draft a warm opening
blurb AND a short interviewer persona (persona_brief) that sets the tone. Everything you
pass is a proposed DEFAULT the owner edits before approving — so propose sensible values
and tell them you've staged it for review.
