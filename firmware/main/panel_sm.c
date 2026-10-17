/* The panel's state machine (plan §8): one mode, one set of targets per mode, one face function,
 * and every effect in order. See `panel_sm.h` for the contract and the frame-boundary rules.
 *
 * The step's order is the plan's (§8.2 "Order within a frame") and is load-bearing: the button,
 * then the finger, then the voice, resolved against what the previous frame drew; the hold; the
 * engine's outcomes; the backstops and mode timers; posting a pending play; the pet turn and the
 * selection; the follow-up on the reply's end; new messages and the chime. */

#include "panel_sm.h"

#include <string.h>

#include "audio.h"   /* AUDIO_PAUSE_MAX_MS */
#include "confirm.h" /* confirm_hit, sendto_hit, the disc geometry */
#include "face.h"    /* FACE_W */
#include "gesture.h" /* gesture_reserved */

_Static_assert(PENDING_MS >= PLAY_BUF_MS + STREAM_SLOT_N * CUE_MAX_MS,
               "a press outlasts a pet reply and a cue in every slot ahead of it");
_Static_assert(CUE_MAX_MS * AUDIO_RATE / 1000 == CUE_MAX_SAMPLES, "CUE_MAX_MS is cue.h's ceiling");
_Static_assert(EXIT_GRACE_MS == HOLD_TALK_MS, "one measure of meant-rather-than-spilled");

/* ── small helpers ───────────────────────────────────────────────────────────────────────── */

/* A timestamp that can be stored where 0 means "none": off by a millisecond once in 49 days. */
static uint32_t stamp(uint32_t now)
{
    return now == 0 ? 1 : now;
}

static void fx(sm_out_t *out, sm_fx_kind_t kind, int arg, int slot)
{
    if (out->n >= SM_FX_MAX) return; /* test_the_action_list_never_overflows holds the bound */
    out->fx[out->n].kind = kind;
    out->fx[out->n].arg = arg;
    out->fx[out->n].slot = slot;
    out->n++;
}

static void cue(sm_out_t *out, cue_t c, stream_slot_t slot)
{
    fx(out, SM_FX_CUE, (int)c, (int)slot);
}

static bool id_eq(const char *a, const char *b)
{
    return a[0] != '\0' && strcmp(a, b) == 0;
}

