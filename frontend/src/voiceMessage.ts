/* Recording a voice message in the browser, and turning it into the ONE audio format that
 * crosses the box's boundaries: raw 16 kHz mono signed 16-bit PCM — exactly what a panel's
 * `/jpanel/send` uploads and exactly what `GET /next` hands back to a panel's speaker.
 *
 * THE CONVERSION IS HERE RATHER THAN ON THE BOX, and that is the whole design decision. A
 * `MediaRecorder` blob is webm/opus in Chrome and Firefox and mp4/aac in Safari, so decoding
 * it server-side would mean a codec dependency in the api container — for a job the browser
 * that produced the blob can already do, because it can always decode its own recording.
 * Keeping one audio format on the wire also means the owner's voice and a child's take the
 * identical path through the box: same trim, same transcription, same blob store.
 *
 * NO `OfflineAudioContext`, WHICH IS NOT A STYLE CHOICE. Rendering through one at 16 kHz is
 * the tidy way to resample, and Safari has historically refused sample rates below 44.1 kHz
 * there — Safari on a phone being exactly where the owner is. Linear interpolation over the
 * decoded samples works everywhere `decodeAudioData` does, and for speech at this rate the
 * difference is inaudible.
 */

export const PANEL_RATE = 16000;

/** What the box accepts (`MAX_MESSAGE_MS` in `jpanel.py`). The recorder stops itself here
 *  rather than letting the box truncate: a message cut mid-word with nothing said about it is
 *  the bug `audio_play` had on the panel, and it should not be reintroduced from this end. */
export const MAX_MESSAGE_MS = 20_000;

/* WHAT A RECORDING IS SCALED TO, AND WHY IT HAS TO BE SCALED AT ALL.
 *
 * The owner: *"my pwa recording seems to be way lower gain than the robot voices."* Kokoro
 * renders speech close to full scale; a browser microphone capture of someone talking normally
 * peaks far below it, often by 20 dB. The panel plays both at the same volume, so a message
 * from Dad in his own voice arrived noticeably quieter than the same words read by the box —
 * which is backwards, since the recording is the version that is supposed to carry more.
 *
 * PEAK, NOT RMS. RMS matches perceived loudness better, but it cannot promise the result will
 * not clip, and a clipped consonant on a small hard-cased speaker is worse than being a decibel
 * off. 0.89 leaves headroom for the resampler's interpolation, which can overshoot the samples
 * it sits between. */
const TARGET_PEAK = 0.89;

/* A CEILING ON THE GAIN, because normalising is not the same as turning it up.
 *
 * Without one, a recording of an empty room gets multiplied until the room hiss is at full
 * scale — a pop-up on a child's wall playing amplified nothing. 8x (~18 dB) rescues a quiet
 * phone held at arm's length and still leaves genuine near-silence quiet. */
const MAX_GAIN = 8;

/* Below this the clip is silence rather than a quiet voice, and nothing is applied at all: the
 * box's `_trim_to_speech` will reject it anyway, and amplifying it first only makes the thing
 * it rejects louder. ~-46 dBFS. */
const SILENCE_PEAK = 0.005;

/** The gain to apply so a recording lands near `TARGET_PEAK`, bounded at both ends. Exported
 *  because the bounds are the interesting part and they are worth testing on their own. */
export function normalisingGain(samples: Float32Array): number {
  let peak = 0;
  for (let i = 0; i < samples.length; i++) {
    const v = Math.abs(samples[i] ?? 0);
    if (v > peak) peak = v;
  }
  if (peak < SILENCE_PEAK) return 1;
  return Math.min(TARGET_PEAK / peak, MAX_GAIN);
}

