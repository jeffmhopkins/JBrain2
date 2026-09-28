/* The arbitration half of the panel's interface — see `ui.h` for why it is a separate file.
 *
 * READ THIS AS A TRANSCRIPTION, NOT AS A DESIGN. Every branch below was lifted out of
 * `face_task()` with its conditions and its ORDER intact, because the point of the move was to
 * make the existing behaviour testable — not to improve it. Several things here are, on the
 * evidence of the tests that now cover them, wrong; they are wrong in exactly the way they were
 * wrong on the panel, and `firmware/host/README.md` lists them. A refactor that also fixed them
 * would have been a refactor nobody could check.
 */

#include "ui.h"

#include <string.h>

#include "audio.h"   /* AUDIO_PAUSE_MAX_MS — header-only here; nothing in `audio.c` is called */
#include "face.h"    /* FACE_W, FACE_H */
#include "gesture.h" /* gesture_reserved */

/* SILENTLY DROPPED PAST THE CAP RATHER THAN OVERFLOWING. The bound is a property of the
   machine — no frame has ever produced more than five — so a full list means a branch was
   added without checking, which the host suite asserts against directly. */
static void act(ui_out_t *out, ui_action_kind_t kind, int arg)
{
    if (out->n >= UI_ACTS_MAX) return;
    out->act[out->n].kind = kind;
    out->act[out->n].arg = arg;
    out->n++;
}

static void cue(ui_out_t *out, cue_t c)
{
    act(out, UI_ACT_CUE, (int)c);
}

static bool in_box(const int box[4], int x, int y)
{
    return box[0] >= 0 && x >= box[0] && x < box[2] && y >= box[1] && y < box[3];
}

void ui_reset(ui_state_t *st)
{
    memset(st, 0, sizeof(*st));
    st->down_x = -1;
    st->down_y = -1;
    st->popup_box[0] = -1;
    st->popup_box[1] = -1;
    st->popup_box[2] = -1;
    st->popup_box[3] = -1;
    /* Not 0: see the field's comment. The first poll after a boot has to be able to be news. */
    st->waiting_shown = -1;
}

ui_talk_t ui_talk(const ui_state_t *st)
{
    return st->talk;
}

bool ui_busy(const ui_state_t *st)
{
    /* THE PAIR IS DELIBERATELY NOT IN HERE, and it used to be. `repeat_until` stopped expiring,
       and it sat in the render loop's idle test — so from the first message ever played the panel
       counted itself as permanently in use and could never dim or darken again. A pair waiting
       patiently for a child is precisely the case where the screen SHOULD be allowed to sleep
       around it; nothing about sleeping drops the offer. Anything that genuinely needs to know
       whether the pair is standing asks `ui_pair_up` instead. */
    return st->talk != UI_TALK_IDLE;
}

bool ui_recording(const ui_state_t *st)
{
    return st->talk == UI_TALK_RECORDING;
}

bool ui_pair_up(const ui_state_t *st)
{
    return st->repeat_until != 0;
}

bool ui_grid_up(const ui_state_t *st)
{
    return st->sendto_until != 0;
}

void ui_popup_restart(ui_state_t *st, uint32_t now)
{
    st->popup_since = now;
}

void ui_talk_send_result(ui_state_t *st, bool ok, uint32_t now)
{
    if (ok) {
        st->talk = UI_TALK_THINKING;
        st->talk_since = now;
    } else {
        /* Nothing recorded, or a turn already in flight. Either way the pet goes straight back
           to being a pet rather than showing a bubble that cannot resolve — a thinking box with
           nothing behind it is the silent hang this whole state machine exists to avoid. */
        st->talk = UI_TALK_IDLE;
    }
}

/* The one refusal test, shared by the four ways in, because a child who presses an icon while
   the pet is already talking must get the same nothing a child who said the phrase would. */
static bool start_listening(ui_state_t *st, const ui_in_t *in, ui_out_t *out, bool speaking)
{
    if (st->talk != UI_TALK_IDLE || speaking || in->net == UI_NET_BUSY) return false;
    st->talk = UI_TALK_LISTENING;
    st->talk_since = in->now;
    st->listen_voice = true;
    st->listen_heard = false;
    st->listen_hush = 0;
    st->listen_lead = LISTEN_LEAD_MS;
    st->follow_turns = 0; /* a deliberate start is a fresh exchange */
    act(out, UI_ACT_CAPTURE_OPEN, 0);
    return true;
}

static bool start_record(ui_state_t *st, const ui_in_t *in, ui_out_t *out, ui_to_t to,
                         bool speaking)
{
    if (st->talk != UI_TALK_IDLE || speaking || in->net == UI_NET_BUSY ||
        in->jstate == UI_JP_BUSY) {
        return false;
    }
    st->talk = UI_TALK_RECORDING;
    st->talk_since = in->now;
    st->rec_to = to;
    st->rec_heard = false;
    st->rec_hush = 0;
    /* The follow-up window is closed: a message is not a turn, and the microphone must not
       reopen after one. */
    st->follow_armed = false;
    act(out, UI_ACT_CAPTURE_OPEN, 0);
    return true;
}

bool ui_start_listening(ui_state_t *st, const ui_in_t *in, ui_out_t *out)
{
    return start_listening(st, in, out, in->speaking);
}

bool ui_start_record(ui_state_t *st, const ui_in_t *in, ui_out_t *out, ui_to_t to)
{
    return start_record(st, in, out, to, in->speaking);
}

