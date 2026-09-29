#pragma once

/* THE PANEL'S INTERACTION LOGIC, WITH NO PANEL IN IT.
 *
 * Every decision this file makes used to live inside `face_task()` in `display.c` — an
 * infinite loop wrapped around ESP-IDF, a QSPI bus and a framebuffer, which meant the whole
 * user interface could only ever be tested by handing a panel to a four-year-old. Six
 * user-facing faults shipped in one evening because that was the only integration test there
 * was: a tap that played nothing while the pet was mid-cue, playback controls raised over an
 * OUTGOING message, a control pair that withdrew itself while a child was deciding.
 *
 * None of those are display faults. They are arbitration faults — which overlay is up, and
 * what a finger at (x,y) means given everything else that is happening — and arbitration is
 * arithmetic. So it lives here, in the same pure-C tier as `confirm.c`, `gesture.c` and
 * `screen.c`, and the host suite drives it directly.
 *
 * THE CONTRACT. `display.c` gathers the frame's facts into `ui_in_t`, calls in, performs the
 * `ui_action_t`s that come back, and draws the overlay `ui_overlay()` describes. This module
 * touches no hardware, allocates nothing, opens no file, logs nothing and draws nothing: it
 * reads a struct and writes a struct. That is what makes it testable, and it is also the rule
 * that keeps it testable — anything here that needs a peripheral belongs on the other side of
 * the boundary as an action.
 *
 * WHY THREE ENTRY POINTS RATHER THAN ONE. A frame is not one decision, it is three at
 * different moments, and the order is load-bearing:
 *
 *   `ui_tap()`     early, before the recogniser is drained and before the calibration routine
 *                  takes over the frame — because a press has to be answered on the frame it
 *                  arrived on.
 *   `ui_frame()`   after `gesture_poll()`, because a hold that the maintenance gestures have
 *                  claimed must not also open the menu, and only `gesture_poll` knows.
 *   `ui_overlay()` inside the draw gate, because the pop-up's hit rectangle is armed where the
 *                  pop-up is considered — a frame that draws nothing must leave the previous
 *                  frame's targets standing, or a control a child can SEE stops working.
 *
 * Collapsing them into one call would move work across those boundaries, and each boundary is
 * a bug this firmware has already shipped once.
 */

#include <stdbool.h>
#include <stdint.h>

#include "confirm.h" /* the targets a finger can land on: `confirm_hit`, `sendto_hit` */
#include "cue.h"     /* which noise a press makes */

/* WHO A MESSAGE IS FOR. The same two values as `jpanel_to_t`, restated because `jpanel.h` reaches
   `cfg.h` and therefore `esp_err.h`, and this file must stay clean of ESP-IDF.
   TWO ENUMS THAT MUST AGREE ARE A THING TO CHECK, NOT TO HOPE FOR: the wiring step owes this one
   and the two below a `_Static_assert` per value in `display.c`, which is the only translation
   unit that can see both. A silent renumbering here would send a child's message to the wrong
   person, which is the failure mode least likely to be noticed and worst to discover. */
typedef enum {
    UI_TO_PANEL = 0, /* the other panel — the twin's unit */
    UI_TO_DAD,       /* the owner's PWA */
} ui_to_t;

/* Where the last voice-post request got to — `jpanel_state_t`, for the same reason. */
typedef enum {
    UI_JP_IDLE = 0,
    UI_JP_BUSY,
    UI_JP_SENT,
    UI_JP_PLAYING,
    UI_JP_NOBODY,
    UI_JP_FAILED,
} ui_jstate_t;

/* Where a conversation turn got to — `talk_net_t`, ditto. */
typedef enum {
    UI_NET_IDLE = 0,
    UI_NET_BUSY,
    UI_NET_SPOKE,
    UI_NET_FAILED,
} ui_net_t;

/* PRESS AND HOLD TO TALK — the owner's interaction, in four states.
 *
 *   "when we long press ... it should make a [sound] when it activates the listening and
 *    then when we release it should show the thinking box"
 *
 * THE HOLD THRESHOLD IS THE WHOLE DESIGN PROBLEM. `gesture.h` records that 4-5 year olds
 * produce ordinary presses lasting up to 4.2 s, which is why the maintenance gestures stopped
 * being a bare hold. A talk gesture cannot wait 4.2 s — nobody holds a button that long
 * before speaking — so it fires at 700 ms, comfortably past the 600 ms that still counts as a
 * tap, and accepts that ordinary play will sometimes start a listen. That is survivable here
 * in a way it was not for "reboot the panel": the cost of a false listen is a beep and a
 * discarded recording, not a toy restarting in a child's hands.
 *
 * It never fires mid-maintenance-gesture: those are taps THEN a hold, so a hold that begins
 * while a tap run is live belongs to them. */
