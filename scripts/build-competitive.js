// Builds companion/buildnow-companion.competitive.user.js from the LITE companion.
// Run AFTER lite: npm run competitive  (== node scripts/build-lite.js && node scripts/build-competitive.js)
//
// COMPETITIVE keeps: capture, tokens/session, stats drawer (F2),
//                    ranked + tourney detections/panels (F9),
//                    leaderboard board tab + click-to-view profile,
//                    graphics presets restricted to None + Warm (+Custom state),
//                    lite perf trims (no FPS chip, no players/chat polls, no gfx
//                    loop, slowed heartbeat/lobby polls).
// COMPETITIVE drops: presence tab + all rich-presence pushes/probes/beacons,
//                    every gfx preset except none/warm.
// Re-run after every full-companion bump: npm run competitive
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'companion', 'buildnow-companion.lite.user.js');
const DST = path.join(__dirname, '..', 'companion', 'buildnow-companion.competitive.user.js');

if (!fs.existsSync(SRC)) {
  throw new Error('lite companion missing — run `npm run lite` first: ' + SRC);
}
let s = fs.readFileSync(SRC, 'utf8');
const origLen = s.length;

function mustReplace(from, to) {
  if (!s.includes(from)) throw new Error('pattern not found: ' + from.slice(0, 80));
  s = s.split(from).join(to);
}

function mustRemoveLine(line) {
  const from = line + '\r\n';
  if (!s.includes(from)) throw new Error('pattern not found: ' + line.slice(0, 80));
  s = s.split(from).join('');
}

// NOTE: sources use CRLF — every multi-line match pattern below spells \r\n.
// Injected code uses bare \n (same precedent as build-lite.js).

// 0. Sanity: this transform chains off lite, never off full.
if (!s.includes('const BNC_LITE = true;')) {
  throw new Error('source is not the lite companion — refusing to build competitive off full');
}
const verMatch = s.match(/const BNC_VERSION = '([^']+)'/);
if (!verMatch) throw new Error('BNC_VERSION not found in lite source');
const liteVer = verMatch[1];
if (!liteVer.endsWith('-lite')) throw new Error('expected a -lite version, got: ' + liteVer);
const compVer = liteVer.replace(/-lite$/, '-competitive');

// 1. Header: rename + competitive version. @updateURL/@downloadURL already
//    disabled by the lite build; @version stays on the base number.
mustReplace('// @name         Nectar Buildnow Companion LITE',
  '// @name         Nectar Buildnow Companion COMPETITIVE');
s = s.replace(/^\/\/ @description.*$/m,
  '// @description  Nectar Buildnow Companion COMPETITIVE v' + compVer +
  ' (stats drawer, ranked/tourney, leaderboard + profiles, warm+none graphics; no presence, no FPS chip)');

// 1b. Re-enable auto-update, pointed at COMPETITIVE's own CDN object.
//     The lite build blanks these on purpose (a lite install must never be
//     silently replaced by the full build) — and it inherited that blank here.
//     Competitive ships on the CDN too, so it needs to update from its own
//     path: never from `nectar-companion.latest` (full) and never from lite.
const COMP_CDN = 'https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/cdn/nectar-companion.competitive.latest.user.js';
s = s.replace(/^\/\/ @updateURL.*$/m, '// @updateURL    ' + COMP_CDN);
s = s.replace(/^\/\/ @downloadURL.*$/m, '// @downloadURL  ' + COMP_CDN);

// 2. Version + competitive flag right after the lite flag.
mustReplace("const BNC_VERSION = '" + liteVer + "';",
  "const BNC_VERSION = '" + compVer + "';");
mustReplace('const BNC_LITE = true; // lite: skips FPS chip, players poll, chat poll, gfx loop',
  'const BNC_LITE = true; // lite: skips FPS chip, players poll, chat poll, gfx loop\n' +
  '  const BNC_COMPETITIVE = true; // competitive: +leaderboard/profiles, warm/none gfx only; no presence\n' +
  '  function BNC_COMP(){ try{ return typeof BNC_COMPETITIVE!=="undefined"&&BNC_COMPETITIVE; }catch{ return false; } }');

// 3. Leaderboard board tab: KEPT in competitive (board + click-to-profile).
//    Only presence stays dead — no fetchLb guard here.
// 4. Default tab: graphics in competitive (board one click away).
mustReplace("let lbTab='board', lbIns",
  "let lbTab=(BNC_COMP()?'graphics':'board'), lbIns");

