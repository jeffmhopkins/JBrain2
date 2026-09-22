// =====================================================================
// PARAMETRIC BACK PLATE
// Waveshare ESP32-S3-Touch-AMOLED-1.8  (replaces the stock black cover)
//
// The stock back is a flat plate with a shallow rim. All ports, buttons
// and the mic live in the FRONT shell, so this part is solid except for
// the battery cavity and four screw holes.
//
// EVERY dimension is adjustable. In OpenSCAD open
//     Window > Customizer
// and the parameters appear as labelled fields and sliders, grouped by
// section. No code editing required.
//
// Workflow:  set values  >  F5 preview  >  F6 render  >  F7 export STL
//
// NOTE ON SCREWS: the holes are counterbored for socket head cap screws
// (the standard hex-key M screws). Making the plate deeper means the
// stock screws no longer reach; the console prints the new length.
//
// Units: millimetres.   OpenSCAD is free at openscad.org
// =====================================================================


/* [1. Battery] */

// Cell thickness (the first number in a size code: 852540 is 8.5)
battery_t = 8.5;    // [2:0.1:25]
// Cell width (852540: 25)
battery_w = 25.0;   // [10:0.5:60]
// Cell length, laid along the long side of the case (852540: 40)
battery_l = 40.0;   // [10:0.5:60]
// flat = lying on its face. edge = standing on its long edge, which fits long cells
battery_orientation = "edge";  // [flat, edge]
// Double-sided tape under the cell (VHB 1mm measures about 1.1)
tape_t = 1.1;       // [0:0.1:3]
// Foam padding above the cell
foam_t = 1.5;       // [0:0.1:5]
// Extra air so nothing is ever compressed
extra_clearance = 0.4;  // [0:0.1:2]


/* [2. Plate outline] */

// Defaults from Waveshare's dimension drawing: case 37.6 x 45.2 mm,
// corner radius fitted to the drawing at about 8.7 mm. See README.md.


// Overall width of the plate
plate_x = 37.6;     // [20:0.1:80]
// Overall height of the plate
plate_y = 45.2;     // [20:0.1:80]
// Outer corner radius
plate_r = 8.7;      // [0.5:0.1:20]
// Thickness of the flat back panel
plate_t = 1.6;      // [0.8:0.1:5]
// Small 45 degree chamfer on the flat back edge, against elephant's foot (0 = sharp)
edge_chamfer = 0.4;  // [0:0.1:1.5]


/* [3. Rim that enters the front shell] */

// Rim defaults assume a ~1.3 mm front-shell wall, estimated from photos.
// These are the least certain numbers in the file: measure them.


// How to define the rim outline.
//   inset    = stepped in from the plate edge by lip_inset
//   absolute = you type its outside width and length directly
lip_mode = "absolute";  // [inset, absolute]

// -- absolute mode: measure the OUTSIDE of the stock rim --
// Rim outside width
lip_outer_x = 35.0;  // [10:0.1:80]
// Rim outside length
lip_outer_y = 42.6;  // [10:0.1:80]
// Rim outside corner radius
lip_outer_r = 7.4;   // [0.5:0.1:20]

// -- inset mode only --
// How far the rim steps in from the outer edge
lip_inset = 1.4;     // [0:0.1:6]

// -- both modes --
// How tall the rim stands above the floor
lip_h = 2.0;         // [0:0.1:8]
// Width of the rim wall (how thick the band is)
lip_wall = 0.8;      // [0.4:0.1:4]
// Clearance taken off the outside so it is not a press fit
lip_slop = 0.15;     // [0:0.05:0.6]
// Inside depth of the STOCK cover (rim top down to the floor)
stock_clear = 3.9;   // [1:0.1:15]


/* [4. Screws] */

// Hole positions come from the PCB's mounting holes in Waveshare's 3D
// model: 24.0 x 36.0 mm apart, centred on the board.


// Centre of plate to hole centre, across X
screw_dx = 12.0;    // [5:0.1:40]
// Centre of plate to hole centre, across Y
screw_dy = 18.0;    // [5:0.1:40]
// Thread size drives the default hole sizes
screw_size = "M2";  // [M1.6, M2, M2.5, M3, Custom]
// Recess the heads (off = plain through holes)
counterbore = true;
// Room around the head so it drops in and a hex key reaches
head_clear = 0.6;   // [0:0.1:2]
// How far below the back face the head sits
head_sink = 0.3;    // [0:0.1:2]
// Plastic over each screw head (the pad's roof)
head_seat = 2.0;    // [0.6:0.1:5]
// Wall around each screw head in the floor pads
pad_wall = 2.0;     // [0.6:0.1:4]
// Height of the 45 degree brace from each pad up into the wall corner (0 = none)
brace_h = 4.0;      // [0:0.5:12]
// Printer hole allowance (holes print undersize; 0.2 suits most FDM)
hole_slop = 0.2;    // [0:0.05:0.6]

