// Pieces for the photoreal shots (make_photoreal.sh), each exported as its own STL.
// The printed parts are exported in their PRINT frames so Blender's layer lines run the way
// the printer lays them; the markers carry each part's pose into the scene.
include <../../amoled18_back_plate.scad>
show_part = false;
which = "plate";

// A dot at the origin and dots 1, 2 and 3 mm along x, y and z; marker.py reads the pose back.
module marker() for (p = [[0, 0, 0], [1, 0, 0], [0, 2, 0], [0, 0, 3]]) translate(p) cube(0.01, center = true);

// The stock front shell, from the seam up. It is not in Waveshare's STEP (that is the board and
// the panel only), so this is drawn from the case outline and the photos: the board hangs on
// standoffs that end at the seam, its back so_h above it, so the glass lands 0.15 under the
// shell's face. The openings sit where the STEP has the USB-C and the two buttons.
front_h = 11.5;
so_h = 4;
step_dz = so_h + 7.4;   // STEP z (board back at -7.4) -> height above the seam
module front_shell() difference() {
    rbox(plate_x, plate_y, front_h, plate_r);
    translate([0, 0, -1]) rbox(plate_x - 2.4, plate_y - 2.4, front_h - 0.5 + 1, plate_r - 1.2);
    hull() {
        translate([0, 0, front_h - 0.6]) linear_extrude(height = 0.01) rrect(29.9, 37.7, 3.2);
        translate([0, 0, front_h]) linear_extrude(height = 0.01) rrect(30.9, 38.7, 3.7);
    }
    translate([0, 0, front_h - 2]) linear_extrude(height = 2) rrect(29.9, 37.7, 3.2);
    translate([plate_x / 2 - 3, 0, step_dz - 6.15]) rotate([0, 90, 0])
        linear_extrude(height = 5) hull() for (dy = [-3.3, 3.3]) translate([0, dy]) circle(d = 3.4);
}
module shell_buttons() for (sy = [-1, 1])
    translate([plate_x / 2 - 0.3, sy * 10.5, step_dz - 5.5]) rotate([0, 90, 0])
        linear_extrude(height = 0.9) hull() for (dy = [-1.6, 1.6]) translate([0, dy]) circle(d = 1.9);

// The lock screws: flush countersunk M2 x 8 with a cross recess, in the box's print frame.
module lock_screws() multmatrix(m_head) for (p = lock_pts)
    translate(p[3] != 0 ? [p[0], p[3] * plate_y / 2, lock_z] : [p[2] * plate_x / 2, p[1], lock_z])
        inward([p[2], p[3]]) difference() {
            union() {
                cylinder(d1 = 3.8, d2 = 2, h = 1.0, $fn = 48);
                cylinder(d = 2, h = 8, $fn = 32);
            }
            for (a = [0, 90]) rotate(a) translate([-1.1, -0.25, -0.01]) cube([2.2, 0.5, 0.7]);
        }

if (which == "plate") back_plate();
if (which == "marker_plate") translate([0, 0, body_h]) marker();
if (which == "box") stand_box();
if (which == "marker_box") stand_pose() marker();
if (which == "marker_head") stand_pose() stand_head_pose() marker();
if (which == "marker_seam") stand_pose() stand_head_pose() translate([0, 0, body_h]) marker();
if (which == "shell") front_shell();
if (which == "buttons") shell_buttons();
if (which == "screws") lock_screws();
