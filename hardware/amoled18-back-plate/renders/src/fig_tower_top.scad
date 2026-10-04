// Detail: top of a screw tower (section at x = 12, seen from +x), with the
// screw, the board's brass standoff and the board shown in section too.
include <../../amoled18_back_plate.scad>
use <annot.scad>
show_part = false;
$fn = 64; $k = 0.25;
v = "right"; x = 16; s = 0.5;
so_h = 4;   // the standoff's height above the board (illustrative)
module cut() intersection() { children(); translate([11.6, 11, 0]) cube([0.4, 13, 60]); }
color("SteelBlue") render() cut() intersection() { back_plate(); translate([-30, 0, tower_top - 5]) cube([60, 40, 10]); }
// The front shell's wall (estimated): its edge sits on the seam, level with the tower top.
color("LightGray", 0.6) translate([11.6, plate_y / 2 - 1.3, body_h]) cube([0.4, 1.3, so_h + 2.5]);
color("YellowGreen") render() cut() translate([-5, 11, tower_top + so_h]) cube([20, 13, 1.2]);
color("Gold") render() cut() translate([12, 18, tower_top]) cylinder(d = 3.6, h = so_h, $fn = 6);
color("Silver") render() cut() union() {
    translate([12, 18, bore_top - shaft_table[2]]) cylinder(d = 3.8, h = shaft_table[2]);
    translate([12, 18, bore_top]) cylinder(d = 2.0, h = 4.0);
}
label([x, 12.8, tower_top + so_h + 0.6], "board", v, s, "left", c = "DarkGreen");
callout([x, 19.2, tower_top + so_h / 2], [x, 21.4, tower_top + so_h - 0.6], "brass standoff on the board", v, s, "left", c = "DarkGoldenrod");
callout([x, 16.4, bore_top - 1], [x, 13.8, bore_top - 1], "screw head", v, s, "right", c = "DimGray");
dim([x, 14.2, bore_top], [x, 14.2, tower_top], "", view = v);
label([x, 13.9, (bore_top + tower_top) / 2], str("head_seat ", head_seat), v, s, "right");
callout([x, 19.3, bore_top + 0.2], [x, 21.4, bore_top - 1.2], str("bridge_skin ", bridge_skin, ": drill through"), v, s, "left");
callout([x, 19.8, tower_top], [x, 23.6, tower_top - 0.5], "tower top presses the standoff", v, s, "left");
callout([x, 20.75, tower_top + 1.2], [x, 23.6, tower_top + 0.9], "rim, inside the front shell", v, s * 0.85, "left");
callout([x, 22.0, tower_top + 2.4], [x, 23.6, tower_top + 2.2], "front shell's wall (ghost)", v, s * 0.85, "left", c = "DimGray");
callout([x, 16.9, bore_top - 3], [x, 13.8, bore_top - 3], "bore Ø4.6", v, s, "right");
label([x, 17.6, tower_top + so_h + 2.6], str("tower_drop ", tower_drop, ": tower top level with the seam,"), v, s, c = "DarkRed");
label([x, 17.6, tower_top + so_h + 1.9], "where the front shell's edge and the standoffs end", v, s, c = "DarkRed");
