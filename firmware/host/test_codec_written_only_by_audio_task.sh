#!/bin/sh
# THE AUDIO TASK OWNS THE CODEC (`audio.c`, "ONE TASK OWNS THE CODEC"). `esp_codec_dev` has no
# lock, and a register poke from the main task while the audio task sat in `esp_codec_dev_read`
# panicked a panel on 2026-09-21. The setters the main task calls only record; the two functions
# that write the codec's control registers are called from `audio_task` and nowhere else. 0.4.0's
# first cut had `audio_set_agc` call `alc_apply` directly. `audio.c` has no host harness, so this
# pins the call sites.
set -eu
main="${1:-../main}/audio.c"

bad=$(awk '
    /^[a-z][a-z_ ]*[ *]([a-z_][a-z0-9_]*)\(.*\)$/ {
        fn = $0
        sub(/\(.*/, "", fn)
        sub(/.*[ *]/, "", fn)
    }
    $0 ~ /^[[:space:]]*(\/\*|\*)/ { next }
    /(alc_apply|apply_levels)\(/ && $0 !~ /^static void (alc_apply|apply_levels)\(void\)/ {
        if (fn != "audio_task") print NR ": " fn ": " $0
    }
' "$main")
if [ -n "$bad" ]; then
    printf '%s\n' "$bad" >&2
    echo "audio.c: the codec is written from outside audio_task (lines above)" >&2
    exit 1
fi
echo "test_codec_written_only_by_audio_task: OK"
