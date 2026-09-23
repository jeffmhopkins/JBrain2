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
// NOTE ON SCREWS: each screw sits in a hollow tower. Its head rests near
// the top, so the tower presses on the board's nut as the stock cover's
// posts do, and a short standard socket head screw is enough. Drive it
// down the tower with a long hex key.
//
// Units: millimetres.   OpenSCAD is free at openscad.org
// =====================================================================


/* [1. Battery] */

// Cell thickness (the first number in a size code: 103035 is 10)
battery_t = 10.0;   // [2:0.1:25]
// Cell width (103035: 30). On edge it's the height; on end it runs along the case
battery_w = 30.0;   // [10:0.5:60]
// Cell length (103035: 35). Along the case, or the height when on end
battery_l = 35.0;   // [10:0.5:60]
// flat = lying on its face. edge = standing on its long edge, which fits long cells.
// end = standing upright on its end: any length fits, the plate just gets taller.
battery_orientation = "edge";  // [flat, edge, end]
// Double-sided tape under the cell (VHB 1mm measures about 1.1)
tape_t = 1.1;       // [0:0.1:3]
// Foam padding above the cell
foam_t = 1.5;       // [0:0.1:5]
// Room above the cell for its wires and plug (the circuit board end faces up)
lead_space = 0.0;   // [0:0.5:10]
// Room past one end of the cell for its wires, when they leave from an end (flat and edge)
lead_end = 2.0;     // [0:0.5:8]
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
// Optional raised pad around the nut, if the flat tower top ever needs to
// stand clear of parts on the board near a nut
nut_pad_d = 4.5;    // [3:0.1:7]
// How far that pad stands above the rest of the tower top (0 = flat top, the default)
nut_pad_h = 0.0;    // [0:0.1:2]
// Room around the head so it slides down the tower and a hex key reaches
head_clear = 0.6;   // [0:0.1:2]
// Plastic between the screw head and the board's nut, at the top of each tower
head_seat = 2.0;    // [0.6:0.1:5]
// Wall around the screw head in each tower
tower_wall = 1.2;   // [0.8:0.1:3]
// How far below the rim top the towers stop; negative = they stand above it.
// Measure the stock cover: rim top to post top
tower_drop = 0.0;   // [-5:0.1:10]
// Thin skin (two layers at 0.2 mm) closing the top of each tower's bore so it prints as a bridge;
// poke it through with the screw. 0 = none
bridge_skin = 0.4;  // [0:0.05:0.8]

// Printer hole allowance (holes print undersize; 0.2 suits most FDM)
hole_slop = 0.2;    // [0:0.05:0.6]

/* [4b. Custom screw sizes - used only when screw_size is Custom] */
custom_shaft_d = 2.0;   // [1:0.1:6]
// Socket head diameter
custom_head_d = 3.8;    // [2:0.1:10]
// Socket head height
custom_head_t = 2.0;    // [0.4:0.1:6]


/* [4c. Hole plugs - press-fit caps that hide the tower openings] */

// Recess each tower opening so a plug sits flush (off = no recess)
plug_recess = true;
// Recess and plug cap depth
plug_cap_t = 0.8;       // [0.4:0.1:2]
// How much wider the recess is than the bore
plug_cap_extra = 1.0;   // [0.6:0.1:3]
// Length of the plug's shank inside the bore
plug_len = 4.0;         // [1:0.5:10]
// Press fit: how far the ribs stand over the printed bore. Raise if loose, lower if tight
plug_interference = 0.1;  // [-0.2:0.05:0.4]
// Gap around the cap in its recess, so a knife tip can pry the plug out
plug_cap_gap = 0.15;    // [0:0.05:0.5]
// Plugs to print (4 plus spares)
plug_count = 6;         // [1:1:12]


/* [5. Battery cavity] */

// The whole inside is hollow apart from the four screw towers; pad gaps
// with foam.
// The walls rise straight up to the rim, so the cavity is the rim opening
// and the rim wall below sets how much room there is.
// 45 degree chamfer where the walls meet the floor, to brace the walls.
// Shrinks by itself on any wall the cell sits close to.
wall_chamfer = 3.0;     // [0:0.5:8]
// 45 degree flare where each screw tower meets the floor. Also shrinks near the cell
tower_flare = 2.0;      // [0:0.5:6]


