// The desk head from inside (the side that faces the board), with the board's
// BAT socket ghosted where it sits.
include <../../amoled18_back_plate.scad>
use <annot.scad>
show_part = false;
desk_stand = true;
$fn = 64; $k = 0.8;
z = 12; t = 1.9; red = "DarkRed";
color("SteelBlue") back_plate();
// BAT socket from Waveshare's 3D model; its mouth faces -y.
color("White", 0.6) translate([-15.98, -5.45, 2.0]) cube([7.65, 5.2, 3.5]);
callout([-12.15, -3, 5.5], [-30, 6, z], "board's BAT socket (ghost)", "top", t, "right");
callout([-12.15, -8.25, 0], [-30, -6, z], "slot: the plug comes up here,", "top", t, "right");
label([-31.2, -9.2, z], "then into the socket's mouth", "top", t, "right");
callout([2, cav_y / 2 - 3, body_h], [26, 28, z], "lock block", "top", t, "left");
callout([0, head_y / 2, lock_z], [26, 23, z], "lock screw hole", "top", t, "left");
callout([12 + tower_r, 18, body_h], [26, 14, z], "screw tower", "top", t, "left");
callout([head_x / 2, 0, 3], [26, 0, z], "USB-C edge (top)", "top", t, "left", c = red);
