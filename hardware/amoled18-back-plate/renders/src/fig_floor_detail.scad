// Detail: bottom of the end wall (section at x = 0, seen from +x), on the print bed.
use <../../amoled18_back_plate.scad>
use <annot.scad>
$fn = 64; $k = 0.25;
v = "right"; x = 1; s = 0.5;
color("SteelBlue") render() intersection() {
    back_plate();
    translate([-0.4, 14, -0.1]) cube([0.4, 11, 8]);
}
color("Gainsboro") translate([-1, 13, -0.6]) cube([1, 13, 0.6]);
label([x, 19, -1.1], "print bed: back face down", v, s, c = "DimGray");
dim([x, 15.2, 0], [x, 15.2, 1.6], "", view = v);
label([x, 14.9, 0.8], "plate_t 1.6", v, s, "right");
callout([x, 19.4, 2.4], [x, 17.0, 4.4], "wall_chamfer 3 (45°)", v, s, "right");
callout([x, 22.45, 0.2], [x, 23.6, 0.6], "edge_chamfer 0.4", v, s, "left");
callout([x, 22.8, 3.3], [x, 23.6, 3.3], "grip ribs start 1.5 up", v, s, "left");
