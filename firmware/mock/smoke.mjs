// Drives the built mock headless through the messaging loop the page is for, and fails if the
// firmware's own machine ends anywhere it should not. `node smoke.mjs [sim.wasm]`.
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const mocks = resolve(here, '../../docs/mocks/jpanel-s3');
const { createPanel, VOICE, KNOB } = await import(resolve(mocks, 'panel.js'));
const wasm = readFileSync(process.argv[2] || resolve(mocks, 'sim.wasm'));

const events = [];
const p = await createPanel(wasm, { onEvent: (e) => events.push(e) });
let failures = 0;
const check = (ok, what) => {
  if (!ok) {
    failures++;
    console.error(`FAIL: ${what}\n  state: ${JSON.stringify(p.state())}`);
  }
};
const tap = (x, y) => {
  p.touch(0, x, y);
  p.advance(80);
  p.touch(2, x, y);
  p.advance(80);
};
const until = (ok, ms) => {
  for (let t = 0; t < ms && !ok(); t += 40) p.advance(40);
  return ok();
};
const W = p.W;
const H = p.H;

p.advance(2000);
check(p.state().mode === 'PET', 'it boots to the pet');

// A message from Dad: the big notice, then it plays, then the again/reply pair.
p.message(true, 3000);
check(until(() => p.state().notice_big, 5000), 'a waiting message shows the big notice');
tap(W / 2, H / 2);
check(until(() => p.state().mode === 'PLAY', 2000), 'the notice press plays it');
check(until(() => p.state().out_ms > 0, 6000), 'and it becomes audible');
check(until(() => p.state().mode === 'PET' && p.state().offer, 10000), 'it ends on AGAIN/REPLY');
check(until(() => p.state().queue.length === 0, 2000), 'the box was told it was heard');

// REPLY records to Dad; the hush sends it; the box confirms.
const before = events.length;
const cy = H - 90; // the pair's row (confirm_cy)
tap(W * 3 / 4, cy);
check(until(() => p.state().mode === 'RECORD', 2000), 'REPLY opens a recording');
p.hearing(true);
p.advance(2500);
p.hearing(false);
check(until(() => p.state().sending, 6000), 'the hush sends it');
check(until(() => events.slice(before).some((e) => e.includes('cue SENT')), 20000), 'and SENT sounds');

// The menu: hold on the pet, pick the sister, nobody there. 409 is what the box answers when there
// is no single other panel (`jpanel.py`), and the only status the firmware reads as NOBODY.
p.set(4, 409);
p.touch(0, W / 2, H / 2);
p.advance(900);
p.touch(2, W / 2, H / 2);
p.advance(800);
check(p.state().mode === 'GRID', 'a hold on the pet opens who?');
const sent = events.length;
tap(W / 4, H / 4); // the sister's cell
check(until(() => p.state().mode === 'RECORD', 2000), 'the sister cell opens a recording');
check(until(() => events.slice(sent).some((e) => / say nobody to send to/.test(e)), 20000),
  'and a 409 comes back as NOBODY, not as a failed send');

// The tilt slider: he leans and rights his head, and nothing else moves.
p.set(KNOB.TILT_DEG, 25);
p.advance(1000);
p.set(KNOB.TILT_DEG, -25);
p.advance(1000);
p.set(KNOB.TILT_DEG, 0);

// Shaken, then told it is loved: each starts its action and sounds its cue.
const pets = events.length;
p.set(KNOB.PET_ACT, 1);
p.advance(6600);
p.set(KNOB.PET_ACT, 2);
p.advance(5800);
check(events.slice(pets).some((e) => e.includes('cue DIZZY')), 'a shake makes him dizzy, and says so');
check(events.slice(pets).some((e) => e.includes('cue LOVE')), '"I love you" makes him coo');
// "I'm going to tickle you": he braces; then pokes are tickles, and the fifth gets him.
const tickled = events.length;
p.set(KNOB.PET_ACT, 3);
p.advance(1800);
check(events.slice(tickled).some((e) => e.includes('cue BRACE')), 'warned, he braces with a titter');
for (let i = 0; i < 5; i++) {
  tap(W / 2, H * 0.62);
  p.advance(i < 4 ? 1300 : 500);
}
// A child tickling keeps poking: a poke mid-fit is a flinch, and the fit runs its course.
tap(W / 2, H * 0.62);
p.advance(4600);
const tk = events.slice(tickled);
check(tk.filter((e) => e.includes('cue TICKLED')).length === 4,
  'four tickles, a held-in "hehehe" each, not the poke giggle');
