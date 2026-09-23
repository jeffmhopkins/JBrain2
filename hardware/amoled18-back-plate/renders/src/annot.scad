// Dimension lines and labels drawn as geometry, so OpenSCAD renders them
// straight into the figure. `view` is the direction the camera looks from:
// "top" (+z), "front" (-y), "back" (+y) or "right" (+x); text is laid flat to face it.

annot_font = "DejaVu Sans:style=Bold";
// Set $k in a figure to scale lines, arrows and dots with the view (1 = whole part).
function k() = is_undef($k) ? 1 : $k;
ink = "Black";

module face(view) {
    if (view == "top") children();
    else if (view == "front") rotate([90, 0, 0]) children();
    else if (view == "back") rotate([90, 0, 180]) children();
    else if (view == "right") rotate([90, 0, 90]) children();
}

module label(p, s, view = "top", size = 2.2, halign = "center", c = ink) {
    color(c) translate(p) face(view)
        linear_extrude(height = 0.05)
            text(s, size = size, font = annot_font, halign = halign, valign = "center");
}

module seg(a, b, r = 0.12, c = ink) {
    r = r * k();
    color(c) hull() { translate(a) sphere(r = r, $fn = 8); translate(b) sphere(r = r, $fn = 8); }
}

module arrowhead(tip, from, r = 0.5, len = 1.4, c = ink) {
    r = r * k();
    len = len * k();
    d = tip - from;
    u = d / norm(d);
    color(c) hull() {
        translate(tip) sphere(r = 0.05, $fn = 6);
        translate(tip - u * len) sphere(r = r, $fn = 8);
    }
}

// A dimension from a to b with arrows at both ends and a label at the middle
// shifted by `off`.
module dim(a, b, s, off = [0, 0, 0], view = "top", size = 2.2, c = ink) {
    seg(a, b, c = c);
    arrowhead(a, b, c = c);
    arrowhead(b, a, c = c);
    label((a + b) / 2 + off, s, view, size, c = c);
}

// A leader line from a point on the part to a label.
module callout(p, at, s, view = "top", size = 2.0, halign = "left", c = ink) {
    seg(p, at, c = c);
    color(c) translate(p) sphere(r = 0.45 * k(), $fn = 12);
    // Nudge the text off the end of the leader, along the view's horizontal.
    h = view == "right" ? [0, 1, 0] : view == "back" ? [-1, 0, 0] : [1, 0, 0];
    label(at + (halign == "left" ? h : halign == "right" ? -h : [0, 0, 0]) * 1.2 * k(),
          s, view, size, halign, c);
}
