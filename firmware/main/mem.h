#pragma once

/* WHERE THE MEMORY WENT, ON THE GLASS'S OWN TERMS.
 *
 * Two versions in a row were misdiagnosed from the boot log because the boot log did not
 * carry the one number that mattered. 0.2.37 crash-looped and read as "ESP-SR's runtime took
 * the RAM"; the truth was 117 KB of STATIC `.bss` belonging to a model that never runs, and
 * it took `idf.py size-components` on a host to see it — a tool nobody has when a panel is on
 * a wall.
 *
 * So the panel now says it itself. `mem_log` prints free and largest-block for internal RAM
 * and for PSRAM, and it is called at every boot stage that takes a big bite. Internal is the
 * scarce one: Wi-Fi's DMA descriptors and mbedTLS's handshake buffers can live nowhere else,
 * and there are ~341 KB of DIRAM on this chip against 8 MB of PSRAM.
 *
 * LARGEST BLOCK, not just free, because they fail differently: a driver asking for one
 * contiguous 32 KB buffer fails with 60 KB free and fragmented, and "free" alone makes that
 * look impossible.
 */
void mem_log(const char *stage);

/* Free internal heap, for a caller that wants to decide rather than print. */
unsigned mem_free_internal(void);
