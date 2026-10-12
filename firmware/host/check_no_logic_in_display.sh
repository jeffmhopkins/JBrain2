#!/bin/sh
# `display.c` SAMPLES, EXECUTES AND DRAWS; IT DOES NOT DECIDE (plan §8.1).
#
# Until 0.4.0 the panel's interaction logic lived twice — a chain of `if`s in `display.c` and the
# table in `ui.c` — and every touch fault the owner reported was the two disagreeing. The machine
# (`panel_sm.c`) is now the only place it lives, and this check fails the build if it starts to
# grow back:
#
#   1. a static in `display.c` (file scope or function-local) whose name, with `s_` stripped, is a
#      field of `sm_t` — the machine's state kept a second time;
#   2. a call to a `jpanel_*` or `stream_*` function outside the executor's list — the engine or
#      the speaker driven from somewhere the machine cannot see. Comments count, as in
#      `check_no_logic_in_audio`, so nobody argues about what a mention means.
#
# A renamed duplicate passes this; the review covers that case.
set -eu
main="${1:-../main}"

# Every field declared inside `sm_t`, nested structs included.
fields=$(awk '
    /^typedef struct \{/ { body = ""; inside = 1; next }
    inside && /^\} sm_t;/ { print body; exit }
    inside && /^\} [a-z_]+;/ { inside = 0; next }
    inside { body = body "\n" $0 }
' "$main/panel_sm.h" | sed 's|/\*.*\*/||; s|//.*||' | awk '
    /;/ {
        line = $0
        sub(/;.*/, "", line)
        gsub(/\[[^]]*\]/, "", line)
        n = split(line, part, ",")
        for (i = 1; i <= n; i++) {
            w = part[i]
            gsub(/^[ \t]+|[ \t]+$/, "", w)
            m = split(w, word, /[ \t*]+/)
            if (m > 0 && word[m] ~ /^[a-z_][a-z0-9_]*$/) print word[m]
        }
    }' | sort -u)
if [ -z "$fields" ]; then
    echo "check_no_logic_in_display: could not read sm_t's fields from panel_sm.h" >&2
    exit 1
fi

fail=0
# The names every `static` declaration in display.c introduces (not functions).
statics=$(grep -E '(^|[^a-z_])static[[:space:]]' "$main/display.c" |
    sed -E 's/.*(^|[^a-z_])static[[:space:]]/static /' | grep -vE '^static[^=;(]*\(' |
    sed 's|/\*.*||; s|=.*||; s|;.*||; s|\[[^]]*\]||g; s|[[:space:]]*$||' |
    awk '{ n = split($0, w, /[ \t*]+/); if (n > 1) print w[n] }' | sed 's/^s_//' | sort -u)
for f in $fields; do
    if printf '%s\n' "$statics" | grep -qx "$f"; then
        echo "display.c: a static named '$f' (or 's_$f') duplicates sm_t.$f" >&2
        fail=1
    fi
done

allowed='jpanel_snapshot|jpanel_post|jpanel_poll_soon|stream_stop|stream_pause_req|stream_cue_[a-z_]+|stream_cue|stream_silence|stream_facts|stream_gen|stream_lease_take|stream_lease_release|stream_leased'
if grep -noE '\b(jpanel|stream)_[a-z_]+\(' "$main/display.c" | grep -vE ":($allowed)\(\$"; then
    echo "display.c: the calls above are not the executor's (check_no_logic_in_display)" >&2
    fail=1
fi

if [ "$fail" -ne 0 ]; then exit 1; fi
echo "check_no_logic_in_display: OK ($(printf '%s\n' "$fields" | wc -l | tr -d ' ') sm_t fields)"
