// Top view: where the default cell stands, its wire room and the clearances.
use <../../amoled18_back_plate.scad>
use <annot.scad>
$fn = 48; $k = 0.6;
z = 40; s = 1.3;
color("SteelBlue") back_plate();
// 103035 on its edge: 10 wide, 35 long, plus 2 mm lead_end at +y.
color("LimeGreen") translate([-5, -18.5, z - 1]) cube([10, 35, 0.5]);
color("Orange") translate([-5, 16.5, z - 1]) cube([10, 2, 0.5]);
label([0, 0, z], "cell 10 × 35", "top", s);
label([0, -2.2, z], "(battery_t × battery_l)", "top", s * 0.8);
callout([3, 17.5, z], [22, 22, z], "lead_end 2: wire room", "top", s, "left", c = "DarkOrange");
dim([-16.55, -8, z], [-5, -8, z], "", c = "DarkRed");
label([-10.8, -6.6, z], "11.55", "top", s, c = "DarkRed");
callout([0, -19.4, z], [-8, -28, z], "1.85 spare at each end", "top", s, "right", c = "DarkRed");
callout([12, -18, z], [22, -22, z], "screw tower", "top", s, "left");
label([0, 26, z], "cavity 33.1 × 40.7 (the rim opening)", "top", s);
