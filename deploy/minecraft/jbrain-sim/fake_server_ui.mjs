// A stand-in for @minecraft/server-ui, enough for the jbrain pack's transport form (plan
// §M10), the Replicator's panel (§M15) and Dave's quick menu (§M7). The supervisor's test_minecraft_jbrain_pack.py installs it as
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

// The Replicator's panel (§M15): a title, a body and buttons, answered like the others.
export class ActionFormData {
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
  button(t) {
    this.form.buttons.push(t);
    return this;
  }
  show(player) {
    const shown = { player, ...this.form };
    forms.shown.push(shown);
    return Promise.resolve().then(() => forms.answer(player, shown));
  }
}

// The quick menu's text boxes (§M7): a title and text fields, answered with formValues.
export class ModalFormData {
  constructor() {
    this.form = { title: "", fields: [] };
  }
  title(t) {
    this.form.title = t;
    return this;
  }
  textField(label, placeholder) {
    this.form.fields.push({ label, placeholder });
    return this;
  }
  show(player) {
    const shown = { player, ...this.form };
    forms.shown.push(shown);
    return Promise.resolve().then(() => forms.answer(player, shown));
  }
}
