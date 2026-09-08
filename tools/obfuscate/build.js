/* Medium-strength obfuscation for the Nectar userscript.
 *
 * Usage: node tools/obfuscate/build.js
 * Reads:  companion/buildnow-companion.user.js   (source of truth, ships private)
 * Writes: companion/buildnow-companion.obfs.user.js (public install file -> CDN)
 *
 * Medium preset: string-array (base64, 50%) + hex identifiers + string
 * splitting. Deliberately OFF: controlFlowFlattening / deadCodeInjection /
 * debugProtection / selfDefending — those bloat runtime, hurt low-end PCs,
 * and selfDefending breaks Tampermonkey GM_* bridges.
 */
const fs = require("fs");
const path = require("path");
const JavaScriptObfuscator = require("javascript-obfuscator");

const ROOT = path.join(__dirname, "..", "..");
const SRC = path.join(ROOT, "companion", "buildnow-companion.user.js");
const OUT = path.join(ROOT, "companion", "buildnow-companion.obfs.user.js");

// Public install location (Supabase Storage public bucket `cdn`, stable name).
// Tampermonkey @updateURL/@downloadURL point here so reinstall + auto-update
// work while the GitHub repo stays private.
const CDN_FILE_URL =
  "https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/cdn/nectar-companion.latest.user.js";

const src = fs.readFileSync(SRC, "utf8");
const marker = "==/UserScript==";
const idx = src.indexOf(marker);
if (idx === -1) throw new Error("userscript header end marker not found");
let header = src.slice(0, idx + marker.length);
const body = src.slice(idx + marker.length);

// Point auto-update at the CDN (works with private repo); keep everything else.
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
  renameGlobals: false, // keep GM_xmlhttpRequest / unsafeWindow / GM_* bridges
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