check(!tk.some((e) => e.includes('cue GIGGLE')), 'nothing about a tickle sounds like a poke');
check(tk.filter((e) => e.includes('cue TICKLE (')).length === 3,
  'and the fifth sets him laughing, and laughing again, and again');
// Laughed out, he farts, and the fart leaves him sitting dazed (owner: "if tickled 5 times,
// he should fart and go dazed").
p.advance(1500 + 6400 + 300);
const out = events.slice(tickled);
const fit = out.findLastIndex((e) => e.includes('cue TICKLE ('));
const fart = out.findIndex((e) => e.includes('cue FART'));
const dazed = out.findIndex((e) => e.includes('cue DIZZY'));
check(fart > fit && dazed > fart, `then a fart, then dazed (${fit}, ${fart}, ${dazed})`);
const later = events.length;
tap(W / 2, H * 0.62);
p.advance(1500);
check(!events.slice(later).some((e) => e.includes('tickled')), 'after which a poke is a poke');
const fed = events.length;
p.set(KNOB.PET_ACT, 4);
p.advance(3200);
check(events.slice(fed).filter((e) => e.includes('cue EAT')).length === 3,
  'a snack is three bites, a crunch each');
const snacks = () => events.slice(fed).filter((e) => e.includes('eats a ')).map((e) => e.split('eats a ')[1]);
// Two more snacks inside the minute: he has to go, and it plops out after the third.
p.set(KNOB.PET_ACT, 4);
p.advance(3200);
p.set(KNOB.PET_ACT, 4);
p.advance(3200);
p.advance(3000);
const after = events.slice(fed);
check(after.some((e) => e.includes('three meals in a minute')), 'three snacks in a minute owe one');
const ate = snacks();
check(ate.length === 3 && ate[0] !== ate[1] && ate[1] !== ate[2], 'a different snack each time');
check(after.some((e) => e.includes('plop')) && after.some((e) => e.includes('cue POOP')),
  'and he poops, with a plop');

// The one-in-ten fart: seven seconds of it, a new piece the moment the last one ends.
const farted = events.length;
p.set(KNOB.PET_ACT, 6);
p.advance(7300);
const pieces = events.slice(farted).filter((e) => e.includes('cue MEGAFART'));
check(pieces.length >= 8, `seven seconds of fart, back to back (${pieces.length} pieces)`);
const stamps = pieces.map((e) => e.split(' ')[0]);
check(new Set(stamps).size === stamps.length, `one piece at a time, never two in a frame (${stamps})`);

// "Happy birthday": the first line of the song in three phrases, in order, one at a time.
const sang = events.length;
p.set(KNOB.PET_ACT, 7);
p.advance(7300);
const song = events.slice(sang).filter((e) => e.includes('cue BDAY_')).map((e) => e.split('cue ')[1].split(' ')[0]);
check(song.join(',') === 'BDAY_1,BDAY_2,BDAY_3', `the song's three phrases, in order (${song})`);

// "Let's play a game": he walks in shrinking, "go", things fly; held level he is soon hit; a
// tap plays again; and the wake word ends it, walking him back to his own size.
const played = events.length;
p.set(KNOB.PET_ACT, 8);
check(until(() => events.slice(played).some((e) => e.includes('cue GAME_GO')), 4000), 'ready, then go');
check(until(() => events.slice(played).some((e) => e.includes('game hit')), 30000), 'standing still, something gets him');
const hitAt = events.length;
p.advance(800);
tap(W / 2, H * 0.8);
check(until(() => events.slice(hitAt).some((e) => e.includes('cue GAME_GO')), 4000), 'a tap plays again');
// Tipped hard one way he runs into the wall and stays there: the round still plays.
p.set(KNOB.TILT_DEG, 30);
p.advance(3000);
p.set(KNOB.TILT_DEG, 0);
const g = events.slice(played);
check(!g.some((e) => e.includes('flinch') || e.includes('tickled')), 'a tap mid-game is the game\'s, not a poke');
// The name is refused as busy over any sound, the hit's included: say it into a quiet moment.
p.advance(1500);
p.voice(VOICE.WAKE);
check(until(() => events.slice(played).some((e) => e.includes('back to his own size')), 4000),
  'the wake word ends the game, and he walks back');

// The wake phrase while idle opens a turn with the pet.
p.voice(VOICE.WAKE);
p.advance(200);

const bugs = events.filter((e) => / bug /.test(e));
check(bugs.length === 0, `no invariant broke: ${bugs.join('; ')}`);
if (process.env.SMOKE_LOG) console.log(events.join('\n'));
if (failures) {
  console.error(`smoke: ${failures} failed`);
  process.exit(1);
}
console.log(`smoke: OK (${events.length} events, ${Math.round(p.now / 1000)} s of panel time)`);
