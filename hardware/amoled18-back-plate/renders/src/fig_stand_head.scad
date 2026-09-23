// The desk head from inside (the side that faces the board), with the board's
// BAT socket ghosted where it sits.
include <../../amoled18_back_plate.scad>
use <annot.scad>
show_part = false;
desk_stand = true;
$fn = 64; $k = 0.8;
z = 12; t = 1.9; red = "DarkRed";
color("SteelBlue") back_plate();
// The lock blocks picked out in orange, and the lock screws in gold where they
// go in from the box's band.
color("DarkOrange") render() difference() {
    translate([0, 0, 0.02]) lock_blocks();
    head_holes();
}
for (p = lock_pts)
    color("Gold") translate([p[0] + p[2] * 2.2, p[1] + p[3] * 2.2, lock_z]) inward([p[2], p[3]]) {
        cylinder(d = 2, h = 6 + 1.2);
        cylinder(d1 = 3.8, d2 = 2, h = 0.9);
    }// BAT socket from Waveshare's 3D model; its mouth faces -y.
color("White", 0.6) translate([-15.98, -5.45, 2.0]) cube([7.65, 5.2, 3.5]);
callout([-12.15, -3, 5.5], [-30, 6, z], "board's BAT socket (ghost)", "top", t, "right");
callout([-12.15, -8.25, 0], [-30, -6, z], "slot: the plug comes up here,", "top", t, "right");
label([-31.2, -9.2, z], "then into the socket's mouth", "top", t, "right");
callout([lock_xs[len(lock_xs) - 1] + 2, cav_y / 2 - 3, body_h], [26, 30, z], str(lock_count, " lock blocks (orange)"), "top", t, "left");
callout([lock_xs[len(lock_xs) - 1], head_y / 2 + 1.5, lock_z], [26, 24, z], str(lock_count, " flush lock screws, M2 × 6 (gold)"), "top", t, "left");
callout([12 + tower_r, 18, body_h], [26, 14, z], "screw tower", "top", t, "left");
callout([head_x / 2, 0, 3], [26, 0, z], "USB-C edge (top)", "top", t, "left", c = red);
