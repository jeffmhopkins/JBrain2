"""The ostrich form, at true panel geometry — the spec the C port transcribes.

THIS IS OUR BIRD, not M.E.R.C. One of the twins asked for an ostrich after a Disney robot of
that name; the design here is our own, in the same visual language as the robot in
`pet-face.html`, and no reference artwork is copied or vendored.

WHY A SCRIPT AND NOT A DRAWING. Every helper below mirrors a primitive that
`firmware/main/face.c` already has — `fill_round_rect` as a rect plus four quarter-discs,
`draw_limb` as a run of circles with a ball on the end, `arc_stroke` as circles along a path,
and the eye with its upper/lower lid geometry. Nothing here needs a primitive the firmware
lacks, which is what makes the port a transcription rather than a redesign. Change this file
first, look at it, then move the numbers across.

ONE HUE, SHADED, like the robot: the tap-to-recolour cycle has to work on this form too, so
every part is `shade(hex, f)` of a single palette entry and never a second colour.

THE EYES ARE THE ROBOT'S EYES, UNCHANGED. Six emotions already work as lid geometry
(`firmware/main/emotion.c`); a form that moved or redrew the eyes would have to re-implement
all six. Keeping them at the same scale, in a head wide enough to hold them, is the single
most important constraint in this layout.

Two sign traps, both of which cost a render each while making this:
  * `draw_limb` takes 0 as straight DOWN and POSITIVE as swinging LEFT (ex = -sin). Negative
    angles sent the tail plumes right, behind the body, where they were invisible.
  * the legs must be long. A short-legged version reads as a duck; the leg length IS the
    ostrich silhouette.

    python3 docs/mocks/room-endpoint/ostrich-mock.py   # writes ostrich-mock.png
"""
import math
from PIL import Image

W, H = 368, 448