/* [4b. Custom screw sizes - used only when screw_size is Custom] */
custom_shaft_d = 2.0;   // [1:0.1:6]
// Socket head diameter
custom_head_d = 3.8;    // [2:0.1:10]
// Socket head height
custom_head_t = 2.0;    // [0.4:0.1:6]


/* [5. Battery cavity] */

// The whole inside is hollow; pad gaps with foam. The screws run bare
// through it, from small pads on the floor that seat their heads.
// The walls rise straight up to the rim, so the cavity is the rim opening
// and the rim wall below sets how much room there is.


/* [6. Output] */

// Curve smoothness. 48 previews fast, 96 is export quality.
smoothness = 72;    // [24:8:144]
// Show a ghost of the battery to check placement (preview only)
show_battery = false;


/* [Hidden] */
eps = 0.01;
$fn = smoothness;


// =====================================================================
// DERIVED VALUES
// =====================================================================

// Clearance hole, then ISO 4762 socket head diameter and height.
shaft_table = screw_size == "M1.6" ? [1.8, 3.0, 1.6]
            : screw_size == "M2"   ? [2.2, 3.8, 2.0]
            : screw_size == "M2.5" ? [2.7, 4.5, 2.5]
            : screw_size == "M3"   ? [3.2, 5.5, 3.0]
            : [custom_shaft_d, custom_head_d, custom_head_t];

shaft_d = shaft_table[0] + hole_slop;
head_d  = counterbore ? shaft_table[1] + head_clear + hole_slop : shaft_d;
head_t  = counterbore ? shaft_table[2] + head_sink : 0;
// The floor is thinner than the counterbores, so each head sits in a pad.
pad_r   = head_d / 2 + pad_wall;
pad_top = max(plate_t, head_t + head_seat);

// Cell footprint and height as it sits in the plate.
edge  = battery_orientation == "edge";
fx     = edge ? battery_t : battery_w;
fy     = battery_l;
cell_h = edge ? battery_w : battery_t;

// Rim outline, from whichever mode is selected
rim_out_x = (lip_mode == "absolute" ? lip_outer_x
                                    : plate_x - 2 * lip_inset) - 2 * lip_slop;
rim_out_y = (lip_mode == "absolute" ? lip_outer_y
                                    : plate_y - 2 * lip_inset) - 2 * lip_slop;
rim_out_r = (lip_mode == "absolute" ? lip_outer_r
                                    : plate_r - lip_inset) - lip_slop;
rim_in_x = rim_out_x - 2 * lip_wall;
rim_in_y = rim_out_y - 2 * lip_wall;
rim_in_r = max(0.3, rim_out_r - lip_wall);

// Straight walls whose inside is the rim's inside, so the rim stands on the
// wall with nothing overhanging.
cav_x = rim_in_x;
cav_y = rim_in_y;
cav_r = rim_in_r;

stack_t     = cell_h + tape_t + foam_t + extra_clearance;
inner_clear = max(stock_clear, stack_t);
// The rim is part of the cavity's depth, so the body only makes up the rest.
spacer_h    = max(0, inner_clear - lip_h);
body_h      = plate_t + spacer_h;
total_h     = body_h + lip_h;
extra_depth = total_h - plate_t - stock_clear;

// Nearest gap between the cell's corner and a floor pad.
pad_gap   = norm([max(0, screw_dx - fx/2), max(0, screw_dy - fy/2)]) - pad_r;
clears_pads = pad_gap >= 0.3;
kx = cav_x/2 - cav_r;
ky = cav_y/2 - cav_r;
fits_walls = fx <= cav_x - 0.6 && fy <= cav_y - 0.6
          && (fx/2 <= kx || fy/2 <= ky || norm([fx/2 - kx, fy/2 - ky]) <= cav_r - 0.3);
rim_fits      = (rim_out_x <= plate_x - 0.4) && (rim_out_y <= plate_y - 0.4);
screws_inside = (screw_dx + head_d/2 < plate_x/2 - 0.6)
             && (screw_dy + head_d/2 < plate_y/2 - 0.6);
// From the head's seat to the rim top, where a stock screw's length is measured from.
screw_reach = total_h - head_t;

echo("================ BACK PLATE ================");
echo(str("Cell:               ", battery_t, " x ", battery_w, " x ", battery_l,
         " mm, ", edge ? "standing on its edge" : "lying flat"));
