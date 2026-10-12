#!/bin/sh
# THE BOOT REPORT CARRIES A REAL STACK HIGH-WATER MARK (#1690).
#
# 0.4.3 moved `face_task`'s stack sample from every pass to the 10 s beat, so `s_stack_free` stayed
# 0 until then, and the boot report, sent within those seconds and the one that carries
# `reset_reason` and `crash_phase`, said the stack had been used up. `display.c` has no host
# harness, so this pins the order: one sample in `face_task` before its loop begins.
set -eu
main="${1:-../main}/display.c"

before=$(awk '
    /^static void face_task\(void \*arg\)/ { inside = 1; next }
    inside && /^    while \(true\) \{/ { exit }
    inside && $0 !~ /^[[:space:]]*(\/\*|\*)/ { print }
' "$main")
if [ -z "$before" ]; then
    echo "display.c: face_task or its loop moved (test_stack_sampled_before_first_report)" >&2
    exit 1
fi
if ! printf '%s\n' "$before" | grep -qF 's_stack_free = (int)uxTaskGetStackHighWaterMark(NULL);'; then
    echo "display.c: face_task reports stack_free 0 until its first 10 s beat (#1690)" >&2
    exit 1
fi
echo "test_stack_sampled_before_first_report: OK"
