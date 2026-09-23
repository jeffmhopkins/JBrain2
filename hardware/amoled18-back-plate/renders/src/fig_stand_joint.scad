// Where the head sits in the box: a side wall cut at the lock screw, drawn
// square to the head (the whole stand tilted back level).
include <../../amoled18_back_plate.scad>
use <annot.scad>
show_part = false;
$fn = 64; $k = 0.22;
lx = lock_xs[len(lock_xs) - 1];   // cut beside this lock screw
v = "right"; x = lx + 2.6; t = 0.45;
m_inv = [[sc, 0, ss, -ss * stand_zh], [0, 1, 0, 0], [-ss, 0, sc, -sc * stand_zh], [0, 0, 0, 1]];
module slice() intersection() { children(); translate([lx + 1.6, 15, -6]) cube([0.4, 10, 13]); }
color("SteelBlue") render() slice() multmatrix(m_inv) stand_box();
color("LightSteelBlue") render() slice() back_plate();
// Front shell (estimated), sitting on the box's top band.
color("LightGray") translate([lx + 1.7, plate_y / 2 - 1.3, body_h]) cube([0.3, 1.3, 3.5]);
label([x, 23.9, 5.8], "front shell (ghost)", v, t, "left", c = "DimGray");
// The lock screw.
// The lock screw lies just beside this cut.
color("Goldenrod", 0.45) translate([lx, plate_y / 2 + 1.6, lock_z]) rotate([90, 0, 0]) cylinder(d = 2, h = 7.6);
color("Goldenrod", 0.45) translate([lx, plate_y / 2 + 1.6, lock_z]) rotate([-90, 0, 0]) cylinder(d = 3.8, h = 1.2);
callout([x, plate_y / 2 + 2.3, lock_z - 1.6], [x, 23.9, -2.6], "M2 × 6 lock screw", v, t, "left");

hy = head_y / 2;
dim([x, hy + pocket_clear, body_h + 0.5], [x, plate_y / 2, body_h + 0.5], "", view = v);
label([x, 22.1, body_h + 1.2], str("pocket_wall ", pocket_wall), v, t);
callout([x, hy + pocket_clear / 2, 2.6], [x, 23.9, 3.3], str("pocket_clear ", pocket_clear, " each side"), v, t, "left");
dim([x, bhy, -0.35], [x, hy, -0.35], "", view = v);
label([x, 19.6, -1.0], str("stand_ledge ", stand_ledge), v, t, "right");
callout([x, 18.0, 2.6], [x, 17.2, 5.2], "lock block", v, t, "right");
label([x, 17.6, -3.5], "battery room", v, t, c = "DimGray");
callout([x, hy - 0.4, 0.3], [x, 23.9, 0.6], "head's back on the ledge", v, t, "left");
