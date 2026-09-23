// The desk stand assembled, with the display unit ghosted on the head.
// view = "assembled" | "section" | "exploded"
include <../../amoled18_back_plate.scad>
show_part = false;
part = "stand";
view = "assembled";
front_h = 11.5;   // front shell + display above the head's seam (stock unit is 15 mm)
lift = view == "exploded" ? 25 : 0;

// Keeps only the half nearer the viewer in a section; render() keeps the colour.
module cut(c, a = 1) {
    if (view == "section")
        color(c, a) render() intersection() {
            children();
            translate([-100, -100, -1]) cube([200, 100, 200]);
        }
    else color(c, a) children();
}

module display_unit() {
    cut("DimGray", view == "section" ? 0.35 : 0.9) translate([0, 0, body_h])
        rbox(plate_x, plate_y, front_h, plate_r);
    color("Black") translate([0, 0, body_h + front_h - 0.1])
        linear_extrude(height = 0.2) rrect(plate_x - 3, plate_y - 3, plate_r - 1.5);
    // USB-C and the two buttons on the top edge
    color("Black") translate([plate_x / 2 - 1, 0, body_h + 5.1]) rotate([0, 90, 0])
        linear_extrude(height = 1.2) hull() for (dy = [-3, 3]) translate([0, dy]) circle(d = 3.2);
    for (sy = [-1, 1])
        color("Gainsboro") translate([plate_x / 2 - 0.2, sy * 10.5, body_h + 5.1]) rotate([0, 90, 0])
            linear_extrude(height = 0.6) hull() for (dy = [-1.5, 1.5]) translate([0, dy]) circle(d = 2);
}

cut("SteelBlue") stand_box();
multmatrix(m_head) translate([0, 0, lift]) {
    cut("LightSteelBlue") back_plate();
    display_unit();
}
if (view != "exploded") {
    cut("Crimson") translate([cell_x0, -fy/2, box_floor]) cube([fx, cell_y, tape_t]);
    cut("LimeGreen") translate([cell_x0, -fy/2, box_floor + tape_t]) cube([fx, cell_y, cell_h]);
    cut("Gold") translate([cell_x0, -fy/2, box_floor + tape_t + cell_h]) cube([fx, cell_y, foam_t]);
}
