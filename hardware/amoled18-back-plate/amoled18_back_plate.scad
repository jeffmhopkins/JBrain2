// =====================================================================
// PARAMETRIC BACK PLATE
// Waveshare ESP32-S3-Touch-AMOLED-1.8  (replaces the stock black cover)
//
// The stock back is a flat plate with a shallow rim. All ports, buttons
// and the mic live in the FRONT shell, so this part is solid except for
// the battery pocket and four screw holes.
//
// EVERY dimension is adjustable. In OpenSCAD open
//     Window > Customizer
// and the parameters appear as labelled fields and sliders, grouped by
// section. No code editing required.
//
// Workflow:  set values  >  F5 preview  >  F6 render  >  F7 export STL
//
// NOTE ON SCREWS: making the plate deeper means the stock screws no
// longer reach the brass inserts. The console prints exactly how much
// longer they need to be.
//
// Units: millimetres.   OpenSCAD is free at openscad.org
// =====================================================================


/* [1. Battery] */

// Cell thickness (the dimension that drives the whole design)
battery_t = 8.6;    // [2:0.1:25]
// Cell width
battery_w = 25.0;   // [10:0.5:60]
// Cell length
battery_l = 25.0;   // [10:0.5:60]
// Double-sided tape under the cell (VHB 1mm measures about 1.1)
tape_t = 1.1;       // [0:0.1:3]
// Foam padding above the cell
foam_t = 1.5;       // [0:0.1:5]
// Extra air so nothing is ever compressed
extra_clearance = 0.4;  // [0:0.1:2]


/* [2. Plate outline] */

// Overall width of the plate
plate_x = 38.0;     // [20:0.1:80]
// Overall height of the plate
plate_y = 43.0;     // [20:0.1:80]
// Outer corner radius
plate_r = 6.5;      // [0.5:0.1:20]
// Thickness of the flat back panel
plate_t = 1.6;      // [0.8:0.1:5]
// Round over the outside back edge (0 = sharp)
edge_fillet = 0.8;  // [0:0.1:3]


/* [3. Rim that enters the front shell] */

// How to define the rim outline.
//   inset    = stepped in from the plate edge by lip_inset
//   absolute = you type its outside width and length directly
lip_mode = "absolute";  // [inset, absolute]

// -- absolute mode: measure the OUTSIDE of the stock rim --
// Rim outside width
lip_outer_x = 35.0;  // [10:0.1:80]
// Rim outside length
lip_outer_y = 40.0;  // [10:0.1:80]
// Rim outside corner radius
lip_outer_r = 5.1;   // [0.5:0.1:20]

// -- inset mode only --
// How far the rim steps in from the outer edge
lip_inset = 1.4;     // [0:0.1:6]

// -- both modes --
// How tall the rim stands above the floor
lip_h = 2.0;         // [0:0.1:8]
// Width of the rim wall (how thick the band is)
lip_wall = 1.6;      // [0.4:0.1:4]
// Clearance taken off the outside so it is not a press fit
lip_slop = 0.15;     // [0:0.05:0.6]
// Inside depth of the STOCK cover (rim top down to the floor)
stock_clear = 4.5;   // [1:0.1:15]


/* [4. Screws] */

// Centre of plate to hole centre, across X
screw_dx = 14.5;    // [5:0.1:40]
// Centre of plate to hole centre, across Y
screw_dy = 17.0;    // [5:0.1:40]
// Thread size drives the default hole sizes
screw_size = "M2";  // [M1.6, M2, M2.5, M3, Custom]
// Head style
head_style = "counterbore";  // [counterbore, countersink, none]
// Printer hole allowance (holes print undersize; 0.2 suits most FDM)
hole_slop = 0.2;    // [0:0.05:0.6]

/* [4b. Custom screw sizes - used only when screw_size is Custom] */
custom_shaft_d = 2.0;   // [1:0.1:6]
custom_head_d = 3.8;    // [2:0.1:10]
custom_head_t = 1.0;    // [0.4:0.1:4]


/* [5. Battery pocket] */

// Size the pocket automatically from the cell dimensions
auto_pocket = true;
// Gap around the cell when auto sizing
pocket_margin = 1.5;    // [0:0.1:5]
// Manual pocket width (used when auto_pocket is off)
manual_pocket_x = 30.0; // [10:0.5:70]
// Manual pocket length
manual_pocket_y = 28.0; // [10:0.5:70]
// Pocket corner radius
pocket_r = 3.0;         // [0.5:0.1:10]
// Soften the bottom edge of the pocket
pocket_floor_fillet = 0.6;  // [0:0.1:2]


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

shaft_table = screw_size == "M1.6" ? [1.8, 3.2, 0.9]
            : screw_size == "M2"   ? [2.2, 3.8, 1.0]
            : screw_size == "M2.5" ? [2.7, 4.6, 1.2]
            : screw_size == "M3"   ? [3.2, 5.6, 1.4]
            : [custom_shaft_d, custom_head_d, custom_head_t];

shaft_d = shaft_table[0] + hole_slop;
head_d  = shaft_table[1] + hole_slop;
head_t  = shaft_table[2];

stack_t     = battery_t + tape_t + foam_t + extra_clearance;
inner_clear = max(stock_clear, stack_t);
extra_depth = inner_clear - stock_clear;
spacer_h    = extra_depth;
total_h     = plate_t + spacer_h + lip_h;

pocket_x = auto_pocket ? battery_w + 2 * pocket_margin : manual_pocket_x;
pocket_y = auto_pocket ? battery_l + 2 * pocket_margin : manual_pocket_y;
pocket_depth = spacer_h + lip_h;

