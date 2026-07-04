// The JPet 3D Wall renderer (docs/plans/JPET_PLAN.md W2) — a self-contained WebGL
// scene, isolated behind this module so WallScreen stays testable (jsdom has no
// WebGL, so the screen's test vi.mock()s this file, exactly like leafletMap).
//
// It draws the Tron/synthwave room + a wireframe robot and animates it toward the
// server-authoritative state (position target, emotion, asleep); the walk
// interpolation, bob, blink, and bloom are client-side between updates. Pointer
// input raycasts the floor (click-to-walk) and the pet (poke) and reports back via
// callbacks, which WallScreen turns into /api/pet/command POSTs. Ported from the
// verified mock docs/mocks/jpet/06-room-3d.html.
//
// Types: matrices/vectors are fixed-length tuples accessed by literal indices —
// under tsconfig's noUncheckedIndexedAccess that's the only form typed `number`
// (plain-array / variable-index reads carry `undefined`), so the math stays clean.

export interface PetSceneState {
  target_x: number; // normalized floor coords in [-1, 1]
  target_z: number;
  emotion: string;
  asleep: boolean;
}

export interface PetSceneHandlers {
  /** A floor point the pet was sent to (normalized [-1, 1]). */
  onFloor: (x: number, z: number) => void;
  /** The robot itself was clicked. */
  onPoke: () => void;
}

export interface PetScene {
  update: (state: PetSceneState) => void;
  destroy: () => void;
}

const R = 6.2; // room half-extent; normalized [-1,1] target scales by this
const WALL = 5.0;

type Vec3 = [number, number, number];
// prettier-ignore
type M4 = [
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
  number,
];