// 5. Panel open: competitive lands on graphics, never board/presence.
mustReplace(
  "if(lbTab==='presence'){ renderLbRpc(); startRpcView(); } else { lbTab='board'; fetchLb(); }",
  "if(lbTab==='presence'&&!BNC_COMP()){ renderLbRpc(); startRpcView(); } else if(BNC_COMP()){ lbTab='graphics'; renderLbPanel(); } else { lbTab='board'; fetchLb(); }"
);

// 6. Tab buttons: presence reroutes to graphics in competitive;
//    board stays live (leaderboard + click-to-profile).
mustReplace(
  "ov.querySelector('#bnc-lb-tab-rpc').onclick=()=>{ lbTab='presence';",
  "ov.querySelector('#bnc-lb-tab-rpc').onclick=()=>{ try{ if(BNC_COMP()){ lbTab='graphics'; renderLbPanel(); return; } }catch{} lbTab='presence';"
);

// 7. ...belt-and-suspenders inside renderLbPanel: presence stays dead
//    (clamped + button hidden); board tab fully works.
mustReplace(
  'function renderLbPanel(){\r\n    if(!lbPanel) return;',
  'function renderLbPanel(){\r\n    if(!lbPanel) return;\n' +
  "    try{ if(BNC_COMP()){ if(lbTab==='presence') lbTab='graphics'; const _rpcb=document.getElementById('bnc-lb-tab-rpc'); if(_rpcb) _rpcb.style.display='none'; } }catch{}"
);

// 8. Panel title: Leaderboard -> Companion.
mustReplace('<b style="font-size:16px;color:#fff">Leaderboard</b>',
  '<b style="font-size:16px;color:#fff">Companion</b>');

// 9. Presence: single choke point — rpcCfg reports disabled in competitive,
//    which stops the boot probe, the 15s push loop, beacons and the tab.
mustReplace('function rpcCfg(){',
  'function rpcCfg(){\n    try{ if(BNC_COMP()) return { enabled:false, port:8765, auto:false }; }catch{}');
mustReplace('function rpcPush(force){',
  'function rpcPush(force){\n    try{ if(BNC_COMP()) return; }catch{}');

// 10. Graphics presets: keep none + custom(state) + warm only. Stored presets
//     from full/lite fall back to 'none' via the existing gfxCfg guard, and
//     renderLbGfx renders buttons from the remaining keys automatically.
mustRemoveLine("    vivid:{ label:'Vivid', values:{ tint:0, saturation:142, brightness:104, contrast:112, grayscale:0 } },");
mustRemoveLine("    competitive:{ label:'Competitive', values:{ tint:0, saturation:118, brightness:104, contrast:124, grayscale:0 } },");
mustRemoveLine("    night:{ label:'Night', values:{ tint:26, saturation:70, brightness:134, contrast:106, grayscale:0 } },");
mustRemoveLine("    cool:{ label:'Cool', values:{ tint:18, saturation:108, brightness:102, contrast:104, grayscale:0 } },");
mustRemoveLine("    mono:{ label:'Mono', values:{ tint:0, saturation:0, brightness:106, contrast:118, grayscale:0 } },");
mustRemoveLine("    protanopia:{ label:'Red-weak (protanopia)', values:{ tint:-30, saturation:132, brightness:106, contrast:110, grayscale:0 } },");
mustRemoveLine("    deuteranopia:{ label:'Green-weak (deuteranopia)', values:{ tint:32, saturation:132, brightness:106, contrast:110, grayscale:0 } },");
mustRemoveLine("    tritanopia:{ label:'Blue-weak (tritanopia)', values:{ tint:-52, saturation:128, brightness:104, contrast:108, grayscale:0 } },");

// 11. Keybinding/shortcut labels: F4 still opens the panel, but it is no
//     longer a leaderboard — relabel display strings only (ids/keys kept so
//     existing user keybinds carry over).
mustReplace("{id:'leaderboard', label:'Leaderboard', key:'bnc_lb_keybind'",
  "{id:'leaderboard', label:'Companion', key:'bnc_lb_keybind'");
mustReplace('<b>F4</b> leaderboard', '<b>F4</b> companion');
mustReplace('F4 leaderboard · F9 ranked', 'F4 companion · F9 ranked');
mustReplace('>Open leaderboard</button>', '>Open companion</button>');

fs.writeFileSync(DST, s);
console.log(`competitive built: ${DST}`);
console.log(`lite=${origLen} chars -> competitive=${s.length} chars`);
