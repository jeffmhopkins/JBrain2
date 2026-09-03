// Rendering one APRS symbol.
//
// The glyphs are in `aprsGlyphs.ts`; this is the part that decides WHICH one and draws
// the overlay on top of it. Both are the same shape as the app's own `icons.tsx` —
// 24x24, no fill, `currentColor` at 1.5 — so a symbol sits next to a house icon without
// looking like a different system.
//
// **The overlay is the subtle part.** A table character that is neither `/` nor `\` IS an
// overlay: it selects the ALTERNATE table's drawing and is painted on top. Four of the
// fifteen symbols on the owner's channel are overlaid, including how the busiest station
// says it is an IGate — so this is the common case, not an edge one.
//
// **The overlay character is packet data.** A station can transmit any of 0-9 A-Z there
// and a malformed frame can carry anything, so it goes through a React text node and is
// clamped to a single character. It is never markup and it is never trusted.

import { APRS_GLYPHS, APRS_OVERLAY_SLOT, type GlyphNode } from "./aprsGlyphs";

// The badge slot scales the base down and puts the character in the freed corner. A
// monochrome outline cannot knock a letter out of a filled shape the way the colour sets
// do, so this is the systematic alternative — and the stroke is pre-divided so it renders
// back to exactly 1.5 after the scale.
const BADGE_SCALE = 0.78;
const BADGE_STROKE = 1.5 / BADGE_SCALE;

/** The spec's own answer for a code with no assigned meaning: the international
 *  circle-and-slash, "meaning NOT". Better than a blank, and honest in a way that
 *  borrowing a neighbouring glyph would not be. */
const UNKNOWN: GlyphNode[] = APRS_GLYPHS["??"] ?? [
  { t: "circle", a: { cx: "12", cy: "12", r: "9" } },
  { t: "path", a: { d: "M5.6 5.6l12.8 12.8" } },
];

/** A key from the shape itself rather than its position — a glyph is a frozen constant,
 *  but keying on an index is a habit that stops being true the moment one is not. */
function shapeKey(node: GlyphNode): string {
  return `${node.t}:${node.a.d ?? ""}:${node.a.cx ?? ""},${node.a.cy ?? ""},${node.a.x ?? ""}`;
}

function Shape({ node }: { node: GlyphNode }) {
  const { t, a, c } = node;
  if (t === "circle") return <circle {...a} />;
  if (t === "rect") return <rect {...a} />;
  if (t === "text") return <text {...a}>{c}</text>;
  return <path {...a} />;
}

/** Which drawing and which overlay character a transmitted symbol resolves to. */
export function glyphFor(
  table: string,
  code: string,
): {
  nodes: GlyphNode[];
  overlay: string | null;
  known: boolean;
} {
  const isOverlay = table !== "" && table !== "/" && table !== "\\";
  // In a COMPRESSED report an overlay digit is transmitted as `a`-`j` for `0`-`9`. Dead
  // code on this channel's traffic today, and two lines.
  const overlay = isOverlay
    ? (table >= "a" && table <= "j" ? String(table.charCodeAt(0) - 97) : table).slice(0, 1)
    : null;
  const key = (isOverlay ? "\\" : table) + code;
  const found = APRS_GLYPHS[key];
  return { nodes: found ?? UNKNOWN, overlay, known: found !== undefined };
}

export function AprsSymbol({
  table,
  code,
  label,
  size = 20,
}: {
  table: string;
  code: string;
  /** What this symbol is called. Required: an icon with no text alternative is an icon
   *  a screen reader cannot report, and these carry real meaning. */
  label: string;
  size?: number;
}) {
  const { nodes, overlay } = glyphFor(table, code);
  const slot = overlay ? APRS_OVERLAY_SLOT[code] : undefined;
  // `\!` draws its own exclamation mark. An overlay replaces it rather than stacking a
  // second mark on top of the first.
  const base =
    overlay && table + code !== "" && `\\${code}` === "\\!"
      ? [{ t: "path", a: { d: "M12 3.5 21.5 20H2.5Z" } } as GlyphNode]
      : nodes;

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
    >
      <title>{label}</title>
      {slot?.[0] === "badge" ? (
        <g transform={`scale(${BADGE_SCALE})`} strokeWidth={BADGE_STROKE}>
          {base.map((node) => (
            <Shape key={shapeKey(node)} node={node} />
          ))}
        </g>
      ) : (
        base.map((node) => <Shape key={shapeKey(node)} node={node} />)
      )}
      {overlay && slot?.[0] === "centre" && (
        <text
          x="12"
          y={slot[1]}
          textAnchor="middle"
          fontSize={slot[2]}
          fontWeight="600"
          fontFamily="ui-sans-serif, system-ui, sans-serif"
          fill="currentColor"
          stroke="none"
        >
          {overlay}
        </text>
      )}
      {overlay && slot?.[0] === "badge" && (
        <text
          x="21.4"
          y="22"
          textAnchor="end"
          fontSize="9"
          fontWeight="600"
          fontFamily="ui-sans-serif, system-ui, sans-serif"
          fill="currentColor"
          stroke="none"
        >
          {overlay}
        </text>
      )}
    </svg>
  );
}