void ui_voice_stop(ui_state_t *st, ui_out_t *out)
{
    /* Three states to leave: a recording in progress is dropped rather than sent, a reply
       already in flight is abandoned rather than spoken, and the follow-up window is closed so
       the microphone does not reopen. The turn counter goes to its cap rather than to a
       separate flag — the next deliberate start resets it, which is the rule that already
       governs the loop. */
    if (st->talk == UI_TALK_LISTENING || st->talk == UI_TALK_RECORDING) {
        act(out, UI_ACT_CAPTURE_DROP, 0);
    } else if (st->talk != UI_TALK_IDLE) {
        act(out, UI_ACT_TALK_CLEAR, 0);
    }
    st->talk = UI_TALK_IDLE;
    st->listen_voice = false;
    st->follow_armed = false;
    st->follow_turns = FOLLOW_MAX_TURNS;
}

/* ── WHO CLAIMS A PRESS ───────────────────────────────────────────────────────────────────*/

/* The order, once, in one place. See `ui.h` for why this is a table and not a chain. */
const ui_target_t UI_TAP_ORDER[UI_TAP_ORDER_LEN] = {
    UI_TARGET_GRID, UI_TARGET_EXIT, UI_TARGET_POPUP, UI_TARGET_PAIR, UI_TARGET_CONFIRM,
    UI_TARGET_PET,
};

const ui_target_t UI_TAP_ORDER_PLAYING[UI_TAP_ORDER_PLAYING_LEN] = {
    /* The menu and the exit before the transport, because the transport consumes every press it is
       offered and both of those are drawn above it — see `ui.h`. */
    UI_TARGET_GRID, UI_TARGET_EXIT, UI_TARGET_TRANSPORT, UI_TARGET_PET,
};

bool ui_run_controls_up(const ui_state_t *st, const ui_in_t *in)
{
    return in->jrunning || in->stream_active || in->jfetching || st->pending == UI_PEND_PLAY ||
           st->pending == UI_PEND_REPLAY;
}

void ui_popup_target(int y0, int over_h, bool big, int box[4])
{
    if (big) {
        box[0] = (FACE_W - UI_POPUP_BIG_W) / 2;
        box[1] = y0 + (over_h - y0 - UI_POPUP_BIG_H) / 2;
        box[2] = box[0] + UI_POPUP_BIG_W;
        box[3] = box[1] + UI_POPUP_BIG_H;
        return;
    }
    box[0] = 0;
    box[1] = y0;
    box[2] = FACE_W / 2;
    box[3] = y0 + (over_h - y0) / 2;
}

const ui_layer_t UI_DRAW_ORDER[UI_LAYER_COUNT] = {
    UI_LAYER_TALK, UI_LAYER_POPUP, UI_LAYER_TRANSPORT, UI_LAYER_GRID,
};

/* IS THIS TARGET ON THE GLASS AT ALL. Kept apart from where it is, because "the control was not
   there" and "the finger missed it" are different answers and the panel has shipped both. */
static bool target_live(ui_target_t t, const ui_state_t *st, const ui_in_t *in)
{
    switch (t) {
    case UI_TARGET_GRID:
        return st->sendto_until != 0;
    case UI_TARGET_EXIT: {
        /* AND NOT WHILE A NOTICE IS OFFERING A MESSAGE — the owner's *"playback doesn't seem to
           work most of the time"*.
         *
           THE ARITHMETIC: this region is x[184,368) y[0,224). The big pop-up is CENTRED, so its
           rectangle is x[36,332) y[117,331), and the sender's face a child is told to press sits
           at cx = 36 + 296/2 = 184 — exactly FACE_W/2. Its entire right half is inside the exit,
           which comes first in the table. Since the pair stopped expiring, the exit has been
           live permanently from the first message ever played, so a tap a hair right of centre on
           the face hit it; the exit plays no cue by design, so the press vanished, and because it
           clears the pair the SECOND press in the same place worked. First press dead, second
           fine, once per message, and only on the big notice — which reads exactly as "most of
           the time".
         *
           A message being OFFERED outranks a message being ended: there is nothing to exit from
           when nothing is playing, and the notice is the thing the child is aiming at. */
        const bool notice_offered = st->popup_box[0] >= 0 && !in->stream_active && !in->jrunning;
        if (notice_offered) return false;
        /* AND NOT FOR THE FIRST `EXIT_GRACE_MS` OF WHAT IT WOULD END — see `ui.h`. The corner
           overlaps a quarter of the notice, so the press that STARTS a message lands where
           cancelling it will be one frame later. */
        if (st->controls_since != 0 && in->now - st->controls_since < EXIT_GRACE_MS) return false;
        return in->jrunning || in->stream_active || st->repeat_until != 0;
    }
    case UI_TARGET_POPUP:
        return st->popup_box[0] >= 0;
    case UI_TARGET_PAIR:
        return st->repeat_until != 0;
    case UI_TARGET_CONFIRM:
        return (st->talk == UI_TALK_LISTENING && st->listen_voice) ||
               st->talk == UI_TALK_RECORDING;
    case UI_TARGET_TRANSPORT:
        return ui_run_controls_up(st, in);
    case UI_TARGET_PET:
        return true;
    case UI_TARGET_COUNT:
        break;
    }
    return false;
}

/* DOES THE PRESS LAND IN IT. Three of these answer `true` unconditionally, and each of the
   three is a deliberate modality rather than an oversight: the grid, the tick-and-cross pair
   and a sounding run all consume a press that missed, because the alternative is a menu a child
   has to aim at twice, a pet that twitches mid-sentence, and a tap that ends a message by
   accident. */
static bool target_hit(ui_target_t t, const ui_state_t *st, const ui_in_t *in)
{
    switch (t) {
    case UI_TARGET_GRID:
        return true;
    case UI_TARGET_EXIT:
        return in->oy >= 0 && in->oy < in->over_h / 2 && in->ox >= FACE_W / 2;
    case UI_TARGET_POPUP:
        return in_box(st->popup_box, in->ox, in->oy);
    case UI_TARGET_PAIR:
        return confirm_hit(in->ox, in->oy, in->over_h) != CONFIRM_NONE;
    case UI_TARGET_CONFIRM:
        return true;
    case UI_TARGET_TRANSPORT:
        return true;
    case UI_TARGET_PET:
        return true;
    case UI_TARGET_COUNT:
        break;
    }
    return false;
}

