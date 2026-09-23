// Detail: top of a screw tower (section at x = 12, seen from +x), with the
// screw, the board's nut and the board shown in section too.
use <../../amoled18_back_plate.scad>
use <annot.scad>
$fn = 64; $k = 0.25;
v = "right"; x = 16; s = 0.5;
module cut() intersection() { children(); translate([11.6, 11, 0]) cube([0.4, 13, 40]); }
color("SteelBlue") render() cut() intersection() { back_plate(); translate([-30, 0, 27.5]) cube([60, 40, 10]); }
color("YellowGreen") render() cut() translate([-5, 11, 36.1]) cube([20, 13, 1.2]);
color("Gold") render() cut() translate([12, 18, 34.6]) cylinder(d = 2.9, h = 1.5);
color("Silver") render() cut() union() {
    translate([12, 18, 30.6]) cylinder(d = 3.8, h = 2.0);
    translate([12, 18, 32.6]) cylinder(d = 2.0, h = 4.0);
}
label([x, 12.8, 36.7], "board", v, s, "left", c = "DarkGreen");
callout([x, 19.0, 35.4], [x, 21.4, 35.9], "brass nut on the board", v, s, "left", c = "DarkGoldenrod");
callout([x, 16.4, 31.6], [x, 13.8, 31.6], "screw head", v, s, "right", c = "DimGray");
dim([x, 14.2, 32.6], [x, 14.2, 34.6], "", view = v);
label([x, 13.9, 33.6], "head_seat 2", v, s, "right");
callout([x, 19.3, 32.8], [x, 21.4, 31.6], "bridge_skin 0.4: drill through", v, s, "left");
callout([x, 22.3, 32.6], [x, 23.4, 33.3], "front shell's wall sits here", v, s * 0.85, "left", c = "DimGray");
callout([x, 20.6, 34.6], [x, 21.4, 34.6], "flat tower top presses the nut", v, s, "left");
callout([x, 16.9, 29.0], [x, 13.8, 29.0], "bore Ø4.6", v, s, "right");
label([x, 17.6, 38.3], "tower_drop 0: tower top level with the rim top", v, s, c = "DarkRed");