const m4 = {
  mul(a: M4, b: M4): M4 {
    const [a0, a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12, a13, a14, a15] = a;
    const [b0, b1, b2, b3, b4, b5, b6, b7, b8, b9, b10, b11, b12, b13, b14, b15] = b;
    return [
      b0 * a0 + b1 * a4 + b2 * a8 + b3 * a12,
      b0 * a1 + b1 * a5 + b2 * a9 + b3 * a13,
      b0 * a2 + b1 * a6 + b2 * a10 + b3 * a14,
      b0 * a3 + b1 * a7 + b2 * a11 + b3 * a15,
      b4 * a0 + b5 * a4 + b6 * a8 + b7 * a12,
      b4 * a1 + b5 * a5 + b6 * a9 + b7 * a13,
      b4 * a2 + b5 * a6 + b6 * a10 + b7 * a14,
      b4 * a3 + b5 * a7 + b6 * a11 + b7 * a15,
      b8 * a0 + b9 * a4 + b10 * a8 + b11 * a12,
      b8 * a1 + b9 * a5 + b10 * a9 + b11 * a13,
      b8 * a2 + b9 * a6 + b10 * a10 + b11 * a14,
      b8 * a3 + b9 * a7 + b10 * a11 + b11 * a15,
      b12 * a0 + b13 * a4 + b14 * a8 + b15 * a12,
      b12 * a1 + b13 * a5 + b14 * a9 + b15 * a13,
      b12 * a2 + b13 * a6 + b14 * a10 + b15 * a14,
      b12 * a3 + b13 * a7 + b14 * a11 + b15 * a15,
    ];
  },
  persp(fovy: number, asp: number, near: number, far: number): M4 {
    const f = 1 / Math.tan(fovy / 2);
    const nf = 1 / (near - far);
    return [
      f / asp,
      0,
      0,
      0,
      0,
      f,
      0,
      0,
      0,
      0,
      (far + near) * nf,
      -1,
      0,
      0,
      2 * far * near * nf,
      0,
    ];
  },
  lookAt(e: Vec3, c: Vec3, up: Vec3): M4 {
    let z0 = e[0] - c[0];
    let z1 = e[1] - c[1];
    let z2 = e[2] - c[2];
    let l = 1 / Math.hypot(z0, z1, z2);
    z0 *= l;
    z1 *= l;
    z2 *= l;
    let x0 = up[1] * z2 - up[2] * z1;
    let x1 = up[2] * z0 - up[0] * z2;
    let x2 = up[0] * z1 - up[1] * z0;
    l = Math.hypot(x0, x1, x2);
    if (!l) {
      x0 = x1 = x2 = 0;
    } else {
      l = 1 / l;
      x0 *= l;
      x1 *= l;
      x2 *= l;
    }
    const y0 = z1 * x2 - z2 * x1;
    const y1 = z2 * x0 - z0 * x2;
    const y2 = z0 * x1 - z1 * x0;
    return [
      x0,
      y0,
      z0,
      0,
      x1,
      y1,
      z1,
      0,
      x2,
      y2,
      z2,
      0,
      -(x0 * e[0] + x1 * e[1] + x2 * e[2]),
      -(y0 * e[0] + y1 * e[1] + y2 * e[2]),
      -(z0 * e[0] + z1 * e[1] + z2 * e[2]),
      1,
    ];
  },
  trans(a: M4, x: number, y: number, z: number): M4 {
    const o: M4 = [...a];
    o[12] = a[0] * x + a[4] * y + a[8] * z + a[12];
    o[13] = a[1] * x + a[5] * y + a[9] * z + a[13];
    o[14] = a[2] * x + a[6] * y + a[10] * z + a[14];
    o[15] = a[3] * x + a[7] * y + a[11] * z + a[15];
    return o;
  },
  rotY(a: M4, r: number): M4 {
    const s = Math.sin(r);
    const c = Math.cos(r);
    const o: M4 = [...a];
    o[0] = a[0] * c - a[8] * s;
    o[1] = a[1] * c - a[9] * s;
    o[2] = a[2] * c - a[10] * s;
    o[3] = a[3] * c - a[11] * s;
    o[8] = a[0] * s + a[8] * c;
    o[9] = a[1] * s + a[9] * c;
    o[10] = a[2] * s + a[10] * c;
    o[11] = a[3] * s + a[11] * c;
    return o;
  },
  rotX(a: M4, r: number): M4 {
    const s = Math.sin(r);
    const c = Math.cos(r);
    const o: M4 = [...a];
    o[4] = a[4] * c + a[8] * s;
    o[5] = a[5] * c + a[9] * s;
    o[6] = a[6] * c + a[10] * s;
    o[7] = a[7] * c + a[11] * s;
    o[8] = a[8] * c - a[4] * s;
    o[9] = a[9] * c - a[5] * s;
    o[10] = a[10] * c - a[6] * s;
    o[11] = a[11] * c - a[7] * s;
    return o;
  },
  scale(a: M4, x: number, y: number, z: number): M4 {
    const o: M4 = [...a];
    o[0] = a[0] * x;
    o[1] = a[1] * x;
    o[2] = a[2] * x;
    o[3] = a[3] * x;
    o[4] = a[4] * y;
    o[5] = a[5] * y;
    o[6] = a[6] * y;
    o[7] = a[7] * y;
    o[8] = a[8] * z;
    o[9] = a[9] * z;
    o[10] = a[10] * z;
    o[11] = a[11] * z;
    return o;
  },
  ident(): M4 {
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
  },
  invert(a: M4): M4 | null {
    const [a00, a01, a02, a03, a10, a11, a12, a13, a20, a21, a22, a23, a30, a31, a32, a33] = a;
    const b00 = a00 * a11 - a01 * a10;
    const b01 = a00 * a12 - a02 * a10;
    const b02 = a00 * a13 - a03 * a10;
    const b03 = a01 * a12 - a02 * a11;
    const b04 = a01 * a13 - a03 * a11;
    const b05 = a02 * a13 - a03 * a12;
    const b06 = a20 * a31 - a21 * a30;
    const b07 = a20 * a32 - a22 * a30;
    const b08 = a20 * a33 - a23 * a30;
    const b09 = a21 * a32 - a22 * a31;
    const b10 = a21 * a33 - a23 * a31;
    const b11 = a22 * a33 - a23 * a32;
    let d = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06;
    if (!d) return null;
    d = 1 / d;
    return [
      (a11 * b11 - a12 * b10 + a13 * b09) * d,
      (a02 * b10 - a01 * b11 - a03 * b09) * d,
      (a31 * b05 - a32 * b04 + a33 * b03) * d,
      (a22 * b04 - a21 * b05 - a23 * b03) * d,
      (a12 * b08 - a10 * b11 - a13 * b07) * d,
      (a00 * b11 - a02 * b08 + a03 * b07) * d,
      (a32 * b02 - a30 * b05 - a33 * b01) * d,
      (a20 * b05 - a22 * b02 + a23 * b01) * d,
      (a10 * b10 - a11 * b08 + a13 * b06) * d,
      (a01 * b08 - a00 * b10 - a03 * b06) * d,
      (a30 * b04 - a31 * b02 + a33 * b00) * d,
      (a21 * b02 - a20 * b04 - a23 * b00) * d,
      (a11 * b07 - a10 * b09 - a12 * b06) * d,
      (a00 * b09 - a01 * b07 + a02 * b06) * d,
      (a31 * b01 - a30 * b03 - a32 * b00) * d,
      (a20 * b03 - a21 * b01 + a22 * b00) * d,
    ];
  },
  apply(m: M4, p: Vec3): Vec3 {
    const [x, y, z] = p;
    const w = m[3] * x + m[7] * y + m[11] * z + m[15] || 1;
    return [
      (m[0] * x + m[4] * y + m[8] * z + m[12]) / w,
      (m[1] * x + m[5] * y + m[9] * z + m[13]) / w,
      (m[2] * x + m[6] * y + m[10] * z + m[14]) / w,
    ];
  },
};