/* [5b. Grip texture on the outside walls] */

// Raised texture so small hands don't drop it. All shapes slope 45 degrees
// underneath, so they print without supports.
grip_style = "ribs";    // [none, honeycomb, nubs, ribs]
// How far the texture stands out from the wall, in mm. 0 = smooth walls
grip_depth = 0.3;       // [0:0.05:0.5]
// Size of each hexagon or square (across)
grip_size = 3.0;        // [1.5:0.1:6]
// Gap between hexagons or squares, or between ribs
grip_gap = 1.0;         // [0.4:0.1:4]
// Keep the texture this far from the bed edge and from the seam at the top
grip_margin = 1.5;      // [0.5:0.5:5]


/* [7. Desk stand (two parts)] */

// Build the desk stand instead of the back plate: a thin head that screws to
// the display like the stock cover, and a battery box whose angled top pocket
// the head drops into, locked by one screw at each end. The unit sits in
// landscape with the USB-C and buttons along the top edge, tilted up.
desk_stand = false;
// How far the display tilts up from flat
stand_angle = 30;       // [10:1:45]
// How deep the head sits into the box's pocket (must stay under the head's top edge)
pocket_h = 3.0;         // [1.5:0.1:3.3]
// Gap between the head and its pocket, each side. Raise if the head won't go in
pocket_clear = 0.2;     // [0:0.05:0.6]
// Pocket wall thickness around the head
pocket_wall = 2.0;      // [1.2:0.1:4]
// Width of the box's wall top the head's back rests on
stand_ledge = 2.0;      // [1:0.1:5]
// Lowest the box's front edge may be
stand_front_min = 8.0;  // [4:0.5:30]
// Thickness of the box's floor
box_floor = 2.0;        // [1.2:0.1:4]
// Leave the pocket open along the top edge, clear of the USB-C plug and the buttons
open_top = true;


/* [6. Output] */

// Curve smoothness. 48 previews fast, 96 is export quality.
smoothness = 72;    // [24:8:144]
// What to output: the plate (the head, for a desk stand), the hole plugs, both side
// by side, the desk stand's box, or the desk stand assembled (preview only)
part = "plate";     // [plate, plugs, both, box, stand]
// Show a ghost of the battery to check placement (preview only)
show_battery = false;


/* [Hidden] */
eps = 0.01;
// Figure files that include this one set it false to draw the part themselves.
show_part = true;
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
head_d  = shaft_table[1] + head_clear + hole_slop;
tower_r = head_d / 2 + tower_wall;

// Plugs. Holes print under size by about hole_slop, so the ribs are sized
// from the printed bore, not the drawn one.
recess_d   = head_d + plug_cap_extra;
cap_d      = recess_d - hole_slop - 2 * plug_cap_gap;
rib_d      = head_d - hole_slop + plug_interference;
plug_core_d = rib_d - 0.6;

// Cell footprint and height as it sits in the plate.
edge   = battery_orientation == "edge";
on_end = battery_orientation == "end";
fx     = edge || on_end ? battery_t : battery_w;
cell_y = on_end ? battery_w : battery_l;
// The wire end needs room too, so fit checks treat it as part of the cell.
fy     = cell_y + (on_end ? 0 : lead_end);
cell_h = on_end ? battery_l : edge ? battery_w : battery_t;

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

stack_t     = cell_h + tape_t + foam_t + lead_space + extra_clearance;
desk        = desk_stand || part == "box" || part == "stand";
// In a desk stand the battery lives in the box, so the head is stock depth.
inner_clear = desk ? stock_clear : max(stock_clear, stack_t);
// The rim is part of the cavity's depth, so the body only makes up the rest.
spacer_h    = max(0, inner_clear - lip_h);
body_h      = plate_t + spacer_h;
total_h     = body_h + lip_h;
extra_depth = total_h - plate_t - stock_clear;

