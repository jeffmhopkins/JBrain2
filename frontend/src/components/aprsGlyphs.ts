// The APRS symbol set, drawn as house glyphs.
//
// A station chooses its own icon and transmits it as two characters. There are ~188 of
// them, and the canonical artwork cannot be used here: `hessu/aprs-symbols` ships no
// LICENSE, marks 69 entries "Licensing: Unknown", carries vendor logos its own copyright
// notice says to check for yourself, and is full-colour raster with drop shadows —
// illegible on this app's background. So these are drawn, in the same idiom as
// `icons.tsx`: 24x24, no fill, `currentColor` at 1.5. They theme themselves and cost no
// request.
//
// All 164 codes with a standard meaning are here, plus legacy `/z` and the fallback.
// Eleven families share their geometry — one cloud, one wheelbase, one head circle — so
// only the DIFFERENCE has to be read at 20px.
//
// Vendor logos are deliberately NOT reproduced: `/M`, `/Z`, `/x`, `\\K`, `\\Y` and `\\R`
// get a generic device, and the brand arrives as the overlay character it is transmitted
// as anyway. Eight drawings are shared by two codes, in every case where the deployed
// sets share them too; the label always separates them.
//
// **Data, not markup.** A glyph is an array of typed nodes rather than an SVG string, so
// nothing here needs `dangerouslySetInnerHTML` and a malformed shape is a type error
// rather than a silently blank icon.
//
// See `docs/research/APRS_ICON_SET.md` for the family system, the validation and the
// near-collisions that survive.

export interface GlyphNode {
  t: "path" | "circle" | "rect" | "text";
  a: Record<string, string>;
  /** Literal text, for the handful of glyphs that carry a character of their own. */
  c?: string;
}

