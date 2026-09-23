// Desk stand from the side, cut down the middle: the angle, the heights and the foot.
include <../../amoled18_back_plate.scad>
use <annot.scad>
show_part = false;
$fn = 64; $k = 0.9;
v = "front"; y = -30; t = 2.4;
red = "DarkRed";
module half() intersection() { children(); translate([-100, 0, -1]) cube([200, 100, 200]); }
color("SteelBlue") render() half() stand_box();
color("LightSteelBlue") render() half() multmatrix(m_head) back_plate();
color("DimGray", 0.5) render() half() multmatrix(m_head) translate([0, 0, body_h]) rbox(plate_x, plate_y, 11.5, plate_r);
color("LimeGreen") render() half() translate([cell_x0, -fy/2, box_floor + tape_t]) cube([fx, cell_y, cell_h]);

function w(x, z) = [x * sc - z * ss, y, stand_zh + x * ss + z * sc];
xf = -plate_x / 2 * sc - body_h * ss;       // front of the band
xb = p_out_x * sc;                          // back wall
// Heights of the box.
dim([xf - 6, y, 0], [xf - 6, y, band_top_front], "", view = v);
label([xf - 7, y, band_top_front / 2], str(round(band_top_front), " mm"), v, t, "right");
seg([xf - 7, y, band_top_front], w(-plate_x / 2, body_h), r = 0.06);
dim([xb + stand_tail + 6, y, 0], [xb + stand_tail + 6, y, band_top_back], "", view = v);
label([xb + stand_tail + 7, y, band_top_back / 2], str(round(band_top_back), " mm"), v, t, "left");
seg([xb, y, band_top_back], [xb + stand_tail + 7, y, band_top_back], r = 0.06);
// The foot.
dim([xb, y, -4], [xb + stand_tail, y, -4], str("stand_tail ", stand_tail), [0, 0, -2.5], v, t);
// Floor.
callout([10, y, box_floor / 2], [8, y, -9], str("box_floor ", box_floor), v, t, "right");
// Tilt: a horizontal line and the head's back.
seg(w(-plate_x / 2, 0) + [0, 0, 0], w(-plate_x / 2, 0) + [44, 0, 0], r = 0.06, c = red);
label(w(-plate_x / 2, 0) + [30, 0, 3.4], str("stand_angle ", stand_angle, "°"), v, t, c = red);
// Which edge is which.
callout(w(plate_x / 2, body_h + 5), w(plate_x / 2, body_h + 5) + [8, 0, 10], "USB-C + buttons", v, t, "left");
label(w(-plate_x / 2, body_h + 11.5) + [-4, 0, 6], "screen faces you ◀", v, t, "right");
callout([cell_x0 + fx / 2, y, box_floor + 20], [xf - 16, y, 35], "battery, against", v, t, "right");
label([xf - 17.2, y, 32], "the back wall", v, t, "right");
