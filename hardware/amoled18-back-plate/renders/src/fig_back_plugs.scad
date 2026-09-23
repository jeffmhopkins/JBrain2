// The back face, with or without the hole plugs fitted (-D fitted=1).
use <../../amoled18_back_plate.scad>
fitted = 0;
$fn = 64;
color("SteelBlue") back_plate();
if (fitted == 1)
    for (sx = [-1, 1], sy = [-1, 1])
        color("Orange") translate([sx * 12, sy * 18, 0]) plug();