#define HOLD_TALK_MS 700
/* AND IT HAS TO LAND ON THE PET. 700 ms is short enough that carrying the panel starts a
 * recording — the owner picked a unit up to photograph it sideways and found the red dot
 * already lit, which is the same false positive the paragraph above waved through as "a beep
 * and a discarded recording". It is not that any more: a listen now uploads six seconds of a
 * child's bedroom and makes the pet answer something nobody asked.
 *
 * The discriminator is free and already computed. A hand carrying a 32 mm panel touches its
 * RIM; a press meant for the pet lands on the pet, which occupies the middle. So the hold has
 * to begin inside an inset rectangle — ~6 mm in on every side, about the half-width of an
 * adult thumb pad, leaving a target of 224x304 that a four-year-old cannot miss. In PANEL
 * coordinates on purpose: the rim is the rim whichever way up the thing is mounted.
 *
 * This is a margin, not a fix for grip contact that lands squarely on the pet's face. The
 * complete answer is the rhythm `gesture.h` uses (slots 1 and 2 are still free, and it was
 * measured at 12 false fires per 20 000 child presses against a bare hold's 1937) — but a
 * rhythm is a thing to teach, and the owner asked for press-and-hold. Teach it only if this
 * is not enough. */
#define TALK_MARGIN_PX 72
/* Long enough that a slow answer is not mistaken for a broken one, short enough that a child
   is not staring at a bubble. Beyond it the panel says it failed rather than returning to
   idle, because "it didn't hear you" and "it broke" must not look the same (§10.4bc).
 *
 * 25 s, AND 12 WAS A GUESS THAT COST A WORKING REPLY BY 777 MILLISECONDS. The first warm turn
 * ever measured from the room took 12,777 ms end to end — whisper 10,668, the model 1,540,
 * Kokoro 568 — and returned 200 OK with 118 KB of speech. The panel had given up at 12,000,
 * called `talk_clear()`, and shown a failure face. Every part of the system worked and the
 * answer was thrown away three quarters of a second before it landed.
 *
 * The budget this has to clear is now measured rather than hoped for: whisper is a FLAT ~10.7 s
 * (it pads every clip to 30 s regardless of length — 10,715 and 10,668 on two utterances of
 * very different length), and the prompt holds replies to one or two sentences, so the model
 * and the speech together run a few seconds more. ~17 s is a bad-but-real turn; 25 leaves
 * headroom without waiting on something that is never coming.
 *
 * This is a SAFETY NET, NOT A TARGET. A longer net costs nothing when turns are fast; it only
 * matters when they are slow, and a slow turn currently produces NOTHING, which is strictly
 * worse than a late answer. The actual fix for the wait is whisper — 10.7 s of a 12.8 s turn
 * is 83% of it, and no timeout value improves that. */
#define TALK_TIMEOUT_MS 60000
#define TALK_FAILED_MS 2500

/* HANDS-FREE, AND THE WHOLE PROBLEM IS KNOWING WHEN THEY STOPPED.
 *
 * The owner: *"a wake word that will allow the same interaction as if I held the panel and it
 * was listening ... but we just need a way for emptiness at the end to stop it."*
 *
 * A hold has a release. A name does not, so the end of the sentence has to be FOUND. The
 * signal already exists and already runs: `speech.c` sets `s_hearing` from the front end's
 * own `vad_state`, which is what gates MultiNet and drives the indicator. Nothing new is
 * computed here — the recogniser has been deciding "is someone talking" every frame since
 * bring-up and nobody had asked it.
 *
 * Three ways out, and each is a different sentence to a four-year-old:
 *
 *   HUSH   they finished  -> send it. 1.8 s. It was 900 ms, and 900 ms was wrong: the owner,
 *                            after watching them use it — *"the babies keep getting cut off
 *                            because they're a little bit slow."* A four-year-old assembling
 *                            a sentence stops for longer than an adult does, and every one of
 *                            those pauses ended their turn for them. The old number was
 *                            reasoned from the SIX-SECOND cap rather than from a child: a
 *                            longer hush used to risk the cap eating the tail. The cap is ten
 *                            seconds now (`CAPTURE_MAX_MS`) and the box trims the silence
 *                            before whisper sees it, so waiting longer costs nothing at all.
 *   LEAD   they said the name and nothing else -> drop it, silently, back to idle. An
 *                            accidental "hey fish" from the television must not become an
 *                            upload, and this is the branch that stops it.
 *   the cap `audio.c` already enforces -> send what we have rather than truncating to nothing.
 */
#define LISTEN_HUSH_MS 1800
#define LISTEN_LEAD_MS 3000

/* AND THEN IT LISTENS AGAIN, WITHOUT BEING ASKED.
 *
 * The owner: *"after the text-to-speech comes back and finishes talking, we should just turn
 * the microphone on and start recording again, and if I start talking within 2 seconds, just
 * automatically record all that until I stopped talking again and send that as the next turn.
 * That way I can have fluid conversations."*
 *
 * Which is the difference between a toy you operate and one you talk to. The machinery is
 * already here — this is the hands-free listen from `VOCAB_LISTEN` with a different trigger
 * and a shorter lead — so the whole feature is: notice the reply finished, and open the same
 * window the name opens.
 *
 * NO BEEP ON THIS ONE. A tone after every reply is the toy interrupting the conversation it
 * just started; the red indicator is the affordance, and by the second turn a child knows what
 * it means.
 *
 * A CAP, BECAUSE THIS IS A LOOP WITH A LOUDSPEAKER IN IT. Every reply reopens the microphone,
 * and a television talking in the room can therefore hold a conversation with the panel
 * indefinitely — each turn costing whisper, a model and a voice. Six consecutive follow-ups is
 * far more than a four-year-old's exchange and bounds the runaway; after that it wants a
 * deliberate start again, which resets the count. */
