#include "convo_model.h"

#include <stddef.h>

bool convo_shows_listen(const msg_t *m, bool tts_on, uint32_t speaking_id)
{
    return !m->mine && !m->voice && !m->pending && !tts_on && m->id != speaking_id;
}

convo_redraw_t convo_redraw(const convo_sig_t *drawn, const convo_sig_t *now)
{
    if (drawn->rev != now->rev || drawn->speaking != now->speaking || drawn->tts != now->tts ||
        drawn->can_speak != now->can_speak || drawn->working != now->working)
        return CONVO_REBUILD;
    return drawn->work_seq != now->work_seq ? CONVO_WORK : CONVO_SAME;
}

const char *convo_talk_toast(talk_outcome_t o)
{
    switch (o) {
    case TALK_CANCELLED: return "Cancelled — nothing sent";
    case TALK_TOO_SHORT: return "Hold KEY the whole time you talk";
    default: return NULL;
    }
}
