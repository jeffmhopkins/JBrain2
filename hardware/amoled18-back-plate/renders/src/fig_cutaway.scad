// The default plate cut through two screw towers (x = 12), seen from the cut side.
use <../../amoled18_back_plate.scad>
$fn = 64;
color("SteelBlue") render() difference() {
    back_plate();
    translate([12, -60, -1]) cube([60, 120, 120]);
}