export const APRS_GLYPHS: Record<string, GlyphNode[]> = {
  "/!": [
    { t: "path", a: { d: "M12 2.6 5 5.2v6.6c0 4.1 2.9 6.9 7 8.6 4.1-1.7 7-4.5 7-8.6V5.2Z" } },
    {
      t: "path",
      a: { d: "m12 8.4 1.4 2.8 3.1.5-2.2 2.2.5 3.1-2.8-1.5-2.8 1.5.5-3.1-2.2-2.2 3.1-.5Z" },
    },
  ], // Police / sheriff
  "/#": [{ t: "path", a: { d: "M12 2 16.6 7.4 22 12 16.6 16.6 12 22 7.4 16.6 2 12 7.4 7.4Z" } }], // Digipeater
  "/$": [
    {
      t: "path",
      a: {
        d: "M6.6 3.5 9.4 3a1.4 1.4 0 0 1 1.5.8l1 2.3a1.4 1.4 0 0 1-.4 1.7l-1.4 1.1a11 11 0 0 0 4.6 4.6l1.1-1.4a1.4 1.4 0 0 1 1.7-.4l2.3 1a1.4 1.4 0 0 1 .8 1.5l-.5 2.8a1.4 1.4 0 0 1-1.4 1.2A15 15 0 0 1 5.4 4.9a1.4 1.4 0 0 1 1.2-1.4Z",
      },
    },
  ], // Phone
  "/%": [
    { t: "circle", a: { cx: "12", cy: "12", r: "2.5" } },
    { t: "circle", a: { cx: "4.4", cy: "6.2", r: "1.6" } },
    { t: "circle", a: { cx: "19.6", cy: "6.2", r: "1.6" } },
    { t: "circle", a: { cx: "4.4", cy: "17.8", r: "1.6" } },
    { t: "circle", a: { cx: "19.6", cy: "17.8", r: "1.6" } },
    {
      t: "path",
      a: { d: "m5.8 7.2 4.3 3.3M18.2 7.2l-4.3 3.3M5.8 16.8l4.3-3.3M18.2 16.8l-4.3-3.3" },
    },
  ], // DX cluster
  "/&": [
    { t: "path", a: { d: "M12 2.5 21.5 12 12 21.5 2.5 12Z" } },
    { t: "path", a: { d: "M7.9 12.5c1.05-1.8 2.1-1.8 3.15 0s2.1 1.8 3.15 0" } },
  ], // HF gateway
  "/'": [
    {
      t: "path",
      a: {
        d: "M12 2.8c.8 0 1.3 1.2 1.3 3v3.4h7.4v2.2h-7.4v4.3l2.2 1.7v1.5L12 18.2l-3.5.7v-1.5l2.2-1.7v-4.3H3.3V9.2h7.4V5.8c0-1.8.5-3 1.3-3Z",
      },
    },
    { t: "path", a: { d: "M9.5 3.4h5" } },
  ], // Small aircraft
  "/(": [
    { t: "path", a: { d: "M4.5 8.5a6.5 6.5 0 0 0 8.2 8.2Z" } },
    { t: "path", a: { d: "M8 13 11.2 9.8" } },
    { t: "path", a: { d: "M14 9.6a4.2 4.2 0 0 0-4.2-4.2" } },
    { t: "path", a: { d: "M17.6 9.2A7.8 7.8 0 0 0 9.8 1.4" } },
    { t: "circle", a: { cx: "6.5", cy: "20", r: "1.5" } },
    { t: "circle", a: { cx: "15.5", cy: "20", r: "1.5" } },
    { t: "path", a: { d: "M6.5 18.5h9" } },
  ], // Mobile satellite station
  "/)": [
    { t: "circle", a: { cx: "10.5", cy: "15.8", r: "5" } },
    { t: "circle", a: { cx: "15", cy: "4.6", r: "1.7" } },
    { t: "path", a: { d: "M13.6 8.4h-3.4a1.7 1.7 0 0 0-1.7 1.9l.6 4.3h4.6l2.6 4.6h2.8" } },
  ], // Wheelchair (accessible)
  "/*": [
    { t: "rect", a: { height: "4.4", rx: "2.2", width: "10.5", x: "3.5", y: "13.8" } },
    { t: "path", a: { d: "M5.8 13.8 8.6 9h4.6l2.6 4.8" } },
    { t: "path", a: { d: "M13.2 9 15.8 6.2h2.4" } },
    { t: "path", a: { d: "m16.4 13.6 1 5" } },
    { t: "path", a: { d: "M14 18.6h5.4c1 0 1.6-.6 1.8-1.6" } },
  ], // Snowmobile
  "/+": [{ t: "path", a: { d: "M9.2 4h5.6v5.2H20v5.6h-5.2V20H9.2v-5.2H4V9.2h5.2Z" } }], // Red Cross
  "/,": [
    {
      t: "path",
      a: { d: "M12 3c1.6 2.4 2.2 4.6 2.2 6.4S13 12.4 12 13.4c-1-1-2.2-2.2-2.2-4S10.4 5.4 12 3Z" },
    },
    { t: "path", a: { d: "M9.8 9.4c-2-1.4-4.2-.6-4.6 1.4-.5 2.4 2 4.2 6.8 4.4" } },
    { t: "path", a: { d: "M14.2 9.4c2-1.4 4.2-.6 4.6 1.4.5 2.4-2 4.2-6.8 4.4" } },
    { t: "path", a: { d: "M8.6 16.6h6.8" } },
    { t: "path", a: { d: "M12 15.2v5.4" } },
  ], // Boy Scouts
  "/-": [
    { t: "path", a: { d: "M3 10.5 12 3.5l9 7V20a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1Z" } },
    { t: "path", a: { d: "M9.5 21v-6h5v6" } },
  ], // House (VHF home station)
  "/.": [{ t: "path", a: { d: "M5 5 19 19M19 5 5 19" } }], // X
  "//": [
    { t: "circle", a: { cx: "12", cy: "12", r: "7" } },
    { t: "circle", a: { cx: "12", cy: "12", fill: "currentColor", r: "2.4", stroke: "none" } },
  ], // Red dot
  "/0": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "0",
    },
  ], // Numbered circle 0
  "/1": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "1",
    },
  ], // Numbered circle 1
  "/2": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "2",
    },
  ], // Numbered circle 2
  "/3": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "3",
    },
  ], // Numbered circle 3
  "/4": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "4",
    },
  ], // Numbered circle 4
  "/5": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "5",
    },
  ], // Numbered circle 5
  "/6": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "6",
    },
  ], // Numbered circle 6
  "/7": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "7",
    },
  ], // Numbered circle 7
  "/8": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "8",
    },
  ], // Numbered circle 8
  "/9": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    {
      t: "text",
      a: {
        fill: "currentColor",
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        fontSize: "10.5",
        fontWeight: "600",
        stroke: "none",
        textAnchor: "middle",
        x: "12",
        y: "15.7",
      },
      c: "9",
    },
  ], // Numbered circle 9
  "/:": [
    {
      t: "path",
      a: {
        d: "M12 20.8a5.8 5.8 0 0 0 5.8-5.8c0-3.9-2.9-5.3-2.9-8.7-2.9 1.5-4.4 3.9-4.4 6.3 0 1-1 1.5-1.4.8a2.9 2.9 0 0 1-.6-2.2A6.7 6.7 0 0 0 6.2 15a5.8 5.8 0 0 0 5.8 5.8Z",
      },
    },
  ], // Fire
  "/;": [
    { t: "path", a: { d: "M12 5 3.5 20h17Z" } },
    { t: "path", a: { d: "m12 11.4 4.8 8.6" } },
    { t: "path", a: { d: "M2 20h20" } },
    { t: "path", a: { d: "M12 5V2.8" } },
  ], // Campground / portable operation
  "/<": [
    { t: "circle", a: { cx: "5.5", cy: "16.6", r: "3.1" } },
    { t: "circle", a: { cx: "18.5", cy: "16.6", r: "3.1" } },
    { t: "path", a: { d: "M5.5 16.6h3l1.2-4.4h4.6l2.2 4.4" } },
    { t: "path", a: { d: "M9.7 12.2 8 9.4H6" } },
    { t: "path", a: { d: "M14.6 12.2h-4.4" } },
    { t: "path", a: { d: "m16.4 12.2 2-3h1.6" } },
  ], // Motorcycle
  "/=": [
    { t: "path", a: { d: "M3.5 16.5V7.5h6v9" } },
    { t: "path", a: { d: "M9.5 16.5v-6h10.5v6" } },
    { t: "path", a: { d: "M3.5 16.5h16.5" } },
    { t: "path", a: { d: "M17 10.5V8h2.2v2.5" } },
    { t: "path", a: { d: "M5 9.6h3v2.6H5Z" } },
    { t: "circle", a: { cx: "6", cy: "18.4", r: "1.5" } },
    { t: "circle", a: { cx: "12.5", cy: "18.4", r: "1.5" } },
    { t: "circle", a: { cx: "17.5", cy: "18.4", r: "1.5" } },
  ], // Railroad engine
  "/>": [
    {
      t: "path",
      a: {
        d: "M4 15.5h16v-2.4a1.6 1.6 0 0 0-1-1.5l-2.2-.9-1.9-2.8a2 2 0 0 0-1.7-.9h-2.4a2 2 0 0 0-1.7.9L7.2 10.7l-2.2.9A1.6 1.6 0 0 0 4 13.1Z",
      },
    },
    { t: "circle", a: { cx: "7.6", cy: "17.4", r: "1.8" } },
    { t: "circle", a: { cx: "16.4", cy: "17.4", r: "1.8" } },
  ], // Car
  "/?": [
    { t: "rect", a: { height: "7", rx: "1.5", width: "18", x: "3", y: "4" } },
    { t: "rect", a: { height: "7", rx: "1.5", width: "18", x: "3", y: "13" } },
    { t: "path", a: { d: "M6.5 7.5h.01M6.5 16.5h.01" } },
  ], // File server
  "/@": [
    { t: "circle", a: { cx: "15", cy: "8.5", r: "1.4" } },
    { t: "path", a: { d: "M15 7.1c0-2.7 1.3-4.9 4.4-4.9-2 1.3-2.9 2.8-3 4.9" } },
    { t: "path", a: { d: "M15 9.9c0 2.7-1.3 4.9-4.4 4.9 2-1.3 2.9-2.8 3-4.9" } },
    { t: "path", a: { d: "M2.5 21c1.4-3.4 3.6-6.2 6.6-8.2", strokeDasharray: "2.6 2.4" } },
  ], // Hurricane predicted path
  "/A": [
    { t: "rect", a: { height: "17", rx: "3", width: "17", x: "3.5", y: "3.5" } },
    { t: "path", a: { d: "M12 8v8M8 12h8" } },
  ], // Aid station
  "/B": [
    { t: "rect", a: { height: "14", rx: "1.5", width: "18", x: "3", y: "3.5" } },
    { t: "path", a: { d: "M12 17.5v3.5" } },
    { t: "path", a: { d: "M6.5 7.5h5M6.5 11h8M6.5 14.5h4" } },
  ], // BBS
  "/C": [
    { t: "path", a: { d: "M2.5 12.5c2.5 5 16.5 5 19 0" } },
    { t: "path", a: { d: "M2.5 12.5c2.5-1.7 16.5-1.7 19 0" } },
    { t: "path", a: { d: "m9.5 5.5 4 7" } },
    { t: "path", a: { d: "M8.6 3.3 6.4 6.6l3.4 1.2Z" } },
  ], // Canoe
  "/E": [
    { t: "path", a: { d: "M2.5 12s3.6-6 9.5-6 9.5 6 9.5 6-3.6 6-9.5 6-9.5-6-9.5-6Z" } },
    { t: "circle", a: { cx: "12", cy: "12", r: "2.6" } },
  ], // Eyeball (live event)
  "/F": [
    { t: "circle", a: { cx: "16.8", cy: "16", r: "4.6" } },
    { t: "circle", a: { cx: "5.5", cy: "18", r: "2.6" } },
    { t: "path", a: { d: "M2.8 14.5V9.5h5l1.6 4" } },
    { t: "path", a: { d: "M7.8 9.5V6.4h4.4l1.1 3.1" } },
    { t: "path", a: { d: "M9.4 13.5h6.2" } },
  ], // Farm vehicle / tractor
  "/G": [
    { t: "rect", a: { height: "17", rx: "1", width: "17", x: "3.5", y: "3.5" } },
    { t: "path", a: { d: "M9.2 3.5v17M14.8 3.5v17M3.5 9.2h17M3.5 14.8h17" } },
  ], // Grid square (6 character)
  "/H": [
    { t: "path", a: { d: "M3 19v-9" } },
    { t: "path", a: { d: "M3 14h18v5" } },
    { t: "path", a: { d: "M21 19v-5a3 3 0 0 0-3-3h-7v3" } },
    { t: "circle", a: { cx: "7", cy: "11.4", r: "2" } },
  ], // Hotel
  "/I": [
    { t: "rect", a: { height: "5", rx: "1", width: "7", x: "8.5", y: "3" } },
    { t: "rect", a: { height: "5", rx: "1", width: "7", x: "2", y: "16" } },
    { t: "rect", a: { height: "5", rx: "1", width: "7", x: "15", y: "16" } },
    { t: "path", a: { d: "M12 8v4M5.5 16v-4h13v4" } },
  ], // TCP/IP network station
  "/K": [
    { t: "path", a: { d: "M4 20.5v-8.8l8-5 8 5v8.8Z" } },
    { t: "path", a: { d: "M12 6.7V2.6l3.6 1.3L12 5.2" } },
    { t: "path", a: { d: "M9.5 20.5v-5h5v5" } },
  ], // School
  "/L": [
    { t: "rect", a: { height: "12.5", rx: "2", width: "19", x: "2.5", y: "4" } },
    { t: "path", a: { d: "M8.5 20.8h7M12 16.5v4.3" } },
    { t: "circle", a: { cx: "12", cy: "8.6", r: "1.8" } },
    { t: "path", a: { d: "M8.8 13.4a3.6 3.6 0 0 1 6.4 0" } },
  ], // Logged-on PC user
  "/M": [
    { t: "rect", a: { height: "12.5", rx: "2", width: "19", x: "2.5", y: "4" } },
    { t: "path", a: { d: "M8.5 20.8h7M12 16.5v4.3" } },
  ], // MacAPRS
  "/N": [
    { t: "rect", a: { height: "9.5", rx: "1.5", width: "13.5", x: "2", y: "7" } },
    { t: "path", a: { d: "m2 8.4 6.75 4.2L15.5 8.4" } },
    { t: "path", a: { d: "M17.8 8.2 21.3 11.8 17.8 15.4" } },
  ], // NTS station
  "/O": [
    {
      t: "path",
      a: { d: "M12 15.4c3.6 0 6.5-3.2 6.5-7A6.5 6.5 0 0 0 5.5 8.4c0 3.8 2.9 7 6.5 7Z" },
    },
    { t: "path", a: { d: "M10.6 15.2 12 17.7l1.4-2.5" } },
    { t: "rect", a: { height: "3.2", rx: "0.6", width: "3.8", x: "10.1", y: "17.7" } },
  ], // Balloon
  "/P": [
    {
      t: "path",
      a: {
        d: "M4 15.5h16v-2.4a1.6 1.6 0 0 0-1-1.5l-2.2-.9-1.9-2.8a2 2 0 0 0-1.7-.9h-2.4a2 2 0 0 0-1.7.9L7.2 10.7l-2.2.9A1.6 1.6 0 0 0 4 13.1Z",
      },
    },
    { t: "circle", a: { cx: "7.6", cy: "17.4", r: "1.8" } },
    { t: "circle", a: { cx: "16.4", cy: "17.4", r: "1.8" } },
    { t: "rect", a: { height: "2", rx: "0.7", width: "5.2", x: "9.4", y: "5.4" } },
  ], // Police car
  "/R": [
    { t: "path", a: { d: "M2 5.5h13.5v11H2Z" } },
    { t: "path", a: { d: "M15.5 8.5h3.2l2.8 3.6v4.4h-6Z" } },
    { t: "path", a: { d: "M4.2 8h6.4v3.2H4.2Z" } },
    { t: "circle", a: { cx: "6.8", cy: "18.2", r: "1.6" } },
    { t: "circle", a: { cx: "17.6", cy: "18.2", r: "1.6" } },
  ], // Recreational vehicle
  "/S": [
    { t: "path", a: { d: "M12 2.5c2.4 2.7 3.4 6 3.4 9.3V16H8.6v-4.2C8.6 8.5 9.6 5.2 12 2.5Z" } },
    { t: "path", a: { d: "M8.6 13.5 5.2 17.6V20l3.4-2M15.4 13.5l3.4 4.1V20l-3.4-2" } },
    { t: "path", a: { d: "M12 16v4" } },
    { t: "circle", a: { cx: "12", cy: "8", r: "1.1" } },
  ], // Space shuttle
  "/T": [
    { t: "rect", a: { height: "13", rx: "2", width: "19", x: "2.5", y: "4" } },
    { t: "path", a: { d: "m5.6 14 3.4-4 2.5 2.6 3-3.5 4 4.9" } },
    { t: "circle", a: { cx: "8", cy: "7.6", r: "1.2" } },
    { t: "path", a: { d: "M9 20.8h6" } },
  ], // SSTV
  "/U": [
    { t: "rect", a: { height: "12", rx: "2", width: "17", x: "3.5", y: "4" } },
    { t: "path", a: { d: "M3.5 8.5h17" } },
    { t: "path", a: { d: "M12 8.5V16" } },
    { t: "circle", a: { cx: "7.5", cy: "18.2", r: "1.6" } },
    { t: "circle", a: { cx: "16.5", cy: "18.2", r: "1.6" } },
  ], // Bus
  "/V": [
    { t: "rect", a: { height: "8.5", rx: "1.5", width: "12.5", x: "2", y: "8" } },
    { t: "path", a: { d: "m14.5 12.5 5.5-3.5v8.5l-5.5-3.5Z" } },
    { t: "path", a: { d: "M6 8V4.4" } },
    { t: "path", a: { d: "m4 3 2 1.4 2-1.4" } },
  ], // ATV (amateur television)
  "/W": [
    { t: "path", a: { d: "M7 10.5a5 5 0 0 1 10 0Z" } },
    { t: "path", a: { d: "M9 10.5 8 20.5M15 10.5l1 10" } },
    { t: "path", a: { d: "M8.5 15h7" } },
    { t: "path", a: { d: "M6.6 20.5h10.8" } },
  ], // National Weather Service site
  "/X": [
    { t: "path", a: { d: "M3.5 4.6h17" } },
    { t: "path", a: { d: "M10.5 4.6v3" } },
    {
      t: "path",
      a: {
        d: "M4.6 12.8a5.2 5.2 0 0 1 5.2-5.2h1.4c2.6 0 4.4 1.8 5.2 4.2l4.6.8v1.8h-5.2a5.2 5.2 0 0 1-5.2 3.6H9.8a5.2 5.2 0 0 1-5.2-5.2Z",
      },
    },
    { t: "path", a: { d: "M5.4 19.4h9" } },
    { t: "path", a: { d: "M7.2 17.4v2M12.2 17.4v2" } },
  ], // Helicopter
  "/Y": [
    { t: "path", a: { d: "M2.5 17c2.6 3.2 16.4 3.2 19 0" } },
    { t: "path", a: { d: "M12.6 3.5v12" } },
    { t: "path", a: { d: "M11 15.5H4.6L11 6Z" } },
    { t: "path", a: { d: "M14.2 15.5h5L14.2 8.4Z" } },
  ], // Yacht (sailboat)
  "/Z": [
    { t: "rect", a: { height: "12.5", rx: "2", width: "19", x: "2.5", y: "4" } },
    { t: "path", a: { d: "M8.5 20.8h7M12 16.5v4.3" } },
    { t: "path", a: { d: "M6.6 7.4h10.8v6.2H6.6Z" } },
    { t: "path", a: { d: "M6.6 9.5h10.8" } },
  ], // WinAPRS
  "/[": [
    { t: "circle", a: { cx: "12", cy: "7.2", r: "3.2" } },
    { t: "path", a: { d: "M4.8 20.5a7.2 7.2 0 0 1 14.4 0" } },
  ], // Person
  "/\\": [
    { t: "path", a: { d: "M12 3 21 20H3Z" } },
    { t: "path", a: { d: "M12 3v17" } },
  ], // DF triangle
  "/]": [
    { t: "rect", a: { height: "13", rx: "2", width: "19", x: "2.5", y: "5.5" } },
    { t: "path", a: { d: "m3 7 9 6.5L21 7" } },
  ], // Mail / post office
  "/^": [
    {
      t: "path",
      a: {
        d: "M12 2.4c1.1 0 1.9 1.7 1.9 4.1v2.2l7.6 5.1v2.3l-7.6-2.6v3.6l2.3 2v1.6L12 19.6l-4.2 1.1v-1.6l2.3-2v-3.6L2.5 16.1v-2.3l7.6-5.1V6.5c0-2.4.8-4.1 1.9-4.1Z",
      },
    },
  ], // Large aircraft
  "/_": [
    { t: "path", a: { d: "M14 14.8V5a2 2 0 1 0-4 0v9.8a4 4 0 1 0 4 0Z" } },
    { t: "path", a: { d: "M10 8h2M10 11h2" } },
  ], // Weather station
  "/`": [
    { t: "path", a: { d: "M4 11a7 7 0 0 0 9 9Z" } },
    { t: "path", a: { d: "M9 16l3.5-3.5" } },
    { t: "path", a: { d: "M15 12a4 4 0 0 0-4-4" } },
    { t: "path", a: { d: "M19 12a8 8 0 0 0-8-8" } },
  ], // Dish antenna
  "/a": [
    { t: "rect", a: { height: "10", rx: "1", width: "12", x: "2", y: "6" } },
    { t: "path", a: { d: "M14 9.5h3.4l3.1 3.4V16H14Z" } },
    { t: "circle", a: { cx: "6", cy: "17.6", r: "1.7" } },
    { t: "circle", a: { cx: "18", cy: "17.6", r: "1.7" } },
    { t: "path", a: { d: "M8 8.6v4.8M5.6 11h4.8" } },
  ], // Ambulance
  "/b": [
    { t: "circle", a: { cx: "5.5", cy: "17", r: "3.6" } },
    { t: "circle", a: { cx: "18.5", cy: "17", r: "3.6" } },
    { t: "path", a: { d: "M5.5 17h4l3-6.5 3 6.5h3" } },
    { t: "path", a: { d: "M9.4 10.5h4.6" } },
    { t: "path", a: { d: "m12.5 10.5 2.5-4h2" } },
  ], // Bicycle
  "/c": [
    { t: "path", a: { d: "M6 3v18" } },
    { t: "path", a: { d: "M6 4.4h11.5L14.6 8l2.9 3.6H6Z" } },
  ], // Incident command post
  "/d": [
    { t: "path", a: { d: "M3 20.5V11l9-6.5 9 6.5v9.5Z" } },
    { t: "path", a: { d: "M8 20.5V15h8v5.5" } },
    { t: "path", a: { d: "M12 6.8c1.4 1.5 2 2.4 2 3.4a2 2 0 0 1-4 0c0-1 .6-1.9 2-3.4Z" } },
  ], // Fire station
  "/e": [
    {
      t: "path",
      a: {
        d: "M8 20.5c0-4 1-6.4 3.4-8.2l-1-2.2-2.6 1.4 1.2-3.6L12.4 5.4V2.9l3 2.7c2.6 1.2 3.6 3.8 3.6 6.9 0 3.2-1.2 5.6-1.2 8Z",
      },
    },
    { t: "circle", a: { cx: "14.6", cy: "7.6", fill: "currentColor", r: "0.7", stroke: "none" } },
  ], // Horse / equestrian
  "/f": [
    { t: "rect", a: { height: "9", rx: "1", width: "11.5", x: "2", y: "7" } },
    { t: "path", a: { d: "M13.5 9.5h3.6l3.4 3.5V16h-7Z" } },
    { t: "circle", a: { cx: "5.8", cy: "18", r: "1.7" } },
    { t: "circle", a: { cx: "16.6", cy: "18", r: "1.7" } },
    { t: "path", a: { d: "M2.8 5.6 12.6 3" } },
    { t: "path", a: { d: "M4.4 5.4 3.9 3.4M7 4.7 6.5 2.7M9.6 4 9.1 2" } },
  ], // Fire truck
  "/g": [
    {
      t: "path",
      a: {
        d: "M12 3.6c.7 0 1.1 1 1.1 2.6v4.3l8.4 1.6v1.6l-8.4-.9v4.3l1.9 1.7v1.2L12 19.4l-3 .6v-1.2l1.9-1.7v-4.3l-8.4.9v-1.6l8.4-1.6V6.2c0-1.6.4-2.6 1.1-2.6Z",
      },
    },
  ], // Glider
  "/h": [
    { t: "rect", a: { height: "14", rx: "1.5", width: "16", x: "4", y: "6.5" } },
    { t: "path", a: { d: "M12 9.6v5M9.5 12.1h5" } },
    { t: "path", a: { d: "M8.6 20.5V17h6.8v3.5" } },
  ], // Hospital
  "/i": [
    { t: "path", a: { d: "M3 19c2 1.8 4.6 1.8 6.6 0s4.6-1.8 6.6 0 3.3 1.4 4.8 0" } },
    { t: "path", a: { d: "M6.5 16c1.3-2.6 3.2-4 5.5-4s4.2 1.4 5.5 4Z" } },
    { t: "path", a: { d: "M12 12V7" } },
    { t: "path", a: { d: "M12 7c-1.4-1.5-3.4-1.5-4.4 0M12 7c1.4-1.5 3.4-1.5 4.4 0" } },
  ], // IOTA (islands on the air)
  "/j": [
    { t: "path", a: { d: "M2.5 15.5v-3.2h19v3.2Z" } },
    { t: "path", a: { d: "M5 12.3 7.5 7h8l2.5 5.3" } },
    { t: "path", a: { d: "M11.5 7v5.3" } },
    { t: "circle", a: { cx: "6.8", cy: "17.6", r: "2" } },
    { t: "circle", a: { cx: "17.2", cy: "17.6", r: "2" } },
  ], // Jeep
  "/k": [
    { t: "path", a: { d: "M2 5.5h11v10H2z" } },
    { t: "path", a: { d: "M13 9h3.8l3.2 3.3v3.2h-7z" } },
    { t: "circle", a: { cx: "6.5", cy: "17.6", r: "1.8" } },
    { t: "circle", a: { cx: "16.8", cy: "17.6", r: "1.8" } },
  ], // Truck
  "/l": [
    { t: "rect", a: { height: "10.5", rx: "1.5", width: "16", x: "4", y: "5" } },
    { t: "path", a: { d: "M2 18.5h20a2 2 0 0 0-2-3H4a2 2 0 0 0-2 3Z" } },
  ], // Laptop
  "/m": [
    { t: "path", a: { d: "M12 8.5V20" } },
    { t: "path", a: { d: "M8.5 20 12 12l3.5 8" } },
    { t: "path", a: { d: "M8.6 3.6a5 5 0 0 0 0 6.8M15.4 3.6a5 5 0 0 1 0 6.8" } },
    { t: "rect", a: { height: "5.2", rx: "1.4", width: "2.8", x: "10.6", y: "2.6" } },
  ], // Mic-E repeater
  "/n": [
    { t: "circle", a: { cx: "12", cy: "12", r: "8.5" } },
    { t: "circle", a: { cx: "12", cy: "12", r: "4.5" } },
    { t: "circle", a: { cx: "12", cy: "12", fill: "currentColor", r: "1.3", stroke: "none" } },
  ], // Node
  "/o": [
    { t: "path", a: { d: "M4 20.5v-9l8-4.5 8 4.5v9Z" } },
    { t: "path", a: { d: "M12 7V3.2" } },
    { t: "path", a: { d: "M9.8 3.8a3.6 3.6 0 0 1 4.4 0" } },
    { t: "path", a: { d: "M9.6 20.5V16h4.8v4.5" } },
  ], // Emergency operations centre
  "/p": [
    { t: "path", a: { d: "M6.5 9.6c0-3 2.5-5.2 5.5-5.2s5.5 2.2 5.5 5.2v2.8a5.5 5.5 0 0 1-11 0Z" } },
    { t: "path", a: { d: "M6.6 9.8C4.6 8.8 4 6.2 5 4.6c1.5.4 2.6 1.5 3.2 2.8" } },
    { t: "path", a: { d: "M17.4 9.8c2-1 2.6-3.6 1.6-5.2-1.5.4-2.6 1.5-3.2 2.8" } },
    { t: "circle", a: { cx: "9.8", cy: "11.4", fill: "currentColor", r: "0.8", stroke: "none" } },
    { t: "circle", a: { cx: "14.2", cy: "11.4", fill: "currentColor", r: "0.8", stroke: "none" } },
    { t: "path", a: { d: "M12 13.8v1.4" } },
    { t: "path", a: { d: "M9.9 15a2.6 2.6 0 0 0 4.2 0" } },
  ], // Dog / rover
  "/q": [
    { t: "rect", a: { height: "17", rx: "1", width: "17", x: "3.5", y: "3.5" } },
    { t: "path", a: { d: "M12 3.5v17M3.5 12h17" } },
    { t: "path", a: { d: "m13.6 18.4 2.6-3.6 2.6 3.6Z" } },
  ], // Grid square (above 128 m)
  "/r": [
    { t: "circle", a: { cx: "12", cy: "6", r: "1.4" } },
    { t: "path", a: { d: "M12 7.6V20" } },
    { t: "path", a: { d: "M8.5 20 12 11.5 15.5 20" } },
    { t: "path", a: { d: "M8.6 3.6a5 5 0 0 0 0 6.8M15.4 3.6a5 5 0 0 1 0 6.8" } },
  ], // Repeater
  "/s": [
    { t: "path", a: { d: "M2.5 15.5h19l-2.5 4.5H5Z" } },
    { t: "path", a: { d: "M5.5 15.5V11h9l2 4.5" } },
    { t: "path", a: { d: "M8.5 11V7.4h3.4V11" } },
  ], // Ship (power boat)
  "/t": [
    { t: "path", a: { d: "M2 6h20v2.2H2Z" } },
    { t: "path", a: { d: "M4 8.2V20.5M20 8.2V20.5" } },
    { t: "rect", a: { height: "8.5", rx: "1", width: "5", x: "7.5", y: "12" } },
    { t: "path", a: { d: "M9 14.2h2" } },
    { t: "path", a: { d: "M12.5 14h1.6a1.2 1.2 0 0 1 1.2 1.2v3a1.1 1.1 0 0 0 2.2 0v-2" } },
  ], // Truck stop
  "/u": [
    { t: "path", a: { d: "M2 8.5h4.2l1.8 3v4.5H2Z" } },
    { t: "path", a: { d: "M8.5 6.5h13v9.5h-13Z" } },
    { t: "circle", a: { cx: "4.6", cy: "17.6", r: "1.6" } },
    { t: "circle", a: { cx: "13.6", cy: "17.6", r: "1.6" } },
    { t: "circle", a: { cx: "18", cy: "17.6", r: "1.6" } },
  ], // Truck (18-wheeler)
  "/v": [
    { t: "path", a: { d: "M2 15.5V9.6l3-4h9.4l4.6 4v5.9Z" } },
    { t: "circle", a: { cx: "6.6", cy: "17.4", r: "1.8" } },
    { t: "circle", a: { cx: "16.4", cy: "17.4", r: "1.8" } },
    { t: "path", a: { d: "M6 9.6h5" } },
  ], // Van
  "/w": [
    {
      t: "path",
      a: { d: "M12 3.5c2.9 3.7 4.6 6.2 4.6 8.3a4.6 4.6 0 0 1-9.2 0c0-2.1 1.7-4.6 4.6-8.3Z" },
    },
    { t: "path", a: { d: "M5 20.5h14" } },
  ], // Water station
  "/x": [
    { t: "rect", a: { height: "16", rx: "2", width: "19", x: "2.5", y: "4" } },
    { t: "path", a: { d: "m7 10 2.5 2.5L7 15" } },
    { t: "path", a: { d: "M12.6 15.4h4.6" } },
  ], // X-APRS (Unix)
  "/y": [
    { t: "path", a: { d: "M4 8h16" } },
    { t: "path", a: { d: "M6 4.6v6.8M9.6 5.2v5.6M13.2 5.6v4.8M16.8 6.2v3.6" } },
    { t: "path", a: { d: "M12 8v12.6" } },
    { t: "path", a: { d: "M9 20.6h6" } },
  ], // Yagi at QTH
  "/z": [
    { t: "path", a: { d: "M2.5 11.5 12 4l9.5 7.5" } },
    { t: "path", a: { d: "M5.5 11.5V20M18.5 11.5V20" } },
    { t: "path", a: { d: "M3.5 20h17" } },
  ], // Shelter (legacy /z)
  "??": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    { t: "path", a: { d: "M5.6 5.6 18.4 18.4" } },
  ], // Unknown symbol (fallback)
  "\\!": [
    { t: "path", a: { d: "M12 3.5 21.5 20H2.5Z" } },
    { t: "path", a: { d: "M12 9.8v4.6" } },
    { t: "circle", a: { cx: "12", cy: "17.4", fill: "currentColor", r: "0.85", stroke: "none" } },
  ], // Emergency   [overlay: centre]
  "\\#": [{ t: "path", a: { d: "M12 2 16.6 7.4 22 12 16.6 16.6 12 22 7.4 16.6 2 12 7.4 7.4Z" } }], // Digipeater (green star)   [overlay: centre]
  "\\$": [
    { t: "rect", a: { height: "12", rx: "2", width: "19", x: "2.5", y: "6" } },
    { t: "path", a: { d: "M5.4 9h1.8M16.8 15h1.8" } },
  ], // Bank or ATM   [overlay: centre]
  "\\%": [
    { t: "path", a: { d: "M2.5 20V11l4.6-2.6V11l4.6-2.6V20Z" } },
    { t: "path", a: { d: "M12 20V4.6h2.9V20" } },
    { t: "path", a: { d: "M5.2 14.4h1.4M9.8 14.4h1.4" } },
  ], // Power plant   [overlay: badge]
  "\\&": [{ t: "path", a: { d: "M12 2.5 21.5 12 12 21.5 2.5 12Z" } }], // Gateway station   [overlay: centre]
  "\\'": [
    {
      t: "path",
      a: { d: "m12 3 2.2 4.6 5-.6-2.6 4.3 2.6 4.3-5-.6L12 20l-2.2-4.6-5 .6 2.6-4.3L4.8 7l5 .6Z" },
    },
  ], // Crash / incident site   [overlay: badge]
  "\\(": [
    {
      t: "path",
      a: { d: "M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z" },
    },
  ], // Cloudy   [overlay: badge]
  "\\)": [
    { t: "path", a: { d: "M2.5 20.5a10 10 0 0 1 19 0" } },
    { t: "rect", a: { height: "5", rx: "1", width: "5.6", x: "9.2", y: "7.5" } },
    { t: "path", a: { d: "M9.2 10H4.6M14.8 10h4.6" } },
    { t: "path", a: { d: "M4.6 7.6v4.8M19.4 7.6v4.8" } },
    { t: "path", a: { d: "M12 7.5V4.6" } },
  ], // Firenet MEO / MODIS Earth observation   [overlay: badge]
  "\\*": [
    { t: "path", a: { d: "M12 3.5v17.0M4.605 7.75l14.79 8.5M19.395 7.75l-14.79 8.5" } },
    { t: "path", a: { d: "m9.4 5.6 2.6 2.4 2.6-2.4M9.4 18.4l2.6-2.4 2.6 2.4" } },
  ], // Snow
  "\\+": [
    { t: "path", a: { d: "M5 20.5V11l7-4.6 7 4.6v9.5Z" } },
    { t: "path", a: { d: "M12 6.4V2.8M10.3 4.4h3.4" } },
    { t: "path", a: { d: "M10 20.5v-5h4v5" } },
  ], // Church
  "\\,": [
    { t: "circle", a: { cx: "12", cy: "6.4", r: "3" } },
    { t: "circle", a: { cx: "7.2", cy: "12", r: "3" } },
    { t: "circle", a: { cx: "16.8", cy: "12", r: "3" } },
    { t: "path", a: { d: "M12 15v5.6" } },
  ], // Girl Scouts
  "\\-": [
    { t: "path", a: { d: "M3.5 11 12 4.5l8.5 6.5V20.5h-17Z" } },
    { t: "path", a: { d: "M17.6 7.6V3" } },
    { t: "path", a: { d: "M15.6 4a3 3 0 0 1 4 0" } },
  ], // House (HF)   [overlay: centre]
  "\\.": [
    { t: "circle", a: { cx: "12", cy: "12", r: "8.5", strokeDasharray: "2.5 3" } },
    { t: "circle", a: { cx: "12", cy: "12", fill: "currentColor", r: "1.4", stroke: "none" } },
  ], // Ambiguous / indeterminate position
  "\\/": [
    { t: "circle", a: { cx: "12", cy: "12", r: "7" } },
    { t: "path", a: { d: "M12 2v3.4M12 18.6V22M2 12h3.4M18.6 12H22" } },
    { t: "circle", a: { cx: "12", cy: "12", fill: "currentColor", r: "1.3", stroke: "none" } },
  ], // Waypoint destination
  "\\0": [{ t: "circle", a: { cx: "12", cy: "12", r: "9" } }], // Circle (IRLP / EchoLink / WIRES)   [overlay: centre]
  "\\8": [
    { t: "rect", a: { height: "7.5", rx: "1.5", width: "17", x: "3.5", y: "13" } },
    { t: "path", a: { d: "M7.6 4.6a6.5 6.5 0 0 1 8.8 0M10.1 7.8a3 3 0 0 1 3.8 0" } },
    { t: "path", a: { d: "M12 10.4v.01" } },
  ], // Network node (802.11)   [overlay: badge]
  "\\9": [
    { t: "rect", a: { height: "16", rx: "1.5", width: "9.5", x: "4.5", y: "4.5" } },
    { t: "path", a: { d: "M7 7.6h4.5v3.4H7Z" } },
    { t: "path", a: { d: "M14 8h2.4a1.4 1.4 0 0 1 1.4 1.4v6.2a1.4 1.4 0 0 0 2.8 0v-4.4l-2-2" } },
  ], // Gas station
  "\\:": [
    {
      t: "path",
      a: { d: "M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z" },
    },
    { t: "circle", a: { cx: "9", cy: "18.6", r: "1.15" } },
    { t: "circle", a: { cx: "14.6", cy: "19.3", r: "1.15" } },
    { t: "circle", a: { cx: "12", cy: "16.4", r: "1.15" } },
  ], // Hail
  "\\;": [
    { t: "path", a: { d: "M3 8h18" } },
    { t: "path", a: { d: "m6.6 8-2.6 12M17.4 8l2.6 12" } },
    { t: "path", a: { d: "M5.4 13.5h13.2" } },
  ], // Park / picnic area   [overlay: badge]
  "\\<": [
    { t: "path", a: { d: "M6 3v18" } },
    { t: "path", a: { d: "M6 4.6h11L6 11Z" } },
  ], // Advisory (single red flag)   [overlay: badge]
  "\\>": [
    {
      t: "path",
      a: {
        d: "M8 3.5h8a1.8 1.8 0 0 1 1.75 1.4l1.25 5.6v8a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 5 18.5v-8l1.25-5.6A1.8 1.8 0 0 1 8 3.5Z",
      },
    },
    { t: "path", a: { d: "M6.6 9.6c3.6-1 7.2-1 10.8 0" } },
    { t: "path", a: { d: "M6.8 15.4c3.4.9 6.8.9 10.4 0" } },
    { t: "path", a: { d: "M4.6 11.4 3 12M19.4 11.4 21 12" } },
  ], // Car (top view)   [overlay: badge]
  "\\?": [
    { t: "path", a: { d: "M4 8.5 12 4l8 4.5" } },
    { t: "path", a: { d: "M6 8.5V20.5h12V8.5" } },
    { t: "path", a: { d: "M12 12.4v5" } },
    { t: "circle", a: { cx: "12", cy: "10.4", fill: "currentColor", r: "0.85", stroke: "none" } },
  ], // Information kiosk
  "\\@": [
    { t: "circle", a: { cx: "12", cy: "12", r: "1.8" } },
    { t: "path", a: { d: "M12 10.2c0-3.6 1.8-6.6 6-6.6-2.8 1.7-4 3.8-4.1 6.6" } },
    { t: "path", a: { d: "M12 13.8c0 3.6-1.8 6.6-6 6.6 2.8-1.7 4-3.8 4.1-6.6" } },
  ], // Hurricane / tropical storm
  "\\A": [{ t: "rect", a: { height: "17", rx: "2", width: "17", x: "3.5", y: "3.5" } }], // Box   [overlay: centre]
  "\\B": [
    { t: "path", a: { d: "M3 6.5h9.6a2.4 2.4 0 1 0-2.4-2.4" } },
    { t: "path", a: { d: "M3 11.5h13" } },
    {
      t: "path",
      a: {
        d: "M7 15.200000000000001v4.8M4.912 16.400000000000002l4.176 2.4M9.088000000000001 16.400000000000002l-4.176 2.4",
      },
    },
    {
      t: "path",
      a: {
        d: "M15.5 15.200000000000001v4.8M13.411999999999999 16.400000000000002l4.176 2.4M17.588 16.400000000000002l-4.176 2.4",
      },
    },
  ], // Blowing snow
  "\\C": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
    { t: "circle", a: { cx: "12", cy: "12", r: "4" } },
    { t: "path", a: { d: "M12 3v5M12 16v5M3 12h5M16 12h5" } },
  ], // Coast Guard
  "\\D": [
    { t: "path", a: { d: "M3 9.8 12 5l9 4.8" } },
    { t: "path", a: { d: "M5 9.8V17h14V9.8" } },
    { t: "path", a: { d: "M3 17h18" } },
    { t: "path", a: { d: "M4.5 20.5h15" } },
  ], // Depot   [overlay: centre]
  "\\E": [
    { t: "path", a: { d: "M3.5 21h17" } },
    { t: "path", a: { d: "M8 21c0-3.6 3-4.2 3-7.4S8 9.6 8 6.4" } },
    { t: "path", a: { d: "M14.5 21c0-3.6 3-4.2 3-7.4s-3-4-3-7.2" } },
  ], // Smoke / visibility
  "\\F": [
    {
      t: "path",
      a: { d: "M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z" },
    },
    { t: "path", a: { d: "M8.5 16.6l0.0 2.4M12 16.6l0.0 2.4M15.5 16.6l0.0 2.4" } },
    { t: "path", a: { d: "M6.5 21.2h11" } },
  ], // Freezing rain
  "\\G": [
    {
      t: "path",
      a: { d: "M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z" },
    },
    {
      t: "path",
      a: { d: "M9 16.6v4.4M7.086 17.7l3.8280000000000003 2.2M10.914 17.7l-3.8280000000000003 2.2" },
    },
    { t: "path", a: { d: "M15.6 16.6l-1 3.4" } },
  ], // Snow shower
  "\\H": [
    { t: "circle", a: { cx: "12", cy: "8.5", r: "3.8" } },
    { t: "path", a: { d: "M12 2.2v1.6M4.8 8.5H3.2M20.8 8.5h-1.6M6.9 3.4 8 4.5M17.1 3.4 16 4.5" } },
    { t: "path", a: { d: "M3.5 16.5h17M6 20h12" } },
  ], // Haze / hazard   [overlay: badge]
  "\\I": [
    {
      t: "path",
      a: { d: "M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z" },
    },
    { t: "path", a: { d: "M9.6 16.6l-1.4 3.2M13.1 16.6l-1.4 3.2M16.6 16.6l-1.4 3.2" } },
  ], // Rain shower
  "\\J": [{ t: "path", a: { d: "M13.6 2.5 5.6 13.6h5l-2 7.9 8-11h-5Z" } }], // Lightning
  "\\K": [
    { t: "rect", a: { height: "15.5", rx: "2", width: "10", x: "7", y: "5.5" } },
    { t: "path", a: { d: "M15 5.5V2" } },
    { t: "path", a: { d: "M9.5 9h5" } },
    { t: "path", a: { d: "M9.5 13h5M9.5 16.6h5" } },
  ], // Kenwood HT
  "\\L": [
    { t: "path", a: { d: "M9 20.5 10 9h4l1 11.5Z" } },
    { t: "path", a: { d: "M9.6 13h4.8" } },
    { t: "path", a: { d: "M10 9V6.4h4V9" } },
    { t: "path", a: { d: "M12 6.4V4" } },
    { t: "path", a: { d: "M6.6 6 4.2 4.6M17.4 6l2.4-1.4M6.6 9.2H4M17.4 9.2H20" } },
    { t: "path", a: { d: "M7 20.5h10" } },
  ], // Lighthouse
  "\\M": [
    { t: "path", a: { d: "M12 2.8 4.5 5.8v6.4c0 4 3 6.8 7.5 8.5 4.5-1.7 7.5-4.5 7.5-8.5V5.8Z" } },
  ], // MARS   [overlay: centre]
  "\\N": [
    { t: "path", a: { d: "M12 20.5V13" } },
    { t: "path", a: { d: "M9 13h6l-1-4h-4Z" } },
    { t: "path", a: { d: "M12 9V6.4" } },
    { t: "circle", a: { cx: "12", cy: "4.6", r: "1.5" } },
    { t: "path", a: { d: "M4 18c2.7-2 5.3 2 8 0s5.3-2 8 0" } },
  ], // Navigation buoy
  "\\O": [
    { t: "path", a: { d: "M12 15c3.4 0 6-3 6-6.6A6 6 0 0 0 6 8.4c0 3.6 2.6 6.6 6 6.6Z" } },
    { t: "path", a: { d: "M10.7 14.8 12 17.3l1.3-2.5" } },
    { t: "path", a: { d: "M10.4 17.3h3.2l-.5 3.4h-2.2Z" } },
  ], // Rocket / balloon   [overlay: badge]
  "\\P": [
    { t: "rect", a: { height: "17", rx: "3", width: "17", x: "3.5", y: "3.5" } },
    { t: "path", a: { d: "M10 17V7.4h3.2a2.9 2.9 0 0 1 0 5.8H10" } },
  ], // Parking
  "\\Q": [
    { t: "path", a: { d: "M2.5 14h4l3-5.5 3 9.5 3-7.5 2 3.5h4" } },
    { t: "path", a: { d: "M4 19.5h16" } },
  ], // Earthquake
  "\\R": [
    { t: "path", a: { d: "M6 3v6a2.5 2.5 0 0 0 5 0V3" } },
    { t: "path", a: { d: "M8.5 11.5v9.5" } },
    { t: "path", a: { d: "M17.5 3c-1.6 1.6-2.2 3.6-2.2 5.6s.8 3.1 2.2 3.1v9.3" } },
  ], // Restaurant   [overlay: badge]
  "\\S": [
    { t: "rect", a: { height: "5", rx: "1", width: "5", x: "9.5", y: "9.5" } },
    { t: "path", a: { d: "M9.5 12H4.5M14.5 12h5" } },
    { t: "path", a: { d: "M4.5 9v6M19.5 9v6" } },
    { t: "path", a: { d: "M12 9.5V6.2M10.4 5.2a2.2 2.2 0 0 1 3.2 0" } },
  ], // Satellite / PACSAT
  "\\T": [
    {
      t: "path",
      a: { d: "M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z" },
    },
    { t: "path", a: { d: "M13.4 15.8 10 20.2h2.7l-1 2" } },
  ], // Thunderstorm
  "\\U": [
    { t: "circle", a: { cx: "12", cy: "12", r: "4.5" } },
    {
      t: "path",
      a: {
        d: "M12 2.6v2.4M12 19v2.4M2.6 12H5M19 12h2.4M5.3 5.3 7 7M17 17l1.7 1.7M18.7 5.3 17 7M7 17l-1.7 1.7",
      },
    },
  ], // Sunny
  "\\V": [
    { t: "path", a: { d: "m12 3 7.8 4.5v9L12 21l-7.8-4.5v-9Z" } },
    { t: "circle", a: { cx: "12", cy: "12", r: "2" } },
    { t: "path", a: { d: "M12 3v3.4M12 17.6V21" } },
  ], // VORTAC navigation aid
  "\\W": [
    { t: "path", a: { d: "M7 10.5a5 5 0 0 1 10 0Z" } },
    { t: "path", a: { d: "M9 10.5 8 20.5M15 10.5l1 10" } },
    { t: "path", a: { d: "M8.5 15h7" } },
    { t: "path", a: { d: "M6.6 20.5h10.8" } },
  ], // NWS site   [overlay: badge]
  "\\X": [
    { t: "path", a: { d: "M5 10.5h14v1.4a7 7 0 0 1-14 0Z" } },
    { t: "path", a: { d: "M12 18.9v2.1M8 21h8" } },
    { t: "path", a: { d: "m10.2 10.5 5.8-6 2.4 2.4-4.4 3.6" } },
  ], // Pharmacy
  "\\Y": [
    { t: "rect", a: { height: "11", rx: "2", width: "19", x: "2.5", y: "8" } },
    { t: "path", a: { d: "M6 8 17 3.6" } },
    { t: "circle", a: { cx: "8", cy: "13.5", r: "2.5" } },
    { t: "path", a: { d: "M13 12h6M13 15.4h6" } },
  ], // Radio / APRS device   [overlay: badge]
  "\\[": [
    { t: "circle", a: { cx: "12", cy: "7.2", r: "3.2" } },
    { t: "path", a: { d: "M4.8 20.5a7.2 7.2 0 0 1 14.4 0" } },
  ], // Wall cloud / person   [overlay: badge]
  "\\\\": [
    { t: "rect", a: { height: "17", rx: "3", width: "17", x: "3.5", y: "3.5" } },
    { t: "path", a: { d: "m7.6 16.8 4.4-9.6 4.4 9.6-4.4-3Z" } },
  ], // GPS / navigation device   [overlay: badge]
  "\\^": [
    {
      t: "path",
      a: {
        d: "M12 2.4c1.1 0 1.9 1.7 1.9 4.1v2.2l7.6 5.1v2.3l-7.6-2.6v3.6l2.3 2v1.6L12 19.6l-4.2 1.1v-1.6l2.3-2v-3.6L2.5 16.1v-2.3l7.6-5.1V6.5c0-2.4.8-4.1 1.9-4.1Z",
      },
    },
  ], // Aircraft (top view)   [overlay: badge]
  "\\_": [
    { t: "path", a: { d: "M13 13.6V5a2 2 0 1 0-4 0v8.6a3.6 3.6 0 1 0 4 0Z" } },
    { t: "path", a: { d: "M9 8h2M9 10.6h2" } },
    { t: "path", a: { d: "M18 3.4v5.2M15.4 6h5.2M16.2 4.2l3.6 3.6M19.8 4.2l-3.6 3.6" } },
  ], // Weather station with digipeater   [overlay: badge]
  "\\`": [
    {
      t: "path",
      a: { d: "M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z" },
    },
    { t: "path", a: { d: "M8.5 16.6l0.0 3.2M12 16.6l0.0 3.2M15.5 16.6l0.0 3.2" } },
  ], // Rain
  "\\a": [{ t: "path", a: { d: "M12 2.5 21.5 12 12 21.5 2.5 12Z" } }], // Diamond — organisation / affiliation   [overlay: centre]
  "\\b": [
    { t: "path", a: { d: "M3 6.5h9.6a2.4 2.4 0 1 0-2.4-2.4" } },
    { t: "path", a: { d: "M3 11.5h11a2.4 2.4 0 1 1-2.4 2.4" } },
    { t: "circle", a: { cx: "6", cy: "18.4", fill: "currentColor", r: "0.9", stroke: "none" } },
    { t: "circle", a: { cx: "11", cy: "19.4", fill: "currentColor", r: "0.9", stroke: "none" } },
    { t: "circle", a: { cx: "16", cy: "18.2", fill: "currentColor", r: "0.9", stroke: "none" } },
  ], // Blowing dust / sand
  "\\c": [
    { t: "circle", a: { cx: "12", cy: "12", r: "9.2" } },
    { t: "path", a: { d: "m12 6.4 5 8.8H7Z" } },
  ], // CD triangle (RACES / CERT / SATERN)   [overlay: badge]
  "\\d": [
    {
      t: "path",
      a: { d: "m12 3.4 2 4.6 5 .5-3.8 3.3 1.1 4.9L12 14.1l-4.3 2.6 1.1-4.9L5 8.5l5-.5Z" },
    },
    { t: "path", a: { d: "M4.5 20.4a10 10 0 0 1 15 0" } },
  ], // DX spot
  "\\e": [
    {
      t: "path",
      a: { d: "M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z" },
    },
    { t: "path", a: { d: "m9 16.6-.9 3.2M16 16.6l-.9 3.2" } },
    { t: "circle", a: { cx: "12.4", cy: "19.2", r: "1.05" } },
  ], // Sleet
  "\\f": [
    {
      t: "path",
      a: { d: "M7 14a3.5 3.5 0 0 1 .3-6.98 4.7 4.7 0 0 1 8.9-.5A3.9 3.9 0 0 1 17 14Z" },
    },
    { t: "path", a: { d: "M8.4 15.4c1.2 3.4 2.4 5 3.4 5.8M15.6 15.4c-.6 2.4-1.6 4-2.8 5.2" } },
  ], // Funnel cloud
  "\\g": [
    { t: "path", a: { d: "M6 3v18" } },
    { t: "path", a: { d: "M6 4.4h10L6 9.8Z" } },
    { t: "path", a: { d: "M6 11.6h10L6 17Z" } },
  ], // Gale (two red flags)
  "\\h": [
    { t: "path", a: { d: "M3.5 9.5 5.5 5h13l2 4.5Z" } },
    { t: "path", a: { d: "M5.2 9.5V20.5h13.6V9.5" } },
    { t: "path", a: { d: "M9.6 20.5V15h4.8v5.5" } },
  ], // Store / hamfest   [overlay: badge]
  "\\i": [
    { t: "path", a: { d: "M12 21.4c0 0 7-6.1 7-11a7 7 0 1 0-14 0c0 4.9 7 11 7 11Z" } },
    { t: "circle", a: { cx: "12", cy: "10.4", r: "2.4" } },
  ], // Point of interest   [overlay: badge]
  "\\j": [
    { t: "path", a: { d: "M12 4.5 18.6 20.5H5.4Z" } },
    { t: "path", a: { d: "M9.4 13h5.2M8.2 16.4h7.6" } },
  ], // Work zone
  "\\k": [
    { t: "path", a: { d: "M2.5 15.5v-3.4l2-.6 2.5-3.5h9l2.5 3.5 2 .6v3.4Z" } },
    { t: "circle", a: { cx: "6.8", cy: "17.4", r: "1.7" } },
    { t: "circle", a: { cx: "17.2", cy: "17.4", r: "1.7" } },
    { t: "path", a: { d: "M11.6 8v4.1" } },
  ], // Special vehicle (SUV / ATV / 4x4)   [overlay: badge]
  "\\l": [
    {
      t: "rect",
      a: { height: "12", rx: "1", strokeDasharray: "3 2.5", width: "17", x: "3.5", y: "6" },
    },
  ], // Area symbol
  "\\m": [
    { t: "path", a: { d: "M12 21v-6.6" } },
    { t: "rect", a: { height: "10.4", rx: "1.5", width: "18", x: "3", y: "4" } },
  ], // Value signpost (3-digit)   [overlay: centre]
  "\\n": [{ t: "path", a: { d: "M12 4 21 19.5H3Z" } }], // Triangle   [overlay: centre]
  "\\o": [{ t: "circle", a: { cx: "12", cy: "12", r: "4.5" } }], // Small circle
  "\\p": [
    { t: "circle", a: { cx: "8", cy: "7.6", r: "3.2" } },
    { t: "path", a: { d: "M8 1.8v1.4M2.2 7.6h1.4M3.9 3.5 4.9 4.5M12.1 3.5 11.1 4.5M8 12v1.4" } },
    { t: "path", a: { d: "M10 19.5a3 3 0 0 1 .3-5.98 4 4 0 0 1 7.6-.4 3.3 3.3 0 0 1 .6 6.38Z" } },
  ], // Partly cloudy
  "\\r": [
    { t: "circle", a: { cx: "6.8", cy: "4.8", r: "1.8" } },
    { t: "path", a: { d: "M6.8 8v6.4M3.8 10.6h6M4.8 21l2-6.6 2 6.6" } },
    { t: "circle", a: { cx: "17.2", cy: "4.8", r: "1.8" } },
    { t: "path", a: { d: "M17.2 8 14.2 15h6Z" } },
    { t: "path", a: { d: "M15.8 15v6M18.6 15v6" } },
  ], // Restrooms
  "\\s": [
    {
      t: "path",
      a: {
        d: "M12 2.5c3 3.5 4.5 8 4.5 12.5v4a1.5 1.5 0 0 1-1.5 1.5H9a1.5 1.5 0 0 1-1.5-1.5v-4C7.5 10.5 9 6 12 2.5Z",
      },
    },
    { t: "path", a: { d: "M9 12h6" } },
  ], // Ship / boat (top view)   [overlay: badge]
  "\\t": [
    { t: "path", a: { d: "M4 4.5h16" } },
    { t: "path", a: { d: "M6 8h13M8.5 11.5h9M11 15h5.5M12.6 18.4h2.4" } },
    { t: "path", a: { d: "M14.6 18.4c-1 1.6-2.6 2.4-4.6 2.6" } },
  ], // Tornado
  "\\u": [
    { t: "path", a: { d: "M2 5.5h11v10H2z" } },
    { t: "path", a: { d: "M13 9h3.8l3.2 3.3v3.2h-7z" } },
    { t: "circle", a: { cx: "6.5", cy: "17.6", r: "1.8" } },
    { t: "circle", a: { cx: "16.8", cy: "17.6", r: "1.8" } },
  ], // Truck   [overlay: badge]
  "\\v": [
    { t: "path", a: { d: "M2 15.5V9.6l3-4h9.4l4.6 4v5.9Z" } },
    { t: "circle", a: { cx: "6.6", cy: "17.4", r: "1.8" } },
    { t: "circle", a: { cx: "16.4", cy: "17.4", r: "1.8" } },
    { t: "path", a: { d: "M6 9.6h5" } },
  ], // Van
  "\\w": [
    { t: "path", a: { d: "M2.5 16c2-2 4-2 6 0s4 2 6 0 4-2 6 0" } },
    { t: "path", a: { d: "M2.5 20.5c2-2 4-2 6 0s4 2 6 0 4-2 6 0" } },
    { t: "path", a: { d: "M12 12.4V3.6M8.4 7.2 12 3.6l3.6 3.6" } },
  ], // Flooding / avalanche / landslide   [overlay: badge]
  "\\x": [
    { t: "path", a: { d: "M6.5 4.5 17.5 15.5M17.5 4.5 6.5 15.5" } },
    { t: "path", a: { d: "M2.5 19.6c2-2 4-2 6 0s4 2 6 0 4-2 6 0" } },
  ], // Wreck or obstruction
  "\\y": [
    { t: "path", a: { d: "M3 8s3.6-4.6 9-4.6S21 8 21 8s-3.6 4.6-9 4.6S3 8 3 8Z" } },
    { t: "circle", a: { cx: "12", cy: "8", r: "2" } },
    { t: "path", a: { d: "M8.6 15.2c.6 3.2 1.8 5 3.4 5.9M15.4 15.2c-.6 3.2-1.8 5-3.4 5.9" } },
  ], // Skywarn
  "\\z": [
    { t: "path", a: { d: "M2.5 11.5 12 4l9.5 7.5" } },
    { t: "path", a: { d: "M5.5 11.5V20M18.5 11.5V20" } },
    { t: "path", a: { d: "M3.5 20h17" } },
  ], // Shelter   [overlay: centre]
  "\\{": [
    { t: "path", a: { d: "M3 7.6c2-1.6 4-1.6 6 0s4 1.6 6 0 4-1.6 6 0" } },
    { t: "path", a: { d: "M3 12.6c2-1.6 4-1.6 6 0s4 1.6 6 0 4-1.6 6 0" } },
    { t: "path", a: { d: "M3 17.6c2-1.6 4-1.6 6 0s4 1.6 6 0 4-1.6 6 0" } },
  ], // Fog
};