class FB:
    def __init__(self):
        self.im = Image.new("RGB", (W, H), (0, 0, 0))
        self.px = self.im.load()

    def point(self, x, y, c):
        if 0 <= x < W and 0 <= y < H:
            self.px[int(x), int(y)] = c

    def rect(self, x, y, w, h, c):
        for j in range(int(y), int(y + h)):
            for i in range(int(x), int(x + w)):
                self.point(i, j, c)

    def circle(self, cx, cy, r, c):
        r = int(r)
        for j in range(-r, r + 1):
            for i in range(-r, r + 1):
                if i * i + j * j <= r * r:
                    self.point(cx + i, cy + j, c)

    def round_rect(self, x, y, w, h, r, c):
        r = int(min(r, w // 2, h // 2))
        self.rect(x + r, y, w - 2 * r, h, c)
        self.rect(x, y + r, r, h - 2 * r, c)
        self.rect(x + w - r, y + r, r, h - 2 * r, c)
        for cx, cy in ((x + r, y + r), (x + w - r - 1, y + r),
                       (x + r, y + h - r - 1), (x + w - r - 1, y + h - r - 1)):
            self.circle(int(cx), int(cy), r, c)

    def arc(self, cx, cy, r, a0, a1, width, c, steps=64):
        for i in range(steps + 1):
            a = a0 + (a1 - a0) * (i / steps)
            self.circle(int(cx + math.cos(a) * r), int(cy + math.sin(a) * r), width // 2, c)

    def limb(self, x, y, deg, ln, w, c):
        """face.c's draw_limb: a rotated capsule with a ball on the end."""
        a = math.radians(deg)
        ex, ey = -math.sin(a), math.cos(a)
        for i in range(int(ln) + 1):
            self.circle(int(x + ex * i), int(y + ey * i), w // 2, c)
        self.circle(int(x + ex * ln), int(y + ey * ln), int(w * 0.62), c)


def shade(hexv, f):
    r = min(255, int(((hexv >> 16) & 0xFF) * f))
    g = min(255, int(((hexv >> 8) & 0xFF) * f))
    b = min(255, int((hexv & 0xFF) * f))
    return (r, g, b)


def eye(fb, cx, cy, dark, scale=1.0, open_=1.0, lower=0.0, bend=0.0):
    """face.c's draw_eye, with the lower-lid cheek raise that makes 'happy' read."""
    w = int(46 * scale)
    h = int(54 * scale * open_)
    if h < 5:
        fb.round_rect(cx - w // 2, cy - 2, w, 5, 2, dark)
        return
    white = (0xF6, 0xF9, 0xFC)
    fb.round_rect(cx - w // 2, cy - h // 2, w, h, int(min(w, h) * 0.38), white)
    pr = int(15 * scale)
    fb.circle(cx, cy, pr, dark)
    fb.circle(cx - int(5 * scale), cy - int(6 * scale), int(4.5 * scale), (255, 255, 255))
    if lower > 0.01:                       # the Duchenne raise, as a quadratic
        hw, hh = w / 2, h / 2
        ly = hh - h * lower
        for j in range(int(-hh), int(hh) + 1):
            for i in range(int(-hw), int(hw) + 1):
                t = (i / hw + 1) * 0.5
                if j >= ly - 2 * t * (1 - t) * bend * h:
                    if i * i / (hw * hw) + j * j / (hh * hh) <= 1.15:
                        fb.point(cx + i, cy + j, (0, 0, 0))


def draw_ostrich(fb, hexv, open_=1.0, lower=0.40, bend=0.40, lean=0):
    col   = shade(hexv, 1.00)   # crest, tail — the brightest accents
    head  = shade(hexv, 0.94)
    body  = shade(hexv, 0.86)
    neck  = shade(hexv, 0.76)
    leg   = shade(hexv, 0.70)
    beak  = shade(hexv, 0.52)
    wing  = shade(hexv, 0.64)
    dark  = shade(hexv, 0.22)
    ox = W // 2 + lean

    # Tail plumes FIRST and clear of the body, or they vanish behind it — which is exactly
    # what happened on the first pass.
    for deg, ln in ((108, 76), (126, 88), (144, 72)):
        fb.limb(ox - 56, 250, deg, ln, 16, col)

    # Legs: LONG. This is the whole silhouette — a short-legged version reads as a duck.
    for side in (-1, 1):
        hx = ox + side * 30
        fb.limb(hx, 318, side * 3, 92, 20, leg)
        ex = hx - math.sin(math.radians(side * 3)) * 92
        ey = 318 + math.cos(math.radians(side * 3)) * 92
        for tdeg in (-62, 0, 62):          # three toes: what makes it a bird, not a stand
            fb.limb(int(ex), int(ey), tdeg, 19, 11, leg)

    # Body: an EGG, wider than tall and tipped forward, not a ball.
    fb.round_rect(ox - 72, 222, 144, 112, 54, body)

    # Wing: a panel with scalloped line-work, kept inside the body outline.
    fb.round_rect(ox + 2, 244, 74, 66, 30, wing)
    for r in (20, 34, 48):
        fb.arc(ox + 4, 252, r, math.pi * 0.06, math.pi * 0.44, 3, col)

    # Neck: segments narrowing toward the head, leaning forward for life.
    for i in range(7):
        t = i / 6
        seg_w = int(50 - 16 * t)
        y = 218 - i * 14
        fb.round_rect(ox - seg_w // 2 + int(7 * t), y, seg_w, 11, 5, neck)

    hx = ox + 7

    # Crest: three thin plumes, the signature of the silhouette.
    for deg, ln in ((-18, 42), (0, 50), (18, 42)):
        fb.limb(hx, 62, deg + 180, ln, 9, col)

    # Head.
    fb.round_rect(hx - 64, 58, 128, 98, 44, head)

    # Beak: a wedge that PROTRUDES below the head, or it reads as a chin. Darkest shade on
    # the figure so it separates without needing a second hue.
    for w, h, y in ((42, 13, 132), (33, 12, 143), (23, 11, 153), (13, 10, 162)):
        fb.round_rect(hx - w // 2, y, w, h, 5, beak)
    fb.rect(hx - 16, 147, 32, 2, shade(hexv, 0.28))   # the mandible line

    # The eyes, unchanged from the robot's — six emotions already work as lid geometry
    # and must not be re-implemented per form.
    eye(fb, hx - 31, 100, dark, 0.98, open_, lower, bend)
    eye(fb, hx + 31, 100, dark, 0.98, open_, lower, bend)


PALETTE = [0x7FA7C9, 0x3BF0FF, 0xFFD23F, 0x49F08A]
out = Image.new("RGB", (W * 4 + 30, H), (18, 18, 20))
for n, hexv in enumerate(PALETTE):
    fb = FB()
    draw_ostrich(fb, hexv, open_=1.0 if n != 3 else 0.25,
                 lower=0.40 if n != 1 else 0.14, bend=0.40 if n != 1 else 0.10)
    out.paste(fb.im, (n * (W + 10), 0))
out.save("ostrich-mock.png")
print("wrote ostrich-mock.png", out.size)
