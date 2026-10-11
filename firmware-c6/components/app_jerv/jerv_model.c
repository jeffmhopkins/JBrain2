#include "jerv_model.h"

#include <stddef.h>

bool jerv_shows_listen(const msg_t *m, bool tts_on, uint32_t speaking_id)
{
    return !m->mine && !m->voice && !m->pending && !tts_on && m->id != speaking_id;
}

const char *jerv_talk_toast(talk_outcome_t o)
{
    switch (o) {
    case TALK_CANCELLED: return "Cancelled - nothing sent";
    case TALK_TOO_SHORT: return "Hold KEY the whole time you talk";
    default: return NULL;
    }
}