// Full chamfer where the cell leaves room. Near the cell it may rise only to the
// tape under it plus whatever keeps 0.5 mm clear of the cell's bottom edge.
chamfer_x = desk ? min(wall_chamfer, spacer_h) : min(wall_chamfer, tape_t + max(0, (cav_x - fx) / 2 - 0.5));
chamfer_y = desk ? min(wall_chamfer, spacer_h) : min(wall_chamfer, tape_t + max(0, (cav_y - fy) / 2 - 0.5));

tower_top = total_h - tower_drop;
bore_top  = tower_top - head_seat;

// Clearances from the cell to each wall pair and to the nearest tower.
side_gap  = (cav_x - fx) / 2;
end_gap   = (cav_y - fy) / 2;
tower_gap = norm([max(0, screw_dx - fx/2), max(0, screw_dy - fy/2)]) - tower_r;
// Same rule as the wall chamfer: keep 0.5 mm off the cell's bottom edge.
flare = min(tower_flare, tape_t + max(0, tower_gap - 0.5));
kx = cav_x/2 - cav_r;
ky = cav_y/2 - cav_r;
corner_gap = (fx/2 <= kx || fy/2 <= ky) ? min(side_gap, end_gap)
           : cav_r - norm([fx/2 - kx, fy/2 - ky]);
min_gap = min(side_gap, end_gap, tower_gap, corner_gap);
// Real cells run up to about half a millimetre over their listed size.
fits  = min_gap >= 0.2;
tight = min_gap < 0.5;
rim_fits      = (rim_out_x <= plate_x - 0.4) && (rim_out_y <= plate_y - 0.4);
screws_inside = (screw_dx + tower_r < plate_x/2) && (screw_dy + tower_r < plate_y/2);

// ---- desk stand -------------------------------------------------------
// World frame: the table is z = 0, the viewer looks along +X, Y runs left to
// right. The head's back face lies on a plane tilted up by stand_angle, and
// the head's +x edge (USB-C and buttons) is the high, back edge.
sc = cos(stand_angle);
ss = sin(stand_angle);
stand_margin = 0.5;     // cell to the box's rounded corners, in the head's plane
p_out_x = plate_x / 2 + pocket_clear + pocket_wall;
p_out_y = plate_y / 2 + pocket_clear + pocket_wall;
p_out_r = plate_r + pocket_clear + pocket_wall;
// Box opening, as seen in the head's plane: the head's back rests on a ledge
// stand_ledge wide all round. Seen from above it is squeezed by cos(angle).
bhx = plate_x / 2 - stand_ledge;
bhy = plate_y / 2 - stand_ledge;
bhr = max(0.5, plate_r - stand_ledge);
// How far back the cell can go before its back corners reach the rounded
// corners, in the plane (u) and from above (X).
b_dy   = fy / 2 - (bhy - bhr);
b_room = bhr - stand_margin;
b_ub   = b_dy <= 0 ? bhx - stand_margin
       : b_dy <= b_room ? bhx - bhr + sqrt(b_room * b_room - b_dy * b_dy) : 0;
cell_x1 = b_ub * sc;
cell_x0 = cell_x1 - fx;
// Height the plane must clear over the cell's front top edge, then the pivot
// height Zh (head centre) that achieves it without dropping the front below
// stand_front_min.
top_needed = box_floor + tape_t + cell_h + foam_t + lead_space + extra_clearance;
stand_zh = max(top_needed - cell_x0 * ss / sc, stand_front_min + p_out_x * ss);
box_front_h = stand_zh - p_out_x * ss;
box_back_h  = stand_zh + p_out_x * ss;
box_side_gap = bhy - fy / 2;
box_x_room   = cell_x0 + cell_x1;   // cell_x0 + b_ub*sc: room left at the front
desk_fits  = b_dy <= b_room && box_side_gap >= 0.2 && box_x_room >= 0;
desk_tight = box_side_gap < 0.5;
m_head = [[sc, 0, -ss, 0], [0, 1, 0, 0], [ss, 0, sc, stand_zh], [0, 0, 0, 1]];

