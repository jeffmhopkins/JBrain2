// Generates the solid-color placeholder PWA icons. Real artwork replaces these
// later; the manifest just needs valid PNGs at the declared sizes.
import { writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { crc32, deflateSync } from "node:zlib";

const COLOR = [0x10, 0x13, 0x1a]; // matches the manifest theme color

function chunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([length, body, crc]);
}

function solidPng(size) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(size, 0);
  ihdr.writeUInt32BE(size, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // color type: truecolor RGB

  // Each scanline is a filter byte (0 = none) followed by RGB pixels.
  const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(size * 3)]);
  for (let x = 0; x < size; x++) {
    row[1 + x * 3] = COLOR[0];
    row[2 + x * 3] = COLOR[1];
    row[3 + x * 3] = COLOR[2];
  }
  const raw = Buffer.concat(Array.from({ length: size }, () => row));

  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const publicDir = join(dirname(fileURLToPath(import.meta.url)), "..", "public");
for (const size of [192, 512]) {
  writeFileSync(join(publicDir, `pwa-${size}x${size}.png`), solidPng(size));
}
