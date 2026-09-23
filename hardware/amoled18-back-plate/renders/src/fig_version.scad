// A version cut open along x = 0, with its tape (red), battery (green) and
// foam (yellow). Render with the preset: -p ../amoled18_back_plate.json -P "<name>".
include <../../amoled18_back_plate.scad>
show_part = false;
color("SteelBlue") render() difference() {
    back_plate();
    translate([-60, -60, -1]) cube([60, 120, 120]);
}
translate([-fx/2, y_lo, plate_t]) {
    color("Crimson") cube([fx, cell_y, tape_t]);
    color("LimeGreen") translate([0, 0, tape_t]) cube([fx, cell_y, cell_h]);
    color("Gold") translate([0, 0, tape_t + cell_h]) cube([fx, cell_y, foam_t]);
}