ui_target_t ui_tap_target(const ui_state_t *st, const ui_in_t *in)
{
    /* THE TABLE FOLLOWS WHAT IS DRAWN. Not `speaking`, which is true for the panel's own cues;
       and not `stream_active` alone either, because the transport lives in the second table ONLY
       and is painted from the moment the press is taken — through the fetch, before a sound. See
       `ui.h`. */
    const bool playing = ui_run_controls_up(st, in);
    const ui_target_t *order = playing ? UI_TAP_ORDER_PLAYING : UI_TAP_ORDER;
    const int n = playing ? UI_TAP_ORDER_PLAYING_LEN : UI_TAP_ORDER_LEN;
    for (int i = 0; i < n; i++) {
        if (target_live(order[i], st, in) && target_hit(order[i], st, in)) return order[i];
    }
    /* Unreachable: both tables end with the pet, which is always live and always hit. Stated
       rather than assumed, because a table someone shortens should fail loudly here. */
    return UI_TARGET_PET;
}

bool ui_layer_up(const ui_overlay_t *ov, ui_layer_t layer)
{
    switch (layer) {
    case UI_LAYER_TALK:
        return ov->listening || ov->recording || ov->confirm || ov->thinking;
    case UI_LAYER_POPUP:
        return ov->popup_big || ov->popup_badge;
    case UI_LAYER_TRANSPORT:
        return ov->run || ov->pair;
    case UI_LAYER_GRID:
        return ov->grid;
    case UI_LAYER_COUNT:
        break;
    }
    return false;
}

/* ── THE PRESS ────────────────────────────────────────────────────────────────────────────*/

