// Desk stand from the side as it sits on the table, cut down the middle: the
// angle and the sizes.
include <../../amoled18_back_plate.scad>
use <annot.scad>
show_part = false;
$fn = 64; $k = 0.9;
v = "front"; y = -30; t = 2.4;
red = "DarkRed";
module half() intersection() { children(); translate([-100, 0, -100]) cube([200, 100, 300]); }
g = p_out_x * sc + grip_depth;              // height of the print frame's long wall above the table
// Print frame (px, pz) to where it sits on the table.
function u(px, pz) = [pz, y, g - px];
// A point on the display (its +x the USB-C edge), which sits in the box turned 180 degrees.
function wd(x, z) = u(-x * sc - z * ss, stand_zh - x * ss + z * sc);
color("Gainsboro") translate([-5, -40, -1]) cube([95, 80, 1]);
stand_pose() {
    color("SteelBlue") render() half() stand_box();
    stand_head_pose() {
        color("LightSteelBlue") render() half() back_plate();
        color("DimGray", 0.5) render() half() translate([0, 0, body_h]) rbox(plate_x, plate_y, 11.5, plate_r);
        color("Black") translate([plate_x / 2 - 1, -4.5, body_h + 3.5]) cube([1.3, 9, 3.2]);
    }
    color("LimeGreen") render() half() translate([cell_x0, -fy/2, box_floor + tape_t]) cube([fx, cell_y, cell_h]);
}
L = band_top_back; H = box_depth - (g - p_out_x * sc);
// Box length on the table and height.
dim([0, y, -4], [L, y, -4], str(round(L), " mm"), [0, 0, -2.6], v, t);
dim([-5, y, 0], [-5, y, H], "", view = v);
label([-6, y, H / 2], str(round(H), " mm"), v, t, "right");
// Floor, at the far end.
callout([box_floor / 2, y, 3], [-4, y, -12], str("box_floor ", box_floor), v, t, "right");
// The lean: a vertical line at the screen's foot, and the screen.
p0 = wd(-plate_x / 2, body_h + 11.5);
seg(p0, p0 + [0, 0, 34], r = 0.06, c = red);
label(p0 + [1.5, 0, 32], str("stand_angle ", stand_angle, "°"), v, t, "left", c = red);
callout(wd(plate_x / 2, body_h + 5), wd(plate_x / 2, body_h + 5) + [-6, 0, 8], "USB-C + buttons", v, t, "right");
label(p0 + [3, 0, 18], "screen faces you ▶", v, t, "left");
label([L / 2 - 6, y, g - 2], "battery", v, t, c = "DarkGreen");
label([25, y, -12], "table", v, t, c = "DimGray");
