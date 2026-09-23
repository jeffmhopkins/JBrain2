// Detail: top of the end wall (section at x = 0, seen from +x), front shell ghosted.
use <../../amoled18_back_plate.scad>
use <annot.scad>
$fn = 64; $k = 0.25;
v = "right"; x = 1; s = 0.5;
color("SteelBlue") render() intersection() {
    back_plate();
    translate([-0.4, 15, 27.5]) cube([0.4, 10, 8]);
}
// Front shell (estimated): its wall sits outside the rim, on the plate's top edge.
color("LightGray") translate([-0.3, 21.3, 32.65]) cube([0.3, 1.3, 2.6]);
callout([x, 22.3, 34.6], [x, 23.3, 35.4], "front shell (ghost)", v, s, "left", c = "DimGray");
label([x, 18.0, 31.0], "battery cavity", v, s, c = "DimGray");

dim([x, 20.35, 35.4], [x, 21.15, 35.4], "", view = v);
label([x, 20.1, 35.4], "lip_wall 0.8", v, s, "right");
dim([x, 19.6, 32.6], [x, 19.6, 34.6], "", view = v);
label([x, 19.3, 33.6], "lip_h 2", v, s, "right");
callout([x, 21.15, 33.3], [x, 23.3, 33.3], "rim outside", v, s, "left");
label([x, 23.8, 32.6], "= lip_outer_y 42.6 − 2 × lip_slop", v, s * 0.8, "left");
dim([x, 20.35, 29.2], [x, 22.6, 29.2], "", view = v);
label([x, 21.5, 28.5], "wall 2.25", v, s);
callout([x, 22.75, 30.9], [x, 23.6, 30.9], "grip rib", v, s, "left");