#define FOLLOW_LEAD_MS 2000
#define FOLLOW_MAX_TURNS 6

/* VOICE POST ON THE GLASS (`docs/plans/JPANEL_PLAN.md`, W3).
 *
 * RECORDING IS A STATE BESIDE LISTENING, NOT A FLAG ON IT, and the three differences are why:
 * where the audio goes, what is drawn, and what ends it. A flag would have every branch of
 * the machine below asking "but which kind" — and the one that forgot would upload a child's
 * message to the pet, which would answer it out loud.
 *
 * WHAT IT SHARES is everything that was tuned for a four-year-old: the same hush (they stop
 * for longer than an adult does), the same lead, the same cap, and the same finger-cancels
 * rule the owner asked for. A second set of numbers would be a second thing to get wrong.
 *
 * A BLUE DOT, NOT THE RED ONE, and that is the owner's actual requirement rather than a
 * palette choice: red means *the robot is listening to you*, and talking to your sister must
 * not look like that. */
#define RECORD_LEAD_MS 4000

/* THE REPEAT ICON, and it is a deadline rather than a flag. It is for *"what did she say?"*,
   not a permanent control, and a button that never leaves would become another thing on the
   glass to poke.

   TEN SECONDS, UP FROM FIVE. The owner, watching the children use it: *"the replay button
   probably needs to stay on there for about 10 seconds after it shows."*
 *
   IT IS NO LONGER A DEADLINE. Nothing expires `repeat_until` any more, so this is the moment
   the pair went up rather than the moment it comes down, and the variable is really a flag
   with a timestamp in it. Kept as a timestamp because it costs nothing and a log that can say
   how long an offer stood is worth more than a bool. The pair leaves on the exit corner, or
   when a new message displaces it — see the badge below. */
#define REPEAT_MS 10000

/* THE POP-UP SHRINKS RATHER THAN NAGS.
 *
 * The owner: *"the notification on the panel is very large when it shows which is fine, but if
 * it's not acknowledged within say 15 seconds, it should kind of be a smaller one up on the
 * top left."*
 *
 * A box over the pet's face is right for the first fifteen seconds — it has to interrupt, the
 * reader is four and is not auditing the screen. It is wrong for the next hour: a message
 * nobody has come to yet should not hold a child's toy hostage. So it stands down to a badge
 * and the pet is a pet again, with the message still there and still tappable.
 *
 * `popup_since` is when the CURRENT run of waiting messages began — reset when the count goes
 * to zero, not on every poll, or a panel that polls every thirty seconds would restart the
 * clock forever and never shrink. */
#define POPUP_BIG_MS 15000

/* THE BIG NOTICE'S BOX, 296x214 and CENTRED — which is the whole reason this is shared rather
 * than a pair of locals in the drawing code. The target used to be the top-left quadrant while
 * the picture was this box, and the two barely overlap: the bubble is x[36,332) y[117,331) on a
 * 368-square face, so most of what a child could SEE was not pressable, and a press on its right
 * half reached whatever was behind it. *"Playback doesn't seem to work most of the time."*
 *
 * `ui_popup_target` is the one answer to "where is the notice", and both the arming and the
 * drawing take it from here. Big gives the bubble's own bounds — 62% of the screen, generous by
 * any measure. The badge gives the whole top-left quadrant instead, because shrinking the
 * picture must not shrink what a four-year-old has to hit: *"capture everything in that top left
 * quadrant as far as clicks to play it."* In both cases the target IS what is drawn. */
/* HOW LONG THE EXIT STAYS UNARMED AFTER THE CONTROLS APPEAR.
 *
 * A DESTRUCTIVE CONTROL MUST NOT ARM ITSELF UNDER A FINGER THAT IS ALREADY THERE. The notice and
 * the exit corner overlap over a quarter of the notice — x[184,332) y[117,224) against the
 * notice's x[36,332) y[117,331) — and that is the part an adult presses. The notice outranks the
 * exit while it is offered, so the first press plays; one frame later the controls are up and
 * that same point means cancel. Any second edge there ends the message the first one started,
 * and `jpanel_stop()` acknowledges, so the message is spent: *"it just shows the screen with the
 * pause icon for half a second and then goes back to the other indicator."*
 *
 * A deliberate double tap does it. So does one press — `touch.c` reports every down edge with no
 * inter-tap debounce, and a fingertip that lightens for a single 15 ms sample is two edges. The
 * button has had 250 ms of debounce since 0.3.26 for precisely this; the glass has none.
 *
 * 700 ms, which is HOLD_TALK_MS and not a coincidence: it is already this panel's measure of
 * "long enough to be meant rather than spilled". Imperceptible to someone reaching for the
 * corner deliberately, and longer than any bounce or double tap. The pause and reply halves are
 * NOT covered, because pausing something by accident is undone by pressing it again — only the
 * control that spends the message needs protecting. */
#define EXIT_GRACE_MS 700

#define UI_POPUP_BIG_W 296
#define UI_POPUP_BIG_H 214