/** Mono-mix, resample to 16 kHz, normalise, and pack to signed 16-bit little-endian. */
export function toPanelPcm(buffer: AudioBuffer): ArrayBuffer {
  const channels = buffer.numberOfChannels;
  const mono = new Float32Array(buffer.getChannelData(0));
  /* Averaged, not just channel 0: a laptop with a stereo array puts real signal in the second
     channel, and taking the first alone quietly halves the level on some hardware. */
  /* `?? 0` throughout, not `!`: `noUncheckedIndexedAccess` types every typed-array read as
     possibly undefined, and a default that is silently WRONG (a click, a doubled sample) is
     worse than one that is silently right. Zero is silence, which is the correct value for a
     read that cannot happen. */
  for (let c = 1; c < channels; c++) {
    const extra = buffer.getChannelData(c);
    for (let i = 0; i < mono.length; i++) mono[i] = (mono[i] ?? 0) + (extra[i] ?? 0);
  }
  if (channels > 1) {
    for (let i = 0; i < mono.length; i++) mono[i] = (mono[i] ?? 0) / channels;
  }

  /* Measured on the mono mix and BEFORE resampling — the same samples either way, and doing it
     here means one pass over the data rather than a second one over the output. */
  const gain = normalisingGain(mono);

  const ratio = buffer.sampleRate / PANEL_RATE;
  const outLength = Math.max(0, Math.floor(mono.length / ratio));
  const out = new Int16Array(outLength);
  for (let i = 0; i < outLength; i++) {
    const at = i * ratio;
    const lo = Math.floor(at);
    const hi = Math.min(lo + 1, mono.length - 1);
    const t = at - lo;
    const v = ((mono[lo] ?? 0) * (1 - t) + (mono[hi] ?? 0) * t) * gain;
    /* Clamped BEFORE scaling: a sample above 1.0 — which a browser's own gain can produce —
       would wrap to full negative through the Int16Array cast. That is the loudest possible
       click, and it is the same trap `cue.c` documents on the firmware side. */
    const clamped = v > 1 ? 1 : v < -1 ? -1 : v;
    out[i] = Math.round(clamped * 32767);
  }
  return out.buffer;
}

export type Recorder = {
  /** Stop, release the microphone, and hand back the panel-ready PCM. */
  stop: () => Promise<ArrayBuffer>;
  /** Give up without producing anything. The microphone is released either way. */
  cancel: () => void;
};

type AudioCtor = typeof AudioContext;

function audioContextCtor(): AudioCtor {
  const ctor =
    window.AudioContext ??
    (window as unknown as { webkitAudioContext?: AudioCtor }).webkitAudioContext;
  if (!ctor) throw new Error("this browser cannot decode audio");
  return ctor;
}

async function decodeToPcm(blob: Blob): Promise<ArrayBuffer> {
  const data = await blob.arrayBuffer();
  const ctx = new (audioContextCtor())();
  try {
    return toPanelPcm(await ctx.decodeAudioData(data));
  } finally {
    /* Closed on the failure path too: an AudioContext left open holds the audio hardware, and
       on iOS a handful of them is enough that the next one will not start. */
    void ctx.close();
  }
}

/** Ask for the microphone and start recording. Rejects if permission is refused, which the
 *  caller MUST surface — a record button that does nothing is indistinguishable from a broken
 *  one, and a browser only prompts once. */
export async function startRecording(): Promise<Recorder> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const chunks: Blob[] = [];
  const recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (e) => {
    if (e.data.size > 0) chunks.push(e.data);
  };
  recorder.start();

  const release = () => {
    for (const track of stream.getTracks()) track.stop();
  };

  return {
    cancel: () => {
      /* Guarded: `stop()` on an already-inactive recorder throws, and a cancel that throws
         would leave the microphone light on with no way back. */
      if (recorder.state !== "inactive") recorder.stop();
      release();
    },
    stop: () =>
      new Promise<ArrayBuffer>((resolve, reject) => {
        if (recorder.state === "inactive") {
          release();
          reject(new Error("recording already stopped"));
          return;
        }
        recorder.onstop = () => {
          release();
          decodeToPcm(new Blob(chunks, { type: recorder.mimeType || "audio/webm" })).then(
            resolve,
            reject,
          );
        };
        recorder.stop();
      }),
  };
}
