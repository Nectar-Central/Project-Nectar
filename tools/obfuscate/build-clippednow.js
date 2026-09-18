/* Medium-strength obfuscation for the ClippedNow standalone clipper.
 *
 * Usage: node tools/obfuscate/build-clippednow.js
 * Reads:  companion/clippednow.user.js          (source of truth)
 * Writes: companion/clippednow.obfs.user.js    (public install file -> CDN)
 *
 * Same medium preset as build.js: string-array (base64, 50%) + hex
 * identifiers + string splitting. Deliberately OFF:
 * controlFlowFlattening / deadCodeInjection / debugProtection /
 * selfDefending — those bloat runtime, hurt low-end PCs, and
 * selfDefending breaks Tampermonkey GM_* bridges.
 */
const fs = require("fs");
const path = require("path");
const JavaScriptObfuscator = require("javascript-obfuscator");

const ROOT = path.join(__dirname, "..", "..");
const SRC = path.join(ROOT, "companion", "clippednow.user.js");
const OUT = path.join(ROOT, "companion", "clippednow.obfs.user.js");

const CDN_FILE_URL =
  "https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/cdn/clippednow.latest.user.js";

const src = fs.readFileSync(SRC, "utf8");
const marker = "==/UserScript==";
const idx = src.indexOf(marker);
if (idx === -1) throw new Error("userscript header end marker not found");
let header = src.slice(0, idx + marker.length);
const body = src.slice(idx + marker.length);

header = header.replace(
  /https:\/\/raw\.githubusercontent\.com\/\S+/g,
  CDN_FILE_URL
);

const result = JavaScriptObfuscator.obfuscate(body, {
  target: "browser",
  compact: true,
  controlFlowFlattening: false,
  deadCodeInjection: false,
  debugProtection: false,
  selfDefending: false,
  renameGlobals: false, // keep GM_getValue / GM_setValue / unsafeWindow bridges
  identifierNamesGenerator: "hexadecimal",
  stringArray: true,
  stringArrayThreshold: 0.5,
  stringArrayEncoding: ["base64"],
  rotateStringArray: true,
  shuffleStringArray: true,
  splitStrings: true,
  splitStringsChunkLength: 8,
  numbersToExpressions: true,
  simplify: true,
  unicodeEscapeSequence: false,
}).getObfuscatedCode();

fs.writeFileSync(OUT, header + "\n" + result + "\n");
const bytes = fs.statSync(OUT).size;
console.log(`wrote ${OUT} (${bytes} bytes, source was ${fs.statSync(SRC).size} bytes)`);