static void str_copy(char *dst, size_t cap, const char *src)
{
    const size_t n = strnlen(src, cap - 1);
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static void id_copy(char dst[JP_ID_MAX], const char *src)
{
    str_copy(dst, JP_ID_MAX, src);
}

static int clampi(int v, int lo, int hi)
{
    return v < lo ? lo : (v > hi ? hi : v);
}

static const jp_who_t NOBODY_WHO;

/* ── what the engine is doing with this machine's PLAY ───────────────────────────────────── */

/* Taken by the engine and not yet ended. A finished `cur` is never claimed. */
static bool claimed(const sm_t *sm, const jp_view_t *s)
{
    return sm->play.seq != JP_SEQ_NONE && s->claimed_seq == sm->play.seq &&
           !jp_seq_ge(s->done_seq, sm->play.seq) && s->cur.seq == sm->play.seq;
}

/* AUDIBLE, which is the only state in which PAUSE and REPLY mean anything. `stream_live` is the
   term that matters in a run's gap: {stream_id, out_ms} keep their last values after the ring
   drains, and a pause armed for a stream that has ended would outlive it. */
static bool on(const sm_t *sm, const sm_in_t *in)
{
    return sm->mode == SM_PLAY && claimed(sm, &in->snap) && in->audio.stream_id != 0 &&
           in->audio.stream_id == in->snap.cur.stream_id && in->audio.out_ms > 0 &&
           in->audio.stream_live;
}

static bool holding(const sm_t *sm)
{
    return sm->stopped_seq != JP_SEQ_NONE;
}

/* ── the selection set S (§8.2) ──────────────────────────────────────────────────────────── */

#define S_CAP (JPANEL_QUEUE_MAX + 1)

typedef struct {
    const jp_who_t *e[S_CAP];
    int n;      /* how many the face, the swipe and the counter range over */
    int extra;  /* the undescribed rest the counter adds (ids only) */
    bool ids;
} sel_set_t;

static int set_find(const sel_set_t *S, const char *id)
{
    if (!S->ids || id[0] == '\0') return -1;
    for (int i = 0; i < S->n && i < S_CAP; i++) {
        if (strcmp(S->e[i]->id, id) == 0) return i;
    }
    return -1;
}

/* The waiting list in the box's order, plus the message playing in a non-replay PLAY at the
   position it was taken from — so `cur` keeps one place whether or not the engine has dropped it
   from `waiting` yet, and a swipe's ends do not move at the stream's first byte. */
static void build_set(const sm_t *sm, const jp_view_t *s, sel_set_t *S)
{
    memset(S, 0, sizeof(*S));
    S->ids = s->waiting.has_ids;
    const bool with_cur = sm->mode == SM_PLAY && !sm->play.replay && claimed(sm, s) && !s->cur.replay;
    const int nq = clampi(s->waiting.n, 0, JPANEL_QUEUE_MAX);
    if (S->ids) {
        for (int i = 0; i < nq; i++) {
            if (s->waiting.q[i].id[0] == '\0' || set_find(S, s->waiting.q[i].id) >= 0) continue;
            S->e[S->n++] = &s->waiting.q[i];
        }
        if (with_cur && s->cur.who.id[0] != '\0' && !s->cur.in_waiting &&
            set_find(S, s->cur.who.id) < 0) {
            const int at = clampi(s->cur.pos, 0, S->n);
            for (int i = S->n; i > at; i--) S->e[i] = S->e[i - 1];
            S->e[at] = &s->cur.who;
            S->n++;
        }
        S->extra = s->waiting.count > s->waiting.n ? s->waiting.count - s->waiting.n : 0;
        return;
    }
    /* No ids (an old box): a count, of which only the head is described. */
    int n = s->waiting.count - (with_cur && s->cur.in_waiting ? 1 : 0) + (with_cur ? 1 : 0);
    S->n = n < 0 ? 0 : n;
    const jp_who_t *head = nq > 0 ? &s->waiting.q[0] : &NOBODY_WHO;
    const int cur_at = with_cur ? clampi(s->cur.pos, 0, S_CAP - 1) : -1;
    for (int i = 0; i < S_CAP; i++) S->e[i] = i == cur_at ? &s->cur.who : head;
}

static const jp_who_t *set_at(const sel_set_t *S, int i)
{
    if (S->n <= 0) return NULL;
    return S->e[clampi(i, 0, (S->n < S_CAP ? S->n : S_CAP) - 1)];
}

/* How far a swipe or a face press can move: what the box described, and no further. */
static int set_reach(const sel_set_t *S)
{
    return S->n < S_CAP ? S->n : S_CAP;
}

static int sel_index(const sm_t *sm, const sel_set_t *S)
{
    const int i = set_find(S, sm->sel.id);
    return i >= 0 ? i : clampi(sm->sel.index, 0, S->n > 0 ? set_reach(S) - 1 : 0);
}

static void sel_set(sm_t *sm, const sel_set_t *S, int i)
{
    sm->sel.index = i;
    const jp_who_t *w = set_at(S, i);
    id_copy(sm->sel.id, w != NULL ? w->id : "");
}

/* What the notice and the face show in PET: the selected entry; while a stop's outcome is pending
   and its id has left S, the stopped message itself. */
static const jp_who_t *selected(const sm_t *sm, const jp_view_t *s)
{
    sel_set_t S;
    build_set(sm, s, &S);
    if (set_find(&S, sm->sel.id) >= 0) return set_at(&S, set_find(&S, sm->sel.id));
    if (holding(sm) && id_eq(sm->sel.id, sm->stopped_who.id)) return &sm->stopped_who;
    if (holding(sm) && !S.ids && S.n == 0) return &sm->stopped_who;
    return set_at(&S, sel_index(sm, &S));
}

/* Re-resolve, every frame: by id, then by index (§8.2). */
static void resolve_sel(sm_t *sm, const jp_view_t *s)
{
    sel_set_t S;
    build_set(sm, s, &S);
    if (sm->mode == SM_PLAY && !sm->play.replay && claimed(sm, s)) {
        /* In a non-replay PLAY the selection IS the message playing. */
        const int i = set_find(&S, s->cur.who.id);
        if (i >= 0) {
            sel_set(sm, &S, i);
        } else if (!S.ids) {
            sm->sel.index = clampi(s->cur.pos, 0, S.n > 0 ? S.n - 1 : 0);
        }
        return;
    }
    if (holding(sm)) return; /* the stopped message keeps the selection until its outcome */
    if (S.n == 0) {
        sm->sel.index = 0;
        sm->sel.id[0] = '\0';
        return;
    }
    if (!S.ids) {
        sm->sel.index = clampi(sm->sel.index, 0, set_reach(&S) - 1);
        return;
    }
    sel_set(sm, &S, sel_index(sm, &S));
}

static bool select_step(sm_t *sm, const jp_view_t *s, int delta, bool wrap)
{
    sel_set_t S;
    build_set(sm, s, &S);
    const int n = set_reach(&S);
    if (n <= 1) return false;
    const int was = sel_index(sm, &S);
    int want = was + delta;
    want = wrap ? ((want % n) + n) % n : clampi(want, 0, n - 1);
    if (want == was) return false;
    sel_set(sm, &S, want);
    return true;
}

/* ── what is drawn ───────────────────────────────────────────────────────────────────────── */

static bool think(const sm_t *sm)
{
    return sm->mode == SM_PET && sm->turn.state != SM_TURN_NONE;
}

static bool notice_drawn(const sm_t *sm, const jp_view_t *s)
{
    return sm->mode == SM_PET && !think(sm) && (s->waiting.count > 0 || holding(sm));
}

/* Hidden, not cleared, while anything waits: a waiting message takes the corner (B3). */
static bool offer_visible(const sm_t *sm, const jp_view_t *s)
{
    return sm->mode == SM_PET && sm->offer.up && s->waiting.count == 0 && !holding(sm);
}

static bool offer_standing(const sm_t *sm, uint32_t now)
{
    return sm->offer.up && jp_due(now, sm->offer.since + REPEAT_MS);
}

static bool notice_big(const sm_t *sm, uint32_t now)
{
    return sm->notice.big &&
           (sm->notice.first_visible == 0 || !jp_due(now, sm->notice.first_visible + POPUP_BIG_MS));
}

void sm_notice_box(int y0, int over_h, bool big, int box[4])
{
    if (big) {
        box[0] = (FACE_W - SM_NOTICE_BIG_W) / 2;
        box[1] = y0 + (over_h - y0 - SM_NOTICE_BIG_H) / 2;
        box[2] = box[0] + SM_NOTICE_BIG_W;
        box[3] = box[1] + SM_NOTICE_BIG_H;
        return;
    }
    box[0] = 0;
    box[1] = y0;
    box[2] = FACE_W / 2;
    box[3] = y0 + (over_h - y0) / 2;
}

bool sm_face(const sm_t *sm, const jp_view_t *snap, jp_who_t *out)
{
    const jp_who_t *w = NULL;
    switch (sm->mode) {
    case SM_PLAY:
        w = claimed(sm, snap) ? &snap->cur.who : &sm->play.target;
        break;
    case SM_RECORD:
        w = &sm->rec.who;
        break;
    case SM_LISTEN: /* the pet's own icon, which is not a person */
    case SM_GRID:   /* the cells are the faces */
        break;
    case SM_PET:
        if (offer_visible(sm, snap)) {
            w = &sm->offer.who;
        } else if (notice_drawn(sm, snap)) {
            w = selected(sm, snap);
        }
        break;
    }
    if (w == NULL) {
        if (out != NULL) *out = NOBODY_WHO;
        return false;
    }
    if (out != NULL) *out = *w;
    return true;
}

bool sm_busy(const sm_t *sm, const jp_view_t *snap)
{
    return sm->mode != SM_PET || sm->turn.state != SM_TURN_NONE || sm->send.seq != JP_SEQ_NONE ||
           snap->phase == JP_FETCHING || snap->phase == JP_SOUNDING;
}

const char *sm_dash_name(sm_dash_t why)
{
    return why == SM_DASH_NET ? "net" : why == SM_DASH_TIMEOUT ? "timeout" : "";
}

/* ── where a finger lands ────────────────────────────────────────────────────────────────── */

static bool exit_quad(int x, int y, int over_h)
{
    return y >= 0 && y < over_h / 2 && x >= FACE_W / 2 && x < FACE_W;
}

static bool face_quad(int x, int y, int y0, int over_h)
{
    return x >= 0 && x < FACE_W / 2 && y >= y0 && y < y0 + (over_h - y0) / 2;
}

static bool in_box(const int b[4], int x, int y)
{
    return x >= b[0] && x < b[2] && y >= b[1] && y < b[3];
}

static bool disc(int x, int y, int cx, int cy)
{
    const int dx = x - cx, dy = y - cy;
    return dx * dx + dy * dy <= CONFIRM_HIT_R * CONFIRM_HIT_R;
}

static bool graced(const sm_t *sm, uint32_t now)
{
    return sm->mode_press_at != 0 && !jp_due(now, sm->mode_press_at + EXIT_GRACE_MS);
}

static sm_target_t pet_target(const sm_t *sm, const sm_in_t *in, int x, int y)
{
    const jp_view_t *s = &in->snap;
    const bool g = graced(sm, in->now);
    if (notice_drawn(sm, s)) {
        int box[4];
        sm_notice_box(in->over_y0, in->over_h, notice_big(sm, in->now), box);
        if (in_box(box, x, y)) return g ? SM_T_IGNORED : SM_T_NOTICE;
        return SM_T_PET;
    }
    if (!offer_visible(sm, s)) return SM_T_PET;
    if (exit_quad(x, y, in->over_h)) return g ? SM_T_IGNORED : SM_T_EXIT;
    sm_target_t t = SM_T_PET;
    if (offer_standing(sm, in->now)) {
        /* Standing: only the drawn discs, so the rest of the pet is the pet again (owner Q1). */
        const int cy = confirm_cy(in->over_h);
        if (disc(x, y, CONFIRM_CX_CANCEL, cy)) t = SM_T_AGAIN;
        if (disc(x, y, CONFIRM_CX_SEND, cy)) t = SM_T_REPLY;
    } else {
        const confirm_hit_t h = confirm_hit(x, y, in->over_h);
        if (h == CONFIRM_CANCEL) t = SM_T_AGAIN;
        if (h == CONFIRM_SEND) t = SM_T_REPLY;
    }
    return (t != SM_T_PET && g) ? SM_T_IGNORED : t;
}

sm_target_t sm_target(const sm_t *sm, const sm_in_t *in, int x, int y)
{
    const bool g = graced(sm, in->now);
    switch (sm->mode) {
    case SM_GRID: {
        sm_target_t t = SM_T_SWALLOW; /* modal: a miss costs a press, never a poke */
        const sendto_hit_t h = sendto_hit(x, y, in->over_h);
        if (sm->grid_page == SM_GRID_GAMES) {
            /* Dodge top left, Jetpack top right; the spare corner swallows until there is a
               third. */
            if (h == SENDTO_SISTER) t = SM_T_CELL_GAME;
            if (h == SENDTO_DAD) t = SM_T_CELL_JET;
            if (h == SENDTO_CANCEL) t = SM_T_CELL_CANCEL;
        } else {
            switch (h) {
            case SENDTO_SISTER: t = SM_T_CELL_SISTER; break;
            case SENDTO_DAD: t = SM_T_CELL_DAD; break;
            case SENDTO_CANCEL: t = SM_T_CELL_CANCEL; break;
            case SENDTO_PET: t = SM_T_CELL_PET; break;
            case SENDTO_NONE: break;
            }
        }
        if (t != SM_T_SWALLOW && g) {
            const int dx = x - sm->hold_x, dy = y - sm->hold_y;
            if (dx * dx + dy * dy <= SM_HOLD_GRACE_PX * SM_HOLD_GRACE_PX) return SM_T_IGNORED;
        }
        return t;
    }
    case SM_LISTEN:
    case SM_RECORD: {
        /* A miss is ignored without a flinch: a pet that twitches mid-sentence invites the next
           poke. */
        const confirm_hit_t h = confirm_hit(x, y, in->over_h);
        if (h == CONFIRM_NONE || g) return SM_T_IGNORED;
        return h == CONFIRM_CANCEL ? SM_T_CROSS : SM_T_TICK;
    }
    case SM_PLAY: {
        if (exit_quad(x, y, in->over_h)) {
            return jp_due(in->now, sm->play.since + EXIT_GRACE_MS) ? SM_T_EXIT : SM_T_IGNORED;
        }
        if (face_quad(x, y, in->over_y0, in->over_h)) {
            if (sm->play.replay) return SM_T_IGNORED;
            sel_set_t S;
            build_set(sm, &in->snap, &S);
            if (set_reach(&S) <= 1) return SM_T_SWALLOW;
            return g ? SM_T_IGNORED : SM_T_FACE;
        }
        const confirm_hit_t h = confirm_hit(x, y, in->over_h);
        if (h == CONFIRM_NONE) return SM_T_SWALLOW;
        if (!on(sm, in)) return SM_T_IGNORED; /* nothing audible to pause or answer yet */
        if (h == CONFIRM_CANCEL) return SM_T_PAUSE;
        /* No envelope is drawn while it talks, so the finger there presses nothing. */
        return sm->play.pause_on ? SM_T_REPLY : SM_T_SWALLOW;
    }
    case SM_PET:
        return pet_target(sm, in, x, y);
    }
    return SM_T_PET;
}

/* ── transitions ─────────────────────────────────────────────────────────────────────────── */

static void enter(sm_t *sm, sm_mode_t m, uint32_t now, bool by_press)
{
    sm->mode = m;
    sm->since = now;
    /* Cleared whenever the mode changes without a press: a PLAY that fails inside the grace must
       not leave AGAIN or the notice silently ignored. */
    sm->mode_press_at = by_press ? stamp(now) : 0;
}

static uint32_t next_seq(sm_t *sm)
{
    if (sm->next_seq == JP_SEQ_NONE) sm->next_seq = 1;
    return sm->next_seq++;
}

static void disarm_follow(sm_t *sm)
{
    sm->talk.follow_armed = false;
}

/* The mic gate (today's rule): nothing else may be using the microphone, the speaker or the
   capture buffer. `reply_in_play` is REPLY during playback, whose own stream this transition is
   about to stop. */
static bool mic_gate(const sm_t *sm, const sm_in_t *in, bool reply_in_play)
{
    if (sm->mode == SM_LISTEN || sm->mode == SM_RECORD) return false;
    if (sm->mode == SM_PLAY && !reply_in_play) return false;
    if (sm->turn.state != SM_TURN_NONE || sm->standby || in->leased || in->net == SM_NET_BUSY ||
        !in->speech_ok) {
        return false;
    }
    if (in->audio.reply_busy || in->audio.clip_busy) return false;
    if (!reply_in_play && in->audio.speaker_busy) return false;
    /* A SEND posted this frame takes the lease after the step. */
    if (sm->send.seq != JP_SEQ_NONE && sm->send.posted_at == in->now) return false;
    return true;
}

static void refuse(sm_out_t *out)
{
    cue(out, CUE_BUSY, STREAM_SLOT_REFUSAL);
}

static void open_capture(sm_t *sm, sm_out_t *out)
{
    sm->capture = true;
    fx(out, SM_FX_CAPTURE_OPEN, 0, 0);
}

static void close_capture(sm_t *sm, sm_out_t *out, bool drop)
{
    if (drop && sm->capture) fx(out, SM_FX_CAPTURE_DROP, 0, 0);
    sm->capture = false;
}

static void open_record(sm_t *sm, const sm_in_t *in, sm_out_t *out, const jp_who_t *who,
                        bool by_press)
{
    enter(sm, SM_RECORD, in->now, by_press);
    sm->rec.who = *who;
    sm->rec.to = who->from_dad ? JP_TO_DAD : JP_TO_PANEL;
    sm->rec.heard = false;
    sm->rec.deliberate = true;
    sm->rec.hush = 0;
    sm->rec.opened_at = in->now;
    /* A message is not a turn: the microphone must not reopen after one. */
    disarm_follow(sm);
    open_capture(sm, out);
}

/* Who a recording addressed by kind (a grid cell, "tell sister") is for. */
static jp_who_t addressee(const sm_in_t *in, jp_to_t to)
{
    jp_who_t w = NOBODY_WHO;
    w.from_dad = to == JP_TO_DAD;
    if (to == JP_TO_PANEL) {
        str_copy(w.from, JP_NAME_MAX, in->snap.waiting.sibling);
    }
    return w;
}

static void open_listen(sm_t *sm, const sm_in_t *in, sm_out_t *out, bool follow, bool by_press)
{
    enter(sm, SM_LISTEN, in->now, by_press);
    sm->talk.voice = true; /* every listen is hands-free; the held listen is not ported */
    sm->talk.heard = false;
    sm->talk.hush = 0;
    sm->talk.lead = follow ? FOLLOW_LEAD_MS : LISTEN_LEAD_MS;
    sm->talk.opened_at = in->now;
    sm->talk.follow = follow;
    sm->talk.follow_turns = follow ? sm->talk.follow_turns + 1 : 0;
    open_capture(sm, out);
}

static void post(sm_out_t *out, const jp_cmd_t *cmd)
{
    out->post = *cmd;
    fx(out, SM_FX_POST, (int)cmd->kind, 0);
}

static void send_capture(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    jp_cmd_t cmd = {0};
    cmd.kind = JP_CMD_SEND;
    cmd.seq = next_seq(sm);
    cmd.expires_at = in->now + JP_SEND_EXPIRE_MS;
    cmd.to = sm->rec.to;
    sm->send.seq = cmd.seq;
    sm->send.posted_at = in->now;
    sm->send.expires_at = cmd.expires_at;
    sm->capture = false; /* the executor closes it into the post */
    post(out, &cmd);
}

static void send_talk(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    sm->capture = false;
    fx(out, SM_FX_SEND_TALK, 0, 0);
    sm->turn.state = SM_TURN_THINKING;
    sm->turn.since = in->now;
    enter(sm, SM_PET, in->now, false);
}

/* A child-chosen stop of a POSTED play holds the selection until its outcome (§8.2). */
static void hold_selection(sm_t *sm, const sm_in_t *in)
{
    if (sm->play.seq == JP_SEQ_NONE) return;
    sm->stopped_seq = sm->play.seq;
    sm->stopped_expires = sm->play.expires_at + JP_UI_MARGIN_MS;
    jp_who_t w;
    (void)sm_face(sm, &in->snap, &w);
    sm->stopped_who = w;
}

typedef enum { END_NATURAL, END_FAIL, END_CHILD } play_end_t;

/* Every exit from PLAY emits PAUSE(0); every exit but a natural finish also emits STOP, ahead of
   the PAUSE(0) so a paused message is never released to sound before the stop lands; a stop the
   child chose cuts whatever sounds first, so its own cue is never dropped behind it. */
static void play_end(sm_t *sm, const sm_in_t *in, sm_out_t *out, play_end_t how, cue_t c,
                     bool by_press)
{
    if (how == END_CHILD) {
        hold_selection(sm, in);
        fx(out, SM_FX_SILENCE, 0, 0);
    }
    if (how != END_NATURAL) fx(out, SM_FX_STOP, 0, 0);
    fx(out, SM_FX_PAUSE, 0, sm->play.pause_stream);
    if (how == END_CHILD) {
        cue(out, c, STREAM_SLOT_PRESS);
        sm->offer.up = false;
        disarm_follow(sm);
    } else if (how == END_FAIL) {
        cue(out, c, STREAM_SLOT_FAIL);
    }
    sm->play.pause_on = false;
    sm->play.seq = JP_SEQ_NONE;
    enter(sm, SM_PET, in->now, by_press);
}

static void start_play(sm_t *sm, const sm_in_t *in, bool replay, const jp_who_t *target)
{
    memset(&sm->play, 0, sizeof(sm->play));
    sm->play.replay = replay;
    sm->play.target = target != NULL ? *target : NOBODY_WHO;
    sm->play.since = in->now;
    sm->play.run_pos = 1;
    enter(sm, SM_PLAY, in->now, true);
}

static void toggle_pause(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    const uint8_t sid = in->snap.cur.stream_id;
    const bool hold = !(sm->play.pause_on && sm->play.pause_stream == sid);
    sm->play.pause_on = hold;
    sm->play.pause_stream = sid;
    sm->play.pause_since = in->now;
    fx(out, SM_FX_PAUSE, hold ? 1 : 0, sid);
    cue(out, hold ? CUE_PAUSE : CUE_RESUME, STREAM_SLOT_PRESS);
}

/* ── the press ───────────────────────────────────────────────────────────────────────────── */

static void press(sm_t *sm, const sm_in_t *in, sm_out_t *out, sm_target_t t)
{
    const jp_view_t *s = &in->snap;
    switch (t) {
    case SM_T_NONE:
        break;
    case SM_T_IGNORED:
        /* Logged and silent: part of the press that opened the mode, which already answered.
           The transport before ON keeps today's flinch. */
        out->tap_missed = true;
        if (sm->mode == SM_PLAY && confirm_hit(in->x, in->y, in->over_h) != CONFIRM_NONE &&
            !exit_quad(in->x, in->y, in->over_h)) {
            fx(out, SM_FX_FLINCH, 0, 0);
        }
        break;
    case SM_T_SWALLOW:
        fx(out, SM_FX_FLINCH, 0, 0);
        break;
    case SM_T_PET:
        out->pet_press = true;
        break;
    case SM_T_NOTICE: {
        if (sm->send.seq != JP_SEQ_NONE) {
            refuse(out);
            break;
        }
        cue(out, CUE_PLAY, STREAM_SLOT_PRESS);
        sm->offer.up = false;
        start_play(sm, in, false, selected(sm, s));
        break;
    }
    case SM_T_AGAIN:
        if (sm->send.seq != JP_SEQ_NONE) {
            refuse(out);
            break;
        }
        cue(out, CUE_PLAY, STREAM_SLOT_PRESS);
        start_play(sm, in, true, &sm->offer.who); /* the offer stays */
        break;
    case SM_T_REPLY: {
        const bool in_play = sm->mode == SM_PLAY;
        if (!mic_gate(sm, in, in_play)) {
            refuse(out); /* and a message that is playing keeps playing */
            break;
        }
        jp_who_t to;
        (void)sm_face(sm, s, &to); /* I2: the face drawn is the person answered */
        if (in_play) {
            hold_selection(sm, in);
            /* STOP before PAUSE(0): no SILENCE here hoists it, and a paused message released
               first sounds again until the stop lands. Heard: answering it is hearing it (§4). */
            fx(out, SM_FX_STOP, 1, 0);
            fx(out, SM_FX_PAUSE, 0, sm->play.pause_stream);
            sm->play.pause_on = false;
            sm->play.seq = JP_SEQ_NONE;
        }
        sm->offer.up = false;
        open_record(sm, in, out, &to, true);
        cue(out, CUE_REPLY, STREAM_SLOT_PRESS);
        break;
    }
    case SM_T_EXIT:
        if (sm->mode == SM_PLAY) {
            play_end(sm, in, out, END_CHILD, CUE_STOP, true);
        } else {
            fx(out, SM_FX_SILENCE, 0, 0);
            cue(out, CUE_STOP, STREAM_SLOT_PRESS);
            sm->offer.up = false;
            disarm_follow(sm);
        }
        break;
    case SM_T_FACE:
        /* CYCLES: a single control with an end is a dead button (JP 0.3.37). */
        if (select_step(sm, s, 1, true)) play_end(sm, in, out, END_CHILD, CUE_HEARD, true);
        break;
    case SM_T_PAUSE:
        toggle_pause(sm, in, out);
        break;
    case SM_T_CROSS:
        close_capture(sm, out, true);
        if (sm->mode == SM_LISTEN) {
            disarm_follow(sm);
            sm->talk.follow_turns = FOLLOW_MAX_TURNS;
        }
        cue(out, CUE_STOP, STREAM_SLOT_PRESS);
        enter(sm, SM_PET, in->now, true);
        break;
    case SM_T_TICK: {
        const bool rec = sm->mode == SM_RECORD;
        if (!(rec ? sm->rec.heard : sm->talk.heard)) {
            /* Nothing heard is not a send, and a child who pressed the tick into silence must
               not be told it went. */
            close_capture(sm, out, true);
            cue(out, CUE_OOPS, STREAM_SLOT_FAIL);
            enter(sm, SM_PET, in->now, true);
            break;
        }
        cue(out, CUE_HEARD, STREAM_SLOT_PRESS);
        if (rec) {
            send_capture(sm, in, out);
            enter(sm, SM_PET, in->now, true);
        } else {
            send_talk(sm, in, out);
            sm->mode_press_at = stamp(in->now);
        }
        break;
    }
    case SM_T_CELL_SISTER:
    case SM_T_CELL_DAD:
    case SM_T_CELL_PET:
    case SM_T_CELL_CANCEL:
    case SM_T_CELL_GAME:
    case SM_T_CELL_JET:
        /* EVERY MENU CELL ACTS ON THE LIFT, NOT THE DOWN EDGE: a swipe that turns the page
           starts with a press, and a cell that acted on it opened the microphone (or a game)
           under the finger before the swipe was known (owner: *"Swipe on menu has conflict with
           selecting a menu item before release"*). The press is silent: its lift answers it
           (`cell_lift`), and a cue now would hold the microphone shut through a quick tap. */
        sm->down.cell_armed = (uint8_t)t;
        break;
    }
}

/* A menu cell's work, on the lift of the press that landed on it and was not a swipe. */
static void cell_lift(sm_t *sm, const sm_in_t *in, sm_out_t *out, sm_target_t t)
{
    switch (t) {
    case SM_T_CELL_SISTER:
    case SM_T_CELL_DAD:
    case SM_T_CELL_PET:
        sm->grid_until = 0;
        if (!mic_gate(sm, in, false)) {
            refuse(out);
            enter(sm, SM_PET, in->now, true);
            break;
        }
        if (t == SM_T_CELL_PET) {
            open_listen(sm, in, out, false, true);
        } else {
            const jp_who_t w = addressee(in, t == SM_T_CELL_DAD ? JP_TO_DAD : JP_TO_PANEL);
            open_record(sm, in, out, &w, true);
        }
        cue(out, CUE_LISTEN, STREAM_SLOT_PRESS);
        break;
    case SM_T_CELL_CANCEL:
        sm->grid_until = 0;
        cue(out, CUE_STOP, STREAM_SLOT_PRESS);
        enter(sm, SM_PET, in->now, true);
        break;
    case SM_T_CELL_GAME:
    case SM_T_CELL_JET:
        sm->grid_until = 0;
        enter(sm, SM_PET, in->now, true);
        out->start_game = t == SM_T_CELL_JET ? SM_GAME_JET : SM_GAME_DODGE;
        break;
    default:
        break;
    }
}

static void on_swipe(sm_t *sm, const sm_in_t *in, sm_out_t *out, int dir)
{
    const jp_view_t *s = &in->snap;
    /* The press that opened this mode IS this swipe's down edge: touch queues the press on the
       down edge and a swipe is recognised only after 60 px of travel, so a smear that began on
       the notice (or a cell) has already pressed it. Unwind it, then apply the swipe. */
    const bool opening = sm->mode_press_at != 0 && sm->down.active && sm->down.at == sm->since;
    /* A SWIPE ON THE MENU TURNS ITS PAGE: left to the games, right back (owner, 0.4.17). A cell
       its down edge landed on is disarmed, so the swipe is only the swipe: since 0.4.18 a cell
       acts on its lift, so there is nothing of it to unwind. */
    if (sm->mode == SM_GRID) {
        sm->down.cell_armed = SM_T_NONE;
        const int page = dir < 0 ? SM_GRID_GAMES : SM_GRID_PEOPLE;
        const bool turned = page != sm->grid_page || sm->mode != SM_GRID;
        sm->grid_page = page;
        if (sm->mode != SM_GRID) enter(sm, SM_GRID, in->now, false);
        sm->grid_until = in->now + SENDTO_MS;
        if (turned) cue(out, CUE_HEARD, STREAM_SLOT_PRESS);
        return;
    }
    switch (sm->mode) {
    case SM_PLAY:
        if (opening) {
            play_end(sm, in, out, END_CHILD, CUE_HEARD, true);
            (void)select_step(sm, s, dir, false);
        } else if (sm->play.replay) {
            out->tap_missed = true;
        } else if (select_step(sm, s, dir, false)) {
            play_end(sm, in, out, END_CHILD, CUE_HEARD, true);
        }
        break;
    case SM_LISTEN:
    case SM_RECORD:
        if (!opening) break;
        close_capture(sm, out, true);
        enter(sm, SM_PET, in->now, true);
        if (select_step(sm, s, dir, false)) cue(out, CUE_HEARD, STREAM_SLOT_PRESS);
        break;
    case SM_PET:
        if (select_step(sm, s, dir, false)) cue(out, CUE_HEARD, STREAM_SLOT_PRESS);
        break;
    case SM_GRID:
        break;
    }
}

static void voice(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    switch (in->voice) {
    case SM_VOICE_NONE:
        break;
    case SM_VOICE_STOP:
        /* "Stop" has to mean stop wherever it is said: a recording is dropped, a reply in flight
           abandoned (talk_clear is a no-op while it is on the network, so an answer still plays —
           with no SPOKE edge, so no follow-up), and the follow-up window closed. */
        if (sm->mode == SM_LISTEN || sm->mode == SM_RECORD) {
            close_capture(sm, out, true);
            enter(sm, SM_PET, in->now, false);
        }
        if (sm->turn.state != SM_TURN_NONE) {
            fx(out, SM_FX_TALK_CLEAR, 0, 0);
            sm->turn.state = SM_TURN_NONE;
        }
        disarm_follow(sm);
        sm->talk.follow_turns = FOLLOW_MAX_TURNS;
        cue(out, CUE_STOP, STREAM_SLOT_PRESS);
        break;
    case SM_VOICE_WAKE:
        if (!mic_gate(sm, in, false)) {
            refuse(out);
            break;
        }
        sm->grid_until = 0;
        open_listen(sm, in, out, false, false);
        cue(out, CUE_LISTEN, STREAM_SLOT_PRESS);
        break;
    case SM_VOICE_SEND_PANEL:
    case SM_VOICE_SEND_DAD: {
        if (!mic_gate(sm, in, false)) {
            refuse(out);
            break;
        }
        sm->grid_until = 0;
        const jp_who_t w = addressee(in, in->voice == SM_VOICE_SEND_DAD ? JP_TO_DAD : JP_TO_PANEL);
        open_record(sm, in, out, &w, false);
        cue(out, CUE_LISTEN, STREAM_SLOT_PRESS);
        break;
    }
    }
}

/* The button: standby or deep sleep, from any mode. True when the frame's touch and voice must be
   dropped (a queued press must not start a PLAY in the dark). */
static bool button(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    if (in->btn_hold) fx(out, SM_FX_DEEP_SLEEP, 0, 0);
    if (!in->btn_short) return false;
    if (sm->standby) {
        sm->standby = false;
        fx(out, SM_FX_STANDBY, 0, 0);
        cue(out, CUE_HEARD, STREAM_SLOT_PRESS);
        return false;
    }
    /* IT BLINKS FIRST: the silence cuts what sounds, and the stop's own cue — at a later seq —
       sounds before the dark. A send continues. */
    fx(out, SM_FX_SILENCE, 0, 0);
    cue(out, CUE_STOP, STREAM_SLOT_PRESS);
    close_capture(sm, out, true);
    fx(out, SM_FX_PAUSE, 0, sm->play.pause_stream);
    fx(out, SM_FX_STOP, 0, 0);
    if (sm->mode == SM_PLAY) hold_selection(sm, in);
    sm->play.seq = JP_SEQ_NONE;
    sm->play.pause_on = false;
    /* The pet turn ends without TALK_CLEAR: a reply already in flight still plays when it lands,
       as today, but nothing follows it — no SPOKE edge, no microphone in the dark. */
    sm->turn.state = SM_TURN_NONE;
    sm->talk.follow_turns = FOLLOW_MAX_TURNS;
    disarm_follow(sm);
    sm->offer.up = false;
    sm->grid_until = 0;
    sm->standby = true;
    fx(out, SM_FX_STANDBY, 1, 0);
    fx(out, SM_FX_BLANK, 0, 0);
    enter(sm, SM_PET, in->now, false);
    return true;
}

static void hold_timer(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    if (!sm->down.active || sm->down.held || sm->down.swiped) return;
    if (!jp_due(in->now, sm->down.at + HOLD_TALK_MS)) return;
    if (sm->down.target != SM_T_PET) return;
    if (gesture_reserved(in->gest_taps)) {
        /* The maintenance hold owns the WHOLE press: the frame it fires resets the count to 0,
           and a finger still down then would open the grid under it. */
        sm->down.held = true;
        return;
    }
    if (!sm->down.on_pet) {
        /* Once per press: a margin that is too wide looks exactly like a menu that stopped
           working unless the log says which it is. */
        sm->down.held = true;
        out->rim_hold = true;
        return;
    }
    if (sm->mode != SM_PET || think(sm) || in->net == SM_NET_BUSY) return;
    /* A fresh offer owns the press (0.3.35); a standing one does not (owner Q1). */
    if (offer_visible(sm, &in->snap) && !offer_standing(sm, in->now)) return;
    sm->down.held = true;
    sm->grid_until = in->now + SENDTO_MS;
    sm->grid_page = SM_GRID_PEOPLE;
    sm->hold_x = in->x;
    sm->hold_y = in->y;
    enter(sm, SM_GRID, in->now, true);
    cue(out, CUE_HEARD, STREAM_SLOT_PRESS);
}

/* ── what the engine said ────────────────────────────────────────────────────────────────── */

static void play_outcome(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    const jp_view_t *s = &in->snap;
    if (sm->late_play != JP_SEQ_NONE && s->play.seq == sm->late_play) {
        out->late_outcome = true;
        sm->late_play = JP_SEQ_NONE;
    }
    if (sm->mode != SM_PLAY || sm->play.seq == JP_SEQ_NONE) return;
    if (in->post_failed == sm->play.seq) {
        play_end(sm, in, out, END_FAIL, CUE_OOPS, false);
        return;
    }
    const bool ours = s->play.seq == sm->play.seq && s->play.kind != JP_PLAY_NONE;
    const bool continues = s->cur.seq == sm->play.seq && s->cur.run_continues;
    if (!jp_seq_ge(s->done_seq, sm->play.seq) && !(ours && !continues)) return;
    const jp_play_kind_t kind = ours ? s->play.kind : JP_PLAY_CANCELLED;
    const bool replay = sm->play.replay;
    if (!replay && (kind == JP_PLAY_HEARD || kind == JP_PLAY_HEARD_UNACKED)) {
        /* From the outcome itself, so a delayed snapshot cannot misaddress a reply. */
        sm->offer.up = true;
        sm->offer.who = s->play.who;
        sm->offer.since = in->now;
        play_end(sm, in, out, END_NATURAL, CUE_COUNT, false);
    } else if (replay && kind == JP_PLAY_HEARD) {
        sm->offer.since = in->now; /* JP 0.3.36: a replay re-arms the pair */
        play_end(sm, in, out, END_NATURAL, CUE_COUNT, false);
    } else if (replay && kind == JP_PLAY_SHORT) {
        play_end(sm, in, out, END_NATURAL, CUE_COUNT, false);
    } else {
        play_end(sm, in, out, END_FAIL, CUE_OOPS, false); /* a replay keeps its offer */
    }
}

static void send_outcome(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    const jp_view_t *s = &in->snap;
    if (sm->late_send != JP_SEQ_NONE && s->send.seq == sm->late_send) {
        out->late_outcome = true;
        sm->late_send = JP_SEQ_NONE;
    }
    if (sm->send.seq == JP_SEQ_NONE) return;
    if (in->post_failed == sm->send.seq) {
        sm->send.seq = JP_SEQ_NONE;
        cue(out, CUE_OOPS, STREAM_SLOT_SEND_RESULT);
        return;
    }
    if (s->send.seq != sm->send.seq || s->send.kind == JP_SEND_NONE) return;
    /* Each its own sentence: "it went", "nobody to send to", "dropped", "it didn't go". */
    switch (s->send.kind) {
    case JP_SEND_SENT:
        cue(out, CUE_SENT, STREAM_SLOT_SEND_RESULT);
        fx(out, SM_FX_POLL_SOON, 0, 0); /* the twin may already have answered */
        break;
    case JP_SEND_NOBODY:
        cue(out, CUE_OOPS, STREAM_SLOT_SEND_RESULT);
        out->say = "nobody to send to";
        break;
    case JP_SEND_EMPTY:
        cue(out, CUE_STOP, STREAM_SLOT_SEND_RESULT);
        break;
    case JP_SEND_FAILED:
    case JP_SEND_NONE:
        cue(out, CUE_OOPS, STREAM_SLOT_SEND_RESULT);
        break;
    }
    sm->send.seq = JP_SEQ_NONE;
}

/* ── backstops and mode timers ───────────────────────────────────────────────────────────── */

/* §7.5c: two backstops, never a sum of durations. */
static bool backstop_due(const jp_view_t *s, uint32_t seq, uint32_t expires_at, uint32_t now)
{
    if (s->claimed_seq == seq && !jp_seq_ge(s->done_seq, seq) && s->deadline_ms != 0) {
        return jp_due(now, s->deadline_ms + JP_UI_MARGIN_MS);
    }
    if (jp_seq_ge(s->done_seq, seq)) return false;
    return jp_due(now, expires_at + JP_UI_MARGIN_MS);
}

static void capture_timers(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    const bool rec = sm->mode == SM_RECORD;
    const uint32_t opened = rec ? sm->rec.opened_at : sm->talk.opened_at;
    if (opened == in->now) return; /* frame rule 1: the inputs describe the previous capture */
    bool *heard = rec ? &sm->rec.heard : &sm->talk.heard;
    uint32_t *hush = rec ? &sm->rec.hush : &sm->talk.hush;
    if (in->hearing) {
        *heard = true;
        *hush = 0;
    } else if (*heard && *hush == 0) {
        *hush = stamp(in->now);
    }
    const bool hushed = *heard && *hush != 0 && jp_due(in->now, *hush + LISTEN_HUSH_MS);
    const bool full = in->capture_ms >= CAPTURE_MAX_MS;
    const uint32_t lead = rec ? RECORD_LEAD_MS : sm->talk.lead;
    const bool nothing = !*heard && jp_due(in->now, opened + lead + 1);
    /* I6's hard bound, should the capture count ever stop arriving. */
    const bool overdue = jp_due(in->now, opened + (rec ? CAPTURE_MAX_MS + 5000 : TALK_TIMEOUT_MS));
    if (nothing || (overdue && !*heard)) {
        close_capture(sm, out, true);
        /* A deliberate recording that nobody spoke into says "dropped" (B8); a name with nothing
           after it costs nothing at all. */
        if (rec && sm->rec.deliberate) cue(out, CUE_STOP, STREAM_SLOT_SEND_RESULT);
        enter(sm, SM_PET, in->now, false);
    } else if (hushed || full || overdue) {
        if (rec) {
            send_capture(sm, in, out); /* no cue: CUE_SENT follows */
            enter(sm, SM_PET, in->now, false);
        } else {
            send_talk(sm, in, out);
        }
    }
}

static void timers(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    const jp_view_t *s = &in->snap;
    if (sm->mode == SM_PLAY) {
        if (sm->play.seq != JP_SEQ_NONE) {
            if (backstop_due(s, sm->play.seq, sm->play.expires_at, in->now)) {
                out->backstop = true;
                sm->late_play = sm->play.seq;
                play_end(sm, in, out, END_FAIL, CUE_OOPS, false);
            }
        } else if (jp_due(in->now, sm->play.since + PENDING_MS)) {
            play_end(sm, in, out, END_FAIL, CUE_OOPS, false); /* E2.10: never silently */
        }
    }
    if (sm->send.seq != JP_SEQ_NONE &&
        backstop_due(s, sm->send.seq, sm->send.expires_at, in->now)) {
        out->backstop = true;
        sm->late_send = sm->send.seq;
        sm->send.seq = JP_SEQ_NONE;
        cue(out, CUE_OOPS, STREAM_SLOT_SEND_RESULT);
    }
    if (sm->play.pause_on) {
        /* A HOLD IS NOT FOREVER: a paused stream is a response held open on the box. It resumes
           rather than aborting — a message playing to an empty room is recoverable. And a pause
           belongs to its own stream only (I5). */
        const bool stale = sm->mode != SM_PLAY || !claimed(sm, s) ||
                           s->cur.stream_id != sm->play.pause_stream;
        if (stale || jp_due(in->now, sm->play.pause_since + AUDIO_PAUSE_MAX_MS)) {
            fx(out, SM_FX_PAUSE, 0, sm->play.pause_stream);
            sm->play.pause_on = false;
        }
    }
    if (sm->mode == SM_GRID && jp_due(in->now, sm->grid_until)) {
        sm->grid_until = 0;
        enter(sm, SM_PET, in->now, false);
    }
    if (sm->mode == SM_LISTEN || sm->mode == SM_RECORD) capture_timers(sm, in, out);
}

/* The command goes out once the press's own cue has had the speaker (today's rule). */
static void post_pending(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    if (sm->mode != SM_PLAY || sm->play.seq != JP_SEQ_NONE) return;
    if (sm->play.since == in->now || in->audio.speaker_busy || holding(sm)) return;
    jp_cmd_t cmd = {0};
    cmd.kind = sm->play.replay ? JP_CMD_REPLAY : JP_CMD_PLAY;
    if (!sm->play.replay) {
        /* Resolved at the post, from S as it stands: a press made during a stop's hold plays the
           message that is there now, never one the stop acknowledged. */
        resolve_sel(sm, &in->snap);
        sel_set_t S;
        build_set(sm, &in->snap, &S);
        const jp_who_t *w = selected(sm, &in->snap);
        if (S.n == 0 || w == NULL) {
            play_end(sm, in, out, END_FAIL, CUE_OOPS, false);
            return;
        }
        sm->play.target = *w;
        cmd.at = sel_index(sm, &S);
    }
    cmd.seq = next_seq(sm);
    cmd.expires_at = in->now + JP_PLAY_EXPIRE_MS;
    cmd.target = sm->play.target;
    sm->play.seq = cmd.seq;
    sm->play.expires_at = cmd.expires_at;
    post(out, &cmd);
}

/* ── the pet turn, the run, the follow-up ────────────────────────────────────────────────── */

static void pet_turn(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    if (sm->turn.state == SM_TURN_THINKING && sm->turn.since != in->now) {
        if (in->net == SM_NET_SPOKE) {
            fx(out, SM_FX_TALK_CLEAR, 0, 0);
            fx(out, SM_FX_NOD, 0, 0);
            sm->talk.follow_armed = true; /* opened on the reply's end, not now */
            sm->turn.state = SM_TURN_NONE;
        } else if (in->net == SM_NET_FAILED || jp_due(in->now, sm->turn.since + TALK_TIMEOUT_MS)) {
            /* EVERY DASH IS COUNTED AND NAMED (*"anytime we get a red – I want you to make sure
               that it's logged"*). The two causes need opposite fixes: "net" has a reason in
               reach.c; "timeout" has no other record anywhere. */
            fx(out, SM_FX_TALK_CLEAR, 0, 0);
            out->dash_why = in->net == SM_NET_FAILED ? SM_DASH_NET : SM_DASH_TIMEOUT;
            sm->dashes++;
            sm->dash_at = stamp(in->now);
            cue(out, CUE_OOPS, STREAM_SLOT_FAIL);
            sm->turn.state = SM_TURN_FAILED;
            sm->turn.since = in->now;
        } else if (in->net == SM_NET_IDLE) {
            fx(out, SM_FX_TALK_CLEAR, 0, 0); /* the box heard silence; nothing to say */
            sm->turn.state = SM_TURN_NONE;
        }
    } else if (sm->turn.state == SM_TURN_FAILED &&
               jp_due(in->now, sm->turn.since + TALK_FAILED_MS + 1)) {
        sm->turn.state = SM_TURN_NONE;
    }
}

/* n/m counts the run this press started: it advances when a new message is seeded after the
   last one began, so "1/3" becomes "2/3" with no gap, and an id that answered 204 is skipped. */
static void run_count(sm_t *sm, const jp_view_t *s)
{
    if (sm->mode != SM_PLAY || sm->play.replay || !claimed(sm, s)) return;
    const bool by_id = s->cur.who.id[0] != '\0';
    const bool fresh = by_id ? strcmp(sm->play.run_id, s->cur.who.id) != 0
                             : (s->cur.stream_id != 0 && s->cur.stream_id != sm->play.run_sid);
    if (fresh) {
        const bool had = sm->play.run_id[0] != '\0' || sm->play.run_sid != 0;
        if (had && sm->play.run_began) sm->play.run_pos++;
        id_copy(sm->play.run_id, s->cur.who.id);
        sm->play.run_sid = s->cur.stream_id;
        sm->play.run_began = false;
    }
    if (s->cur.stream_id != 0) sm->play.run_began = true;
}

/* ── new messages and the chime (§8.4) ───────────────────────────────────────────────────── */

static bool in_waiting(const jp_view_t *s, const char *id)
{
    const int n = clampi(s->waiting.n, 0, JPANEL_QUEUE_MAX);
    for (int i = 0; i < n; i++) {
        if (id_eq(id, s->waiting.q[i].id)) return true;
    }
    return false;
}

static bool seen(const sm_t *sm, const char *id)
{
    for (int i = 0; i < sm->seen_n; i++) {
        if (strcmp(sm->seen_ids[i], id) == 0) return true;
    }
    return false;
}

/* Evicts the oldest id that is neither waiting nor playing: the box describes at most eight, so
   there is always one, and an id still waiting is never forgotten (and never re-announced). */
static void see(sm_t *sm, const jp_view_t *s, const char *id)
{
    if (sm->seen_n >= SM_SEEN_MAX) {
        int victim = 0;
        for (int i = 0; i < sm->seen_n; i++) {
            if (!in_waiting(s, sm->seen_ids[i]) && !id_eq(sm->seen_ids[i], s->cur.who.id)) {
                victim = i;
                break;
            }
        }
        for (int i = victim; i < sm->seen_n - 1; i++) {
            memcpy(sm->seen_ids[i], sm->seen_ids[i + 1], JP_ID_MAX);
        }
        sm->seen_n--;
    }
    id_copy(sm->seen_ids[sm->seen_n++], id);
}

static void owe_chime(sm_t *sm, const char *id)
{
    for (int i = 0; i < JPANEL_QUEUE_MAX; i++) {
        if (sm->chime_ids[i][0] == '\0') {
            id_copy(sm->chime_ids[i], id);
            return;
        }
    }
}

static bool chime_owed(const sm_t *sm, const jp_view_t *s)
{
    if (!s->waiting.has_ids) return sm->chime_owed;
    for (int i = 0; i < JPANEL_QUEUE_MAX; i++) {
        if (in_waiting(s, sm->chime_ids[i])) return true;
    }
    return false;
}

static void arrivals(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    const jp_view_t *s = &in->snap;
    const jp_waiting_t *was = &sm->prev.waiting;
    bool edge = false;
    if (s->waiting.has_ids) {
        /* Scrolled into the described eight as older ones left: not an arrival. */
        const bool scroll = was->count > was->n && s->waiting.count <= was->count;
        const int n = clampi(s->waiting.n, 0, JPANEL_QUEUE_MAX);
        for (int i = 0; i < n; i++) {
            const char *id = s->waiting.q[i].id;
            if (id[0] == '\0' || seen(sm, id)) continue;
            see(sm, s, id);
            if (scroll) continue;
            owe_chime(sm, id);
            edge = true;
        }
        /* Ids played in the run they arrived during, or played elsewhere, never chime. */
        for (int i = 0; i < JPANEL_QUEUE_MAX; i++) {
            if (!in_waiting(s, sm->chime_ids[i])) sm->chime_ids[i][0] = '\0';
        }
    } else {
        if (s->waiting.count > was->count) {
            sm->chime_owed = true;
            edge = true;
        }
        if (s->waiting.count == 0) sm->chime_owed = false;
        /* An index only: any change of shape sends it home (today's rule). */
        if (s->waiting.count != was->count && sm->mode == SM_PET) {
            sm->sel.index = 0;
            sm->sel.id[0] = '\0';
        }
    }
    out->arrived = edge;
    if (edge && sm->mode == SM_PET && !holding(sm) && s->waiting.has_ids) {
        sel_set_t S;
        build_set(sm, s, &S);
        sel_set(sm, &S, 0);
    }

    /* A wait starts big only if one of its ids is still owed a chime, so a message handed back
       by a stop reappears as a badge. */
    if (s->waiting.count > 0 && was->count <= 0) {
        sm->notice.first_visible = 0;
        sm->notice.big = chime_owed(sm, s);
    }
    if (s->waiting.count <= 0) {
        sm->notice.first_visible = 0;
        sm->notice.big = false;
    }
    if (in->dark_wake && s->waiting.count > 0) {
        sm->notice.first_visible = 0;
        sm->notice.big = true;
    }

    /* The chime sounds on the first frame she can act on it: the notice drawn in PET, the
       microphone closed and no pet reply. */
    if (chime_owed(sm, s) && notice_drawn(sm, s) && !sm->capture && !in->audio.reply_busy) {
        cue(out, CUE_MESSAGE, STREAM_SLOT_CHIME);
        memset(sm->chime_ids, 0, sizeof(sm->chime_ids));
        sm->chime_owed = false;
    }
}

static void follow_up(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    if (!(sm->reply_was && !in->audio.reply_busy) || !sm->talk.follow_armed) return;
    /* Disarmed, not deferred, when she is elsewhere, a chime is owed or the gate refuses: the
       child's attention has moved. */
    sm->talk.follow_armed = false;
    if (sm->mode != SM_PET || chime_owed(sm, &in->snap) ||
        sm->talk.follow_turns >= FOLLOW_MAX_TURNS || !mic_gate(sm, in, false)) {
        return;
    }
    open_listen(sm, in, out, true, false);
}

/* ── the view, and the invariants ────────────────────────────────────────────────────────── */

static void make_view(const sm_t *sm, const sm_in_t *in, sm_view_t *v)
{
    const jp_view_t *s = &in->snap;
    memset(v, 0, sizeof(*v));
    v->mode = sm->mode;
    v->think = think(sm);
    v->dash = v->think && sm->turn.state == SM_TURN_FAILED;
    v->notice = notice_drawn(sm, s);
    v->notice_big = v->notice && notice_big(sm, in->now);
    v->offer = offer_visible(sm, s);
    v->offer_standing = v->offer && offer_standing(sm, in->now);
    const bool play = sm->mode == SM_PLAY;
    const bool audible = on(sm, in);
    /* A replay draws the run bar until it is audible, then the pair with the playing glyph. */
    v->pair = v->offer || (play && sm->play.replay && audible);
    v->run_bar = play && !(sm->play.replay && audible);
    v->transport = play;
    v->paused = play && sm->play.pause_on;
    v->playing_glyph = play && !sm->play.pause_on;
    /* THE RING ROUND THE PLAY BUTTON (owner: *"a little circle border that progresses to show
       how far it is until it's all the way complete"*). The audible ms of THIS message's stream
       against the length the box gave it. Not `on()`: that wants the stream live, and a paused
       message or the last of one draining must keep its ring where it got to, not lose it. */
    v->progress = -1;
    if (play && claimed(sm, s) && s->cur.duration_ms > 0 && in->audio.stream_id != 0 &&
        in->audio.stream_id == s->cur.stream_id) {
        const uint64_t p = (uint64_t)in->audio.out_ms * 1000u / s->cur.duration_ms;
        v->progress = p > 1000u ? 1000 : (int)p;
    }
    v->reply = v->offer || (play && sm->play.pause_on);
    v->exit_corner = play || v->offer;
    v->face_none = !sm_face(sm, s, &v->face);
    sel_set_t S;
    build_set(sm, s, &S);
    if (play) {
        v->sel_n = sm->play.replay ? 1 : sm->play.run_pos;
        v->sel_m = sm->play.replay ? 1 : sm->play.run_pos - 1 + S.n + S.extra;
    } else if (v->notice) {
        v->sel_n = sel_index(sm, &S) + 1;
        v->sel_m = S.n + S.extra > 0 ? S.n + S.extra : 1;
    }
    v->sending = sm->send.seq != JP_SEQ_NONE;
    v->grid = sm->mode == SM_GRID;
    v->grid_page = v->grid ? sm->grid_page : SM_GRID_PEOPLE;
    v->confirm = sm->mode == SM_LISTEN || sm->mode == SM_RECORD;
    v->listen_dot = sm->capture;
    v->rec_to = sm->rec.to;
}

static bool probe(const sm_t *sm, const sm_in_t *in, int x, int y, sm_target_t want)
{
    const sm_target_t t = sm_target(sm, in, x, y);
    return t == want || t == SM_T_IGNORED;
}

uint32_t sm_check(const sm_t *sm, const sm_in_t *in)
{
    const jp_view_t *s = &in->snap;
    const uint32_t now = in->now;
    uint32_t v = 0;
    const bool capture_mode = sm->mode == SM_LISTEN || sm->mode == SM_RECORD;
    if (sm->capture != capture_mode || (unsigned)sm->mode > SM_PLAY) v |= SM_I1;

    sm_view_t view;
    make_view(sm, in, &view);
    jp_who_t face;
    const bool has = sm_face(sm, s, &face);
    if (has == view.face_none || (has && memcmp(&face, &view.face, sizeof(face)) != 0)) v |= SM_I2;
    if (sm->mode == SM_RECORD && sm->rec.to != (sm->rec.who.from_dad ? JP_TO_DAD : JP_TO_PANEL)) {
        v |= SM_I2;
    }

    /* I3: what is drawn is pressable, and what is pressable is drawn. */
    const int over_h = in->over_h, cy = confirm_cy(over_h), q = over_h / 4;
    if (view.transport != (sm->mode == SM_PLAY) || view.grid != (sm->mode == SM_GRID) ||
        view.confirm != capture_mode || (view.notice && view.offer)) {
        v |= SM_I3;
    }
    const bool games = view.grid_page == SM_GRID_GAMES;
    if (view.grid &&
        !(probe(sm, in, FACE_W / 4, q, games ? SM_T_CELL_GAME : SM_T_CELL_SISTER) &&
          probe(sm, in, FACE_W * 3 / 4, q, games ? SM_T_CELL_JET : SM_T_CELL_DAD) &&
          probe(sm, in, FACE_W / 4, over_h - q, SM_T_CELL_CANCEL) &&
          probe(sm, in, FACE_W * 3 / 4, over_h - q, games ? SM_T_SWALLOW : SM_T_CELL_PET))) {
        v |= SM_I3;
    }
    if (view.notice) {
        int box[4];
        sm_notice_box(in->over_y0, over_h, view.notice_big, box);
        if (!probe(sm, in, (box[0] + box[2]) / 2, (box[1] + box[3]) / 2, SM_T_NOTICE)) v |= SM_I3;
    }
    if (view.offer && !(probe(sm, in, CONFIRM_CX_CANCEL, cy, SM_T_AGAIN) &&
                        probe(sm, in, CONFIRM_CX_SEND, cy, SM_T_REPLY) &&
                        probe(sm, in, FACE_W * 3 / 4, over_h / 4, SM_T_EXIT))) {
        v |= SM_I3;
    }
    if (sm->mode == SM_PLAY && !probe(sm, in, FACE_W * 3 / 4, over_h / 4, SM_T_EXIT)) v |= SM_I3;
    if (sm->mode == SM_PLAY &&
        (view.reply ? !probe(sm, in, CONFIRM_CX_SEND, cy, SM_T_REPLY)
                    : sm_target(sm, in, CONFIRM_CX_SEND, cy) == SM_T_REPLY)) {
        v |= SM_I3;
    }
    if (sm->mode == SM_PET && !view.notice && !view.offer &&
        sm_target(sm, in, FACE_W / 2, over_h / 2) != SM_T_PET) {
        v |= SM_I3;
    }

    if (sm->play.pause_on &&
        (sm->mode != SM_PLAY || sm->play.pause_stream != s->cur.stream_id)) {
        v |= SM_I5;
    }

    switch (sm->mode) {
    case SM_GRID:
        if (jp_due(now, sm->grid_until)) v |= SM_I6;
        break;
    case SM_RECORD:
        if (jp_due(now, sm->rec.opened_at + CAPTURE_MAX_MS + 5000 + 1)) v |= SM_I6;
        break;
    case SM_LISTEN:
        if (jp_due(now, sm->talk.opened_at + TALK_TIMEOUT_MS + 1)) v |= SM_I6;
        break;
    case SM_PLAY:
        if (sm->play.seq == JP_SEQ_NONE ? jp_due(now, sm->play.since + PENDING_MS)
                                        : backstop_due(s, sm->play.seq, sm->play.expires_at, now)) {
            v |= SM_I6;
        }
        break;
    case SM_PET:
        break;
    }
    if (sm->play.pause_on && jp_due(now, sm->play.pause_since + AUDIO_PAUSE_MAX_MS)) v |= SM_I6;
    /* Not while calibrating: the turn runs on to be read on the first frame back, and recovering
       it here would end it with neither the reply nor the dash. */
    if (sm->turn.state == SM_TURN_THINKING && !in->calibrating &&
        jp_due(now, sm->turn.since + TALK_TIMEOUT_MS + 1)) {
        v |= SM_I6;
    }
    return v;
}

/* Only a state that would leave the microphone or the speaker wrong recovers; a drawing or a
   missing cue is counted, never worth a child's recording. */
static void recover(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    const bool dropped = sm->capture && sm->mode == SM_RECORD;
    close_capture(sm, out, true);
    fx(out, SM_FX_STOP, 0, 0);
    fx(out, SM_FX_PAUSE, 0, sm->play.pause_stream);
    if (dropped) cue(out, CUE_OOPS, STREAM_SLOT_FAIL);
    sm->play.pause_on = false;
    sm->play.seq = JP_SEQ_NONE;
    sm->grid_until = 0;
    if (sm->turn.state == SM_TURN_THINKING && jp_due(in->now, sm->turn.since + TALK_TIMEOUT_MS)) {
        sm->turn.state = SM_TURN_NONE;
    }
    enter(sm, SM_PET, in->now, false);
}

/* ── the frame ───────────────────────────────────────────────────────────────────────────── */

void sm_init(sm_t *sm, uint32_t now)
{
    memset(sm, 0, sizeof(*sm));
    sm->mode = SM_PET;
    sm->since = now;
    sm->next_seq = 1;
}

/* Calibration is the owner's maintenance act: an open recording is dropped silently, the offer and
   any send are kept, and the pet turn runs on to be read on the first frame back. */
static void calibrate(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    if (sm->cal) return;
    sm->cal = true;
    close_capture(sm, out, true);
    fx(out, SM_FX_STOP, 0, 0);
    fx(out, SM_FX_PAUSE, 0, sm->play.pause_stream);
    sm->play.pause_on = false;
    sm->play.seq = JP_SEQ_NONE;
    sm->grid_until = 0;
    enter(sm, SM_PET, in->now, false);
}

static bool answered(const sm_out_t *out, int from, sm_mode_t was, const sm_t *sm)
{
    /* A menu cell answers on its lift (`cell_lift`), or the swipe it began answers instead. */
    if (out->tap_missed || out->pet_press || sm->mode != was || sm->down.cell_armed != SM_T_NONE) {
        return true;
    }
    for (int i = from; i < out->n; i++) {
        if (out->fx[i].kind == SM_FX_CUE || out->fx[i].kind == SM_FX_FLINCH) return true;
    }
    return false;
}

void sm_step(sm_t *sm, const sm_in_t *in, sm_out_t *out)
{
    memset(out, 0, sizeof(*out));
    const jp_view_t *s = &in->snap;
    uint32_t i4 = 0;

    if (in->calibrating) {
        calibrate(sm, in, out);
    } else {
        sm->cal = false;
        if (in->wake) sm->standby = false;

        /* 1. the button, then the finger, then the voice — against what the last frame drew */
        const bool dark = button(sm, in, out);
        if (in->down) {
            sm->down.active = true;
            sm->down.at = in->now;
            sm->down.x = in->x;
            sm->down.y = in->y;
            sm->down.on_pet = in->on_pet;
            sm->down.swiped = false;
            sm->down.held = false;
            sm->down.cell_armed = SM_T_NONE;
            sm->down.target = SM_T_NONE;
            if (!dark) {
                const sm_target_t t = sm_target(sm, in, in->x, in->y);
                sm->down.target = t;
                const int from = out->n;
                const sm_mode_t was = sm->mode;
                press(sm, in, out, t);
                if (!answered(out, from, was, sm)) i4 = SM_I4;
            }
        } else if (sm->down.active) {
            sm->down.x = in->x;
            sm->down.y = in->y;
        }
        if (in->swipe != 0 && !dark) {
            sm->down.swiped = true;
            on_swipe(sm, in, out, in->swipe > 0 ? 1 : -1);
        }
        if (!dark) voice(sm, in, out);

        /* 2. the hold */
        hold_timer(sm, in, out);
        if (in->up && sm->down.cell_armed != SM_T_NONE) {
            const sm_target_t cell = (sm_target_t)sm->down.cell_armed;
            sm->down.cell_armed = SM_T_NONE;
            if (sm->mode == SM_GRID && !dark) cell_lift(sm, in, out, cell);
        }
        if (in->up) sm->down.active = false;

        /* 3. outcomes: play, then send (post_failed with each), then a refused capture */
        play_outcome(sm, in, out);
        send_outcome(sm, in, out);
        if ((sm->mode == SM_LISTEN || sm->mode == SM_RECORD) && in->capture_refused &&
            (sm->mode == SM_RECORD ? sm->rec.opened_at : sm->talk.opened_at) != in->now) {
            const bool silent = sm->mode == SM_LISTEN && sm->talk.follow;
            sm->capture = false;
            if (!silent) refuse(out);
            enter(sm, SM_PET, in->now, false);
        }

        /* 4. backstops and mode timers; 5. the pending play */
        timers(sm, in, out);
        post_pending(sm, in, out);

        /* 6. the pet turn, then the selection */
        pet_turn(sm, in, out);
        if (holding(sm) && (jp_seq_ge(s->done_seq, sm->stopped_seq) ||
                            jp_due(in->now, sm->stopped_expires))) {
            sm->stopped_seq = JP_SEQ_NONE;
        }
        run_count(sm, s);
        resolve_sel(sm, s);

        /* 7. the follow-up, before 8 clears what the chime was owed */
        follow_up(sm, in, out);
        arrivals(sm, in, out);
        sm->prev = *s;
        sm->reply_was = in->audio.reply_busy;
    }

    /* Deaf to commands while the microphone is committed, in standby, and while a message plays.
       That last one is 0.4.2's: a word the recogniser thinks it heard sounds its cue in the press
       slot, and a message stream yields the speaker to a cue chunk by chunk, so room noise mistaken
       for "jump" paused a sister's message mid-sentence. 0.3.39 refused the cue instead; the
       owner heard the difference on the first afternoon. */
    fx(out, SM_FX_MUTE_COMMANDS,
       sm->mode == SM_LISTEN || sm->mode == SM_RECORD || sm->mode == SM_PLAY || sm->standby ||
               in->audio.stream_live
           ? 1
           : 0,
       0);

    uint32_t v = sm_check(sm, in);
    if (v & SM_RECOVERS) {
        recover(sm, in, out);
        v |= sm_check(sm, in);
    }
    out->violations = v | i4;
    make_view(sm, in, &out->view);
    if (out->view.notice && sm->notice.first_visible == 0) {
        sm->notice.first_visible = stamp(in->now);
    }
}