echo(str("Stack height:       ", stack_t, " mm  (cell + tape + foam + air)"));
echo(str("Cavity:             ", cav_x, " x ", cav_y, " x ", spacer_h + lip_h, " mm, open"));
echo(str("Gap cell to pad:    ", pad_gap, " mm"));
echo(str("EXTRA DEPTH:        ", extra_depth, " mm over stock"));
echo(str("Total plate height: ", total_h, " mm"));
echo(str("SCREWS:             ", screw_size, " socket head cap, reach ", screw_reach,
         " mm. Length = reach + how far a stock screw pokes through the stock cover."));
echo(str("Rim outside:        ", rim_out_x, " x ", rim_out_y,
         "  (r ", rim_out_r, ", wall ", lip_wall, ")"));
echo(str("Rim opening:        ", rim_in_x, " x ", rim_in_y, " mm"));
echo("-------------------------------------------");
if (!rim_fits)      echo("*** RIM IS LARGER THAN THE PLATE ***");
if (!clears_pads)   echo("*** CELL SITS ON A SCREW PAD - try the other orientation or a narrower cell ***");
if (!fits_walls)    echo("*** CELL DOES NOT FIT INSIDE THE WALLS ***");
if (!screws_inside) echo("*** SCREW HOLES FALL OFF THE PLATE EDGE ***");
if (rim_fits && clears_pads && fits_walls && screws_inside)
    echo("All checks passed.");
echo("===========================================");


// =====================================================================
// GEOMETRY
// =====================================================================

module rrect(x, y, r) {
    rr = min(r, min(x, y)/2 - 0.01);
    hull() for (sx = [-1, 1], sy = [-1, 1])
        translate([sx * (x/2 - rr), sy * (y/2 - rr)]) circle(r = rr);
}

module rbox(x, y, z, r) { linear_extrude(height = z) rrect(x, y, r); }

// Each pad, plus a brace that climbs at 45 degrees away from the cell into
// the wall corner, tying the pad to the wall.
module pads() {
    for (sx = [-1, 1], sy = [-1, 1]) {
        c = [sx * screw_dx, sy * screw_dy];
        u = c / norm(c);
        hull() {
            translate([c[0], c[1], plate_t - eps])
                cylinder(r = pad_r, h = pad_top - plate_t + eps);
            if (brace_h > 0.05)
                translate([c[0] + u[0] * brace_h, c[1] + u[1] * brace_h, pad_top])
                    cylinder(r = pad_r, h = brace_h);
        }
    }
}

module solid_body() {
    if (edge_chamfer > 0.05)
        hull() {
            linear_extrude(height = eps)
                rrect(plate_x - 2*edge_chamfer, plate_y - 2*edge_chamfer,
                      max(0.1, plate_r - edge_chamfer));
            translate([0, 0, edge_chamfer])
                rbox(plate_x, plate_y, body_h - edge_chamfer, plate_r);
        }
    else
        rbox(plate_x, plate_y, body_h, plate_r);

    if (lip_h > 0.05)
        translate([0, 0, body_h - eps])
            linear_extrude(height = lip_h + eps)
                difference() {
                    rrect(rim_out_x, rim_out_y, rim_out_r);
                    rrect(rim_in_x, rim_in_y, rim_in_r);
                }
}

module battery_cavity() {
    difference() {
        translate([0, 0, plate_t]) rbox(cav_x, cav_y, spacer_h + lip_h + eps, cav_r);
        pads();
    }
}

module screw_holes() {
    for (sx = [-1, 1], sy = [-1, 1])
        translate([sx * screw_dx, sy * screw_dy, 0]) {
            translate([0, 0, -eps])
                cylinder(d = shaft_d, h = total_h + 2*eps);

            if (counterbore)
                translate([0, 0, -eps])
                    cylinder(d = head_d, h = head_t + eps);
        }
}

module back_plate() {
    difference() {
        solid_body();
        battery_cavity();
        screw_holes();
    }
}

back_plate();

if (show_battery)
    color("green", 0.35)
        translate([-fx/2, -fy/2, plate_t + tape_t])
            cube([fx, fy, cell_h]);


// =====================================================================
// PRINTING NOTES
//   Orientation : flat face down on the bed, rim upward. No supports.
//   Layer       : 0.2 mm
//   Walls       : 3 perimeters or more
//   Infill      : 40% or more
//   Material    : PETG or ABS if it may sit in a warm car; PLA softens
//   First print : check the screw holes line up before printing a final
// =====================================================================