/* ── SWIPING BETWEEN WHAT IS WAITING ──────────────────────────────────────────────────────
 *
 * The owner, after watching two messages arrive at once: *"if there's more than one message it
 * shows the number in the middle of the menu, which isn't a bad thing but maybe we can add a new
 * gesture which is swipe left and swipe right to change between the messages. When changing
 * between messages, we again need to make sure that the icon on the top left updates, as well as
 * the number in the middle. And that we handle stopping the current playing message if it's
 * playing."*
 *
 * THE SELECTION IS A UI FACT, SO IT LIVES HERE. `jpanel.c` holds the queue and can play any index
 * of it; which index a finger is pointing at is arbitration, and this is the module that can be
 * stepped frame by frame in a test.
 *
 * ONLY WHERE THERE IS SOMETHING TO SWIPE BETWEEN. With one message queued the gesture does
 * nothing at all — not "nothing visible", nothing: a lone message must not be losable to a smear,
 * and the 700 ms hold that opens the "who?" menu starts with a finger on the same glass.
 *
 * IT CLAMPS, IT DOES NOT WRAP. Three messages and a wrap means swiping right three times lands
 * back where you started, which reads as the panel ignoring you. Clamped, the ends are ends, and
 * the count in the middle says where you are.
 *
 * AND IT STOPS WHAT IS PLAYING, which the owner asked for and which is also what makes the
 * gesture safe to leave live during playback: a press on the notice or the transport fires on the
 * DOWN edge, as every press on this panel has since children-jab was measured, so a swipe that
 * began on a control has already triggered it by the time the travel is visible. Stopping is how
 * that unwinds. */
/* HOW FAR A FINGER MUST TRAVEL. 60 px is 4.7 mm on this glass (322 ppi) — far enough that no jab
   crosses it, close enough to be one motion of a small hand. And the drag must be more horizontal
   than vertical (`|dx| > |dy|`), which needs no second constant: a diagonal smear is a swipe, a
   vertical one is not, and nothing else on this panel wants a vertical drag. */
#define SWIPE_MIN_PX 60

/* HOW MANY QUEUED MESSAGES THIS MODULE CAN BE TOLD THE SENDER OF. `JPANEL_QUEUE_MAX` in
   `jpanel.h` and `JPANEL_QUEUE_MAX` in the backend are the same number; a test pins all three. */
#define UI_QUEUE_MAX 8

void ui_popup_target(int y0, int over_h, bool big, int box[4]);

/* A TOUCH THAT MAKES A SOUND AND STILL PLAYS AT ONCE.
 *
 * The owner asked for a sound on these two controls and the first attempt produced none, on an
 * argument that was simply wrong: the cue was made conditional on nothing already sounding, and
 * once the prefetch landed the message ALWAYS starts on the same frame — so the condition was
 * never true. "The message is its own acknowledgement" is not an answer to a four-year-old who
 * pressed something; the press has to answer.
 *
 * The real constraint is that `audio_play` refuses while anything else sounds, so a cue and a
 * message cannot overlap: an unconditional cue would simply eat the message. So the cue plays
 * and the audio is DEFERRED by one speaker — `CUE_BLIP` is 55 ms, which is under the 100 ms a
 * press and its sound can be apart and still feel like one event, and far under the ~2 s this
 * release removed.
 *
 * A DEADLINE, because a deferral that never fires is a button that did nothing. If the speaker
 * is somehow still busy after this, the action is dropped rather than firing late into silence
 * a child has stopped associating with their finger. */
/* HOW LONG THE "WHO?" GRID STAYS UP. A menu a child walked away from must not sit on the pet's
   face forever — the pet is the thing they came back to — and it must not be so brief that
   opening it and then deciding is a race. Ten seconds is the pop-up's own big-badge window
   twice over, which is the closest thing here to a measured attention span. */
#define SENDTO_MS 10000

/* HOW LONG A PRESS WAITS FOR THE SPEAKER BEFORE IT IS GIVEN UP ON. It was 1500 ms, and that
   is shorter than it sounds: `audio_playing()` counts the panel's own cues, so a press landing
   while the pet was mid-noise could be dropped with nothing but a log line to show for it —
   which is the other half of the owner's *"it doesn't really play every single time"*. Eight
   seconds is long enough to outlast anything the pet does to itself and still short enough that
   a genuinely stuck speaker does not strand the tap forever. A child's press should not
   evaporate because the toy happened to be burping. */
#define PENDING_MS 8000

/* ── THE STATE ────────────────────────────────────────────────────────────────────────────
 *
 * Every one of these was a file-scope `static` in `display.c`. They are a struct now for one
 * reason: a test can make a fresh one, and a second test cannot inherit the first one's mood.
 */

typedef enum {
    UI_TALK_IDLE = 0,
    UI_TALK_LISTENING,
    UI_TALK_RECORDING,
    UI_TALK_THINKING,
    UI_TALK_FAILED
} ui_talk_t;

typedef enum { UI_PEND_NONE = 0, UI_PEND_PLAY, UI_PEND_REPLAY, UI_PEND_REPLY } ui_pending_t;

