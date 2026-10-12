#!/bin/sh
# A BROKEN IMAGE MUST ALWAYS BE REPLACEABLE OVER THE AIR (plan §6a, gate 4).
#
# The image is marked valid before the tasks start, so the bootloader will not roll back a crash
# that comes later — 0.3.39's PHY abort came 3-4 s into every boot, after the manifest check. What
# saved those panels was the ORDER in `app_main`: reach the box, `ota_confirm_health`, report, the
# manifest check and `ota_apply`, and only then speech, talk, voice post, the nudge and the
# watchdogs. Every wave keeps that order; this pins it, line by line, inside `app_main`.
set -eu
main="${1:-../main}/main.c"

line_of() {
    awk -v pat="$1" '
        /^void app_main\(void\)/ { inside = 1 }
        inside && index($0, pat) && $0 !~ /^[[:space:]]*(\/\*|\*)/ { print NR; exit }
    ' "$main"
}

fail=0
apply=$(line_of 'ota_apply(')
health=$(line_of 'ota_confirm_health(reachable)')
if [ -z "$apply" ] || [ -z "$health" ]; then
    echo "main.c: app_main no longer checks for an update (ota_apply / ota_confirm_health)" >&2
    exit 1
fi
for start in 'speech_start(' 'talk_start(' 'jpanel_start(' 'nudge_start(' 'wd_start('; do
    at=$(line_of "$start")
    if [ -z "$at" ]; then
        echo "main.c: $start is not started from app_main" >&2
        fail=1
    elif [ "$at" -le "$apply" ] || [ "$at" -le "$health" ]; then
        echo "main.c: $start (line $at) runs before the update check (line $apply)" >&2
        fail=1
    fi
done
if [ "$fail" -ne 0 ]; then exit 1; fi
echo "test_update_check_precedes_task_starts: OK"
