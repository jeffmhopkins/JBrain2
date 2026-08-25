# Vendored chat templates

llama-server renders each model's prompt from the chat template embedded in its GGUF (via
`--jinja`). One template here **overrides** that embedded one with `--chat-template-file`, to
fix a prompt-cache problem the stock template causes.

## `gpt-oss-120b.jinja`

A byte-for-byte copy of llama.cpp's official harmony template
(`models/templates/openai-gpt-oss-120b.jinja`, which matches the `ggml-org/gpt-oss-120b-GGUF`
weights the box runs — same upstream) with **one change**: the live `Current date:
strftime_now(...)` line is moved out of the *system* message (the very head of the prompt)
to the **end of the developer block** (the tail of the static persona+tools prefix).

**Why.** The harmony template injects today's date into the prompt's leading tokens. That
date is invisible to the KV-prefix store's fingerprint (`backend/src/jbrain/llm/kv_prefix.py`)
because llama-server renders it, not us — so a warm/disk-restored ~29k-token prefix saved on
one day no longer matched the next day's rendered prompt at that token, and the whole prefix
re-prefilled (~60 s on gpt-oss-120b) while reporting a clean restore. With the date at the
tail, a date rollover re-prefills only the date itself; the persona+tools prefix stays
reusable across days. `strftime_now` is kept, so the model still gets the real date.

**Detection is preserved.** llama.cpp picks the gpt-oss tool-call parser when the template
source contains `<|channel|>` (`common/chat.cpp`). This copy keeps every harmony marker
untouched — only the date line moved — so tool calling parses exactly as before.

### Maintenance — re-diff on every llama.cpp repin

This is a fork, so it can drift from upstream. When the `LOCAL_LLM_BASE` pin moves
(`deploy/Dockerfile.local-llm`), re-vendor it:

```
# from a checkout of llama.cpp at the newly pinned commit
diff models/templates/openai-gpt-oss-120b.jinja deploy/chat-templates/gpt-oss-120b.jinja
```

The only differences should be the two `JBrain patch` blocks (the removed system-message date
line and the added developer-tail date line). If upstream changed anything else, port those
changes in and keep the date-move on top. `test_gpt_oss_chat_template.py` guards the shape
(date at the tail, harmony markers intact) but cannot see upstream drift — this diff is the
check that does.