if (desk) {
    echo("================ DESK STAND ================");
    echo(str("Cell:               ", battery_t, " x ", battery_w, " x ", battery_l,
             " mm, ", on_end ? "standing on its end" : edge ? "standing on its edge" : "lying flat"));
    echo(str("Tilt:               ", stand_angle, " degrees, USB-C and buttons on the top edge"));
    echo(str("Box footprint:      ", 2 * p_out_x * sc, " (front to back) x ", 2 * p_out_y, " (left to right) mm"));
    echo(str("Box height:         ", box_front_h, " mm at the front, ", box_back_h, " at the back"));
    echo(str("Room around cell:   ", box_side_gap, " mm each side; it stands against the back wall"));
    echo(str("Head:               ", total_h, " mm thick, sits ", pocket_h, " mm into the box's pocket"));
    echo(str("SCREWS:             4 x ", screw_size, " x 4 socket head (display to head), 2 x ",
             screw_size, " x 6 (head to box, one each end)"));
    echo("-------------------------------------------");
    if (!desk_fits) echo("*** CELL DOES NOT FIT THE BOX - try another orientation or a smaller cell ***");
    if (desk_fits && desk_tight)
        echo(str("TIGHT: only ", box_side_gap, " mm spare. Measure the real cell before printing."));
    if (desk_fits) echo("All checks passed.");
    echo("===========================================");
    assert(desk_fits, "CELL DOES NOT FIT THE BOX - try another orientation or a smaller cell");
} else {
    echo("================ BACK PLATE ================");
    echo(str("Cell:               ", battery_t, " x ", battery_w, " x ", battery_l,
             " mm, ", on_end ? "standing on its end" : edge ? "standing on its edge" : "lying flat"));
    echo(str("Stack height:       ", stack_t, " mm  (cell + tape + foam + wires + air)"));
    echo(str("Cavity:             ", cav_x, " x ", cav_y, " x ", spacer_h + lip_h, " mm"));
    echo(str("Room around cell:   ", side_gap, " mm each side, ", end_gap, " mm each end, ",
             tower_gap, " mm to the nearest tower"));
    echo(str("Wall chamfer:       ", chamfer_x, " mm on the long walls, ", chamfer_y, " mm on the end walls; tower flare ", flare, " mm"));
    echo(str("EXTRA DEPTH:        ", extra_depth, " mm over stock"));
    echo(str("Total plate height: ", total_h, " mm"));
    echo(str("SCREWS:             ", screw_size, " socket head cap. Length = ", head_seat,
             " + how far a stock screw pokes past the top of its post on the stock cover."));
    echo(str("Hex key reach:      ", bore_top, " mm down each tower"));
    echo(str("Rim outside:        ", rim_out_x, " x ", rim_out_y,
             "  (r ", rim_out_r, ", wall ", lip_wall, ")"));
    echo(str("Rim opening:        ", rim_in_x, " x ", rim_in_y, " mm"));
    echo("-------------------------------------------");
    if (!rim_fits)      echo("*** RIM IS LARGER THAN THE PLATE ***");
    if (!fits)          echo("*** CELL DOES NOT FIT - try another orientation or a smaller cell ***");
    if (fits && tight)  echo(str("TIGHT: only ", min_gap, " mm spare. Measure the real cell before printing."));
    if (!screws_inside) echo("*** SCREW TOWERS FALL OFF THE PLATE EDGE ***");
    if (rim_fits && fits && screws_inside)
        echo("All checks passed.");
    echo("===========================================");
    assert(fits, "CELL DOES NOT FIT - try another orientation or a smaller cell");
}


// =====================================================================
// GEOMETRY
// =====================================================================

module rrect(x, y, r) {
    rr = min(r, min(x, y)/2 - 0.01);
    hull() for (sx = [-1, 1], sy = [-1, 1])
        translate([sx * (x/2 - rr), sy * (y/2 - rr)]) circle(r = rr);
}

module rbox(x, y, z, r) { linear_extrude(height = z) rrect(x, y, r); }

