#!/bin/sh
# A TOUCH THAT WAKES A DARK SCREEN BUYS THE SCREEN AND NOTHING ELSE, a swipe included.
#
# `display.c`'s wake branch swallows the press, so the swipe detector never re-anchors to it; a
# branch that leaves the PREVIOUS press's point in `s_down_x`/`s_down_y` lets a finger that drags
# on the next frame be measured from there, and it becomes a swipe. 0.3.39 forgot the point on a
# wake. `display.c` has no host harness, so this pins the branch itself.
set -eu
main="${1:-../main}/display.c"

block=$(awk '
    /if \(s_sleep == SCREEN_DARK && \(tapped \|\| down\)\) \{/ { inside = 1; next }
    inside && /^        \}$/ { exit }
    inside && $0 !~ /^[[:space:]]*(\/\*|\*)/ { print }
' "$main")
if [ -z "$block" ]; then
    echo "display.c: the dark-screen wake branch is gone (test_wake_press_starts_no_swipe)" >&2
    exit 1
fi
for want in 's_down_x = -1;' 's_down_y = -1;'; do
    if ! printf '%s\n' "$block" | grep -qF "$want"; then
        echo "display.c: a wake press keeps the last press's point ($want missing)" >&2
        exit 1
    fi
done
echo "test_wake_press_starts_no_swipe: OK"
