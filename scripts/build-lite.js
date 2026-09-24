// Builds companion/buildnow-companion.lite.user.js from the full companion.
// LITE keeps: capture, drawer, leaderboard, graphics presets, presence,
//             tourney/ranked panels.
// LITE drops/disables (FPS hogs):
//   - FPS chip rAF loop (permanent requestAnimationFrame + 4x/sec DOM paint)
//   - players tab auto-poll (15s fetchPlayers + roster innerHTML renders)
//   - leaderboard chat poll (5s fetchLbChat)
//   - gfx 4s re-apply loop (keeps one-shot apply + manual changes)
//   - slows heartbeat 5s -> 15s, bg lobby poll 15s -> 30s
// Re-run after every full-companion bump: node scripts/build-lite.js
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', 'companion', 'buildnow-companion.user.js');
const DST = path.join(__dirname, '..', 'companion', 'buildnow-companion.lite.user.js');

let s = fs.readFileSync(SRC, 'utf8');
const origLen = s.length;

function mustReplace(from, to) {
  if (!s.includes(from)) throw new Error('pattern not found: ' + from.slice(0, 80));
  s = s.split(from).join(to);
}

// 1. Header: rename + lite version, drop auto-update URLs so Tampermonkey
//    doesn't silently replace lite with full on next update check.
mustReplace('// @name         Nectar Buildnow Companion',
  '// @name         Nectar Buildnow Companion LITE');
mustReplace('// @description  Nectar Buildnow Companion v2.35.0',
  '// @description  Nectar Buildnow Companion LITE v2.35.0-lite (no player list, no FPS chip, slowed polls)');
s = s.replace(/^\/\/ @updateURL.*$/m, '// @updateURL    disabled-for-lite');
s = s.replace(/^\/\/ @downloadURL.*$/m, '// @downloadURL  disabled-for-lite');

// 2. LITE flag right after version const.
mustReplace("const BNC_VERSION = '2.35.0';",
  "const BNC_VERSION = '2.35.0-lite';\n  const BNC_LITE = true; // lite: skips FPS chip, players poll, chat poll, gfx loop");

// 3. FPS chip: never mount in lite (kills the permanent rAF loop).
mustReplace('function mountFpsChip(){',
  'function mountFpsChip(){ try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE) return; }catch{}');
mustReplace('function _fpsChipFrame(ts){',
  'function _fpsChipFrame(ts){ try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE) return; }catch{}');

// 4. Players poll: disabled in lite (user asked: no player list).
mustReplace(
  'function startPlayersPoll(){ stopPlayersPoll(); fetchPlayers(false); playersPollTimer=setInterval(()=> fetchPlayers(false), 15000); }',
  'function startPlayersPoll(){ try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE) return; }catch{} stopPlayersPoll(); fetchPlayers(false); playersPollTimer=setInterval(()=> fetchPlayers(false), 15000); }'
);
mustReplace('function schedulePlayersRefresh(reason){',
  'function schedulePlayersRefresh(reason){ try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE) return; }catch{}');

// 5. Leaderboard chat poll: disabled in lite (was every 5s).
mustReplace(
  "lbChatTimer=setInterval(()=>{ try{ if(lbTab==='friends' && lbChatWith && lbPanel && lbPanel.style.display!=='none') fetchLbChat(); }catch{} }, 5000);",
  "if(typeof BNC_LITE!==\"undefined\"&&BNC_LITE){ try{ console.log(\"[BNC-LITE] chat poll disabled\"); }catch{} } else lbChatTimer=setInterval(()=>{ try{ if(lbTab==='friends' && lbChatWith && lbPanel && lbPanel.style.display!=='none') fetchLbChat(); }catch{} }, 5000);"
);

// 6. gfx 4s re-apply loop: off in lite (one-shot apply at boot + manual preset clicks stay).
mustReplace(
  'try{ setInterval(()=>{ try{ gfxApply(); }catch{} }, 4000); }catch{}',
  'try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE){} else setInterval(()=>{ try{ gfxApply(); }catch{} }, 4000); }catch{}'
);

// 7. Heartbeat 5s -> 15s in lite (keeps session warm, 3x fewer wakeups).
mustReplace(
  'function startHeartbeat(){ try{ if(_hbTimer) return; _hbTimer=setInterval(sendHeartbeat, 5000); }catch{} }',
  'function startHeartbeat(){ try{ if(_hbTimer) return; _hbTimer=setInterval(sendHeartbeat, (typeof BNC_LITE!=="undefined"&&BNC_LITE)?15000:5000); }catch{} }'
);

// 8. Bg lobby poll 15s -> 30s in lite.
mustReplace(
  'function startBgLobbyPoll(){ if(bgLobbyPollTimer) return; bgLobbyPollTimer=setInterval(()=>{ if(sessionId) fetchStats(false); }, 15000); }',
  'function startBgLobbyPoll(){ if(bgLobbyPollTimer) return; bgLobbyPollTimer=setInterval(()=>{ if(sessionId) fetchStats(false); }, (typeof BNC_LITE!=="undefined"&&BNC_LITE)?30000:15000); }'
);

fs.writeFileSync(DST, s);
console.log(`lite built: ${DST}`);
console.log(`full=${origLen} chars -> lite=${s.length} chars`);