typedef struct {
    ui_talk_t talk;
    uint32_t talk_since;
    /* WHEN the finger landed, not HOW MANY passes ago. The first cut counted `+= TOUCH_POLL_MS`
       per iteration, which silently assumes the render loop runs every 40 ms — it does not. The
       delay is 40 ms and then the frame's work happens, so a tally of nominal ticks always lags
       the wall clock and the hold felt longer than the 700 ms it claimed. A timestamp cannot
       drift. */
    uint32_t down_since;
    /* Where the current press landed, in PANEL coordinates, sampled once at the down edge
       rather than read at the threshold: the tap coordinates outlive their press, so a finger
       already down when the loop started would otherwise inherit the last press's position.
       -1 when the touch gave no point, which the margin test rejects for free. */
    int down_x;
    int down_y;
    /* The hands-free listen: whether this turn was started by the name rather than by a finger,
       whether anyone has actually spoken yet, and when the room went quiet. */
    bool listen_voice;
    bool listen_heard;
    uint32_t listen_hush;
    /* How long this particular listen waits for someone to start: the name gives 3 s, a
       follow-up 2 s, and a hold does not use it at all. */
    uint32_t listen_lead;
    /* A reply has finished playing and has not yet been followed up, and how many turns this
       exchange has run without a deliberate start. */
    bool follow_armed;
    int follow_turns;
    /* Last frame's speaking state, so the follow-up fires on the EDGE where the speaker falls
       silent rather than on every frame after it. */
    bool was_speaking;
    /* Who the message being recorded is for, and the hush machinery for it — separate from the
       listen's so a message cannot inherit half of a conversation that was in flight. */
    ui_to_t rec_to;
    bool rec_heard;
    uint32_t rec_hush;
    /* When the again/reply pair went up (`REPEAT_MS`). Non-zero means it is on screen. */
    uint32_t repeat_until;
    /* When the CURRENT run of waiting messages began (`POPUP_BIG_MS`). */
    uint32_t popup_since;
    /* WHEN THE PLAYBACK CONTROLS CAME UP, 0 when they are not. Only the exit reads it, and only
       to refuse for `EXIT_GRACE_MS` — a destructive control must not arm itself under a finger
       that is already on the glass. Stamped on the rise and cleared on the fall, so a pause and
       resume does not re-arm the grace: it is about the finger that started this, not about the
       sound. */
    uint32_t controls_since;
    /* Non-zero while the "who?" grid is up: the moment it closes itself. */
    uint32_t sendto_until;
    ui_pending_t pending;
    uint32_t pending_until;
    /* WHO THE DEFERRED REPLY IS FOR, captured at the press rather than read when it fires:
       ending the run is what frees the speaker, and the in-from kind is about the message that
       was playing — which by then is over. */
    ui_to_t pend_reply_to;
    /* WHEN THE FINGER PUT IT ON HOLD, 0 when nothing is held. See `AUDIO_PAUSE_MAX_MS`. */
    uint32_t paused_since;
    /* Where the pop-up was drawn, in OVERLAY coordinates — the space `tap_to_overlay` hands
       back, not the space `panel_to_frame` does. -1 in the first slot means not on screen. */
    int popup_box[4];
    /* The waiting count this has already reacted to; -1 rather than 0 so whatever is already
       queued at the first poll after a boot still counts as news — which it is, to a child who
       has just turned the panel on. */
    int waiting_shown;
    bool popup_was_big;
    /* WHICH QUEUED MESSAGE THE FINGER IS POINTING AT — an index, 0 being the oldest, which is
       where every arriving message and every emptied queue puts it back. Clamped to the queue on
       every frame rather than trusted: the queue shrinks underneath it whenever something is
       played, here or on the box. */
    int sel;
    /* THIS PRESS HAS ALREADY BEEN A SWIPE. Latched for the rest of the press rather than a frame:
       a finger that travelled is not going to become a 700 ms hold by staying down, and without
       this a drag across the pet would change message AND open the "who?" menu. */
    bool swiped;
} ui_state_t;

/* ── THE FRAME'S FACTS ────────────────────────────────────────────────────────────────────
 *
 * Gathered by `display.c`, which is the only side that can ask a peripheral anything.
 */
