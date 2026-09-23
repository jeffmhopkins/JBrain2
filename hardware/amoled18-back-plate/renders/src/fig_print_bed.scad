// How it sits on the printer: back face down, rim up, plugs caps-down beside it.
use <../../amoled18_back_plate.scad>
use <annot.scad>
$fn = 64; $k = 0.7;
color("SteelBlue") back_plate();
color("Orange") translate([26, -12, 0]) plugs();
color("Gainsboro") translate([-28, -32, -1]) cube([76, 64, 1]);
label([10, -29.5, 0.05], "print bed", "top", 2.2, c = "DimGray");