void ui_tap(ui_state_t *st, const ui_in_t *in, ui_out_t *out)
{
    /* THE EXCHANGE CONTINUES ITSELF. Armed when a reply starts playing, fired on the edge where
       the speaker falls silent — not on a timer, because a long reply must not have the
       microphone opened underneath it. */
    if (st->was_speaking && !in->speaking && st->follow_armed) {
        st->follow_armed = false;
        if (st->talk == UI_TALK_IDLE && in->net != UI_NET_BUSY &&
            st->follow_turns < FOLLOW_MAX_TURNS) {
            st->talk = UI_TALK_LISTENING;
            st->talk_since = in->now;
            st->listen_voice = true;
            st->listen_heard = false;
            st->listen_hush = 0;
            st->listen_lead = FOLLOW_LEAD_MS;
            st->follow_turns++;
            act(out, UI_ACT_CAPTURE_OPEN, 0);
            out->dirty = true;
        }
    }
    st->was_speaking = in->speaking;

    if (!in->tapped) return;

    const int ox = in->ox, oy = in->oy;

    /* ONE SWITCH OVER ONE ORDERED TABLE. Which target won is `ui_tap_target`'s answer and
       nothing below re-litigates it; what each target DOES is here and nowhere else. */
    switch (ui_tap_target(st, in)) {

    case UI_TARGET_TRANSPORT: {
        /* A FINGER STOPS A RUN OF MESSAGES, and this is the third place that rule applies — it
           already ends a listen and abandons a recording.
         *
           Only a RUN. A poke during the pet's OWN reply still just flinches: that is one
           sustained utterance the panel is making, not a queue the child is sitting through.
           Asked of the speaker rather than of the queue, because a replay sets none of the
           queue's flags and would otherwise have no working controls at all. */
        {
            const confirm_hit_t half = confirm_hit(ox, oy, in->over_h);
            if (half == CONFIRM_CANCEL) {
                const bool hold = !in->stream_paused;
                act(out, UI_ACT_PAUSE, hold ? 1 : 0);
                st->paused_since = hold ? in->now : 0;
                /* No cue either way: a beep on top of the sentence it is holding, or on the
                   first instant of the one it is resuming, is the panel talking over itself. */
            } else if (half == CONFIRM_SEND) {
                /* ANSWER THE PERSON TALKING, without waiting for them to finish. The recipient
                   is the message being played, so it is read BEFORE the run ends. Deferred
                   rather than started here: the fetch is still unwinding and a recording would
                   be refused a microphone it cannot have yet — silently, which is the one
                   outcome a child cannot interpret. */
                st->pend_reply_to = in->in_from;
                act(out, UI_ACT_PAUSE, 0); /* never end a run holding the ring */
                st->paused_since = 0;
                act(out, UI_ACT_STOP, 0);
                st->pending = UI_PEND_REPLY;
                st->pending_until = in->now + PENDING_MS;
            }
            /* Off both targets: the flinch alone. This is where a tap used to end the run, so
               the one thing it must not do now is end it by accident. */
        }
        out->flinch = true;
        out->dirty = true;
        return;
    }

    case UI_TARGET_GRID: {
        /* THE GRID OUTRANKS EVERYTHING WHILE IT IS UP, and it has to: a child pressed a button
           to put it there, so this is the one overlay on the panel that was asked for
           explicitly rather than offered.
           EVERY PRESS IS CONSUMED while it is open, including the empty corner and the dead
           bands, which is what makes it modal: the pet cannot be poked through a menu, so a
           miss costs a press rather than a fart — stated in `target_hit`. */
        const sendto_hit_t who = sendto_hit(ox, oy, in->over_h);
        if (who == SENDTO_SISTER || who == SENDTO_DAD) {
            st->sendto_until = 0;
            /* ONE CUE, AND IT IS THE ONE THE SPOKEN PHRASE ALREADY PLAYS. A refusal says so
               instead of going quiet — a control that answers with silence is the thing the
               repeat icon's cue was added to stop. */
            if (ui_start_record(st, in, out, who == SENDTO_DAD ? UI_TO_DAD : UI_TO_PANEL)) {
                cue(out, CUE_LISTEN);
            } else {
                cue(out, CUE_STOP);
            }
        } else if (who == SENDTO_PET) {
            st->sendto_until = 0;
            if (ui_start_listening(st, in, out)) {
                cue(out, CUE_LISTEN);
            } else {
                cue(out, CUE_STOP);
            }
        } else if (who == SENDTO_CANCEL) {
            st->sendto_until = 0;
            cue(out, CUE_STOP);
        }
        /* A miss inside the menu keeps it up: closing on a stray finger would make the grid
           something a child has to aim at twice. */
        out->flinch = true;
        out->dirty = true;
        return;
    }

    case UI_TARGET_EXIT: {
        /* THE WAY OUT, in the one quadrant this screen does not use — the sender's face has the
           top left, the pair has the bottom. Ahead of the overlays that outrank the pet rather
           than down among the taps that missed: the pet is drawn centred and its head reaches
           into that corner, so a finger aimed at the exit can land ON it, and left down there
           the gesture would work or make the pet blink depending on where exactly a
           four-year-old put her finger — indistinguishable from it not working.
         *
           AND IT IS TOO FAR UP THE TABLE, which only became visible once the order WAS a table.
           The region is the whole top-right quadrant and it overlaps the centred notice, so
           while the again/reply pair is live — which, since nothing expires it, is permanently
           after the first message the panel ever plays — the first press on a NEW notice is
           eaten here and only the second one plays it. Preserved as it shipped, and listed in
           `firmware/host/README.md` rather than fixed inside a move. */
        act(out, UI_ACT_STOP, 0);
        act(out, UI_ACT_PAUSE, 0); /* never leave the ring held after a stop */
        st->paused_since = 0;
        st->pending = UI_PEND_NONE; /* a deferred play must not resurrect what she ended */
        st->repeat_until = 0;
        out->flinch = true;
        out->dirty = true;
        /* No cue: the silence IS the answer, and a sound in the half-second after a child asks
           for quiet is the panel arguing with her. */
        return;
    }

    case UI_TARGET_POPUP: {
        out->flinch = true;
        /* Cleared the moment it is pressed, not when the audio arrives: a box that stays up
           through a fetch invites a second press, and the fetch refuses that one — so the child
           would be pressing a button that had stopped working. */
        st->popup_box[0] = -1;
        /* The press sounds FIRST and the message follows it — see `PENDING_MS`. */
        cue(out, CUE_HEARD);
        st->pending = UI_PEND_PLAY;
        st->pending_until = in->now + PENDING_MS;
        out->dirty = true;
        return;
    }

    case UI_TARGET_PAIR: {
        /* REPLAY AND REPLY, the two halves that replace a lone centred repeat icon. Hit through
           `confirm_hit` — the same halves the tick and cross use — rather than a third geometry:
           the places a child has learned are the places, and a second set of rules for the same
           two corners is how a press once landed 289 px from the icon it was aimed at.
           A press that hit NEITHER half never arrives here: `target_hit` sends it on to the next
           candidate, which is what lets a tick-and-cross turn under a standing pair still
           work. */
        const confirm_hit_t half = confirm_hit(ox, oy, in->over_h);
        if (half == CONFIRM_CANCEL) {
            out->flinch = true;
            /* IT ASKS THE BOX NOW. Streaming discards the audio as it plays, so "again" is a
               fetch and it needs the link to be up. A sound for the finger, then the audio. */
            cue(out, CUE_HEARD);
            st->pending = UI_PEND_REPLAY;
            st->pending_until = in->now + PENDING_MS;
            st->repeat_until = in->now + REPEAT_MS; /* still asking; keep it up */
            out->dirty = true;
            return;
        }
        if (half == CONFIRM_SEND) {
            /* THE REPLY, AND IT NEEDS NO CHOICE MADE. The recipient is whoever just spoke,
               which the panel already knows — answering a message used to mean opening the menu
               and picking the person who had this second finished talking. That is the
               difference between a message and a conversation. */
            st->repeat_until = 0; /* the pair is gone; the tick and cross take over */
            if (ui_start_record(st, in, out, in->in_from)) {
                cue(out, CUE_LISTEN);
            } else {
                cue(out, CUE_STOP);
            }
            out->flinch = true;
            out->dirty = true;
            return;
        }
        return; /* `target_hit` admits only the two halves; never fall into the next case */
    }

    case UI_TARGET_CONFIRM: {
        /* THE TICK AND THE CROSS, AND THEY REPLACE "A TOUCH ANYWHERE CANCELS". The cross
           cancels, the tick sends, and ANYTHING ELSE ON THE GLASS DOES NOTHING — including the
           pet, which cannot be poked mid-message. ONLY THE HANDS-FREE TURNS: a held listen ends
           on the release of the finger that started it, so it never reaches here. */
        const confirm_hit_t pressed = confirm_hit(ox, oy, in->over_h);
        const bool recording = (st->talk == UI_TALK_RECORDING);
        if (pressed == CONFIRM_CANCEL) {
            /* Exactly what "stop stop" and the old touch-anywhere did, so the two ways to say
               stop still behave identically. */
            act(out, UI_ACT_CAPTURE_DROP, 0);
            st->talk = UI_TALK_IDLE;
            st->listen_voice = false;
            if (!recording) {
                st->follow_armed = false;
                st->follow_turns = FOLLOW_MAX_TURNS;
            }
            out->flinch = true;
            cue(out, CUE_STOP);
            out->dirty = true;
            return;
        }
        if (pressed == CONFIRM_SEND) {
            /* NOTHING HEARD IS NOT A SEND. The hush branch already refuses to send a room
               nobody spoke into, and a tick pressed into that same silence must refuse too —
               otherwise the one exit that skips the silence check becomes the way six seconds
               of a bedroom reaches dad. Said out loud, because a child who pressed the tick and
               heard nothing has been told it went. */
            if (!(recording ? st->rec_heard : st->listen_heard)) {
                act(out, UI_ACT_CAPTURE_DROP, 0);
                st->talk = UI_TALK_IDLE;
                st->listen_voice = false;
                out->flinch = true;
                cue(out, CUE_OOPS);
                out->dirty = true;
                return;
            }
            /* A SOUND FOR THE FINGER, THEN THE OUTCOME. `CUE_SENT` is played only when the BOX
               confirms, which is a network round trip away, while the cross answers instantly —
               so the two targets felt different in the hand and the owner reported exactly
               that. The one control a child presses to send their voice must not be the one
               that answers with silence. */
            cue(out, CUE_HEARD);
            st->listen_voice = false;
            if (recording) {
                st->talk = UI_TALK_IDLE;
                act(out, UI_ACT_SEND_JPANEL, (int)st->rec_to);
            } else {
                /* Optimistic, and taken back by `ui_talk_send_result` if the upload refused. */
                st->talk = UI_TALK_THINKING;
                st->talk_since = in->now;
                act(out, UI_ACT_SEND_TALK, 0);
            }
            out->flinch = true;
            out->dirty = true;
            return;
        }
        /* Off both targets: CONSUMED, and deliberately without a flinch. Every other tap on
           this glass answers somehow, and that is exactly what must not happen here — a pet
           that twitches while a child is talking to it is the panel inviting the next poke
           mid-sentence. BUT IT SAYS SO: a miss silent on the glass AND in the log is a control
           that cannot be diagnosed without a cable. */
        out->tap_missed = true;
        return;
    }

    case UI_TARGET_PET:
    case UI_TARGET_COUNT:
        break;
    }

    /* THE PET, and this is the one branch that does care about any sound at all: cutting across
       the pet mid-sentence with a colour change and a fresh beep is what the old whole-dispatcher
       guard was really protecting. So `speaking` here, `stream_active` for the arbitration. */
    if (in->speaking) {
        /* Poked mid-sentence with no run to control. The flinch stays — ignoring the finger
           entirely would read as a frozen pet — but no beep, no colour change and no new action,
           so the reply finishes with the mouth still moving. */
        out->flinch = true;
        out->dirty = true;
        return;
    }
    /* Nothing above claimed it. The poke, the colour and the label are `display.c`'s. */
    out->tap_fell_through = true;
}