typedef struct {
    uint32_t now; /* one clock read per frame, shared by every branch */
    /* HOW LONG THE LAST PASS ACTUALLY TOOK, MEASURED — never the nominal poll interval.
     *
     * This is a separate field rather than a constant because assuming the constant is a fault
     * this firmware has shipped twice. `gesture_poll` is fed `TOUCH_POLL_MS` (40) as its `dt`
     * while a real render pass is 80-100 ms, so every gesture threshold on the panel is
     * stretched two to two and a half times: the "five second" reboot hold is really twelve.
     * And the hold-to-talk timer counted nominal ticks per iteration before it was a timestamp,
     * for the same reason and with the same result — the hold felt longer than the 700 ms it
     * claimed.
     *
     * A delay of 40 ms and then a frame's work is not a 40 ms frame. Anything here that
     * measures time either takes a timestamp difference or takes this. */
    int dt_ms;

    /* THE TOUCH, ALREADY RESOLVED, AND RESOLVED FOR EVERY PRESS RATHER THAN SOME.
     *
     * `ox`/`oy` are OVERLAY coordinates — everything drawn before the 180 flip is hit-tested in
     * that space, and a hit test against frame coordinates on an upside-down panel misses by
     * 289 px (measured 2026-09-25). `panel_x`/`panel_y` are the glass, which is what the rim
     * margin wants: the rim is the rim whichever way up the thing is mounted.
     *
     * THIS FIELD IS WHY THE BOUNDARY IS HERE. The correction used to live INSIDE the quiet
     * dispatcher, so the short one that runs while a message plays tested the coordinates from
     * the last time the panel was silent — and on the first press of a session they were still
     * -1 and could match nothing at all ("nothing responds to it"). A struct filled once, above
     * any fork, cannot have that shape: there is one press and one answer, and both dispatchers
     * read the same field. */
    bool tapped;
    int ox, oy;
    int panel_x, panel_y;
    bool down;
    /* The overlay band for this orientation: a quarter turn carries only the square. */
    int over_y0;
    int over_h;

    /* This frame's maintenance-gesture tap count, so a hold they own is not also a menu. */
    int gest_taps;

    bool btn_short; /* a completed short press of the boot button */
    bool btn_hold;  /* the five-second hold completed */
    bool standby;   /* the screen is off and the microphone deaf, by request */

    /* SPEAKING, SAMPLED AT THE TOP OF THE FRAME, and `audio_playing` read again later. They are
       different facts: a tap LATER in the same frame is what starts a message, so on the frame
       a child presses the pop-up `speaking` still says false. Conflating them is how one
       message came back every thirty seconds forever. */
    bool speaking;
    bool audio_playing;
    bool stream_active;
    bool stream_paused;

    int waiting;           /* how many voice posts are queued on the box */
    bool waiting_from_dad; /* whether the OLDEST waiting one is from the owner */
    /* WHO EACH QUEUED MESSAGE IS FROM, OLDEST FIRST — as many as the box described (`known`),
       which is at most `UI_QUEUE_MAX` and may be fewer than `waiting`. `waiting_from_dad` above is
       `from_dad[0]` on any box new enough to send a queue at all, and is the only answer on one
       that is not. */
    int known;
    bool from_dad[UI_QUEUE_MAX];
    /* A HORIZONTAL DRAG, RESOLVED: -1 for a finger that travelled LEFT, +1 for right, 0 for no
       swipe this frame. Resolved by `display.c` because only it can see the finger move — the
       touch controller is sampled on its own task and this module never touches a peripheral.
       At most one per press, so a long drag steps once. */
    int swipe;
    ui_to_t in_from;       /* where a reply to the last PLAYED message goes */
    ui_jstate_t jstate;
    bool jrunning;
    bool jfetching;

    ui_net_t net; /* the conversation turn's state */
    bool hearing; /* the front end's VAD: is anyone talking */
    int capture_ms;
    int capture_cap_ms;
} ui_in_t;

/* ── WHAT COMES BACK ──────────────────────────────────────────────────────────────────────
 *
 * Actions, in the order they must be performed. A list rather than a set of flags because
 * ORDER MATTERS and always did: a cue sounds before the audio it announces, and a stream is
 * un-paused before its run is stopped so a finish never leaves the ring held.
 */
typedef enum {
    UI_ACT_CUE = 0,       /* arg: cue_t */
    UI_ACT_PLAY_NEXT,     /* fetch a waiting message and play it; arg: which, 0 the oldest */
    UI_ACT_REPLAY,        /* ask the box for the last one again */
    UI_ACT_STOP,          /* end a run on a finger */
    UI_ACT_PAUSE,         /* arg: 1 hold, 0 resume */
    UI_ACT_CAPTURE_OPEN,  /* open the microphone */
    UI_ACT_CAPTURE_DROP,  /* close it and throw the bytes away */
    UI_ACT_SEND_TALK,     /* close it and hand the bytes to the conversation */
    UI_ACT_SEND_JPANEL,   /* close it and post them as a message; arg: ui_to_t */
    UI_ACT_TALK_CLEAR,
    UI_ACT_JPANEL_CLEAR,
    UI_ACT_POLL_SOON,
    UI_ACT_ENTER_STANDBY, /* screen off, microphone off, still reachable */
    UI_ACT_WAKE,
    UI_ACT_BLANK,         /* paint one black frame — dark is PAINTED, never switched off */
    UI_ACT_POWER_OFF,     /* deep sleep; the button is the only way back */
} ui_action_kind_t;

/* SIZED FROM THE WORST FRAME THAT CAN BE CONSTRUCTED, not from the worst one anybody has seen.
 *
 * An ordinary busy frame is five — the standby press is cue, drop, stop, standby, blank — and
 * eight looked generous until the host suite built the pathological pass on purpose: a short
 * press AND a completed hold AND a hold-to-grid AND the paused-stream deadline AND a deferred
 * reply settling AND a send outcome AND a new message arriving is thirteen. None of those
 * combinations is likely and every one of them is reachable, and an action list that silently
 * drops its tail would drop a peripheral call — a microphone left open, a stream left paused.
 * Sixteen, with `test_the_action_list_never_overflows` standing on it, so adding a branch either
 * fits or fails loudly. */
#define UI_ACTS_MAX 16

typedef struct {
    ui_action_kind_t kind;
    int arg;
} ui_action_t;

typedef struct {
    ui_action_t act[UI_ACTS_MAX];
    int n;
    bool dirty;            /* this frame has something new to show */
    bool flinch;           /* the pet was touched: recoil, wherever the finger landed */
    bool tap_fell_through; /* nothing above claimed the press — the poke and the label may */
    bool tap_missed;       /* consumed by the tick/cross pair without hitting either */
    bool nod;              /* a reply arrived: the pet acknowledges it */
    bool rim_hold;         /* a hold that began on the rim rather than on the pet */
    const char *say;       /* a line for the caption, NULL when there is nothing to say */
} ui_out_t;

