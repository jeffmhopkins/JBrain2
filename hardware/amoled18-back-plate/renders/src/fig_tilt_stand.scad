// The whole unit standing on a table on the tilted plate's sloped face, seen
// from the side. Render with the tilted preset. The front shell and display
// are ghosted (15 mm stock unit, 3.5 mm of it the old back).
include <../../amoled18_back_plate.scad>
use <annot.scad>
show_part = false;
$fn = 64; $k = 0.6;
shell_h = 11.5;

// Put the sloped face on the table: turn it level, then move its seam edge to the origin.
rotate([90 - tilt_angle, 0, 0]) translate([0, -tilt_s[0], -tilt_s[1]]) {
    color("SteelBlue") back_plate();
    color("DimGray") translate([0, 0, body_h]) rbox(plate_x, plate_y, shell_h - 0.8, plate_r);
    color("Black") translate([0, 0, body_h + shell_h - 0.8]) rbox(plate_x - 3, plate_y - 3, 0.8, plate_r - 1.5);
}
color("Gainsboro") translate([-30, -35, -1]) cube([60, 70, 1]);
label([0, 0, 52], str("leans back ", tilt_angle, "°"), "right", 3.2, c = "DarkRed");
callout([0, -9.2, 24], [0, -26, 30], "display", "right", 2.6, "right", c = "DimGray");
label([0, 2, -4.5], "stands on the plate's sloped face", "right", 2.4);
label([0, 2, -8.5], "and the front shell's bottom edge", "right", 2.4);