/* ── THE REST OF THE FRAME ────────────────────────────────────────────────────────────────*/

void ui_frame(ui_state_t *st, const ui_in_t *in, ui_out_t *out)
{
    /* THE EDGE THE EXIT'S GRACE IS MEASURED FROM — see `EXIT_GRACE_MS`. Sampled here rather than
       inside `target_live`, which is deliberately side-effect-free so the arbitration can be
       asserted without driving it. */
    if (ui_run_controls_up(st, in)) {
        if (st->controls_since == 0) st->controls_since = in->now == 0 ? 1 : in->now;
    } else {
        st->controls_since = 0;
    }
    /* THE BUTTON IS THE POWER CONTROL, not the menu: one gesture per job is worth more than a
       second way to reach the same grid, because the button is the only control on this unit
       that can turn it off. */
    if (in->btn_short) {
        if (in->standby) {
            act(out, UI_ACT_WAKE, 0);
            cue(out, CUE_HEARD);
            out->dirty = true;
        } else {
            /* IT BLINKS FIRST: the screen going dark is indistinguishable from the screen
               having crashed unless something acknowledges the press. The cue is that
               acknowledgement, and it sounds BEFORE the dark rather than into it. */
            cue(out, CUE_STOP);
            st->sendto_until = 0; /* nothing modal survives being told to be quiet */
            st->repeat_until = 0;
            if (st->talk != UI_TALK_IDLE) {
                st->talk = UI_TALK_IDLE;
                act(out, UI_ACT_CAPTURE_DROP, 0);
            }
            act(out, UI_ACT_STOP, 0);
            act(out, UI_ACT_ENTER_STANDBY, 0);
            act(out, UI_ACT_BLANK, 0);
            out->dirty = true;
        }
    }
    /* THE HOLD LEAVES FROM THE SAME PLACE A REBOOT DOES — see `display.c`: that is the one
       point where a frame has just finished and nothing is in flight on the QSPI bus. */
    if (in->btn_hold) act(out, UI_ACT_POWER_OFF, 0);

    /* Closed by its own clock. The pet is what a child came back to, so a menu nobody answered
       gets out of the way rather than waiting forever. */
    if (st->sendto_until != 0 && in->now >= st->sendto_until) {
        st->sendto_until = 0;
        out->dirty = true;
    }

    /* PRESS AND HOLD. After `gesture_poll`, so the tap count is this frame's: the maintenance
       gestures are taps THEN a hold, so a hold that begins while a tap run is live belongs to
       them and must not also open the menu. */
    if (!in->down) {
        st->down_since = 0;
    } else if (st->down_since == 0) {
        st->down_since = in->now;
        /* Same frame as the edge that set them, so this is THIS press's origin. */
        st->down_x = in->tapped ? in->panel_x : -1;
        st->down_y = in->tapped ? in->panel_y : -1;
    }
    const uint32_t held = (in->down && st->down_since != 0) ? in->now - st->down_since : 0;
    const bool on_the_pet = st->down_x >= TALK_MARGIN_PX && st->down_x < FACE_W - TALK_MARGIN_PX &&
                            st->down_y >= TALK_MARGIN_PX && st->down_y < FACE_H - TALK_MARGIN_PX;

    /* A HOLD ON THE PET OPENS THE MENU, rather than talking to it: the grid offers her sister,
       her dad and the pet, so a hold that went straight to a conversation would be the one
       gesture on this panel that could not reach the other three people.
       ANY COUNT THE MAINTENANCE GESTURES HAVE NOT CLAIMED, which used to be zero alone —
       children do not hold from a standing start, they poke the pet, it does something, they
       poke it again, and then they hold. */
    if (st->talk == UI_TALK_IDLE && in->down && on_the_pet && !in->stream_active &&
        !gesture_reserved(in->gest_taps) && held >= HOLD_TALK_MS && in->net != UI_NET_BUSY &&
        st->sendto_until == 0) {
        st->sendto_until = in->now + SENDTO_MS;
        /* The same cue the button's press makes, because it is the same event: something has
           appeared and it is waiting to be pressed. */
        cue(out, CUE_HEARD);
        out->dirty = true;
    } else if (st->talk == UI_TALK_IDLE && in->down && !on_the_pet &&
               !gesture_reserved(in->gest_taps) && held >= HOLD_TALK_MS &&
               held < (uint32_t)(HOLD_TALK_MS + in->dt_ms)) {
        /* Once per press, on the frame the threshold passes — the owner has no terminal but
           does have the log, and a margin that is too wide looks exactly like a microphone that
           stopped working unless the panel says which it is. */
        out->rim_hold = true;
    } else if (st->talk == UI_TALK_LISTENING && st->listen_voice) {
        /* WAITING FOR THE ROOM TO GO QUIET. A held turn ends when the finger lifts; this one
           has to be read off the front end's VAD. */
        if (in->hearing) {
            st->listen_heard = true;
            st->listen_hush = 0;
        } else if (st->listen_heard && st->listen_hush == 0) {
            st->listen_hush = in->now;
        }
        const bool hushed = st->listen_heard && st->listen_hush != 0 &&
                            in->now - st->listen_hush >= LISTEN_HUSH_MS;
        const bool full = in->capture_ms >= in->capture_cap_ms;
        const bool nothing = !st->listen_heard && in->now - st->talk_since > st->listen_lead;
        if (nothing) {
            /* The name and then silence — a television, or a child who changed their mind.
               Dropped without a bubble: an accidental wake must cost nothing. */
            act(out, UI_ACT_CAPTURE_DROP, 0);
            st->talk = UI_TALK_IDLE;
            st->listen_voice = false;
        } else if (hushed || full) {
            st->listen_voice = false;
            st->talk = UI_TALK_THINKING;
            st->talk_since = in->now;
            act(out, UI_ACT_SEND_TALK, 0);
            out->dirty = true;
        }
    } else if (st->talk == UI_TALK_RECORDING) {
        /* THE SAME THREE WAYS OUT A HANDS-FREE LISTEN HAS, and deliberately the same numbers:
           hush sends, silence drops it, the cap sends what there is. The only difference is
           where it goes. */
        if (in->hearing) {
            st->rec_heard = true;
            st->rec_hush = 0;
        } else if (st->rec_heard && st->rec_hush == 0) {
            st->rec_hush = in->now;
        }
        const bool hushed =
            st->rec_heard && st->rec_hush != 0 && in->now - st->rec_hush >= LISTEN_HUSH_MS;
        const bool full = in->capture_ms >= in->capture_cap_ms;
        const bool nothing = !st->rec_heard && in->now - st->talk_since > RECORD_LEAD_MS;
        if (nothing) {
            act(out, UI_ACT_CAPTURE_DROP, 0);
            st->talk = UI_TALK_IDLE;
            out->dirty = true;
        } else if (hushed || full) {
            st->talk = UI_TALK_IDLE;
            act(out, UI_ACT_SEND_JPANEL, (int)st->rec_to);
            out->dirty = true;
        }
    } else if (st->talk == UI_TALK_LISTENING && !in->down) {
        /* The held turn, ended by the finger lifting. */
        st->talk = UI_TALK_THINKING;
        st->talk_since = in->now;
        act(out, UI_ACT_SEND_TALK, 0);
    } else if (st->talk == UI_TALK_THINKING && in->net == UI_NET_SPOKE) {
        /* Speaking. The bubble goes and the pet reacts, and the state is held on the speaker
           rather than a timer so a long reply cannot end on screen mid-sentence. */
        st->talk = UI_TALK_IDLE;
        act(out, UI_ACT_TALK_CLEAR, 0);
        /* Armed, not opened: the reply has not started coming out of the speaker yet, let alone
           finished. The edge in `ui_tap` does the opening. */
        st->follow_armed = true;
        out->nod = true;
    } else if (st->talk == UI_TALK_THINKING && in->net == UI_NET_IDLE) {
        st->talk = UI_TALK_IDLE; /* the box heard silence; nothing to say about it */
        act(out, UI_ACT_TALK_CLEAR, 0);
    } else if (st->talk == UI_TALK_THINKING &&
               (in->net == UI_NET_FAILED || in->now - st->talk_since > TALK_TIMEOUT_MS)) {
        act(out, UI_ACT_TALK_CLEAR, 0);
        /* NOT a silent return to idle. On a panel whose owner has no terminal, "it did not hear
           you" and "it is broken" must not look identical — and not to a child either: to a
           four-year-old who has just spoken to a toy, silence IS the failure. A low falling
           pair says try again, and is deliberately gentle. */
        st->talk = UI_TALK_FAILED;
        st->talk_since = in->now;
        cue(out, CUE_OOPS);
    } else if (st->talk == UI_TALK_FAILED && in->now - st->talk_since > TALK_FAILED_MS) {
        st->talk = UI_TALK_IDLE;
    }

    /* A HOLD IS NOT FOREVER. A paused stream is an HTTP response held open on the box, and a
       four-year-old who put the pet down mid-message would otherwise hold it until the panel
       rebooted. It RESUMES rather than aborting: a message playing out to an empty room costs
       nothing and is recoverable, where discarding one they had not finished hearing is the
       outcome the queue exists to prevent. */
    if (st->paused_since != 0 && in->now - st->paused_since > AUDIO_PAUSE_MAX_MS) {
        act(out, UI_ACT_PAUSE, 0);
        st->paused_since = 0;
        out->dirty = true;
    }

    /* THE DEFERRED HALF OF A TOUCH: the cue has finished, so the audio it announced starts now.
       Checked every frame rather than on a timer, so it fires on the first frame the speaker is
       free — the 55 ms cue and this are what a child experiences as one press. */
    if (st->pending != UI_PEND_NONE) {
        if (st->pending == UI_PEND_REPLY) {
            /* WAITS FOR THE FETCH AS WELL AS THE SPEAKER, which is the whole reason this is
               deferred: stopping a run returns before it has finished letting go, and a
               recording refused in that window would be a press that did nothing. */
            if (!in->audio_playing && in->jstate != UI_JP_BUSY) {
                if (start_record(st, in, out, st->pend_reply_to, false)) {
                    cue(out, CUE_LISTEN);
                } else {
                    cue(out, CUE_OOPS);
                }
                st->pending = UI_PEND_NONE;
                out->dirty = true;
            } else if (in->now > st->pending_until) {
                /* SAID OUT LOUD, unlike the dropped play below. That one loses a message still
                   sitting in the queue with a badge to prove it; this one loses a child's
                   answer, and they would stand there having pressed reply and been given
                   neither a microphone nor a reason. */
                cue(out, CUE_OOPS);
                st->pending = UI_PEND_NONE;
                out->dirty = true;
            }
        } else if (!in->audio_playing) {
            act(out, st->pending == UI_PEND_PLAY ? UI_ACT_PLAY_NEXT : UI_ACT_REPLAY, 0);
            st->pending = UI_PEND_NONE;
            out->dirty = true;
        } else if (in->now > st->pending_until) {
            st->pending = UI_PEND_NONE;
        }
    }

    /* WHAT THE BOX SAID ABOUT THE MESSAGE, answered in sound because the child who sent it is
       four and the screen is showing a pet. Each outcome gets its OWN cue: "it went", "there is
       nobody to send it to" and "it did not go" are three different sentences. */
    switch (in->jstate) {
    case UI_JP_SENT:
        act(out, UI_ACT_JPANEL_CLEAR, 0);
        cue(out, CUE_SENT);
        act(out, UI_ACT_POLL_SOON, 0); /* the twin may already have answered */
        break;
    case UI_JP_NOBODY:
        act(out, UI_ACT_JPANEL_CLEAR, 0);
        cue(out, CUE_OOPS);
        out->say = "nobody to send to";
        out->dirty = true;
        break;
    case UI_JP_FAILED:
        act(out, UI_ACT_JPANEL_CLEAR, 0);
        cue(out, CUE_OOPS);
        break;
    case UI_JP_PLAYING:
        /* Held until the speaker stops, so the repeat window starts when the message ENDS
           rather than when it began — measured from the wrong end it would expire before a
           twenty-second message finished. `audio_playing` rather than the frame's `speaking`:
           that flag is sampled at the top of the frame and a tap LATER in the same frame is
           what starts the message, so this branch would otherwise fire the instant playback
           began. It used to clear the state that armed the acknowledgement, which is how one
           message came back every thirty seconds forever. */
        if (!in->audio_playing) {
            act(out, UI_ACT_JPANEL_CLEAR, 0);
            st->repeat_until = in->now + REPEAT_MS;
            out->dirty = true;
        }
        break;
    default:
        break;
    }

    /* A MESSAGE ARRIVING IS A FRAME, and it would otherwise not be one: nothing about a poll on
       another task sets `dirty`, so the pop-up would appear whenever the pet next happened to
       blink — a wait a child would spend looking at a panel that knows something and is not
       saying it. */
    if (in->waiting != st->waiting_shown) {
        /* A SOUND ON THE WAY UP ONLY. The count falls when a message is played, and announcing
           that would be the panel telling a child about the thing they just did. */
        if (in->waiting > 0 && in->waiting > st->waiting_shown) cue(out, CUE_MESSAGE);
        /* THE CLOCK STARTS WHEN THE WAIT DOES, not when the count last moved. A second message
           arriving while the first is still unheard must not restore the big box — the child
           has already been interrupted once and has chosen not to come yet. */
        if (in->waiting > 0 && st->waiting_shown <= 0) st->popup_since = in->now;
        st->waiting_shown = in->waiting;
        out->dirty = true;
    }
    /* THE SHRINK IS A FRAME NOBODY ELSE ASKS FOR. The count has not changed, no finger has
       landed and the pet may be perfectly still — so without this the big box would sit there
       until the next blink happened to repaint it. */
    {
        const bool big = in->waiting > 0 && in->now - st->popup_since < POPUP_BIG_MS;
        if (big != st->popup_was_big) {
            st->popup_was_big = big;
            out->dirty = true;
        }
    }
    if (in->jrunning) out->dirty = true;             /* the count in the run bar has to stay true */
    if (st->talk != UI_TALK_IDLE) out->dirty = true; /* the dot pulses and the dots cycle */
}