/* Which overlay is up, and with what. Filled by `ui_overlay()`, consumed by the draw block —
   `display.c` still owns every pixel. */
typedef struct {
    bool listening;
    bool recording;
    ui_to_t rec_to;
    bool confirm; /* the tick and the cross */
    bool thinking;
    bool thinking_failed;

    bool popup_big;
    bool popup_badge;
    bool popup_from_dad;

    bool run;
    /* HOW MANY ARE WAITING. Paired with `sel_shown` below, which is the half that changed. */
    int run_count;
    bool run_playing;
    bool run_from_dad;

    bool pair; /* the sender's face, again and reply */
    bool pair_playing;
    bool pair_from_dad;

    bool exit_corner; /* the way out, top right */
    bool grid;
    /* WHICH QUEUE ENTRY EVERY FACE AND NAME ON THIS FRAME IS ABOUT. `display.c` reads the NAME
       from `jpanel.c` and cannot be left to guess which entry that should be — the two would drift
       apart within a release, which is the whole reason the notice's hit box and its picture were
       collapsed into one answer. */
    int sel;
    /* THE SAME THING ONE-BASED, for a numeral a four-year-old is being taught to read. It used to
       be "how many are still to come"; now that a finger can point at one of them it says WHERE
       THAT FINGER IS — `sel_shown` of `run_count` — because a position is only legible next to its
       total. Drawn on both screens that carry a count, and only while `run_count > 1`: with one
       message there is no position to report. */
    int sel_shown;
} ui_overlay_t;

/* ── WHO CLAIMS A PRESS ───────────────────────────────────────────────────────────────────
 *
 * THE ORDER IS THE DESIGN, SO IT IS A LIST RATHER THAN A CHAIN OF `if`s.
 *
 * It used to be the chain, and the chain is how the arbitration went wrong without anybody
 * being able to see it: the exit corner is tested before the pop-up, and the two regions
 * OVERLAP — the top-right quadrant contains the right half of the centred notice. While the
 * again/reply pair never expires, that branch is live permanently after the first message the
 * panel ever plays, so the first tap on a new notice silently does nothing and the second one
 * works. Nothing in a chain of `if`s says "these two targets overlap and this one wins"; a
 * table does, and a test can walk it.
 *
 * TWO TABLES, BECAUSE THERE ARE TWO ORDERS, and WHICH ONE A PRESS USES IS THE WHOLE QUESTION.
 * While a message is sounding the second table is everything a press can reach: the menu, the
 * exit, the transport, then the pet.
 *
 * BOTH TABLES BEGIN THE SAME WAY — the modal menu, then the way out — and that is the rule, not a
 * coincidence. The grid is drawn last of all (`UI_DRAW_ORDER`) precisely because a child asked for
 * it, so it is on top of the transport and has to be offered the press first; leaving it out of
 * this table let a message starting under an open menu take presses from the menu covering it. A notice, the pair and the tick are all unreachable, which is right —
 * they are offers about what to do next, and there is a thing happening now.
 *
 * THE EXIT IS IN BOTH TABLES, and leaving it out of this one is why the owner reported that
 * pressing the corner during a message *"doesn't happen either"*. `target_hit(TRANSPORT)` is
 * unconditional — a sounding run consumes every press so the pet cannot twitch mid-sentence —
 * so the exit has to come FIRST or it can never be reached. The geometry does not collide: the
 * transport pair is the bottom half (`confirm.h`), the sender's face the top left, the exit the
 * top right. Four quadrants, each meaning one thing, which is the screen that was asked for.
 *
 * THE TEST IS A MESSAGE, NOT A SOUND, and the difference was three reported faults. It used to
 * be `speaking` (`audio_playing()`), true for the panel's own cues as well — so a press could
 * not reach a notice, the exit, the pair or the tick while ANY sound came out, including the
 * 440 ms chirp that fires on the frame the notice appears. A child reacting to the beep is
 * pressing during the beep, and the exit lives in the first table, so it could not end a
 * message while the message was audible — the one state it exists for. `stream_active` is the
 * message ring alone, so a cue no longer decides what a press means. The pet's own branch
 * still defers to `speaking`, because a colour change and a new beep mid-sentence is the thing
 * that guard was genuinely protecting.
 */
typedef enum {
    UI_TARGET_GRID = 0,  /* the "who?" menu: modal, and consumes even a miss */
    UI_TARGET_EXIT,      /* the way out, top right */
    UI_TARGET_POPUP,     /* the waiting-message notice */
    UI_TARGET_PAIR,      /* again and reply */
    UI_TARGET_CONFIRM,   /* the tick and the cross: also consumes a miss */
    UI_TARGET_TRANSPORT, /* pause and reply, while a run is sounding */
    UI_TARGET_PET,       /* whatever is left: the poke, the label, the colour */
    UI_TARGET_COUNT,
} ui_target_t;

#define UI_TAP_ORDER_LEN 6
#define UI_TAP_ORDER_PLAYING_LEN 4
extern const ui_target_t UI_TAP_ORDER[UI_TAP_ORDER_LEN];
extern const ui_target_t UI_TAP_ORDER_PLAYING[UI_TAP_ORDER_PLAYING_LEN];