const hx = (h: string): Vec3 => [
  Number.parseInt(h.slice(1, 3), 16) / 255,
  Number.parseInt(h.slice(3, 5), 16) / 255,
  Number.parseInt(h.slice(5, 7), 16) / 255,
];
const CY = hx("#3bf0ff");
const MG = hx("#ff4fd8");
const GD = hx("#ffd23f");
const RD = hx("#ff477e");
const BL = hx("#6a7bff");
const OR = hx("#ffb03a");

export function createPetScene(
  canvas: HTMLCanvasElement,
  bloom: HTMLCanvasElement,
  handlers: PetSceneHandlers,
): PetScene {
  const gl = canvas.getContext("webgl", {
    antialias: true,
    alpha: false,
    preserveDrawingBuffer: true,
  });
  const bctx = bloom.getContext("2d");
  if (!gl || !bctx) {
    return { update: () => {}, destroy: () => {} };
  }

  const VS = "attribute vec3 p;uniform mat4 mvp;void main(){gl_Position=mvp*vec4(p,1.0);}";
  const FS =
    "precision mediump float;uniform vec3 col;uniform float alpha;void main(){gl_FragColor=vec4(col,alpha);}";
  const sh = (t: number, s: string): WebGLShader => {
    const o = gl.createShader(t) as WebGLShader;
    gl.shaderSource(o, s);
    gl.compileShader(o);
    return o;
  };
  const prog = gl.createProgram() as WebGLProgram;
  gl.attachShader(prog, sh(gl.VERTEX_SHADER, VS));
  gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, FS));
  gl.linkProgram(prog);
  gl.useProgram(prog);
  const aP = gl.getAttribLocation(prog, "p");
  const uMVP = gl.getUniformLocation(prog, "mvp");
  const uCol = gl.getUniformLocation(prog, "col");
  const uAl = gl.getUniformLocation(prog, "alpha");
  gl.enable(gl.DEPTH_TEST);
  gl.enable(gl.BLEND);
  gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
  gl.clearColor(0.03, 0.015, 0.06, 1);

  const buf = (arr: number[]): WebGLBuffer => {
    const b = gl.createBuffer() as WebGLBuffer;
    gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(arr), gl.STATIC_DRAW);
    return b;
  };
  const draw = (
    b: WebGLBuffer,
    count: number,
    mode: number,
    mvp: M4,
    col: Vec3,
    alpha = 1,
  ): void => {
    gl.bindBuffer(gl.ARRAY_BUFFER, b);
    gl.enableVertexAttribArray(aP);
    gl.vertexAttribPointer(aP, 3, gl.FLOAT, false, 0, 0);
    gl.uniformMatrix4fv(uMVP, false, new Float32Array(mvp));
    gl.uniform3fv(uCol, col);
    gl.uniform1f(uAl, alpha);
    gl.drawArrays(mode, 0, count);
  };

  const gridLines = (): number[] => {
    const v: number[] = [];
    for (let x = -R; x <= R + 1e-3; x += 1) v.push(x, 0, -R, x, 0, R);
    for (let z = -R; z <= R + 1e-3; z += 1) v.push(-R, 0, z, R, 0, z);
    return v;
  };
  const backWall = (): number[] => {
    const v: number[] = [];
    for (let x = -R; x <= R + 1e-3; x += 1) v.push(x, 0, -R, x, WALL, -R);
    for (let y = 0; y <= WALL + 1e-3; y += 1) v.push(-R, y, -R, R, y, -R);
    return v;
  };
  const sideWalls = (): number[] => {
    const v: number[] = [];
    for (const sx of [-R, R]) {
      for (let z = -R; z <= R + 1e-3; z += 1) v.push(sx, 0, z, sx, WALL, z);
      for (let y = 0; y <= WALL + 1e-3; y += 1) v.push(sx, y, -R, sx, y, R);
    }
    return v;
  };
  const cubeEdges = (): number[] => {
    const c = 0.5;
    const p: Vec3[] = [
      [-c, -c, -c],
      [c, -c, -c],
      [c, c, -c],
      [-c, c, -c],
      [-c, -c, c],
      [c, -c, c],
      [c, c, c],
      [-c, c, c],
    ];
    const e: [number, number][] = [
      [0, 1],
      [1, 2],
      [2, 3],
      [3, 0],
      [4, 5],
      [5, 6],
      [6, 7],
      [7, 4],
      [0, 4],
      [1, 5],
      [2, 6],
      [3, 7],
    ];
    const v: number[] = [];
    for (const [a, b] of e) {
      const pa = p[a];
      const pb = p[b];
      if (pa && pb) v.push(...pa, ...pb);
    }
    return v;
  };
  const circle = (n: number): number[] => {
    const v: number[] = [];
    for (let i = 0; i < n; i++) {
      const a = (i / n) * Math.PI * 2;
      v.push(Math.cos(a), 0, Math.sin(a));
    }
    return v;
  };
  const gFloor = buf(gridLines());
  const nFloor = gridLines().length / 3;
  const gBack = buf(backWall());
  const nBack = backWall().length / 3;
  const gSide = buf(sideWalls());
  const nSide = sideWalls().length / 3;
  const gCube = buf(cubeEdges());
  const gCirc = buf(circle(40));

  const P = {
    x: 0,
    z: 0,
    tx: 0,
    tz: 0,
    facing: 0,
    walkSpeed: 0,
    emotion: "happy",
    asleep: false,
    blink: 0,
    nextBlink: 2,
  };
  let vp: M4 = m4.ident();
  let W = 1;
  let H = 1;
  let DPR = 1;

  const resize = (): void => {
    DPR = Math.min(2, devicePixelRatio || 1);
    W = canvas.clientWidth || canvas.width;
    H = canvas.clientHeight || canvas.height;
    canvas.width = W * DPR;
    canvas.height = H * DPR;
    gl.viewport(0, 0, canvas.width, canvas.height);
    bloom.width = W * DPR;
    bloom.height = H * DPR;
  };
  addEventListener("resize", resize);

  const project = (w: Vec3): { x: number; y: number } | null => {
    const c = m4.apply(vp, w);
    if (c[2] > 1) return null;
    return { x: (c[0] * 0.5 + 0.5) * W, y: (1 - (c[1] * 0.5 + 0.5)) * H };
  };
  const floorPick = (px: number, py: number): { x: number; z: number } | null => {
    const inv = m4.invert(vp);
    if (!inv) return null;
    const nx = (px / W) * 2 - 1;
    const ny = 1 - (py / H) * 2;
    const a = m4.apply(inv, [nx, ny, -1]);
    const b = m4.apply(inv, [nx, ny, 1]);
    const dy = b[1] - a[1];
    if (Math.abs(dy) < 1e-6) return null;
    const t = -a[1] / dy;
    if (t < 0) return null;
    return { x: a[0] + (b[0] - a[0]) * t, z: a[2] + (b[2] - a[2]) * t };
  };
  const onDown = (e: PointerEvent): void => {
    const r = canvas.getBoundingClientRect();
    const px = e.clientX - r.left;
    const py = e.clientY - r.top;
    const head = project([P.x, 1.0, P.z]);
    if (head && Math.hypot(px - head.x, py - head.y) < 70) {
      handlers.onPoke();
      return;
    }
    const f = floorPick(px, py);
    if (f) {
      handlers.onFloor(Math.max(-1, Math.min(1, f.x / R)), Math.max(-1, Math.min(1, f.z / R)));
    }
  };
  canvas.addEventListener("pointerdown", onDown);

  const part = (
    model: M4,
    lx: number,
    ly: number,
    lz: number,
    sx: number,
    sy: number,
    sz: number,
    col: Vec3,
    alpha = 1,
  ): void => {
    draw(
      gCube,
      24,
      gl.LINES,
      m4.mul(vp, m4.scale(m4.trans(model, lx, ly, lz), sx, sy, sz)),
      col,
      alpha,
    );
  };

  const drawRobot = (t: number): void => {
    const walking = P.walkSpeed > 0.05 && !P.asleep;
    const bob = walking ? Math.sin(t * 10) * 0.05 : Math.sin(t * 2) * 0.03;
    const sit = P.asleep ? -0.28 : 0;
    let model = m4.ident();
    model = m4.trans(model, P.x, 0, P.z);
    model = m4.rotY(model, P.facing);
    const baseY = bob + sit;
    draw(
      gCirc,
      40,
      gl.LINE_LOOP,
      m4.mul(vp, m4.scale(m4.trans(m4.ident(), P.x, 0.02, P.z), 0.55, 1, 0.55)),
      MG,
      0.5,
    );
    const swing = walking ? Math.sin(t * 10) : 0;
    const sw2 = walking ? Math.sin(t * 10 + Math.PI) : 0;
    part(model, -0.22, baseY + 0.28, swing * 0.16, 0.17, 0.5, 0.17, CY);
    part(model, 0.22, baseY + 0.28, sw2 * 0.16, 0.17, 0.5, 0.17, CY);
    part(model, 0, baseY + 0.92, 0, 0.82, 0.72, 0.56, MG);
    const mc = P.emotion === "sad" ? RD : P.emotion === "hungry" ? OR : P.asleep ? BL : CY;
    part(model, 0, baseY + 0.92, 0.3, 0.34, 0.34, 0.06, mc);
    part(model, 0, baseY + 0.92, 0.3, 0.2, 0.2, 0.05, mc, 0.6);
    const asw = walking ? Math.sin(t * 10 + Math.PI) * 0.25 : Math.sin(t * 2 + 1) * 0.06;
    part(m4.rotX(m4.trans(model, -0.52, baseY + 1.06, 0), asw), 0, -0.22, 0, 0.15, 0.5, 0.15, MG);
    part(m4.rotX(m4.trans(model, 0.52, baseY + 1.06, 0), -asw), 0, -0.22, 0, 0.15, 0.5, 0.15, MG);
    part(model, 0, baseY + 1.58, 0, 0.7, 0.52, 0.56, CY);
    part(model, -0.38, baseY + 1.58, 0, 0.09, 0.14, 0.14, MG);
    part(model, 0.38, baseY + 1.58, 0, 0.09, 0.14, 0.14, MG);
    const eb = P.asleep || P.blink > 0.6 ? 0.02 : 0.13;
    part(model, -0.16, baseY + 1.6, 0.29, 0.13, eb, 0.05, GD);
    part(model, 0.16, baseY + 1.6, 0.29, 0.13, eb, 0.05, GD);
    const mw =
      P.emotion === "excited"
        ? 0.3
        : P.emotion === "happy"
          ? 0.24
          : P.emotion === "sad" || P.emotion === "hungry"
            ? 0.14
            : 0.2;
    part(model, 0, baseY + 1.44, 0.29, mw, 0.05, 0.04, GD);
    part(model, 0, baseY + 1.9, 0, 0.03, 0.22, 0.03, MG);
    part(model, 0, baseY + 2.04, 0, 0.09, 0.09, 0.09, GD);
  };

  let raf = 0;
  let last = 0;
  const frame = (ts: number): void => {
    const dt = Math.min(0.05, (ts - last) / 1000 || 0);
    last = ts;
    const t = ts / 1000;
    const eye: Vec3 = [0, 4.6, 11.5];
    const view = m4.lookAt(eye, [0, 1.3, 0], [0, 1, 0]);
    const proj = m4.persp((46 * Math.PI) / 180, W / H, 0.1, 100);
    vp = m4.mul(proj, view);

    const dx = P.tx - P.x;
    const dz = P.tz - P.z;
    const dist = Math.hypot(dx, dz);
    P.walkSpeed = 0;
    if (!P.asleep && dist > 0.06) {
      const step = Math.min(dist, 2.2 * dt);
      P.x += (dx / dist) * step;
      P.z += (dz / dist) * step;
      P.walkSpeed = 2.2;
      const want = Math.atan2(dx, dz);
      let d = want - P.facing;
      while (d > Math.PI) d -= 2 * Math.PI;
      while (d < -Math.PI) d += 2 * Math.PI;
      P.facing += d * Math.min(1, dt * 8);
    }
    P.nextBlink -= dt;
    if (P.nextBlink <= 0) {
      P.blink = 1;
      P.nextBlink = 1.6 + Math.random() * 3;
    }
    if (P.blink > 0) P.blink = Math.max(0, P.blink - dt * 7);

    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    draw(gFloor, nFloor, gl.LINES, vp, MG, 0.55);
    draw(gBack, nBack, gl.LINES, vp, MG, 0.32);
    draw(gSide, nSide, gl.LINES, vp, CY, 0.14);
    drawRobot(t);

    bctx.clearRect(0, 0, bloom.width, bloom.height);
    bctx.globalAlpha = 0.9;
    bctx.filter = "blur(7px)";
    bctx.drawImage(canvas, 0, 0, bloom.width, bloom.height);
    bctx.filter = "none";

    raf = requestAnimationFrame(frame);
  };
  resize();
  raf = requestAnimationFrame(frame);

  return {
    update(state: PetSceneState): void {
      P.tx = Math.max(-R, Math.min(R, state.target_x * R));
      P.tz = Math.max(-R, Math.min(R, state.target_z * R));
      P.emotion = state.emotion;
      P.asleep = state.asleep;
    },
    destroy(): void {
      cancelAnimationFrame(raf);
      removeEventListener("resize", resize);
      canvas.removeEventListener("pointerdown", onDown);
    },
  };
}
