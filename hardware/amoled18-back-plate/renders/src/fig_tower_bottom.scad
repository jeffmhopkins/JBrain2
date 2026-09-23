// Detail: foot of a screw tower (section at x = 12, seen from +x), with a plug fitted.
use <../../amoled18_back_plate.scad>
use <annot.scad>
$fn = 64; $k = 0.25;
v = "right"; x = 16; s = 0.5;
module cut() intersection() { children(); translate([11.6, 11, -0.1]) cube([0.4, 13, 7.5]); }
color("SteelBlue") render() cut() back_plate();
color("Orange") render() cut() translate([12, 18, 0]) plug();
callout([x, 18.0, 0.4], [x, 21.4, -0.4], "plug cap, flush in a Ø5.6 × 0.8 recess", v, s, "left", c = "DarkOrange");
callout([x, 18.9, 3.0], [x, 21.4, 2.4], "ribbed shank grips the bore", v, s, "left", c = "DarkOrange");
callout([x, 17.2, 6.0], [x, 21.4, 6.2], "bore Ø4.6 continues up", v, s, "left");
callout([x, 13.6, 2.6], [x, 12.3, 5.0], "tower_flare 2 (45°)", v, s, "right");