// Which slot an alternate-table base uses for its overlay character.
// ["centre", baselineY, fontSize] — the character sits inside the shape.
// ["badge"]                       — the base is scaled 0.78 about the top-left and the
//                                   character takes the freed bottom-right corner.
export type OverlaySlot = ["centre", number, number] | ["badge"];
export const APRS_OVERLAY_SLOT: Record<string, OverlaySlot> = {
  "!": ["centre", 17.4, 8.5], // \!  Emergency
  "#": ["centre", 15.4, 9.0], // \#  Digipeater (green star)
  $: ["centre", 15.6, 10.0], // \$  Bank or ATM
  "%": ["badge"], // \%  Power plant
  "&": ["centre", 15.7, 10.0], // \&  Gateway station
  "'": ["badge"], // \'  Crash / incident site
  "(": ["badge"], // \(  Cloudy
  ")": ["badge"], // \)  Firenet MEO / MODIS Earth observation
  "-": ["centre", 17.8, 8.5], // \-  House (HF)
  "0": ["centre", 15.7, 10.5], // \0  Circle (IRLP / EchoLink / WIRES)
  "8": ["badge"], // \8  Network node (802.11)
  ";": ["badge"], // \;  Park / picnic area
  "<": ["badge"], // \<  Advisory (single red flag)
  ">": ["badge"], // \>  Car (top view)
  A: ["centre", 15.7, 10.5], // \A  Box
  D: ["centre", 15.4, 8.0], // \D  Depot
  H: ["badge"], // \H  Haze / hazard
  M: ["centre", 15.6, 9.5], // \M  MARS
  O: ["badge"], // \O  Rocket / balloon
  R: ["badge"], // \R  Restaurant
  W: ["badge"], // \W  NWS site
  Y: ["badge"], // \Y  Radio / APRS device
  "[": ["badge"], // \[  Wall cloud / person
  "\\": ["badge"], // \\  GPS / navigation device
  "^": ["badge"], // \^  Aircraft (top view)
  _: ["badge"], // \_  Weather station with digipeater
  a: ["centre", 15.7, 10.0], // \a  Diamond — organisation / affiliation
  c: ["badge"], // \c  CD triangle (RACES / CERT / SATERN)
  h: ["badge"], // \h  Store / hamfest
  i: ["badge"], // \i  Point of interest
  k: ["badge"], // \k  Special vehicle (SUV / ATV / 4x4)
  m: ["centre", 11.8, 8.0], // \m  Value signpost (3-digit)
  n: ["centre", 17.6, 8.5], // \n  Triangle
  s: ["badge"], // \s  Ship / boat (top view)
  u: ["badge"], // \u  Truck
  w: ["badge"], // \w  Flooding / avalanche / landslide
  z: ["centre", 18.4, 8.0], // \z  Shelter
};