/* ── WHAT IS ON THE GLASS ─────────────────────────────────────────────────────────────────*/

void ui_overlay(ui_state_t *st, const ui_in_t *in, ui_overlay_t *ov)
{
    memset(ov, 0, sizeof(*ov));

    if (st->talk == UI_TALK_LISTENING) {
        ov->listening = true;
    } else if (st->talk == UI_TALK_RECORDING) {
        ov->recording = true;
        ov->rec_to = st->rec_to;
    }
    /* ONLY WHERE THERE IS SOMETHING TO CONFIRM, and a HELD listen is not it: that turn ends on
       the release of the finger that started it, so a tick would be a second way to finish a
       gesture that already has one, and a cross would be a target the finger is not free to
       reach. */
    if ((st->talk == UI_TALK_LISTENING && st->listen_voice) || st->talk == UI_TALK_RECORDING) {
        ov->confirm = true;
    } else if (st->talk != UI_TALK_IDLE) {
        ov->thinking = true;
        ov->thinking_failed = (st->talk == UI_TALK_FAILED);
    }

    /* THE TARGET IS A FACT ABOUT WHAT IS WAITING, NOT ABOUT WHAT WAS DRAWN, and tying the two
       together is why the owner reported that a notification *"doesn't really play every single
       time"*. The rectangle used to be set only where the pop-up was PAINTED, inside the same
       guard — so any frame that skipped the painting (mid-cue, so `speaking`; the fetch in
       flight, so BUSY) also cleared the target while the glass went on showing the notice from
       the previous frame. A control you can see is a control you can press; the two must not be
       able to disagree. So the hit test is armed here, from the queue, and only the PICTURE
       stays behind the idle guard. */
    /* ONE CONDITION FOR THE PICTURE AND THE TARGET, so they cannot disagree — which is the fault
       underneath *"it doesn't really play every single time"*, in both directions. The rectangle
       used to be armed from the queue alone while the painting sat behind a fuller guard, so the
       notice could be armed and invisible; before that the arming lived INSIDE the painting, so it
       could be visible and dead. Neither is fixable by moving the arming somewhere better. It is
       fixable by there being one answer to "is there a notice", used twice. */
    const bool notice_up = in->waiting > 0 && st->talk == UI_TALK_IDLE && !in->stream_active &&
                           in->jstate != UI_JP_BUSY;
    const bool notice_big = in->now - st->popup_since < POPUP_BIG_MS;
    st->popup_box[0] = -1;
    if (notice_up) ui_popup_target(in->over_y0, in->over_h, notice_big, st->popup_box);
    /* THE POP-UP OVER EVERYTHING, and only when the panel is otherwise idle. A box announcing a
       message on top of a pet that is mid-sentence, or mid-recording, would be two demands on a
       four-year-old at once — and the one it covers is the one they are already doing. It is not
       lost: the count lives on the box and the next idle frame draws it. */
    /* AND THE PICTURE, FROM THE SAME TWO FACTS. Big for the first fifteen seconds, then a badge.
       OVER THE PAIR, NOT DEFERRED BEHIND IT: the badge used to wait for the pair to lapse, which
       was fine while that was a ten-second deadline and is a bug now that the pair waits for a
       finger instead — a message arriving while the last one's again-and-reply stood would have
       been hidden for as long as nobody pressed the exit, which could be all night. */
    if (notice_up) {
        if (notice_big) {
            ov->popup_big = true;
        } else {
            ov->popup_badge = true;
        }
        ov->popup_from_dad = in->waiting_from_dad;
    }

    /* FROM THE PRESS, NOT FROM THE STREAM. The controls used to appear only once the run went
       true, which is after the cue has finished AND the fetch has opened AND the preroll has
       landed — seconds in which a child who has just pressed something sees nothing happen.
       FETCHING, NOT MERELY BUSY: `UI_JP_BUSY` covers a SEND as well, so reading "the panel is
       doing something" put the playback controls over an OUTGOING message — a pause button and
       a sender's face for audio that did not exist, with a tap handler that correctly refused to
       honour any of it. */
    const bool starting =
        st->pending == UI_PEND_PLAY || st->pending == UI_PEND_REPLAY || in->jfetching;
    if (in->jrunning || starting) {
        ov->run = true;
        /* PLAYING IS AN AUDIO FACT, NOT A QUEUE FACT: a replay deliberately sets none of the
           queue's flags, so it would otherwise fall through to the ended state — a play triangle
           over a message that was audibly playing. While STARTING, show pause: the press has
           been taken and sound is coming, so a play icon would invite a second press at exactly
           the moment the first is still being served. */
        ov->run_playing = starting || (in->audio_playing && !in->stream_paused);
        /* WHO IT IS FROM IS NOT KNOWN YET WHILE STARTING, and showing the wrong face for those
           seconds is worse than showing none: the in-from kind is read off the fetch's OWN
           response header, so until that fetch lands it still holds the PREVIOUS message's
           sender. The queue already knows who is waiting; ask it until the fetch can answer. */
        ov->run_from_dad = starting ? in->waiting_from_dad : (in->in_from == UI_TO_DAD);
        ov->run_count = in->waiting;
        ov->exit_corner = true;
    } else if (st->repeat_until != 0) {
        /* THE FACE STAYS FOR THE WHOLE EXCHANGE. The two buttons underneath are AGAIN and
           REPLY, and both are about a person — the one who just spoke. Taking their face away
           at exactly the moment those appear removes the answer to "reply to whom?" from the
           one screen that asks it. */
        ov->pair = true;
        ov->pair_from_dad = (in->in_from == UI_TO_DAD);
        ov->pair_playing = in->audio_playing && !in->stream_paused;
        ov->exit_corner = true;
    }

    /* THE GRID LAST OF ALL, over the pop-up and over the run control, because it is the one
       overlay here that a child asked for by pressing. Everything under it is something the
       panel offered; a menu that could be covered by an offer would be a question answered by
       an interruption. */
    if (st->sendto_until != 0) ov->grid = true;
}
