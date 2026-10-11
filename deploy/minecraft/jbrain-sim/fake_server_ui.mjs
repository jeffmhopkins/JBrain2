// A stand-in for @minecraft/server-ui, enough for the jbrain pack's transport form (plan
// §M10). The supervisor's test_minecraft_jbrain_pack.py installs it as
// node_modules/@minecraft/server-ui. A test decides each answer with `forms.answer`.

export const FormCancelationReason = { UserBusy: "UserBusy", UserClosed: "UserClosed" };

/** Test-side controls, not part of the game API. */
export const forms = {
  shown: [], // {player, title, body, buttons}
  // (player, form) => a MessageFormResponse; by default the player closes it.
  answer: () => ({ canceled: true, cancelationReason: FormCancelationReason.UserClosed }),
  reset() {
    forms.shown.length = 0;
    forms.answer = () => ({ canceled: true, cancelationReason: FormCancelationReason.UserClosed });
  },
};

export class MessageFormData {
  constructor() {
    this.form = { title: "", body: "", buttons: [] };
  }
  title(t) {
    this.form.title = t;
    return this;
  }
  body(b) {
    this.form.body = b;
    return this;
  }
  button1(t) {
    this.form.buttons[0] = t;
    return this;
  }
  button2(t) {
    this.form.buttons[1] = t;
    return this;
  }
  show(player) {
    const shown = { player, ...this.form };
    forms.shown.push(shown);
    return Promise.resolve().then(() => forms.answer(player, shown));
  }
}