// Rim outline, from whichever mode is selected
rim_out_x = (lip_mode == "absolute" ? lip_outer_x
                                    : plate_x - 2 * lip_inset) - 2 * lip_slop;
rim_out_y = (lip_mode == "absolute" ? lip_outer_y
                                    : plate_y - 2 * lip_inset) - 2 * lip_slop;
rim_out_r = (lip_mode == "absolute" ? lip_outer_r
                                    : plate_r - lip_inset) - lip_slop;

// Inside of the rim, which also caps how big the pocket can be
rim_in_x = rim_out_x - 2 * lip_wall;
rim_in_y = rim_out_y - 2 * lip_wall;

pocket_fits_x = pocket_x <= rim_in_x - 0.5;
pocket_fits_y = pocket_y <= rim_in_y - 0.5;
rim_fits      = (rim_out_x <= plate_x - 0.4) && (rim_out_y <= plate_y - 0.4);
screw_clear_x = screw_dx - head_d/2 > pocket_x/2 + 0.8;
screw_clear_y = screw_dy - head_d/2 > pocket_y/2 + 0.8;
screws_solid  = screw_clear_x || screw_clear_y;
screws_inside = (screw_dx + head_d/2 < plate_x/2 - 0.6)
             && (screw_dy + head_d/2 < plate_y/2 - 0.6);

echo("================ BACK PLATE ================");
echo(str("Cell:               ", battery_t, " x ", battery_w, " x ", battery_l, " mm"));
echo(str("Stack height:       ", stack_t, " mm  (cell + tape + foam + air)"));
echo(str("Pocket:             ", pocket_x, " x ", pocket_y, " x ", pocket_depth, " mm"));
echo(str("EXTRA DEPTH:        ", extra_depth, " mm over stock"));
echo(str("Total plate height: ", total_h, " mm"));
echo(str("SCREWS:             ", screw_size, ", ", extra_depth, " mm longer than stock"));
echo(str("Rim outside:        ", rim_out_x, " x ", rim_out_y,
         "  (r ", rim_out_r, ", wall ", lip_wall, ")"));
echo(str("Rim opening:        ", rim_in_x, " x ", rim_in_y, " mm"));
echo("-------------------------------------------");
if (!rim_fits)      echo("*** RIM IS LARGER THAN THE PLATE ***");
if (!pocket_fits_x) echo("*** POCKET TOO WIDE for the rim opening ***");
if (!pocket_fits_y) echo("*** POCKET TOO LONG for the rim opening ***");
if (!screws_solid)  echo("*** POCKET OVERLAPS A SCREW HOLE - shrink pocket or move screws ***");
if (!screws_inside) echo("*** SCREW HOLES FALL OFF THE PLATE EDGE ***");
if (rim_fits && pocket_fits_x && pocket_fits_y && screws_solid && screws_inside)
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

module solid_body() {
    if (edge_fillet > 0.05)
        minkowski() {
            rbox(plate_x - 2*edge_fillet, plate_y - 2*edge_fillet,
                 max(0.1, plate_t + spacer_h - edge_fillet),
                 max(0.1, plate_r - edge_fillet));
            sphere(r = edge_fillet);
        }
    else
        rbox(plate_x, plate_y, plate_t + spacer_h, plate_r);

    if (lip_h > 0.05)
        translate([0, 0, plate_t + spacer_h - eps])
            linear_extrude(height = lip_h + eps)
                difference() {
                    rrect(rim_out_x, rim_out_y, rim_out_r);
                    rrect(rim_in_x, rim_in_y, max(0.3, rim_out_r - lip_wall));
                }
}

module battery_pocket() {
    translate([0, 0, plate_t])
        if (pocket_floor_fillet > 0.05)
            minkowski() {
                rbox(pocket_x - 2*pocket_floor_fillet,
                     pocket_y - 2*pocket_floor_fillet,
                     pocket_depth, max(0.1, pocket_r - pocket_floor_fillet));
                cylinder(r1 = pocket_floor_fillet, r2 = 0,
                         h = pocket_floor_fillet);
            }
        else
            rbox(pocket_x, pocket_y, pocket_depth + eps, pocket_r);
}

module screw_holes() {
    for (sx = [-1, 1], sy = [-1, 1])
        translate([sx * screw_dx, sy * screw_dy, 0]) {
            translate([0, 0, -eps])
                cylinder(d = shaft_d, h = total_h + 2*eps);

            if (head_style == "counterbore")
                translate([0, 0, -eps])
                    cylinder(d = head_d, h = head_t + eps);

            if (head_style == "countersink")
                translate([0, 0, -eps])
                    cylinder(d1 = head_d, d2 = shaft_d,
                             h = (head_d - shaft_d)/2 + eps);
        }
}

module back_plate() {
    difference() {
        solid_body();
        battery_pocket();
        screw_holes();
    }
}

back_plate();

if (show_battery)
    color("green", 0.35)
        translate([-battery_w/2, -battery_l/2, plate_t + tape_t])
            cube([battery_w, battery_l, battery_t]);


// =====================================================================
// PRINTING NOTES
//   Orientation : flat face down on the bed, rim upward. No supports.
//   Layer       : 0.2 mm
//   Walls       : 3 perimeters minimum around the screw columns
//   Infill      : 40% or more
//   Material    : PETG or ABS if it may sit in a warm car; PLA softens
//   First print : check the screw holes line up before printing a final
// =====================================================================
