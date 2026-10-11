"""Builds the three variant pages from the PNGs make_icons.py drew (README.md here).

Each page embeds its PNGs as data URIs so it opens offline as a single file.

    python3 make_icons.py && python3 make_pages.py
"""

import base64
from pathlib import Path

HERE = Path(__file__).resolve().parent

VARIANTS = {
    "a": {
        "file": "a-salvaged-copper.html",
        "name": "Salvaged copper",
        "thesis": "Hardware pulled from the wreck: warm copper with a verdigris bloom, "
        "amethyst for anything that carries a signal, the Replicator in tuff.",
        "tricorder": "A copper handheld with an amethyst antenna crystal, its screen showing "
        "the waypoint arrow.",
        "pack": "A copper-capped glass cell: a battery silhouette with the eye's green "
        "sealed inside.",
        "rep": "A tuff furnace in a copper frame. The program window is dark amethyst until "
        "it's taught; the two side lights go from dim to lit green.",
    },
    "b": {
        "file": "b-deep-dark.html",
        "name": "Deep dark",
        "thesis": "Tech from the ancient cities: deepslate housings, sculk-teal glow and "
        "echo-shard trim. Cold, dark and luminous.",
        "tricorder": "A slim deepslate slab under a glowing sculk lens, which is Dave's eye. "
        "Its screen shows a cyan arrow.",
        "pack": "The eye set in a deepslate-and-echo diamond, with sculk glow at its four "
        "points.",
        "rep": "Deepslate tiles with an echo-shard frame. The furnace mouth glows teal; the "
        "side lights go from dark to bright sculk.",
    },
    "c": {
        "file": "c-ship-quartz.html",
        "name": "Ship's quartz",
        "thesis": "The ship as it was built: pale quartz casings, gold contacts and redstone "
        "status lights. Clean and bright, from the ore table's parts list.",
        "tricorder": "A flip-open case, gold grille lid up, with a redstone arrow on the "
        "screen.",
        "pack": "An octagonal quartz cell: a gold-ringed eye lens, four rivets and a red "
        "status light.",
        "rep": "Polished quartz with a gold frame. The side lights are redstone: dark red "
        "when blank, bright once programmed.",
    },
}


def uri(path):
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


PAGE = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
<title>Old Maker Items · __V__ — __NAME__</title>
<!-- GUI gate mock — docs/mocks/minecraft-items/README.md. Spec: docs/plans/MINECRAFT_BEDROCK_PLAN.md
     §M9, §M10, §DW1; canon: Bleepbloop_City notes/lore.md. Self-contained and offline; the icons are
     the 16x16 PNGs beside this file, embedded. The vanilla neighbours are our own stand-ins. -->
