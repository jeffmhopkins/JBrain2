// How the desk stand's two parts sit on the printer.
include <../../amoled18_back_plate.scad>
use <annot.scad>
show_part = false;
$fn = 64; $k = 0.8;
color("SteelBlue") stand_box();
color("LightSteelBlue") translate([-52, 0, 0]) back_plate();
color("Gainsboro") translate([-76, -32, -1]) cube([122, 64, 1]);
label([-52, -26, 0.05], "head: back face down", "top", 2.6);
label([12, -26, 0.05], "box: on its bottom", "top", 2.6);