/* ARE THE PLAYBACK CONTROLS ON THE GLASS. Asked by the drawing and by the arbitration, from one
 * place, because they used to ask different questions and disagree for seconds at a time: through
 * the fetch window the controls were painted and a press on the pause button went down the IDLE
 * table and poked the pet, and on a replay — which sets no run — an audibly playing message drew
 * the ended-state pair over itself. Four facts, one answer: a run in progress, a message sounding
 * however it started, a press taken and not yet served, or a fetch in flight. */
bool ui_run_controls_up(const ui_state_t *st, const ui_in_t *in);

/* WHICH TARGET A PRESS AT `in->ox,in->oy` REACHES, and nothing else — no state changed, no
   action emitted. Split out from `ui_tap` so the arbitration can be asserted on its own: the
   question "what does this tap hit" has a wrong answer on the panel today, and a test of the
   answer should not have to also drive the consequences. */
ui_target_t ui_tap_target(const ui_state_t *st, const ui_in_t *in);

/* ── THE DRAW LAYERS ──────────────────────────────────────────────────────────────────────
 *
 * Bottom to top, and stated as a list for the same reason the targets are: the pop-up covering
 * the run controls or the other way round is a decision, not an accident of where the `if`
 * happened to be written. */
typedef enum {
    UI_LAYER_TALK = 0,  /* listening, recording, the tick and cross, the thinking bubble */
    UI_LAYER_POPUP,     /* the waiting notice, big or badge */
    UI_LAYER_TRANSPORT, /* the run controls, or the again/reply pair */
    UI_LAYER_GRID,      /* the menu a child asked for, over everything the panel offered */
    UI_LAYER_COUNT,
} ui_layer_t;

extern const ui_layer_t UI_DRAW_ORDER[UI_LAYER_COUNT];

bool ui_layer_up(const ui_overlay_t *ov, ui_layer_t layer);

/* ── THE API ───────────────────────────────────────────────────────────────────────────── */

void ui_reset(ui_state_t *st);

/* EARLY IN THE FRAME: the follow-up edge, then the press. Nothing that outranks the pet in the
   arbitration order is decided anywhere else. */
void ui_tap(ui_state_t *st, const ui_in_t *in, ui_out_t *out);

/* AFTER `gesture_poll`: the button, the grid's own clock, the hold that opens it, the talk
   machine's ways out, the deferred press, and what the box said about a message. */
void ui_frame(ui_state_t *st, const ui_in_t *in, ui_out_t *out);

/* INSIDE THE DRAW GATE: which overlay to draw, and — the reason this is not merely a query —
   where the pop-up's hit rectangle is for the next frame. */
void ui_overlay(ui_state_t *st, const ui_in_t *in, ui_overlay_t *ov);

/* THE TWO WAYS INTO THE MICROPHONE, public because the recogniser reaches them too: the wake
   phrase and `tell sister` ask for exactly what the grid's icons ask for, and the refusals must
   be identical either way — one microphone, one thing at a time. Return whether it started; the
   caller owns the cue, because a press has already sounded for the finger and a phrase has
   not. */
bool ui_start_listening(ui_state_t *st, const ui_in_t *in, ui_out_t *out);
bool ui_start_record(ui_state_t *st, const ui_in_t *in, ui_out_t *out, ui_to_t to);

/* "STOP", SAID OUT LOUD. Three states to leave, because stop has to mean stop wherever it is
   said — see the VOCAB_STOP case in `display.c`. */
void ui_voice_stop(ui_state_t *st, ui_out_t *out);

/* THE ONE ANSWER THIS MODULE CANNOT WORK OUT FOR ITSELF. `UI_ACT_SEND_TALK` is performed by
   `display.c`, and a turn the network refused is a turn that never started — so the optimistic
   THINKING this sets has to be taken back. Called once, immediately after the action. */
void ui_talk_send_result(ui_state_t *st, bool ok, uint32_t now);

/* WAKING TO A MESSAGE LEFT OVERNIGHT SHOWS THE BIG BOX AGAIN — the sleep block's call. */
void ui_popup_restart(ui_state_t *st, uint32_t now);

/* What the rest of the render loop still has to ask: the emotion the face wears, whether the
   idle timer should count this frame as used, and whether the recogniser is deaf. */
ui_talk_t ui_talk(const ui_state_t *st);
/* A CONVERSATION OR A RECORDING IS RUNNING — what the idle timer should count as the panel being
   used. Deliberately NOT the again/reply pair: see the body for the night that cost. */
bool ui_busy(const ui_state_t *st);
bool ui_recording(const ui_state_t *st);
bool ui_pair_up(const ui_state_t *st);
bool ui_grid_up(const ui_state_t *st);

/* IS ANY OVERLAY A PRESS WOULD MEAN SOMETHING TO ON THE GLASS — the notice, the again/reply pair,
   the playback controls or the grid. Used to keep a HOLD from opening the grid on top of a menu:
   "on the pet" is a 224x224 square in the middle of the face, so it covers every one of them, and
   without this a long press on a menu item did the item AND opened a second menu over it. */
bool ui_menu_up(const ui_state_t *st, const ui_in_t *in);