<style>
  :root{
    color-scheme:dark;
    --bg:#0E0F11; --surface:#17181B; --surface-2:#1E2024; --border:#26282C;
    --text:#E6E7E9; --text-2:#9A9DA3; --text-3:#5C5F66;
    --steel:#7FA7C9; --steel-tint:rgba(127,167,201,.13); --steel-text:var(--steel);
    --r-card:16px; --r-pill:999px;
    --fs-micro:12px; --fs-head:13px; --fs-secondary:14px; --fs-body:16px; --fs-title:18px; --fs-screen:22px;
    --font-mono:ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,monospace;
    --frame:#2A2C30; --page:#000; --tile:#2a2c31;
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
  }
  [data-theme="light"]{
    color-scheme:light;
    --bg:#F7F7F5; --surface:#FFFFFF; --surface-2:#F0F0EE; --border:#E2E2DF;
    --text:#1A1B1E; --text-2:#5C5F66; --text-3:#9A9DA3;
    --steel-text:color-mix(in srgb, var(--steel) 52%, var(--text));
    --frame:#C9C9C4; --page:#DADAD6; --tile:#e4e4e0;
  }
  *{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}
  body{background:var(--page);color:var(--text);display:flex;flex-direction:column;align-items:center;gap:12px;padding:18px 0 40px;min-height:100vh;line-height:1.4;}
  button{font:inherit;color:inherit;cursor:pointer;}
  a{color:inherit;}
  :focus-visible{outline:2px solid var(--steel);outline-offset:2px;}
  .num{font-variant-numeric:tabular-nums;}
  img,canvas,.px{image-rendering:pixelated;image-rendering:crisp-edges;}

  /* reviewer chrome — OUTSIDE the design */
  .caption{max-width:390px;width:100%;padding:0 16px;color:var(--text-2);font-size:13px;line-height:1.55;}
  .caption b{color:var(--text);font-weight:500;}
  .caption .tag{display:inline-block;background:var(--steel-tint);color:var(--steel-text);font-size:11px;padding:2px 8px;border-radius:999px;margin-bottom:6px;}
  .sim{max-width:390px;width:calc(100% - 32px);padding:10px 12px;border:1px dashed var(--text-3);border-radius:14px;display:flex;flex-direction:column;gap:8px;}
  .sim .stitle{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--text-2);}
  .sim .srow{display:flex;flex-wrap:wrap;align-items:center;gap:6px;}
  .sim .sl{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--text-2);width:62px;flex:none;}
  .sim button,.sim a{appearance:none;border:1px solid var(--border);background:var(--surface-2);color:var(--text-2);font-size:12px;padding:5px 10px;border-radius:999px;text-decoration:none;min-height:32px;display:inline-flex;align-items:center;}
  .sim [aria-pressed="true"],.sim [aria-current="page"]{background:var(--steel-tint);border-color:transparent;color:var(--steel-text);font-weight:600;}

  /* the phone */
  .phone{width:390px;height:820px;background:var(--bg);border:1px solid var(--frame);border-radius:34px;overflow:hidden;display:flex;flex-direction:column;}
  .screen{flex:1;overflow-y:auto;overflow-x:hidden;padding:16px 12px 28px;display:flex;flex-direction:column;gap:14px;}
  @media (max-width:430px){ body{padding-top:12px;} .phone{width:100%;height:100dvh;border-radius:0;border-left:0;border-right:0;} }
  @media (min-width:900px){ .phone{width:560px;} .caption,.sim{max-width:560px;} }

  .hd h1{font-size:var(--fs-screen);font-weight:600;letter-spacing:-.01em;}
  .hd p{color:var(--text-2);font-size:var(--fs-secondary);margin-top:2px;}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r-card);padding:14px 12px;display:flex;flex-direction:column;gap:10px;}
  .card h2{font-size:var(--fs-head);font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--text-2);}
  .card .note{font-size:var(--fs-secondary);color:var(--text-2);}

  /* the set: 1x, 2x, 8x */
  .item{display:grid;grid-template-columns:1fr auto;gap:6px 10px;align-items:end;padding-top:10px;border-top:1px solid var(--border);}
  .item:first-of-type{border-top:0;padding-top:0;}
  .item .nm{font-size:var(--fs-body);font-weight:600;}
  .item .ds{grid-column:1/-1;font-size:var(--fs-secondary);color:var(--text-2);}
  .scales{grid-column:1/-1;display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;}
  .sc{display:flex;flex-direction:column;align-items:center;gap:4px;}
  .sc span{font-size:var(--fs-micro);color:var(--text-2);}
  .tile{background:var(--tile);border-radius:6px;padding:6px;display:grid;place-items:center;}

  /* a block, drawn from its faces the way the game draws one in a slot */
  .blk{position:relative;display:inline-block;width:calc(16px * var(--s,1));height:calc(16px * var(--s,1));flex:none;}
  .blk i{position:absolute;left:0;top:0;width:16px;height:16px;background-size:16px 16px;image-rendering:pixelated;transform-origin:0 0;}
  .blk .t{background-image:var(--top);transform:scale(var(--s,1)) matrix(.5,.25,-.5,.25,8,0) scale(1.02);}
  .blk .l{background-image:var(--front);transform:scale(var(--s,1)) matrix(.5,.25,0,.5,0,4) scale(1.02);filter:brightness(.82);}
  .blk .r{background-image:var(--side);transform:scale(var(--s,1)) matrix(.5,-.25,0,.5,8,8) scale(1.02);filter:brightness(.62);}
  .rep{--top:url(__rep_top__);--front:url(__rep_front__);--side:url(__rep_side__);}
  .rep.iron{--front:url(__rep_front_iron__);}
  .fur{--top:url(__fur_top__);--front:url(__fur_front__);--side:url(__fur_side__);}

  /* in-game pieces: part of the picture, not page chrome */
  .scene{border-radius:12px;overflow:hidden;background:linear-gradient(#7fb0ff 0 52%, #6aa84f 52% 60%, #8a6a46 60%);padding:14px 4px 8px;display:flex;flex-direction:column;align-items:center;gap:6px;}
  .scene.night{background:linear-gradient(#0f1830 0 52%, #2f5b2a 52% 60%, #4a3826 60%);}
  .mcname{font-family:var(--font-mono);font-weight:700;color:#fff;text-shadow:2px 2px 0 #3f3f3f;font-size:13px;min-height:18px;}
  .hotbar{display:flex;background:rgba(0,0,0,.55);border:2px solid #1b1b1b;outline:1px solid #8b8b8b;}
  .hotbar .sl{--sz:calc(20px * var(--k));width:var(--sz);height:var(--sz);display:grid;place-items:center;border:1px solid rgba(139,139,139,.6);position:relative;padding:0;background:transparent;}
  .hotbar .sl[aria-pressed="true"]{outline:calc(2px * var(--k)) solid #f0f0f0;outline-offset:-1px;z-index:1;box-shadow:0 0 0 calc(1px * var(--k)) #000;}
  .hotbar img,.hotbar canvas{width:calc(16px * var(--k));height:calc(16px * var(--k));}
  .cnt{position:absolute;right:1px;bottom:0;font-family:var(--font-mono);font-weight:700;color:#fff;text-shadow:1px 1px 0 #3f3f3f;line-height:1;pointer-events:none;}
  .k1{--k:1;} .k1 .cnt{font-size:8px;}
  .k2{--k:1.75;} .k2 .sl img,.k2 .sl canvas{width:32px;height:32px;} .k2 .cnt{font-size:13px;right:2px;bottom:1px;text-shadow:2px 2px 0 #3f3f3f;}
  @media (max-width:380px){ .k2{--k:1.45;} .k2 .sl img,.k2 .sl canvas{width:24px;height:24px;} .k2 .sl .blk{--s:1.5 !important;} .scene{padding-left:0;padding-right:0;} }

  .inv{padding:8px;border-radius:6px;display:grid;grid-template-columns:repeat(9,1fr);gap:0;max-width:340px;width:100%;}
  .inv.lite{background:#c6c6c6;border:2px solid;border-color:#fff #555 #555 #fff;}
  .inv.dark{background:#313233;border:2px solid;border-color:#4f5052 #1a1a1b #1a1a1b #4f5052;}
  .inv .sl{aspect-ratio:1;display:grid;place-items:center;position:relative;padding:0;border:2px solid;}
  .inv.lite .sl{background:#8b8b8b;border-color:#373737 #fff #fff #373737;}
  .inv.dark .sl{background:#1e1f20;border-color:#0e0e0f #48494b #48494b #0e0e0f;}
  .inv .sl[aria-pressed="true"]{background:#c5c5c5;}
  .inv.dark .sl[aria-pressed="true"]{background:#4a4b4d;}
  .inv img,.inv canvas{width:32px;height:32px;}
  .inv .cnt{font-size:12px;text-shadow:1px 1px 0 #3f3f3f;right:1px;}
  @media (max-width:360px){ .inv img,.inv canvas{width:16px;height:16px;} .inv .blk{--s:1 !important;} .inv .cnt{font-size:9px;} }
  .invwrap{display:flex;flex-direction:column;gap:8px;align-items:center;}

  .tip{background:rgba(16,8,24,.94);border:2px solid #2a0f5c;outline:1px solid #100818;padding:6px 8px;font-family:var(--font-mono);color:#fff;font-size:13px;line-height:1.45;align-self:flex-start;max-width:100%;}
  .tip .lo{color:#c4b0ec;font-style:italic;}
  .tip .id{color:#9a9da3;font-size:11px;}
  .tips{display:flex;flex-wrap:wrap;gap:8px;}

  .faces{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;}
  .faces figure{display:flex;flex-direction:column;align-items:center;gap:4px;}
  .faces figcaption{font-size:var(--fs-micro);color:var(--text-2);text-align:center;}
  .faces img{width:128px;height:128px;}
  @media (max-width:340px){ .faces img{width:112px;height:112px;} }

  .hand{position:relative;height:200px;border-radius:12px;overflow:hidden;background:linear-gradient(#7fb0ff 0 45%, #6aa84f 45% 55%, #8a6a46 55%);}
  .hand .xh{position:absolute;left:50%;top:40%;width:14px;height:14px;transform:translate(-50%,-50%);}
  .hand .xh::before,.hand .xh::after{content:"";position:absolute;background:#fff;mix-blend-mode:difference;}
  .hand .xh::before{left:6px;top:0;width:2px;height:14px;} .hand .xh::after{top:6px;left:0;width:14px;height:2px;}
  .hand .held{position:absolute;right:14px;bottom:-18px;width:96px;height:96px;transform:rotate(-12deg);}
  .hand .ab{position:absolute;z-index:2;left:50%;top:58%;transform:translateX(-50%);font-family:var(--font-mono);font-weight:700;color:#fff;text-shadow:2px 2px 0 #3f3f3f;font-size:13px;white-space:nowrap;}
  .seg{display:flex;gap:6px;flex-wrap:wrap;}
  .seg button{border:1px solid var(--border);background:var(--surface-2);color:var(--text-2);font-size:var(--fs-secondary);padding:0 12px;min-height:44px;border-radius:var(--r-pill);}
  .seg button[aria-pressed="true"]{background:var(--steel-tint);border-color:transparent;color:var(--steel-text);font-weight:600;}
</style>
</head>
<body>
  <div class="caption">
    <span class="tag">GUI gate · Minecraft items · __V__</span><br />
    <b>__V__ — __NAME__.</b> __THESIS__ Every icon is a real 16×16 PNG; tap a slot to select it.
  </div>
  <div class="sim" aria-label="Mock controls">
    <div class="stitle">Mock controls</div>
    <div class="srow"><span class="sl">Variant</span>__NAV__</div>
    <div class="srow"><span class="sl">Theme</span>
      <button data-theme-btn="system" aria-pressed="true">System</button>
      <button data-theme-btn="dark" aria-pressed="false">Dark</button>
      <button data-theme-btn="light" aria-pressed="false">Light</button></div>
    <div class="srow"><span class="sl">Glint</span>
      <button id="glintBtn" aria-pressed="true">Moving</button></div>
  </div>

  <div class="phone"><div class="screen">
    <div class="hd"><h1>Old Maker items</h1><p>Variant __V__ · __NAME__</p></div>

    <section class="card" aria-labelledby="h-set">
      <h2 id="h-set">The set at 1×, 2× and 8×</h2>
      <div class="item">
        <div class="nm">Tricorder</div>
        <div class="ds">__D_TRICORDER__</div>
        <div class="scales">
          <div class="sc"><div class="tile"><img src="__tricorder__" width="16" height="16" alt="Tricorder at 1×" /></div><span>1×</span></div>
          <div class="sc"><div class="tile"><img src="__tricorder__" width="32" height="32" alt="Tricorder at 2×" /></div><span>2×</span></div>
          <div class="sc"><div class="tile"><img src="__tricorder__" width="128" height="128" alt="Tricorder at 8×" /></div><span>8×</span></div>
        </div>
      </div>
      <div class="item">
        <div class="nm">Power Pack</div>
        <div class="ds">__D_PACK__ With the enchant glint.</div>
        <div class="scales">
          <div class="sc"><div class="tile"><canvas class="pk" width="128" height="128" style="width:16px;height:16px" aria-label="Power Pack at 1×"></canvas></div><span>1×</span></div>
          <div class="sc"><div class="tile"><canvas class="pk" width="128" height="128" style="width:32px;height:32px" aria-label="Power Pack at 2×"></canvas></div><span>2×</span></div>
          <div class="sc"><div class="tile"><canvas class="pk" width="128" height="128" style="width:128px;height:128px" aria-label="Power Pack at 8×, glinting"></canvas></div><span>8×</span></div>
        </div>
      </div>
      <div class="item">
        <div class="nm">Replicator</div>
        <div class="ds">__D_REP__ The block is drawn from its faces, as the game draws a block in a slot.</div>
        <div class="scales">
          <div class="sc"><div class="tile"><span class="blk rep" style="--s:1" role="img" aria-label="Replicator at 1×"><i class="t"></i><i class="l"></i><i class="r"></i></span></div><span>1×</span></div>
          <div class="sc"><div class="tile"><span class="blk rep" style="--s:2" role="img" aria-label="Replicator at 2×"><i class="t"></i><i class="l"></i><i class="r"></i></span></div><span>2×</span></div>
          <div class="sc"><div class="tile"><span class="blk rep" style="--s:8" role="img" aria-label="Replicator at 8×"><i class="t"></i><i class="l"></i><i class="r"></i></span></div><span>8×</span></div>
        </div>
      </div>
      <div class="item">
        <div class="nm">Iron Replicator</div>
        <div class="ds">Programmed: the window shows a swatch of iron and the lights come on.</div>
        <div class="scales">
          <div class="sc"><div class="tile"><span class="blk rep iron" style="--s:1" role="img" aria-label="Iron Replicator at 1×"><i class="t"></i><i class="l"></i><i class="r"></i></span></div><span>1×</span></div>
          <div class="sc"><div class="tile"><span class="blk rep iron" style="--s:2" role="img" aria-label="Iron Replicator at 2×"><i class="t"></i><i class="l"></i><i class="r"></i></span></div><span>2×</span></div>
          <div class="sc"><div class="tile"><span class="blk rep iron" style="--s:8" role="img" aria-label="Iron Replicator at 8×"><i class="t"></i><i class="l"></i><i class="r"></i></span></div><span>8×</span></div>
        </div>
      </div>
    </section>

    <section class="card" aria-labelledby="h-hot">
      <h2 id="h-hot">In the hotbar</h2>
      <p class="note">Next to an Eye of Ender, an Ender Pearl, a compass, a spyglass and a furnace. The top bar is true size (1×); the lower one is a phone's GUI scale.</p>
      <div class="scene" id="scene">
        <div class="mcname" id="hbName" aria-live="polite"></div>
        <div class="hotbar k1" role="group" aria-label="Hotbar at 1×" data-hb></div>
        <div style="height:6px"></div>
        <div class="mcname" id="hbName2" aria-hidden="true"></div>
        <div class="hotbar k2" role="group" aria-label="Hotbar at 2×" data-hb></div>
      </div>
      <div class="seg" role="group" aria-label="Scene">
        <button data-scene="day" aria-pressed="true">Day</button>
        <button data-scene="night" aria-pressed="false">Night</button>
      </div>
    </section>

    <section class="card" aria-labelledby="h-inv">
      <h2 id="h-inv">In the inventory</h2>
      <p class="note">The light-grey Bedrock panel and the dark one. Tap an item to see its tooltip.</p>
      <div class="invwrap">
        <div class="inv lite" role="group" aria-label="Inventory, light panel" data-inv></div>
        <div class="tip" id="invTip"></div>
        <div class="inv dark" role="group" aria-label="Inventory, dark panel" data-inv></div>
      </div>
    </section>

    <section class="card" aria-labelledby="h-tip">
      <h2 id="h-tip">Tooltips</h2>
      <div class="tips">
        <div class="tip">BlockyFox's Tricorder<br /><span class="lo">Points where Dave says</span><br /><span class="id">jbrain:tricorder</span></div>
        <div class="tip">Power Pack<br /><span class="lo">Keeps your things, or takes you somewhere</span><br /><span class="id">jbrain:power_pack</span></div>
        <div class="tip">Replicator<br /><span class="lo">Ask Dave to program it</span></div>
        <div class="tip">Iron Replicator<br /><span class="lo">Burns any block into iron</span></div>
      </div>
      <p class="note">The Tricorder and Power Pack lore lines are M9's and M10's. The two Replicator lines are placeholders.</p>
    </section>

    <section class="card" aria-labelledby="h-face">
      <h2 id="h-face">The Replicator's faces</h2>
      <p class="note">Each 16×16 at 8×. The top is the recipe seen from above: eight Power Packs around the furnace.</p>
      <div class="faces">
        <figure><img src="__rep_front__" alt="Front, blank" /><figcaption>Front · blank</figcaption></figure>
        <figure><img src="__rep_front_iron__" alt="Front, programmed for iron" /><figcaption>Front · Iron Replicator</figcaption></figure>
        <figure><img src="__rep_side__" alt="Side" /><figcaption>Side</figcaption></figure>
        <figure><img src="__rep_top__" alt="Top" /><figcaption>Top</figcaption></figure>
      </div>
    </section>

    <section class="card" aria-labelledby="h-hand">
      <h2 id="h-hand">In hand</h2>
      <div class="hand" role="img" aria-label="First-person view holding the Tricorder, with the waypoint arrow in the actionbar">
        <div class="xh"></div>
        <div class="ab">↖ Home · 412 blocks</div>
        <img class="held" src="__tricorder__" alt="" />
      </div>
      <p class="note">Held, it shows the waypoint arrow (M9). Anyone but its owner gets static.</p>
    </section>
  </div></div>

<script>
const IMG = {tricorder:"__tricorder__", pack:"__power_pack__", eye:"__eye__", pearl:"__pearl__", compass:"__compass__", spyglass:"__spyglass__"};
const NAMES = {tricorder:"BlockyFox's Tricorder", pack:"Power Pack", eye:"Eye of Ender", pearl:"Ender Pearl", compass:"Compass", spyglass:"Spyglass", furnace:"Furnace", rep:"Replicator", iron:"Iron Replicator"};
const LORE = {tricorder:"Points where Dave says", pack:"Keeps your things, or takes you somewhere", rep:"Ask Dave to program it", iron:"Burns any block into iron"};

function icon(kind, k){
  if (kind === "pack"){ const c = document.createElement("canvas"); c.width = c.height = 128; c.className = "pk"; return c; }
  if (kind === "rep" || kind === "iron" || kind === "furnace"){
    const s = document.createElement("span");
    s.className = "blk " + (kind === "furnace" ? "fur" : "rep" + (kind === "iron" ? " iron" : ""));
    s.style.setProperty("--s", k); s.innerHTML = '<i class="t"></i><i class="l"></i><i class="r"></i>';
    return s;
  }
  const i = new Image(); i.src = IMG[kind]; i.alt = ""; return i;
}
function slot(kind, count, k){
  const b = document.createElement("button"); b.className = "sl"; b.type = "button";
  b.setAttribute("aria-pressed", "false");
  if (kind){ b.dataset.kind = kind; b.setAttribute("aria-label", NAMES[kind] + (count ? ", " + count : "")); b.append(icon(kind, k));
    if (count){ const n = document.createElement("span"); n.className = "cnt num"; n.textContent = count; b.append(n); } }
  else b.setAttribute("aria-label", "Empty slot");
  return b;
}

const HOT = [["eye",12],["pearl",7],["tricorder"],["pack",3],["compass"],["spyglass"],["furnace"],["rep"],["iron"]];
document.querySelectorAll("[data-hb]").forEach((hb, n) => {
  const k = n === 0 ? 1 : 2;
  HOT.forEach(([kind, c]) => hb.append(slot(kind, c, k)));
});
function selectHot(i){
  document.querySelectorAll("[data-hb]").forEach(hb => [...hb.children].forEach((b, j) => b.setAttribute("aria-pressed", j === i ? "true" : "false")));
  const nm = NAMES[HOT[i][0]] || "";
  document.getElementById("hbName").textContent = nm;
  document.getElementById("hbName2").textContent = nm;
}
document.querySelectorAll("[data-hb]").forEach(hb => [...hb.children].forEach((b, j) => b.addEventListener("click", () => selectHot(j))));
selectHot(2);

const INV = [["tricorder"],["pack",16],["pack",3],["eye",12],["pearl",16],["compass"],["spyglass"],["rep"],["iron"],
             ["furnace",4],["eye",9],[],["pack",16],[],["pearl",3],[],[],[],
             [],[],[],[],[],[],[],[],[]];
document.querySelectorAll("[data-inv]").forEach(g => INV.forEach(([kind, c]) => g.append(slot(kind, c, 2))));
function tip(kind, count){
  const t = document.getElementById("invTip");
  if (!kind){ t.textContent = "Empty slot"; return; }
  t.innerHTML = "";
  t.append(NAMES[kind] + (count ? " (" + count + ")" : ""));
  if (LORE[kind]){ t.append(document.createElement("br")); const l = document.createElement("span"); l.className = "lo"; l.textContent = LORE[kind]; t.append(l); }
}
document.querySelectorAll("[data-inv]").forEach(g => [...g.children].forEach((b, j) => b.addEventListener("click", () => {
  document.querySelectorAll("[data-inv] .sl").forEach(x => x.setAttribute("aria-pressed", "false"));
  document.querySelectorAll("[data-inv]").forEach(gg => gg.children[j].setAttribute("aria-pressed", "true"));
  tip(INV[j][0], INV[j][1]);
})));
document.querySelectorAll("[data-inv]").forEach(g => g.children[1].setAttribute("aria-pressed", "true"));
tip("pack", 16);

document.querySelectorAll("[data-scene]").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll("[data-scene]").forEach(x => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
  document.getElementById("scene").classList.toggle("night", b.dataset.scene === "night");
}));

// The enchant glint: a purple band sweeping across the icon's own pixels only.
const reduce = matchMedia("(prefers-reduced-motion: reduce)");
let moving = !reduce.matches;
const packImg = new Image(); packImg.src = IMG.pack;
const gl = document.createElement("canvas"); gl.width = gl.height = 128;
const gx = gl.getContext("2d");
function drawPack(c, t){
  const x = c.getContext("2d");
  x.imageSmoothingEnabled = false; x.globalCompositeOperation = "source-over";
  x.clearRect(0, 0, 128, 128); x.drawImage(packImg, 0, 0, 128, 128);
  gx.globalCompositeOperation = "source-over"; gx.clearRect(0, 0, 128, 128);
  const off = (t / 3200 % 1) * 384 - 192;
  const g = gx.createLinearGradient(off, off * .4, off + 128, off * .4 + 128);
  g.addColorStop(0, "rgba(128,64,204,0)"); g.addColorStop(.45, "rgba(150,90,230,.32)");
  g.addColorStop(.55, "rgba(180,120,255,.4)"); g.addColorStop(1, "rgba(128,64,204,0)");
  gx.fillStyle = "rgba(110,60,190,.07)"; gx.fillRect(0, 0, 128, 128);
  gx.fillStyle = g; gx.fillRect(0, 0, 128, 128);
  gx.globalCompositeOperation = "destination-in"; gx.imageSmoothingEnabled = false;
  gx.drawImage(packImg, 0, 0, 128, 128);
  x.globalCompositeOperation = "lighter"; x.drawImage(gl, 0, 0);
}
let last = 0;
function frame(t){
  if (t - last > 33){ last = t; document.querySelectorAll("canvas.pk").forEach(c => drawPack(c, t)); }
  if (moving) requestAnimationFrame(frame);
}
function still(){ document.querySelectorAll("canvas.pk").forEach(c => drawPack(c, 1100)); }
const glintBtn = document.getElementById("glintBtn");
function setMoving(m){ moving = m; glintBtn.setAttribute("aria-pressed", m ? "true" : "false"); glintBtn.textContent = m ? "Moving" : "Still"; if (m) requestAnimationFrame(frame); else still(); }
glintBtn.addEventListener("click", () => setMoving(!moving));
reduce.addEventListener("change", () => setMoving(!reduce.matches));
packImg.onload = () => setMoving(moving);

const sys = matchMedia("(prefers-color-scheme: light)");
let themePick = "system";
function applyTheme(){ document.documentElement.dataset.theme = themePick === "system" ? (sys.matches ? "light" : "dark") : themePick; }
document.querySelectorAll("[data-theme-btn]").forEach(b => b.addEventListener("click", () => {
  themePick = b.dataset.themeBtn;
  document.querySelectorAll("[data-theme-btn]").forEach(x => x.setAttribute("aria-pressed", x === b ? "true" : "false"));
  applyTheme();
}));
sys.addEventListener("change", applyTheme);
applyTheme();
</script>
</body>
</html>
"""


def build():
    van = HERE / "vanilla"
    for v, meta in VARIANTS.items():
        d = HERE / v
        subs = {
            "__tricorder__": uri(d / "tricorder.png"),
            "__power_pack__": uri(d / "power_pack.png"),
            "__rep_front__": uri(d / "replicator_front.png"),
            "__rep_front_iron__": uri(d / "replicator_front_iron.png"),
            "__rep_side__": uri(d / "replicator_side.png"),
            "__rep_top__": uri(d / "replicator_top.png"),
            "__fur_front__": uri(van / "furnace_front.png"),
            "__fur_side__": uri(van / "furnace_side.png"),
            "__fur_top__": uri(van / "furnace_top.png"),
            "__eye__": uri(van / "ender_eye.png"),
            "__pearl__": uri(van / "ender_pearl.png"),
            "__compass__": uri(van / "compass.png"),
            "__spyglass__": uri(van / "spyglass.png"),
            "__V__": v.upper(),
            "__NAME__": meta["name"],
            "__THESIS__": meta["thesis"],
            "__D_TRICORDER__": meta["tricorder"],
            "__D_PACK__": meta["pack"],
            "__D_REP__": meta["rep"],
            "__NAV__": "".join(
                f'<a href="{m["file"]}"'
                + (' aria-current="page"' if k == v else "")
                + f">{k.upper()} · {m['name']}</a>"
                for k, m in VARIANTS.items()
            ),
        }
        page = PAGE
        for key, val in subs.items():
            page = page.replace(key, val)
        assert "__" not in page.replace("__file__", ""), "unfilled placeholder"
        (HERE / meta["file"]).write_text(page)


if __name__ == "__main__":
    build()
