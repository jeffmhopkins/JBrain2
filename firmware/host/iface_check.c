/* Compiles the 0.4.0 interface headers on the host (plan §15) before their implementations exist,
   so a header that pulls in ESP-IDF or does not parse fails here. Deleted once `stream.c`,
   `jpanel_core.c` and `panel_sm.c` are in SRC and include them. */

#include "jpanel_core.h"
#include "jpanel_types.h"
#include "link_types.h"
#include "panel_sm.h"
#include "stream.h"

_Static_assert(JP_SEQ_NONE == 0, "seq 0 is none");
_Static_assert(JP_UI_MARGIN_MS + 2000u < JP_PLAY_EXPIRE_MS, "a PLAY is claimable");
_Static_assert(sizeof(((jp_view_t *)0)->waiting.q) / sizeof(jp_who_t) == JPANEL_QUEUE_MAX,
               "the waiting list holds the box's queue cap");
_Static_assert(STREAM_SLOT_N == 5, "five cue slots");
