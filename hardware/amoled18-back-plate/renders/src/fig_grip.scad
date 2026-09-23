// A slice through the side wall at y = 0, seen from -y, showing the grip ribs.
// Render with -D grip_depth=...
include <../../amoled18_back_plate.scad>
use <annot.scad>
show_part = false;
$fn = 48;
color("SteelBlue") render() intersection() {
    back_plate();
    translate([13, 0, -0.1]) cube([8, 0.4, 20]);
}
label([17, 0, -1.6], str("grip_depth = ", grip_depth), "front", 1.1);
