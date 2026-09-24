// Obfuscate the companion userscripts, preserving the ==UserScript== header
// (Tampermonkey reads @name/@version/@match from it — it must stay plain).
//
//   companion/buildnow-companion.user.js            -> companion/buildnow-companion.obfs.user.js
//   companion/buildnow-companion.lite.user.js       -> companion/buildnow-companion.lite.obfs.user.js
//   companion/buildnow-companion.competitive.user.js -> companion/buildnow-companion.competitive.obfs.user.js
//
// Perf-safe options: NO controlFlowFlattening / deadCodeInjection /
// selfDefending / debugProtection — those add runtime overhead and risk in a
// game-page userscript where we just fought for FPS. String-array + base64 +
// identifier mangling is enough to deter casual copying.
//
// Run: npm run obfs
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import JavaScriptObfuscator from 'javascript-obfuscator';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const JOBS = [
  ['companion/buildnow-companion.user.js', 'companion/buildnow-companion.obfs.user.js'],
  ['companion/buildnow-companion.lite.user.js', 'companion/buildnow-companion.lite.obfs.user.js'],
  ['companion/buildnow-companion.competitive.user.js', 'companion/buildnow-companion.competitive.obfs.user.js'],
];

const OPTIONS = {
  compact: true,
  target: 'browser',
  renameGlobals: false, // GM_*, unsafeWindow, window must stay intact
  selfDefending: false,
  debugProtection: false,
  controlFlowFlattening: false, // runtime cost — never in a game userscript
  deadCodeInjection: false,
  stringArray: true,
  stringArrayEncoding: ['base64'],
  stringArrayThreshold: 0.75,
  rotateStringArray: true,
  shuffleStringArray: true,
  splitStrings: true,
  splitStringsChunkLength: 5,
  numbersToExpressions: true,
  identifierNamesGenerator: 'hexadecimal',
  disableConsoleOutput: false,
  simplify: true,
};

for (const [srcRel, dstRel] of JOBS) {
  const src = readFileSync(join(root, srcRel), 'utf8');
  const endMarker = '==/UserScript==';
  const idx = src.indexOf(endMarker);
  if (idx === -1) throw new Error(`no UserScript header in ${srcRel}`);
  const headerEnd = src.indexOf('\n', idx) + 1;
  const header = src.slice(0, headerEnd);
  const body = src.slice(headerEnd);
  const out = JavaScriptObfuscator.obfuscate(body, OPTIONS).getObfuscatedCode();
  writeFileSync(join(root, dstRel), header + out);
  console.log(`obfuscated: ${srcRel} (${src.length} chars) -> ${dstRel} (${header.length + out.length} chars)`);
}
