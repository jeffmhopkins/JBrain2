// Top view (looking into the plate): outline, corner radius and screw spacing.
use <../../amoled18_back_plate.scad>
use <annot.scad>
$fn = 48;
z = 40;
red = "DarkRed";
color("SteelBlue") back_plate();
// Hole centres.
for (sx = [-1, 1], sy = [-1, 1]) color(red) translate([sx * 12, sy * 18, z]) sphere(r = 0.8, $fn = 16);
color(red) translate([0, 0, z]) sphere(r = 0.6, $fn = 12);
label([0, -2.5, z], "centre", size = 1.8, c = red);

// Outline.
dim([-18.8, -30, z], [18.8, -30, z], "plate_x  37.6", [0, -2.8, 0]);
seg([-18.8, -23, z], [-18.8, -31, z], r = 0.06); seg([18.8, -23, z], [18.8, -31, z], r = 0.06);
dim([25, -22.6, z], [25, 22.6, z], "plate_y  45.2", [8.5, 0, 0]);
seg([19, 22.6, z], [26, 22.6, z], r = 0.06); seg([19, -22.6, z], [26, -22.6, z], r = 0.06);
callout([-18.8 + 8.7 * (1 - cos(45)), 22.6 - 8.7 * (1 - sin(45)), z], [-26, 29, z], "plate_r  8.7", halign = "right");

// Screw spacing, drawn outside the part with extension lines from the holes.
dim([-12, 28, z], [12, 28, z], "2 × screw_dx = 24", [0, 2.6, 0], c = red);
seg([-12, 18, z], [-12, 29, z], r = 0.06, c = red); seg([12, 18, z], [12, 29, z], r = 0.06, c = red);
dim([-25, -18, z], [-25, 18, z], "2 × screw_dy = 36", [-9.5, 0, 0], c = red);
seg([-12, 18, z], [-26, 18, z], r = 0.06, c = red); seg([-12, -18, z], [-26, -18, z], r = 0.06, c = red);