module towers() {
    for (sx = [-1, 1], sy = [-1, 1])
        translate([sx * screw_dx, sy * screw_dy, plate_t - eps]) {
            cylinder(r = tower_r, h = tower_top - nut_pad_h - plate_t + eps);
            cylinder(d = nut_pad_d, h = tower_top - plate_t + eps);
        }
}

// Cavity limited by a 45 degree chamfer of size c along one pair of walls:
// inset by c at the floor, full size from c up.
module chamfered(ix, iy, c) {
    hull() {
        translate([0, 0, plate_t - eps])
            linear_extrude(height = eps) rrect(ix - 2*c, iy, max(0.5, cav_r - c));
        translate([0, 0, plate_t + c])
            rbox(ix, iy, total_h, cav_r);
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

    // Tower tops that rise into the rim, trimmed to its outline so they
    // never reach the front shell.
    if (tower_top > body_h)
        intersection() {
            towers();
            translate([0, 0, body_h - eps])
                rbox(rim_out_x, rim_out_y, tower_top - body_h + eps, rim_out_r);
        }
}

module battery_cavity() {
    difference() {
        intersection() {
            translate([0, 0, plate_t]) rbox(cav_x, cav_y, spacer_h + lip_h + eps, cav_r);
            chamfered(cav_x, cav_y + 2*chamfer_y + 2, chamfer_x);
            rotate(90) chamfered(cav_y, cav_x + 2*chamfer_x + 2, chamfer_y);
        }
        towers();
        tower_flares();
    }
}

module tower_flares() {
    if (flare > 0.05)
        for (sx = [-1, 1], sy = [-1, 1])
            translate([sx * screw_dx, sy * screw_dy, plate_t - eps])
                cylinder(r1 = tower_r + flare, r2 = tower_r, h = flare + eps);
}

module screw_holes() {
    for (sx = [-1, 1], sy = [-1, 1])
        translate([sx * screw_dx, sy * screw_dy, 0]) {
            // Bore for the head and hex key, from the back face to the seat.
            translate([0, 0, -eps])
                cylinder(d = head_d, h = bore_top + eps);
            translate([0, 0, bore_top + bridge_skin])
                cylinder(d = shaft_d, h = total_h);
            if (plug_recess && !desk)
                translate([0, 0, -eps])
                    cylinder(d = recess_d, h = plug_cap_t + eps);
        }
}

// Printed cap-down: the flat face that shows is on the bed. Six ribs crush
// slightly as the shank goes in; the tip is chamfered to start it straight.
module plug() {
    cylinder(d = cap_d, h = plug_cap_t);
    translate([0, 0, plug_cap_t - eps]) {
        cylinder(d = plug_core_d, h = plug_len + eps);
        intersection() {
            for (a = [0 : 60 : 359])
                rotate(a) translate([0, -0.3, 0]) cube([rib_d / 2, 0.6, plug_len]);
            cylinder(d = rib_d, h = plug_len - 0.6);
        }
        translate([0, 0, plug_len - 0.6])
            cylinder(d1 = rib_d, d2 = plug_core_d - 0.4, h = 0.6);
    }
}

module plugs() {
    cols = min(plug_count, 3);
    for (i = [0 : plug_count - 1])
        translate([(i % cols) * (cap_d + 3), floor(i / cols) * (cap_d + 3), 0]) plug();
}

// ---- grip texture ------------------------------------------------------

grip_z0 = grip_margin;
grip_z1 = body_h - grip_margin;

// A frustum standing out of the wall along +y: its base (the 2D shape) sits
// in the wall and it shrinks by the depth at the tip, so every side slopes
// at 45 degrees.
module grip_bump() {
    hull() {
        rotate([-90, 0, 0]) translate([0, 0, -eps])
            linear_extrude(height = eps) children();
        translate([0, grip_depth - eps, 0]) rotate([-90, 0, 0])
            linear_extrude(height = eps) offset(delta = -grip_depth) children();
    }
}

module hex_2d()    { rotate(30) circle(d = grip_size / cos(30), $fn = 6); }
module square_2d() { square(grip_size, center = true); }

// Lays bumps over one straight face of width w centred at the origin.
module grip_face(w) {
    pitch_u = grip_size + grip_gap;
    hexes   = grip_style == "honeycomb";
    pitch_z = hexes ? pitch_u * cos(30) : pitch_u;
    usable  = w - grip_size;
    rows    = floor((grip_z1 - grip_z0 - grip_size) / pitch_z) + 1;
    cols    = floor(usable / pitch_u) + 1;
    z_start = (grip_z0 + grip_z1) / 2 - (rows - 1) * pitch_z / 2;
    for (j = [0 : rows - 1]) {
        shift = hexes && j % 2 == 1 ? pitch_u / 2 : 0;
        n     = hexes && j % 2 == 1 ? cols - 1 : cols;
        for (i = [0 : n - 1])
            translate([-(cols - 1) * pitch_u / 2 + i * pitch_u + shift, 0,
                       z_start + j * pitch_z])
                grip_bump() {
                    if (hexes) hex_2d(); else square_2d();
                }
    }
}

// Bumps go on the four straight faces; the rounded corners stay smooth.
module grip_bumps() {
    fx_w = plate_y - 2 * plate_r;
    fy_w = plate_x - 2 * plate_r;
    for (sx = [-1, 1])
        translate([sx * plate_x / 2, 0, 0]) rotate(sx > 0 ? -90 : 90) grip_face(fx_w);
    for (sy = [-1, 1])
        translate([0, sy * plate_y / 2, 0]) rotate(sy > 0 ? 0 : 180) grip_face(fy_w);
}

// Ribs run right round the body, corners included: each is a band that
// steps out at 45 degrees, runs flat, and steps back in at 45 degrees.
module grip_ribs() {
    flat  = max(0.4, grip_size - 2 * grip_depth);
    rib_h = 2 * grip_depth + flat;
    pitch = rib_h + grip_gap;
    n     = floor((grip_z1 - grip_z0 - rib_h) / pitch) + 1;
    z_start = (grip_z0 + grip_z1) / 2 - ((n - 1) * pitch + rib_h) / 2;
    for (j = [0 : n - 1])
        translate([0, 0, z_start + j * pitch])
            hull() {
                rbox(plate_x, plate_y, eps, plate_r);
                translate([0, 0, grip_depth])
                    rbox(plate_x + 2 * grip_depth, plate_y + 2 * grip_depth, flat,
                         plate_r + grip_depth);
                translate([0, 0, rib_h - eps]) rbox(plate_x, plate_y, eps, plate_r);
            }
}

module grip() {
    if (grip_depth <= 0) { }
    else if (grip_style == "ribs") grip_ribs();
    else if (grip_style != "none") grip_bumps();
}

// ---- desk stand: the head -----------------------------------------------

lock_y     = plate_y / 2 + pocket_clear + pocket_wall / 2;
lock_z     = pocket_h / 2;
lock_pilot = shaft_table[0] * 0.8;
lock_len   = 6.5;

// A solid block inside each end wall for the lock screw to bite into.
module lock_blocks() {
    for (sy = [-1, 1])
        translate([-3, sy > 0 ? cav_y / 2 - 5 : -cav_y / 2 - eps, plate_t - eps])
            cube([6, 5 + eps, body_h - plate_t + eps]);
}

module head_holes() {
    for (sy = [-1, 1])
        translate([0, sy * (plate_y / 2 + eps), lock_z])
            rotate([sy * 90, 0, 0]) cylinder(d = lock_pilot, h = lock_len);
    // The battery's wires come up through the floor beside the board's BAT socket.
    translate([-10, -3, -eps]) linear_extrude(height = total_h)
        hull() for (dx = [-1.5, 1.5]) translate([dx, 0]) circle(d = 5);
}

module back_plate() {
    difference() {
        union() {
            difference() {
                union() {
                    solid_body();
                    if (!desk) grip();
                }
                battery_cavity();
            }
            if (desk) lock_blocks();
        }
        screw_holes();
        if (desk) head_holes();
    }
}

// ---- desk stand: the box ------------------------------------------------

// Everything the box has, seen from above: the head's outline squeezed by
// cos(angle) front to back.
module box_outline(grow = 0) {
    offset(r = grow) scale([sc, 1]) rrect(2 * p_out_x, 2 * p_out_y, p_out_r);
}

module below_head(extra = 0) {
    multmatrix(m_head) translate([-100, -100, -200]) cube([200, 200, 200 + extra]);
}

module box_prism() {
    h = box_back_h + 1;
    if (edge_chamfer > 0.05)
        hull() {
            linear_extrude(height = eps) offset(delta = -edge_chamfer) box_outline();
            translate([0, 0, edge_chamfer]) linear_extrude(height = h - edge_chamfer) box_outline();
        }
    else
        linear_extrude(height = h) box_outline();
}

// The same bands as the plate's ribs, stopping below the box's lowest top edge.
module box_ribs() {
    flat  = max(0.4, grip_size - 2 * grip_depth);
    rib_h = 2 * grip_depth + flat;
    pitch = rib_h + grip_gap;
    z0    = grip_margin;
    z1    = box_front_h - pocket_wall - grip_margin;
    n     = floor((z1 - z0 - rib_h) / pitch) + 1;
    z_start = (z0 + z1) / 2 - ((n - 1) * pitch + rib_h) / 2;
    if (n > 0)
        for (j = [0 : n - 1])
            translate([0, 0, z_start + j * pitch])
                hull() {
                    linear_extrude(height = eps) box_outline();
                    translate([0, 0, grip_depth])
                        linear_extrude(height = flat) box_outline(grip_depth);
                    translate([0, 0, rib_h - eps]) linear_extrude(height = eps) box_outline();
                }
}

// Stands on the head's plane and holds the head's edge; open along the top
// edge so the USB-C plug and the buttons stay clear.
module pocket_ring() {
    difference() {
        translate([0, 0, -eps]) linear_extrude(height = pocket_h + eps)
            difference() {
                rrect(2 * p_out_x, 2 * p_out_y, p_out_r);
                rrect(plate_x + 2 * pocket_clear, plate_y + 2 * pocket_clear, plate_r + pocket_clear);
            }
        if (open_top)
            translate([plate_x / 2 - 1, -(plate_y / 2 - plate_r), -1])
                cube([pocket_wall + 5, plate_y - 2 * plate_r, pocket_h + 2]);
        for (sy = [-1, 1])
            translate([0, sy * lock_y, lock_z]) rotate([90, 0, 0])
                cylinder(d = shaft_d, h = pocket_wall + 2, center = true);
    }
}

module stand_box() {
    difference() {
        union() {
            intersection() {
                union() {
                    box_prism();
                    if (grip_depth > 0 && grip_style != "none") box_ribs();
                }
                below_head();
            }
            multmatrix(m_head) pocket_ring();
        }
        // Open to the head, whose back is the lid.
        intersection() {
            translate([0, 0, box_floor])
                linear_extrude(height = box_back_h) scale([sc, 1]) rrect(2 * bhx, 2 * bhy, bhr);
            below_head(1);
        }
    }
}

if (show_part) {
    if (part == "box" || part == "stand") stand_box();
    if (part == "stand") multmatrix(m_head) back_plate();
    if (part == "plate" || part == "both") back_plate();
    if (part == "plugs" || part == "both")
        translate([part == "both" ? plate_x/2 + 6 : 0, 0, 0]) plugs();
}

if (show_battery && (part == "box" || part == "stand"))
    color("green", 0.35)
        translate([cell_x0, -fy/2, box_floor + tape_t])
            cube([fx, cell_y, cell_h]);
else if (show_battery && part != "plugs" && !desk)
    color("green", 0.35)
        translate([-fx/2, -fy/2, plate_t + tape_t])
            cube([fx, cell_y, cell_h]);


// =====================================================================
// PRINTING NOTES
//   Orientation : flat face down on the bed, rim upward. No supports.
//   Layer       : 0.2 mm
//   Walls       : 3 perimeters or more
//   Infill      : 40% or more
//   Material    : PETG or ABS if it may sit in a warm car; PLA softens
//   First print : check the screw holes line up before printing a final
// =====================================================================
