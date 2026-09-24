// ==UserScript==
// @name         ClippedNow.gg - BuildNow GG Clipper
// @namespace    https://github.com/jaredv2
// @version      2.36
// @description  ClippedNow.gg v1.5.1 - auto-clip your BuildNow GG kills, deaths and wins. 100% local: no account, no server, nothing leaves your PC.
// @author       @cis6led
// @match        https://buildnow-gg.game-files.crazygames.com/*
// @match        https://*.crazygames.com/*
// @all-frames   true
// @run-at       document-start
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        unsafeWindow
// @updateURL    https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/cdn/clippednow.latest.user.js
// @downloadURL  https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/cdn/clippednow.latest.user.js
// ==/UserScript==

// ClippedNow.gg - standalone BuildNow GG clipper.
// No account. No server. No network calls at all: every byte stays local.
// Kill / death / win detection is pure client-side Photon byte parsing
// (killfeed ev 101, match markers stG, own name + game mode from your own
// join/create packets). Clips live in IndexedDB until you download them.

(function () {
  'use strict';
  const CN_VERSION = '2.36';
  const HREF = (() => { try { return location.href; } catch { return ''; } })();
  const IS_GAME_FRAME = HREF.includes('buildnow-gg.game-files.crazygames.com');
  if (!IS_GAME_FRAME) return; // game frame only: lean, no shell-frame overhead

  // ---- storage helpers ----
  const GMGet = (k, d = null) => { try { return typeof GM_getValue !== 'undefined' ? GM_getValue(k, d) : d; } catch { return d; } };
  const GMSet = (k, v) => { try { if (typeof GM_setValue !== 'undefined') GM_setValue(k, v); } catch {} };
  const esc = (v) => String(v == null ? '' : v).replace(/</g, '&lt;');
  function logInfo(m) { try { console.log(`%c[CN]%c ${m}`, 'color:#22d3ee;font-weight:900', ''); } catch {} }
  function dbg(tag, data) { try { console.log(`%c[CN][DEBUG]%c ${tag}`, 'color:#f472b6;font-weight:900', '', data !== undefined ? data : ''); } catch {} }

  // ---- DOM-only toasts (no CDN: zero external requests, ever) ----
  function toast(text, isError, action) {
    // action = { label, fn } renders an inline button (e.g. Watch). Stays
    // 6s when actionable so it can actually be tapped mid-fight.
    try {
      document.querySelectorAll('.cn-toast').forEach(el => el.remove());
      const d = document.createElement('div');
      d.className = 'cn-toast';
      Object.assign(d.style, { position: 'fixed', top: '16px', right: '16px', zIndex: '2147483647', background: isError ? '#991b1b' : '#000', color: '#fff', borderRadius: '8px', border: '1px solid rgb(255 255 255 / 12%)', boxShadow: '0 4px 16px rgb(0 0 0 / 40%)', fontFamily: 'Inter,"Segoe UI",system-ui,-apple-system,sans-serif', fontSize: '14px', fontWeight: '500', padding: '12px 16px', maxWidth: '320px' });
      const span = document.createElement('span');
      span.textContent = text;
      d.appendChild(span);
      let ms = 3000;
      if (action && action.label && typeof action.fn === 'function') {
        const b = document.createElement('button');
        b.textContent = String(action.label).slice(0, 20);
        Object.assign(b.style, { marginLeft: '10px', background: '#fff', color: '#000', border: '0', borderRadius: '6px', padding: '4px 10px', fontSize: '12px', fontWeight: '800', cursor: 'pointer', fontFamily: 'inherit' });
        b.onclick = (e) => { try { if (e && e.stopPropagation) e.stopPropagation(); } catch {} try { d.remove(); } catch {} try { action.fn(); } catch {} };
        d.appendChild(b);
        ms = 6000;
      }
      (document.body || document.documentElement).appendChild(d);
      setTimeout(() => { try { d.remove(); } catch {} }, ms);
    } catch {}
  }

  // ---- UI ear candy: tiny synthesized SFX, zero files, all local ----
  // Soft sine/triangle blips with fast attack + exponential decay. Plays on
  // the REAL destination (never through the record tap, so clips stay clean).
  let _sfxCtx = null;
  function cnSfxCtx() {
    try {
      if (_sfxCtx && _sfxCtx.state !== 'closed') return _sfxCtx;
      let AC = null;
      try { AC = (typeof window !== 'undefined' && (window.AudioContext || window.webkitAudioContext)) || null; } catch {}
      if (!AC && typeof AudioContext !== 'undefined') { try { AC = AudioContext; } catch {} }
      if (!AC) return null;
      _sfxCtx = new AC();
      try { if (typeof clipMarkOwn === 'function') clipMarkOwn(_sfxCtx); } catch {}
      return _sfxCtx;
    } catch { return null; }
  }
  function cnTone(freq, delay, dur, vol, type) {
    try {
      const ctx = cnSfxCtx();
      if (!ctx) return;
      try { if (ctx.state === 'suspended' && ctx.resume) ctx.resume().catch(() => {}); } catch {}
      const t0 = ctx.currentTime + (delay || 0);
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = type || 'sine';
      try { o.frequency.setValueAtTime(freq, t0); } catch { o.frequency.value = freq; }
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(Math.max(0.0002, vol || 0.06), t0 + 0.015);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
      o.connect(g);
      let out = null;
      try { out = _clipRealDest ? _clipRealDest.call(ctx) : ctx.destination; } catch {}
      if (!out) { try { out = ctx.destination; } catch {} }
      if (!out) return;
      try { g.connect(out); } catch { return; }
      try { o.start(t0); o.stop(t0 + dur + 0.05); } catch {}
    } catch {}
  }
  function cnSfx(name) {
    try {
      if (!clipCfg().sounds) return;
      if (name === 'ok') { cnTone(659.25, 0, 0.35, 0.09); cnTone(880, 0.09, 0.45, 0.08); }
      else if (name === 'err') { cnTone(196, 0, 0.3, 0.06, 'triangle'); }
      else if (name === 'tap') { cnTone(740, 0, 0.07, 0.04); }
      else if (name === 'tick') { cnTone(1174.66, 0, 0.06, 0.025); }
    } catch {}
  }

  // ---- identity + mode (learned from YOUR OWN join/create packets) ----
  const cnMe = { name: GMGet('cn_player_name', null) || null, at: 0, actorNr: null, actorAt: 0 };
  let cnJoinRespAt = 0, cnFirstJoinSeen = false;
  const cnMode = { code: null, at: 0 };
  const cnScene = { cur: null, at: 0, inMatch: false };
  const GM_NAMES = { zw_lg: 'Late Game Zonewars', zw: 'Zone Wars', wwzw: 'Duo Wild West', ww: 'Solo Wild West', WW: 'Solo Wild West', bf: 'Boxfights', BF: 'Boxfights', bf2v2: 'Boxfights 2v2', bf_2v2: 'Boxfights 2v2', BF2V2: 'Boxfights 2v2', va: 'V-Arena', VA: 'V-Arena', tdm_bvr: 'Red Vs Blue', RVB: 'Red Vs Blue', r_1v1: 'Realistic 1v1', R_1V1: 'Realistic 1v1', df: 'Free Mode', DF: 'Free Mode', lobby: 'Lobby',
    lobby_main: 'Lobby', mainlobby: 'Lobby', main_lobby: 'Lobby', MainLobby: 'Lobby', MainWorld: 'Free Mode', mainworld: 'Free Mode' };
  function cnGmFromCurScn(cur) {
    try {
      if (!cur) return null;
      const low = String(cur).toLowerCase();
      if (low === 'mainlobby' || low === 'main_lobby' || low === 'lobby') return 'lobby';
      if (low.includes('lategame') || low.includes('zw_lg')) return 'zw_lg';
      if (low.includes('wildwest') || low.includes('wild west') || low.includes('wwzw')) return 'wwzw';
      if (low.includes('zonewars') || low.includes('zone wars')) return 'zw';
      if (low.includes('boxfight') && low.includes('2v2')) return 'BF2V2';
      if (low.includes('boxfight')) return 'BF';
      if (low.includes('varena') || low.includes('v-arena') || low === 'va') return 'va';
      if (low.includes('red vs blue') || low.includes('rvb') || low.includes('tdm_bvr')) return 'tdm_bvr';
      if (low.includes('realistic') || low.includes('r_1v1')) return 'r_1v1';
      if (low.includes('mainworld') || low.includes('free')) return 'DF';
      if (low.includes('wild')) return 'WW';
    } catch {}
    return null;
  }
  function cnGmLabel(raw) {
    if (!raw) return 'Lobby';
    const k = String(raw).trim();
    if (GM_NAMES[k]) return GM_NAMES[k];
    if (GM_NAMES[k.toLowerCase()]) return GM_NAMES[k.toLowerCase()];
    return k.replace(/[_-]+/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }
  function cnNormName(n) {
    try {
      let s = String(n || '').replace(/<[^>]*>/g, '').trim();
      if (s.toLowerCase().startsWith('dc:')) s = s.slice(3).trim();
      return s;
    } catch { return ''; }
  }

  // ---- hex parsers (local port of the wire format) ----
  function hexOf(bytes) { try { const out = new Array(bytes.length); for (let i = 0; i < bytes.length; i++) { const b = bytes[i]; out[i] = (b < 16 ? '0' : '') + b.toString(16); } return out.join(''); } catch { let s = ''; for (let i = 0; i < bytes.length; i++) s += (bytes[i] < 16 ? '0' : '') + bytes[i].toString(16); return s; } }
  function cnUtf8(bytes) {
    try { if (typeof TextDecoder !== 'undefined') return new TextDecoder('utf-8').decode(new Uint8Array(bytes)); } catch {}
    let s = ''; for (const b of bytes) { if (b >= 32 && b < 127) s += String.fromCharCode(b); }
    return s;
  }
  function cnReadVarint(h, p) {
    try {
      let v = 0, shift = 0;
      for (let k = 0; k < 5; k++) {
        const b = parseInt(h.slice(p, p + 2), 16);
        if (isNaN(b)) return null;
        p += 2; v |= (b & 0x7f) << shift; shift += 7;
        if (!(b & 0x80)) return [v, p];
      }
    } catch {}
    return null;
  }
  function cnReadStr(h, p, maxLen) {
    try {
      if (h.slice(p, p + 2) !== '07') return null;
      const r = cnReadVarint(h, p + 2);
      if (!r) return null;
      const [len, q] = r;
      if (len <= 0 || len > maxLen) return null;
      const bytes = [];
      for (let j = 0; j < len; j++) {
        const b = parseInt(h.slice(q + j * 2, q + j * 2 + 2), 16);
        if (isNaN(b)) return null;
        bytes.push(b);
      }
      return [cnUtf8(bytes), q + len * 2];
    } catch { return null; }
  }
  function sniffName255(hexStr) {
    try {
      const h = String(hexStr || '').toLowerCase();
      let i = h.indexOf('03ff07');
      while (i >= 0) {
        const r = cnReadStr(h, i + 4, 32);
        if (r) {
          const s = String(r[0] || '').trim();
          if (s.length >= 2 && s.length <= 32 && /^[^\x00-\x1F\x7F]+$/.test(s)) return s;
        }
        i = h.indexOf('03ff07', i + 1);
      }
    } catch {}
    return null;
  }
  function sniffActorNr(hexStr) {
    try {
      const h = String(hexStr || '').toLowerCase();
      let i = h.indexOf('fe0b');
      if (i >= 0 && i + 6 <= h.length) {
        const n = parseInt(h.slice(i + 4, i + 6), 16);
        if (n >= 1 && n <= 255) return n;
      }
      i = h.indexOf('fe09');
      if (i >= 0) {
        const r = cnReadVarint(h, i + 4);
        if (r && r[0] >= 1 && r[0] <= 65535) return r[0];
      }
    } catch {}
    return null;
  }
  function sniffStrProp(hexStr, key) {
    try {
      const h = String(hexStr || '').toLowerCase();
      let kb = '';
      for (let i = 0; i < key.length; i++) kb += key.charCodeAt(i).toString(16).padStart(2, '0');
      const pat = key.charCodeAt(0).toString(16).padStart(2, '0') + key.length.toString(16).padStart(4, '0') + kb;
      const i = h.indexOf(pat);
      if (i < 0) return null;
      const r = cnReadStr(h, i + pat.length, 16);
      if (!r) return null;
      const s = String(r[0] || '').trim();
      if (!s || !/^[A-Za-z0-9_]{1,16}$/.test(s)) return null;
      return s;
    } catch { return null; }
  }
  function sniffCurScn(hexStr) {
    try {
      const h = String(hexStr || '').toLowerCase();
      const m = h.match(/63757253636e07([0-9a-f]{2})([0-9a-f]+)/);
      if (!m) return null;
      const len = parseInt(m[1], 16);
      if (!(len > 0 && len <= 64)) return null;
      const valHex = m[2].slice(0, len * 2);
      if (valHex.length < len * 2) return null;
      let s = '';
      for (let i = 0; i < valHex.length; i += 2) {
        const c = parseInt(valHex.slice(i, i + 2), 16);
        if (c >= 32 && c < 127) s += String.fromCharCode(c);
        else return null;
      }
      s = s.trim();
      if (!s || !/^[A-Za-z0-9_ \-]{1,64}$/.test(s)) return null;
      return s;
    } catch { return null; }
  }
  function cnLearnScene(rawHex) {
    try {
      const cur = sniffCurScn(rawHex);
      if (!cur) return;
      const now = Date.now();
      try { clipKickSoon(); } catch {}
      if (cur === cnScene.cur) return;
      cnScene.cur = cur; cnScene.at = now;
      cnScene.inMatch = cur !== 'MainLobby';
      try { logInfo(`scene -> ${cur} inMatch=${cnScene.inMatch}`); } catch {}
      const inferred = cnGmFromCurScn(cur);
      if (cur === 'MainLobby') { cnMode.code = 'lobby'; cnMode.at = now; try { cnMe.actorNr = null; cnFirstJoinSeen = false; cnJoinRespAt = 0; } catch {} return; }
      // DF (Free Mode / MainWorld) scenes also show up inside other flows,
      // so DF may claim lobby/empty but never overwrite a real mode. The
      // old code excluded DF from the main branch entirely, which pinned
      // Free Mode on 'lobby' forever once you'd visited the lobby.
      if (inferred && inferred !== 'unknown') {
        if (inferred === 'DF' && cnMode.code && cnMode.code !== 'lobby') return;
        cnMode.code = inferred; cnMode.at = now; return;
      }
      if (!inferred && cnScene.inMatch && !cnMode.code) { cnMode.code = cur; cnMode.at = now; }
    } catch {}
  }
  function sniffStg(hexStr) {
    try {
      const h = String(hexStr || '').toUpperCase();
      const key = '730003737447';
      const i = h.indexOf(key);
      if (i < 0) return null;
      const t = h.slice(i + key.length, i + key.length + 2);
      const d = h.slice(i + key.length + 2, i + key.length + 12);
      if (t === '62' && d.length >= 2) return parseInt(d.slice(0, 2), 16);
      if (t === '6B' && d.length >= 4) return parseInt(d.slice(0, 4), 16);
      if (t === '69' && d.length >= 8) return parseInt(d.slice(0, 8), 16);
    } catch {}
    return null;
  }
  function extractGpbStrings(hex) {
    const strings = [];
    try {
      const end = Math.min(hex.length - 2, 16384);
      let i = 0;
      while (i < end) {
        if (hex.slice(i, i + 2) === '07') {
          const r = cnReadVarint(hex, i + 2);
          if (!r) { i += 2; continue; }
          const [len, q] = r;
          if (len > 0 && len <= 256 && q + len * 2 <= hex.length) {
            let s = '';
            for (let j = 0; j < len; j++) {
              const c = parseInt(hex.slice(q + j * 2, q + j * 2 + 2), 16);
              if (c >= 32 && c < 127) s += String.fromCharCode(c);
            }
            strings.push(s);
            i = q + len * 2;
          } else i += 2;
        } else i += 2;
      }
    } catch {}
    return strings;
  }
  function cnLearnIdentity(rawHex) {
    try {
      const nm = sniffName255(rawHex);
      if (nm) { cnMe.name = nm; cnMe.at = Date.now(); try { GMSet('cn_player_name', nm); } catch {} }
      const gm = sniffStrProp(rawHex, 'gm');
      if (gm) { cnMode.code = gm; cnMode.at = Date.now(); }
      cnLearnScene(rawHex);
    } catch {}
  }
  function cnLearnMode(rawHex) {
    try {
      const gm = sniffStrProp(rawHex, 'gm');
      if (gm) { cnMode.code = gm; cnMode.at = Date.now(); }
      cnLearnScene(rawHex);
    } catch {}
  }

  // ---- WebSocket hook (game frame only, essential packets only) ----
  const CN_OPS = { 224: 'CancelJoinRandomRoom', 225: 'JoinRandomRoom', 226: 'JoinRoom', 227: 'CreateRoom', 228: 'LeaveLobby', 229: 'JoinLobby', 230: 'Authenticate', 231: 'AuthenticateOnce' };
  const CN_EVS = { 101: 'Killfeed', 253: 'PropertiesChanged', 254: 'Leave', 255: 'Join' };
  function cnOpName(type, code) {
    try {
      if (type === 2 || type === 3) return CN_OPS[code] || ('op#' + code);
      if (type === 4) return CN_EVS[code] || ('ev#' + code);
      return 'type#' + type;
    } catch { return 'pkt'; }
  }
  const _cnPkts = [];
  function cnLogPacket(o) {
    try {
      _cnPkts.push(o);
      if (_cnPkts.length > 40) _cnPkts.shift();
    } catch {}
    try { console.log(`%c[CN]%c[PKT] ${JSON.stringify(o)}`, 'color:#22d3ee;font-weight:900', ''); } catch {}
    try { if (cnOpen && clipCfg().debug && typeof renderCnPanel === 'function') renderCnPanel(); } catch {}
  }
  function cnPacketJson(dir, type, code, bytes, rawHex, extra) {
    try {
      const o = { dir, op: cnOpName(type, code), code, len: bytes ? bytes.length : 0 };
      if (cnMe.name) o.me = cnMe.name;
      if (cnMode.code) o.mode = cnMode.code;
      if (cnScene.cur) o.scene = cnScene.cur;
      if (extra) { for (const k in extra) { try { if (extra[k] !== null && extra[k] !== undefined) o[k] = extra[k]; } catch {} } }
      void rawHex;
      return o;
    } catch { return { dir, code }; }
  }
  const _cnWireSeen = {};
  function cnWiretap(dir, type, code, bytes) {
    try {
      if (!clipCfg().debug) return;
      const k = dir + ':' + type + ':' + code;
      if (_cnWireSeen[k]) return;
      _cnWireSeen[k] = true;
      cnLogPacket({ dir, op: cnOpName(type, code), code, len: bytes ? bytes.length : 0, wire: 'first-seen' });
    } catch {}
  }
  const CN_APP_ID = '724';
  function cnIsPhotonUrl(u) {
    try { u = String(u || ''); return u.includes(CN_APP_ID) || u.includes('exitgames.com') || u.includes('photon'); }
    catch { return false; }
  }
  function cnDataTag(data) {
    try { return Object.prototype.toString.call(data); } catch { return ''; }
  }
  function cnIsBlob(data) {
    try {
      if (typeof Blob !== 'undefined' && data instanceof Blob) return true;
      if (cnDataTag(data) === '[object Blob]') return true;
    } catch {}
    return false;
  }
  function cnBytesOf(data) {
    try {
      if (!data) return null;
      if (data instanceof ArrayBuffer) return new Uint8Array(data);
      if (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView && ArrayBuffer.isView(data)) {
        try { return new Uint8Array(data.buffer, data.byteOffset, data.byteLength); } catch {}
      }
      const tag = cnDataTag(data);
      if (tag === '[object ArrayBuffer]') {
        try { return new Uint8Array(data.slice(0)); } catch { return null; }
      }
      if ((tag.includes('Array') || tag.includes('Buffer')) && data && data.buffer) {
        try {
          const off = data.byteOffset || 0;
          const buf = data.buffer.slice ? data.buffer.slice(off, off + data.byteLength) : data.buffer;
          return new Uint8Array(buf);
        } catch {}
      }
    } catch {}
    return null;
  }
  function cnRoute(dir, bytes, url) {
    try {
      if (!bytes || bytes.length < 3) return;
      const type = bytes[1], code = bytes[2];
      try { cnWiretap(dir, type, code, bytes); } catch {}
      const want = (dir === 'out' && ((type === 2 || type === 3) && (code === 225 || code === 226 || code === 227 || code === 228 || code === 229 || code === 230)))
        || (dir === 'in' && ((type === 3 && (code === 225 || code === 226 || code === 227)) || (type === 4 && (code === 101 || code === 253 || code === 255))));
      if (!want) return;
      const rawHex = hexOf(bytes);
      if (dir === 'out' && (type === 2 || type === 3) && (code === 225 || code === 226 || code === 227)) {
        cnLearnIdentity(rawHex);
        try { clipKickSoon(); } catch {}
        try { cnLogPacket(cnPacketJson(dir, type, code, bytes, rawHex, { stG: sniffStg(rawHex) })); } catch {}
        try { const v = sniffStg(rawHex); if (v === 3) clipOnWin(); } catch {}
        return;
      }
      if (dir === 'out' && (type === 2 || type === 3) && (code === 228 || code === 229 || code === 230)) {
        try { cnScene.cur = 'MainLobby'; cnScene.at = Date.now(); cnScene.inMatch = false; } catch {}
        try { if (!cnMode.code) { cnMode.code = 'lobby'; cnMode.at = Date.now(); } } catch {}
        try { clipKickSoon(); } catch {}
        try { cnLogPacket(cnPacketJson(dir, type, code, bytes, rawHex, null)); } catch {}
        return;
      }
      if (dir === 'in' && type === 4 && code === 101) { cnKillfeed(rawHex); return; }
      if (dir === 'in' && type === 3 && (code === 225 || code === 226 || code === 227)) {
        const prevScene = cnScene.cur, prevMode = cnMode.code;
        cnLearnMode(rawHex);
        try {
          const an = sniffActorNr(rawHex);
          if (an) {
            cnMe.actorNr = an; cnMe.actorAt = Date.now(); cnJoinRespAt = Date.now();
            try { logInfo(`my actorNr=${an} via ${cnOpName(type, code)}-resp`); } catch {}
          } else { cnJoinRespAt = Date.now(); }
          try { cnFirstJoinSeen = false; } catch {}
        } catch {}
        try { cnLogPacket(cnPacketJson(dir, type, code, bytes, rawHex, { stG: sniffStg(rawHex), resp: true, actorNr: cnMe.actorNr })); } catch {}
        try { clipKickSoon(); } catch {}
        try {
          if (cnScene.cur !== prevScene || cnMode.code !== prevMode) {
            try { logInfo(`join-resp ${cnOpName(type, code)} mode=${cnGmLabel(cnMode.code)}${cnScene.cur ? ' scene=' + cnScene.cur : ''}`); } catch {}
          }
        } catch {}
        try { const v = sniffStg(rawHex); if (v === 3) clipOnWin(); } catch {}
        return;
      }
      if (dir === 'in' && type === 4 && code === 255) {
        try {
          const nm = sniffName255(rawHex);
          const an = sniffActorNr(rawHex);
          let selfWhy = '';
          if (nm && an && cnMe.actorNr && an === cnMe.actorNr) selfWhy = 'actorNr-pin';
          else if (nm && !cnMe.name && !cnMe.actorNr && cnJoinRespAt && (Date.now() - cnJoinRespAt) < 12000 && !cnFirstJoinSeen) {
            cnFirstJoinSeen = true; selfWhy = 'first-join heuristic';
          }
          if (nm && selfWhy) {
            cnMe.name = nm; cnMe.at = Date.now();
            try { GMSet('cn_player_name', nm); } catch {}
            try { logInfo(`you=${nm} (${selfWhy}${an ? ' actorNr=' + an : ''})`); } catch {}
          }
          cnLogPacket(cnPacketJson(dir, type, code, bytes, rawHex, { joinName: nm, actorNr: an || undefined, self: selfWhy || undefined }));
        } catch {}
        return;
      }
      if (dir === 'in' && type === 4 && code === 253) {
        const prevScene = cnScene.cur, prevMode = cnMode.code;
        cnLearnMode(rawHex);
        const stG = sniffStg(rawHex);
        try {
          if (stG !== null || cnScene.cur !== prevScene || cnMode.code !== prevMode || clipCfg().debug) {
            cnLogPacket(cnPacketJson(dir, type, code, bytes, rawHex, { stG }));
          }
        } catch {}
        try { if (stG === 3) clipOnWin(); } catch {}
        return;
      }
    } catch {}
  }
  function cnCaptureOut(data, url) {
    try {
      if (typeof data === 'string') return;
      if (cnIsBlob(data)) {
        try {
          data.arrayBuffer().then(ab => { try { const b = cnBytesOf(ab); if (b) cnRoute('out', b, url); } catch {} }).catch(() => {});
        } catch {}
        return;
      }
      const b = cnBytesOf(data);
      if (b) cnRoute('out', b, url);
    } catch {}
  }
  function cnCaptureIn(data, url) {
    try {
      if (typeof data === 'string') return;
      if (cnIsBlob(data)) {
        try {
          data.arrayBuffer().then(ab => { try { const b = cnBytesOf(ab); if (b) cnRoute('in', b, url); } catch {} }).catch(() => {});
        } catch {}
        return;
      }
      const b = cnBytesOf(data);
      if (b) cnRoute('in', b, url);
    } catch {}
  }
  let _cnKfLast = { key: '', at: 0 };
  function cnKillfeed(rawHex) {
    try {
      const strings = extractGpbStrings(rawHex);
      const kfStr = strings.find(s => s.includes('killed') || s.includes('eliminated') || s.includes('knocked'));
      if (!kfStr) return;
      const isKill = kfStr.includes('killed') || kfStr.includes('eliminated');
      const sep = kfStr.includes('killed') ? 'killed' : (kfStr.includes('eliminated') ? 'eliminated' : 'knocked');
      const parts = kfStr.split(sep);
      if (parts.length < 2) return;
      const rawKiller = parts[0].replace(/<[^>]*>/g, '').trim();
      const rawVictim = parts[1].replace(/<[^>]*>/g, '').trim();
      // Empty names are real events, not garbage: zone/fall deaths have no
      // killer, and some kills arrive with a blank victim. Old code dropped
      // them, so your own zone death or a kill on an unnamed player never
      // clipped. Display them as '?' but match on the raw value (empty
      // never equals your name, so no false self-matches).
      if (!rawKiller && !rawVictim) return;
      const killer = rawKiller || '?', victim = rawVictim || '?';
      try { clipKickSoon(); } catch {}
      try {
        const key = killer + '>' + victim, now = Date.now();
        if (key === _cnKfLast.key && now - _cnKfLast.at < 5000) return;
        _cnKfLast = { key, at: now };
      } catch {}
      try { cnLogPacket({ dir: 'in', op: 'Killfeed', code: 101, killer, victim, kill: isKill }); } catch {}
      try { console.log(`%c[CN]%c KILL killer=${killer} victim=${victim}`, 'color:#22d3ee;font-weight:900', ''); } catch {}
      clipOnKill(killer, victim, isKill);
    } catch {}
  }
  const _cnWrappedMsg = (typeof WeakMap !== 'undefined') ? new WeakMap() : null;
  function cnHookWS(w) {
    try {
      if (!w) return;
      try {
        const P = w.WebSocket && w.WebSocket.prototype;
        if (P && P.send && !P.send.__cnSend) { try { w.__cnHooked = false; } catch {} try { P.__cnProto = false; } catch {} }
      } catch {}
      if (w.__cnHooked) return;
      const Orig = w.WebSocket;
      if (!Orig || !Orig.prototype) return;
      const Proto = Orig.prototype;
      try {
        if (!Proto.__cnProto) {
          const hadSend = !!(Proto.send && Proto.send.__cnSend);
          const origSend = Proto.send;
          Proto.send = function (data) {
            try {
              if (clipCfg().debug) {
                let tag = '', len = -1, photon = false;
                try { tag = cnDataTag(data); } catch {}
                try {
                  if (typeof data === 'string') len = data.length;
                  else { const b = cnBytesOf(data); if (b) len = b.length; else if (cnIsBlob(data)) len = data.size || -1; }
                } catch {}
                try { photon = cnIsPhotonUrl(this.url || ''); } catch {}
                try { logInfo(`SEND tag=${tag} len=${len} photon=${photon} url=${String(this.url || '').slice(0, 60)}`); } catch {}
              }
            } catch {}
            try { if (cnIsPhotonUrl(this.url || '')) cnCaptureOut(data, this.url); } catch {}
            return origSend.call(this, data);
          };
          try { Proto.send.__cnSend = true; } catch {}
          try { if (!hadSend) logInfo('send hook installed'); else logInfo('send hook re-installed (was stomped)'); } catch {}
          const origAdd = Proto.addEventListener;
          const wrapMsg = (fn, getUrl) => {
            try {
              if (_cnWrappedMsg) {
                let ex = _cnWrappedMsg.get(fn);
                if (!ex) { ex = function (ev) { try { cnCaptureIn(ev.data, getUrl()); } catch {} return fn.call(this, ev); }; _cnWrappedMsg.set(fn, ex); }
                return ex;
              }
            } catch {}
            return function (ev) { try { cnCaptureIn(ev.data, getUrl()); } catch {} return fn.call(this, ev); };
          };
          Proto.addEventListener = function (t, fn, o) {
            try {
              if (t === 'message' && typeof fn === 'function' && cnIsPhotonUrl(this.url || '')) {
                const self = this;
                return origAdd.call(this, t, wrapMsg(fn, () => self.url), o);
              }
            } catch {}
            return origAdd.call(this, t, fn, o);
          };
          try {
            const origRemove = Proto.removeEventListener;
            Proto.removeEventListener = function (t, fn, o) {
              try {
                if (t === 'message' && typeof fn === 'function' && _cnWrappedMsg && _cnWrappedMsg.get(fn)) {
                  return origRemove.call(this, t, _cnWrappedMsg.get(fn), o);
                }
              } catch {}
              return origRemove.call(this, t, fn, o);
            };
          } catch {}
          try {
            const desc = Object.getOwnPropertyDescriptor(Proto, 'onmessage');
            if (desc && desc.set) {
              const nativeSet = desc.set, nativeGet = desc.get;
              Object.defineProperty(Proto, 'onmessage', {
                configurable: true,
                get() { try { const v = nativeGet.call(this); return v; } catch { return null; } },
                set(fn) {
                  try {
                    if (typeof fn === 'function' && cnIsPhotonUrl(this.url || '')) {
                      const self = this;
                      return nativeSet.call(this, wrapMsg(fn, () => self.url));
                    }
                  } catch {}
                  return nativeSet.call(this, fn);
                },
              });
            }
          } catch {}
          try { Proto.__cnProto = true; } catch {}
        }
      } catch {}
      function Hooked(url, proto) {
        const ws = proto === undefined ? new Orig(url) : new Orig(url, proto);
        try { if (cnIsPhotonUrl(url)) { try { ws.binaryType = 'arraybuffer'; } catch {} } } catch {}
        return ws;
      }
      Hooked.prototype = Orig.prototype;
      Hooked.CONNECTING = Orig.CONNECTING; Hooked.OPEN = Orig.OPEN; Hooked.CLOSING = Orig.CLOSING; Hooked.CLOSED = Orig.CLOSED;
      w.WebSocket = Hooked;
      w.__cnHooked = true;
      logInfo('WebSocket hook installed');
    } catch {}
  }

  // ---- recorder (segmented, local-first) ----
  const CN_DEF = { enabled: true, len: 30, fps: 15, bitrate: 1500000, autoKill: true, autoDeath: true, autoWin: true, after: 5, audio: true, mic: false, watermark: true, wmName: true, wmMode: true, wmBrand: true, wmPos: 'br', fab: true, layout: 'landscape', keyClip: 'F8', keyPanel: 'F4', keyLib: 'F9', sounds: true };
  // ---- config cache (v1.3.9 / Phase 1: zero per-frame GM IPC) ----
  // clipCfg() used to hit GM_getValue ~20x per call, and it is called on
  // every rAF meter frame, every watermark draw, every 2.5s tick and every
  // packet log. GM IPC per frame is the #1 FPS killer. Now: read once,
  // serve from memory, patch the cache on every write path. Same GM keys,
  // same defaults, same validation - only the read path changes.
  let _cfgCache = null;
  function clipCfgRead() {
    try {
      const lenRaw = Number(GMGet('cn_clip_len', CN_DEF.len));
      const layoutRaw = String(GMGet('cn_clip_layout', CN_DEF.layout) || 'landscape');
      const fpsRaw = GMGet('cn_clip_fps', CN_DEF.fps);
      // fps is a number, or 'unlimited' = capture at source rate (no
      // frame cap: captureStream() with no rate arg + uncapped paint).
      const fpsVal = (fpsRaw === 'unlimited') ? 'unlimited' : (Number(fpsRaw) || CN_DEF.fps);
      return {
        enabled: GMGet('cn_clip_enabled', CN_DEF.enabled),
        len: [8, 15, 30, 45, 60].includes(lenRaw) ? lenRaw : CN_DEF.len,
        fps: fpsVal,
        bitrate: Number(GMGet('cn_clip_bitrate', CN_DEF.bitrate)) || CN_DEF.bitrate,
        autoKill: GMGet('cn_clip_autokill', CN_DEF.autoKill),
        autoDeath: GMGet('cn_clip_autodeath', CN_DEF.autoDeath),
        autoWin: GMGet('cn_clip_autowin', CN_DEF.autoWin),
        after: Number(GMGet('cn_clip_after', CN_DEF.after)) || 0,
        audio: GMGet('cn_clip_audio', CN_DEF.audio) !== false,
        mic: GMGet('cn_clip_mic', CN_DEF.mic) === true,
        micDevice: String(GMGet('cn_mic_device', 'default') || 'default'),
        watermark: GMGet('cn_clip_watermark', CN_DEF.watermark) !== false,
        wmName: GMGet('cn_wm_name', CN_DEF.wmName) !== false,
        wmMode: GMGet('cn_wm_mode', CN_DEF.wmMode) !== false,
        wmBrand: GMGet('cn_wm_brand', CN_DEF.wmBrand) !== false,
        wmPos: ['tl', 'tc', 'tr', 'bl', 'bc', 'br'].includes(GMGet('cn_wm_pos', CN_DEF.wmPos)) ? GMGet('cn_wm_pos', CN_DEF.wmPos) : CN_DEF.wmPos,
        fab: GMGet('cn_fab_visible', CN_DEF.fab) !== false,
        layout: layoutRaw === 'vertical' ? 'vertical' : 'landscape',
        // Container: mp4 = smooth hardware-decoded playback, weak seeking
        // (Chrome's fragmented MP4 seeks land mid-GOP -> blocky frames).
        // webm = precise seeking + heavier software decode. User's choice.
        format: GMGet('cn_clip_format', 'mp4') === 'webm' ? 'webm' : 'mp4',
        sounds: GMGet('cn_sounds', CN_DEF.sounds) !== false,
        keyClip: String(GMGet('cn_key_clip', CN_DEF.keyClip) || 'F8'),
        keyPanel: String(GMGet('cn_key_panel', CN_DEF.keyPanel) || 'F4'),
        keyLib: String(GMGet('cn_key_lib', CN_DEF.keyLib) || 'F9'),
        debug: GMGet('cn_debug', false) === true,
      };
    } catch { return Object.assign({}, CN_DEF); }
  }
  function clipCfg() {
    try {
      if (!_cfgCache) _cfgCache = clipCfgRead();
      return _cfgCache;
    } catch { return Object.assign({}, CN_DEF); }
  }
  function clipCfgReload() {
    try { _cfgCache = clipCfgRead(); } catch {}
    return _cfgCache;
  }
  function clipCfgApplyKey(k, v) {
    // Mirror clipCfgRead validation so cache == fresh read, always.
    try {
      if (!_cfgCache) _cfgCache = clipCfgRead();
      const c = _cfgCache;
      if (k === 'cn_clip_enabled') c.enabled = v;
      else if (k === 'cn_clip_len') { const n = Number(v); c.len = [8, 15, 30, 45, 60].includes(n) ? n : CN_DEF.len; }
      else if (k === 'cn_clip_fps') c.fps = (v === 'unlimited') ? 'unlimited' : (Number(v) || CN_DEF.fps);
      else if (k === 'cn_clip_bitrate') c.bitrate = Number(v) || CN_DEF.bitrate;
      else if (k === 'cn_clip_autokill') c.autoKill = v;
      else if (k === 'cn_clip_autodeath') c.autoDeath = v;
      else if (k === 'cn_clip_autowin') c.autoWin = v;
      else if (k === 'cn_clip_after') c.after = Number(v) || 0;
      else if (k === 'cn_clip_audio') c.audio = v !== false;
      else if (k === 'cn_clip_mic') c.mic = v === true;
      else if (k === 'cn_mic_device') c.micDevice = String(v || 'default');
      else if (k === 'cn_clip_watermark') c.watermark = v !== false;
      else if (k === 'cn_wm_name') c.wmName = v !== false;
      else if (k === 'cn_wm_mode') c.wmMode = v !== false;
      else if (k === 'cn_wm_brand') c.wmBrand = v !== false;
      else if (k === 'cn_wm_pos') c.wmPos = ['tl', 'tc', 'tr', 'bl', 'bc', 'br'].includes(v) ? v : CN_DEF.wmPos;
      else if (k === 'cn_fab_visible') c.fab = v !== false;
      else if (k === 'cn_clip_layout') c.layout = String(v) === 'vertical' ? 'vertical' : 'landscape';
      else if (k === 'cn_clip_format') c.format = String(v) === 'webm' ? 'webm' : 'mp4';
      else if (k === 'cn_sounds') c.sounds = v !== false;
      else if (k === 'cn_key_clip') c.keyClip = String(v || 'F8');
      else if (k === 'cn_key_panel') c.keyPanel = String(v || 'F4');
      else if (k === 'cn_key_lib') c.keyLib = String(v || 'F9');
      else if (k === 'cn_debug') c.debug = v === true;
    } catch {}
  }
  function clipSet(k, v) {
    try { GMSet(k, v); } catch {}
    try { clipCfgApplyKey(k, v); } catch {}
    try { if (k === 'cn_clip_len' || k === 'cn_clip_fps' || k === 'cn_clip_bitrate' || k === 'cn_clip_enabled' || k === 'cn_clip_audio' || k === 'cn_clip_mic' || k === 'cn_clip_watermark' || k === 'cn_clip_layout' || k === 'cn_clip_format') { try { clipEnsureMic(); } catch {} rsReboot('settings:' + k); } } catch {}
    try { if (k === 'cn_fab_visible') cnApplyFab(); } catch {}
    try { if (typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {}
  }
  function clipKickSoon() {
    try { if (rsState === RS_IDLE) rsEnter(RS_STARTING); } catch {}
  }
  function clipPickMime(withAudio) {
    // Container follows the Format setting (mp4 unless explicitly webm).
    // MP4/H264 = smooth hardware-decoded playback everywhere, but Chrome's
    // fragmented MP4 seeks can land mid-GOP (blocky frames after scrubbing).
    // WebM/VP8 = precise seeking, heavier software decode. MP4 stays the
    // default (matches the proven 1.3.x pipeline); WebM is one click away.
    // The old stitch fragility is gone independently (continuous sessions).
    try {
      let webmFirst = false;
      try { webmFirst = clipCfg().format === 'webm'; } catch {}
      const mp4A = ['video/mp4;codecs="avc1.42E01E,mp4a.40.2"', 'video/mp4'];
      const mp4V = ['video/mp4;codecs="avc1.42E01E"', 'video/mp4'];
      const webmA = ['video/webm;codecs=vp8,opus', 'video/webm;codecs=vp9,opus', 'video/webm'];
      const webmV = ['video/webm;codecs=vp8', 'video/webm;codecs=vp9', 'video/webm'];
      const cands = withAudio ? (webmFirst ? [...webmA, ...mp4A] : [...mp4A, ...webmA]) : (webmFirst ? [...webmV, ...mp4V] : [...mp4V, ...webmV]);
      for (const m of cands) { try { if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m)) return m; } catch {} }
    } catch {}
    return '';
  }
  function clipExt(mime) { try { if (String(mime || '').includes('mp4')) return 'mp4'; } catch {} return 'webm'; }
  function clipPickMimeExport(withAudio) {
    // Trim/export mime: WebM FIRST, always. The recorder prefers MP4 when
    // available, but Chrome's MP4 muxer output (fragmented MP4, short
    // re-captured clips) is exactly what VLC and even Chrome previews
    // glitch on. WebM via MediaRecorder is the decade-old reliable path.
    try {
      const cands = withAudio
        ? ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm']
        : ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'];
      for (const m of cands) { try { if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m)) return m; } catch {} }
    } catch {}
    try { return clipPickMime(withAudio); } catch {}
    return '';
  }
  let _clip = { rec: null, stream: null, chunks: [], segStartedAt: 0, finalizing: false, started: false, lastErr: '', mime: '', withAudio: false, wmActive: false, recStartedAt: 0, killAt: 0, deathAt: 0, winAt: 0, pendingTimer: null, pendingTrigger: null, lastKill: null, cw: 0, ch: 0 };
  let _clipCanvasCache = null;
  function clipFindCanvas() {
    try {
      try { if (_clipCanvasCache && document.contains(_clipCanvasCache) && (_clipCanvasCache.width || 0) > 0) return _clipCanvasCache; } catch {}
      _clipCanvasCache = null;
      const cs = document.querySelectorAll('canvas');
      if (!cs || !cs.length) return null;
      let best = null, bestArea = 0;
      for (const c of cs) { try { const a = (c.width || 0) * (c.height || 0); if (a > bestArea) { bestArea = a; best = c; } } catch {} }
      try { _clipCanvasCache = best; } catch {}
      return best;
    } catch { return null; }
  }
  let _clipSizeFlaps = 0;
  function clipCanvasChanged() {
    // Watchdog runs every 15s: require the new size on TWO consecutive
    // passes before believing it, so transient flaps (loading screens,
    // resolution transitions) don't wipe the rolling window.
    try {
      const c = clipFindCanvas();
      if (!c || !_clip.cw || !_clip.ch) { _clipSizeFlaps = 0; return false; }
      const w = c.width || 0, h = c.height || 0;
      if (!(w > 0 && h > 0 && (w !== _clip.cw || h !== _clip.ch))) { _clipSizeFlaps = 0; return false; }
      _clipSizeFlaps++;
      if (_clipSizeFlaps < 2) return false;
      _clipSizeFlaps = 0;
      return true;
    } catch { return false; }
  }

  // game-audio tap: transparent GainNode passthrough on ctx.destination
  const _clipCtxs = new Set();
  const _clipTaps = (typeof WeakMap !== 'undefined') ? new WeakMap() : null;
  let _clipAudioPatched = false, _clipRealDest = null;
  function clipRealms() {
    try {
      const rs = [];
      try { if (typeof window !== 'undefined' && window && rs.indexOf(window) < 0) rs.push(window); } catch {}
      try { if (typeof unsafeWindow !== 'undefined' && unsafeWindow && rs.indexOf(unsafeWindow) < 0) rs.push(unsafeWindow); } catch {}
      return rs;
    } catch { return []; }
  }
  function clipIsOfflineCtx(ctx) {
    try {
      try { if (typeof OfflineAudioContext !== 'undefined' && ctx instanceof OfflineAudioContext) return true; } catch {}
      try { const uw = unsafeWindow; if (uw && uw.OfflineAudioContext && ctx instanceof uw.OfflineAudioContext) return true; } catch {}
    } catch {}
    return false;
  }
  function clipPatchAudio() {
    if (_clipAudioPatched) return true;
    try {
      const realms = clipRealms();
      const protos = [];
      for (const w of realms) {
        try { const b = w.BaseAudioContext; if (b && b.prototype && protos.indexOf(b.prototype) < 0) protos.push(b.prototype); } catch {}
        try { const a = w.AudioContext; if (a && a.prototype && protos.indexOf(a.prototype) < 0) protos.push(a.prototype); } catch {}
        try { const x = w.webkitAudioContext; if (x && x.prototype && protos.indexOf(x.prototype) < 0) protos.push(x.prototype); } catch {}
      }
      if (!protos.length) { dbg('clipPatchAudio: no protos found in any realm', { realms: realms.length }); return false; }
      let ok = false;
      for (const p of protos) {
        try {
          if (p.__cnClipTap) { ok = true; continue; }
          let realGet = null, owner = p;
          while (owner) { const d = Object.getOwnPropertyDescriptor(owner, 'destination'); if (d && d.get) { realGet = d.get; break; } owner = Object.getPrototypeOf(owner); }
          if (!realGet) continue;
          try { _clipRealDest = _clipRealDest || realGet; } catch {}
          Object.defineProperty(p, 'destination', {
            configurable: true, enumerable: false, get: function () {
              try {
                const ctx = this;
                try { _clipCtxs.add(ctx); } catch {}
                let t = null;
                try { t = _clipTaps ? _clipTaps.get(ctx) : ctx.__cnClipTap; } catch {}
                if (!t) {
                  const real = realGet.call(ctx);
                  const msd = ctx.createMediaStreamDestination();
                  const proxy = ctx.createGain(); proxy.gain.value = 1;
                  // Record-side gain INSIDE the game context (same realm):
                  // proxy -> real (speakers, unity) + proxy -> recGain ->
                  // msd (file). The volume slider drives recGain only, so
                  // speakers are untouched. recGain -> msd stays same-realm,
                  // which is the path proven to carry signal to the file.
                  const recGain = ctx.createGain();
                  try { recGain.gain.value = clipVol('game'); } catch {}
                  try { proxy.connect(real); } catch {}
                  try { proxy.connect(recGain); } catch {}
                  try { recGain.connect(msd); } catch {}
                  t = { proxy: proxy, real: real, msd: msd, recGain: recGain, ctx: ctx };
                  try {
                    const an = ctx.createAnalyser();
                    try { an.fftSize = 256; } catch {}
                    try { proxy.connect(an); } catch {}
                    t.analyser = an;
                  } catch {}
                  try { if (_clipTaps) _clipTaps.set(ctx, t); else ctx.__cnClipTap = t; } catch {}
                  try { dbg('clipPatchAudio: new tap created', { realm: (typeof unsafeWindow !== 'undefined' && ctx === unsafeWindow) ? 'unsafeWindow' : 'window/other' }); } catch {}
                }
                return t.proxy;
              } catch (e) { try { return realGet.call(this); } catch (e2) { return null; } }
            },
          });
          try { p.__cnClipTap = true; } catch {}
          ok = true;
        } catch {}
      }
      try {
        const nodeProtos = [];
        for (const w of realms) { try { const an = w.AudioNode; if (an && an.prototype && nodeProtos.indexOf(an.prototype) < 0) nodeProtos.push(an.prototype); } catch {} }
        for (const np of nodeProtos) {
        if (!np.__cnClipDisc) {
          const origDisc = np.disconnect;
          np.disconnect = function () {
            try {
              const a = Array.prototype.slice.call(arguments);
              if (a.length && a[0] && typeof a[0] === 'object') {
                let t = null;
                try { t = this.context && (_clipTaps ? _clipTaps.get(this.context) : this.context.__cnClipTap); } catch {}
                if (t && a[0] === t.real) a[0] = t.proxy;
              }
              return origDisc.apply(this, a);
            } catch (e) { try { return origDisc.apply(this, arguments); } catch (e2) {} }
          };
          try { np.__cnClipDisc = true; } catch {}
        }
        }
      } catch {}
      _clipAudioPatched = ok;
      dbg('clipPatchAudio: done', { ok, protoCount: protos.length });
      return ok;
    } catch (e) { dbg('clipPatchAudio: threw', e); return false; }
  }
  const _clipOwnCtxs = (typeof Set !== 'undefined') ? new Set() : null;
  function clipMarkOwn(ctx) { try { if (ctx && _clipOwnCtxs) _clipOwnCtxs.add(ctx); } catch {} }
  function clipIsOwnCtx(ctx) { try { return !!(ctx && _clipOwnCtxs && _clipOwnCtxs.has(ctx)); } catch { return false; } }
  function clipMediaTracks() {
    const out = [];
    try {
      const els = document.querySelectorAll ? document.querySelectorAll('audio,video') : [];
      for (const el of els) {
        try {
          if (!el || el.muted) continue;
          const cs = (el.captureStream && el.captureStream.bind(el)) || (el.mozCaptureStream && el.mozCaptureStream.bind(el));
          if (!cs) continue;
          let s = el.__cnCap || null;
          try {
            if (!s) { s = cs(); el.__cnCap = s; }
          } catch { continue; }
          if (!s) continue;
          const trs = (s.getAudioTracks && s.getAudioTracks()) || [];
          for (const tr of trs) {
            try { if (tr && tr.readyState === 'live' && !tr.muted && out.indexOf(tr) < 0) out.push(tr); } catch {}
          }
        } catch {}
      }
    } catch {}
    return out;
  }
  function clipAttachAudio(stream, at) {
    try {
      if (!stream || !at) return null;
      try {
        const has = (stream.getAudioTracks() || []).indexOf(at) >= 0;
        if (!has) stream.addTrack(at);
        return stream;
      } catch {}
      // Fallback rebuilds the stream object: construct it in the page realm
      // like clipMixerAttach does - an isolated-world MediaStream around a
      // page track can be silently dropped by the recorder.
      try {
        let MS = null;
        try {
          const uw = (typeof unsafeWindow !== 'undefined' && unsafeWindow) || null;
          MS = (uw && uw.MediaStream) || (typeof window !== 'undefined' && window.MediaStream) || null;
        } catch {}
        if (!MS && typeof MediaStream !== 'undefined') { try { MS = MediaStream; } catch {} }
        const vt = (stream.getVideoTracks() || [])[0] || null;
        if (vt && MS) return new MS([vt, at]);
      } catch {}
    } catch {}
    return null;
  }
  function clipGameAudioTracks() {
    const out = [];
    try {
      let i = 0;
      _clipCtxs.forEach((ctx) => {
        i++;
        try {
          if (clipIsOfflineCtx(ctx)) { logInfo(`AUDIO-DIAG ctx#${i} skipped: offline`); return; }
          try { if (clipIsOwnCtx(ctx)) { logInfo(`AUDIO-DIAG ctx#${i} skipped: own (SFX/mixer ctx)`); return; } } catch {}
          let t = null;
          try { t = _clipTaps ? _clipTaps.get(ctx) : ctx.__cnClipTap; } catch {}
          if (!t) {
            try { void ctx.destination; } catch {}
            try { t = _clipTaps ? _clipTaps.get(ctx) : ctx.__cnClipTap; } catch {}
          }
          if (!t) { logInfo(`AUDIO-DIAG ctx#${i} has NO tap object (patch never fired on it)`); return; }
          if (!t.msd || !t.msd.stream) { logInfo(`AUDIO-DIAG ctx#${i} tap exists but no msd/stream`); return; }
          const trs = t.msd.stream.getAudioTracks() || [];
          const lvl = clipAnLevel(t.analyser);
          logInfo(`AUDIO-DIAG ctx#${i} state=${ctx.state} msdTracks=${trs.length} states=[${trs.map(x=>x.readyState).join(',')}] analyserLevel=${lvl==null?'?':lvl.toFixed(3)}`);
          for (const tr of trs) { if (tr && tr.readyState === 'live' && out.indexOf(tr) < 0) out.push(tr); }
        } catch (e) { logInfo(`AUDIO-DIAG ctx#${i} threw: ` + e); }
      });
    } catch {}
    return out;
  }
  function clipFirstGameTap() {
    // Hottest live game MSD track WITH its tap object (for recGain + levels).
    // Same-realm by construction: the MSD was created in the game's own
    // context, so recording this track direct carries real signal. Hottest
    // wins because the first context alive is often a silent utility context
    // (fresh MSD = live track with zero signal).
    try {
      let best = null, bestLvl = -1;
      _clipCtxs.forEach((ctx) => {
        try {
          if (clipIsOfflineCtx(ctx)) return;
          try { if (clipIsOwnCtx(ctx)) return; } catch {}
          let t = null;
          try { t = _clipTaps ? _clipTaps.get(ctx) : ctx.__cnClipTap; } catch {}
          if (!t || !t.msd || !t.msd.stream) {
            // Tap object exists (analyser may even read signal) but the MSD
            // side is unreadable - stale after a ctx recreation. Rebuild once
            // per minute: drop the entry and force the patched getter to run
            // again in the game's own realm.
            try {
              if (t && t.analyser) {
                const now = Date.now();
                let last = 0;
                try { last = ctx.__cnRebuiltAt || 0; } catch {}
                if (now - last > 60000 && ctx.state !== 'closed') {
                  try { ctx.__cnRebuiltAt = now; } catch {}
                  try { if (_clipTaps) _clipTaps.delete(ctx); } catch {}
                  try { delete ctx.__cnClipTap; } catch {}
                  try { void ctx.destination; } catch {}
                  try { t = _clipTaps ? _clipTaps.get(ctx) : ctx.__cnClipTap; } catch {}
                  try { logInfo('audio: rebuilt stale tap (had analyser, no live msd track)'); } catch {}
                } else {
                  try { logInfo('audio: tap without live msd track (rebuild throttled/closed)'); } catch {}
                }
              }
            } catch {}
            if (!t || !t.msd || !t.msd.stream) return;
          }
          let trs = null;
          try { trs = t.msd.stream.getAudioTracks() || []; }
          catch (e) { try { logInfo('audio: msd getAudioTracks threw: ' + e); } catch {} return; }
          if (!trs.length) { try { logInfo('audio: msd stream has zero tracks'); } catch {} return; }
          for (const tr of trs) {
            if (!(tr && tr.readyState === 'live')) continue;
            let lvl = -1;
            try { if (t.analyser) { const l = clipAnLevel(t.analyser); if (l != null) lvl = l; } } catch {}
            if (!best || lvl > bestLvl) { best = { track: tr, tap: t }; bestLvl = lvl; }
          }
        } catch {}
      });
      return best;
    } catch { return null; }
  }
  function clipTapSetGameVol() {
    // Push the Game-audio slider into every tap's record-side gain node.
    // Speakers (proxy -> real) stay at unity; only the file path changes.
    try {
      const v = clipVol('game');
      _clipCtxs.forEach((ctx) => {
        try {
          let t = null;
          try { t = _clipTaps ? _clipTaps.get(ctx) : ctx.__cnClipTap; } catch {}
          if (t && t.recGain) {
            try {
              if (t.recGain.gain.setTargetAtTime && t.recGain.context) t.recGain.gain.setTargetAtTime(v, t.recGain.context.currentTime, 0.01);
              else t.recGain.gain.value = v;
            } catch { try { t.recGain.gain.value = v; } catch {} }
          }
        } catch {}
      });
    } catch {}
  }
  // ---- microphone (opt-in, local only) ----
  let _micStream = null, _micTrack = null, _micAsking = false, _micErr = '', _micToasted = false;
  function clipMicTrack() {
    try {
      if (_micTrack && _micTrack.readyState === 'live') return _micTrack;
      if (_micStream) {
        try {
          const trs = _micStream.getAudioTracks() || [];
          for (const t of trs) { if (t && t.readyState === 'live') { _micTrack = t; return t; } }
        } catch {}
      }
    } catch {}
    return null;
  }
  // ---- mic input picker (Discord voice needs OS routing, not script code:
  // a userscript can only tap its own document, so Discord desktop / another
  // tab is unreachable. Route Discord through a virtual cable (VoiceMeeter /
  // Stereo Mix), set it as an input, and pick it here as the Mic source.)
  let _micDevices = [], _micDevicesAt = 0;
  function clipListMicDevices() {
    // enumerateDevices labels stay empty until mic permission is granted, so
    // this is called post-grant and on panel render while mic is on.
    try {
      if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
      navigator.mediaDevices.enumerateDevices().then((devs) => {
        try {
          const ins = (devs || []).filter((d) => d && d.kind === 'audioinput');
          _micDevices = ins.map((d) => ({ id: d.deviceId || '', label: d.label || 'Microphone' }));
          _micDevicesAt = Date.now();
          try { if (cnView === 'main' && typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {}
        } catch {}
      }).catch(() => {});
    } catch {}
  }
  function cnSetMicDevice(id) {
    // Switch mic source: stop the old tracks (mixer prunes dead nodes),
    // persist, re-request on the new device, reboot the recorder onto it.
    try {
      id = String(id || 'default');
      try { GMSet('cn_mic_device', id); } catch {}
      try { clipCfgApplyKey('cn_mic_device', id); } catch {}
      try { if (_micStream) { _micStream.getTracks().forEach((t) => { try { t.stop(); } catch {} }); } } catch {}
      _micStream = null; _micTrack = null; _micErr = ''; _micToasted = false;
      try { logInfo(`mic input -> ${id === 'default' ? 'default' : id.slice(0, 8) + '…'} (${(_micDevices.find((d) => d.id === id) || {}).label || 'default mic'})`); } catch {}
      try { clipEnsureMic(); } catch {}
      try { if (typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {}
    } catch {}
  }
  function clipEnsureMic() {
    try {
      if (!clipCfg().mic) return;
      if (clipMicTrack()) return;
      if (_micAsking) return;
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { _micErr = 'Mic not supported here'; return; }
      _micAsking = true; _micErr = '';
      dbg('clipEnsureMic: requesting getUserMedia');
      // Honour an explicitly chosen input (e.g. a VoiceMeeter virtual cable
      // carrying Discord + game). 'default' = browser's default mic.
      let constraints = { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } };
      try {
        const dev = clipCfg().micDevice || 'default';
        if (dev && dev !== 'default') constraints = { audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, deviceId: { exact: dev } } };
      } catch {}
      navigator.mediaDevices.getUserMedia(constraints).then((s) => {
        try { _micStream = s; _micTrack = null; clipMicTrack(); } catch {}
        dbg('clipEnsureMic: granted', { trackLive: !!clipMicTrack() });
        try { clipListMicDevices(); } catch {} // labels need granted permission
        try { if (clipCfg().mic) rsReboot('mic-ready'); } catch {}
      }).catch((e) => {
        try {
          const nm = (e && e.name) || '';
          _micErr = (nm === 'NotAllowedError') ? 'Mic blocked - allow it in the browser'
            : (nm === 'OverconstrainedError' || nm === 'NotFoundError') ? 'Mic device gone - pick another input'
            : 'Mic unavailable';
          dbg('clipEnsureMic: rejected', { name: nm, msg: e && e.message });
          if (!_micToasted) { _micToasted = true; try { toast(_micErr + ' - game audio still records', true); } catch {} }
        } catch {}
      }).finally(() => { try { _micAsking = false; } catch {} });
    } catch { try { _micAsking = false; } catch {} }
  }

  // ---- game+mic mixdown with per-source gain ----
  // MediaRecorder keeps a single audio track, so game + mic are summed via
  // WebAudio into ONE track, each through its own persistent GainNode (user
  // adjustable, live, no restart needed) and its own AnalyserNode (for the
  // mixer UI waveform meters). Routing ALWAYS goes through this mixer, even
  // for a single source - a raw "direct tap" bypass would have no gain node
  // to attach a slider to, and no analyser to feed a meter. Persisted
  // volumes: cn_vol_game / cn_vol_mic, range 0..2 (1 = unity gain).
  let _mixCtx = null, _mixDest = null, _mixAn = null, _mixLogged = false, _mixActive = false, _mixGestureAt = 0;
  // Per-lane state, addressed by 'game' or 'mic'. `src` is the live
  // MediaStreamAudioSourceNode for the CURRENT track in that lane (rebuilt
  // whenever the underlying track object changes); `gain`/`meter` persist
  // across track swaps so slider position and meter wiring survive things
  // like a mic reconnect or the game's AudioContext being recreated.
  let _mixLane = {
    game: { track: null, src: null, gain: null, meter: null },
    mic: { track: null, src: null, gain: null, meter: null },
  };
  // Volume cache: clipVol() is called per tick + per save + per attach.
  // Same keys (cn_vol_game / cn_vol_mic), same 0..2 clamp, memory-served.
  const _volCache = { game: 1, mic: 1, loaded: false };
  function clipVolLoad() {
    try {
      const g = Number(GMGet('cn_vol_game', 1)), m = Number(GMGet('cn_vol_mic', 1));
      _volCache.game = (isNaN(g) || g < 0) ? 1 : Math.max(0, Math.min(2, g));
      _volCache.mic = (isNaN(m) || m < 0) ? 1 : Math.max(0, Math.min(2, m));
      _volCache.loaded = true;
    } catch {}
  }
  function clipVol(lane) {
    try {
      if (!_volCache.loaded) clipVolLoad();
      return lane === 'mic' ? _volCache.mic : _volCache.game;
    } catch { return 1; }
  }
  function clipSetVol(lane, v) {
    try {
      v = Math.max(0, Math.min(2, Number(v)));
      if (isNaN(v)) return;
      GMSet(lane === 'mic' ? 'cn_vol_mic' : 'cn_vol_game', v);
      try { _volCache[lane === 'mic' ? 'mic' : 'game'] = v; _volCache.loaded = true; } catch {}
      const node = _mixLane[lane] && _mixLane[lane].gain;
      if (node) {
        try { node.gain.setTargetAtTime(v, (_mixCtx && _mixCtx.currentTime) || 0, 0.01); }
        catch { try { node.gain.value = v; } catch {} }
      }
      // Game-only clips record the tap MSD direct (same-realm path), so the
      // slider must also drive the taps' record-side gains - not just the
      // mixer lane (which is only in the path when mic is mixed in).
      try { if (lane === 'game' && typeof clipTapSetGameVol === 'function') clipTapSetGameVol(); } catch {}
      dbg('clipSetVol', { lane, v, hadNode: !!node });
    } catch (e) { dbg('clipSetVol: threw', e); }
  }
  function clipMixResume() {
    try { if (_mixCtx && _mixCtx.state === 'suspended' && _mixCtx.resume) _mixCtx.resume().catch(() => {}); } catch {}
  }
  function clipMixEnsureCtx() {
    // Page-realm constructors preferred: tap/mic tracks are page objects,
    // and constructing our mix graph in the isolated world can yield a
    // silent stream even though everything "looks" connected.
    try {
      if (_mixCtx) return _mixCtx;
      let AC = null;
      try {
        const uw = (typeof unsafeWindow !== 'undefined' && unsafeWindow) || null;
        AC = (uw && (uw.AudioContext || uw.webkitAudioContext)) || (typeof window !== 'undefined' && (window.AudioContext || window.webkitAudioContext)) || null;
      } catch {}
      if (!AC && typeof AudioContext !== 'undefined') { try { AC = AudioContext; } catch {} }
      if (!AC) { dbg('clipMixEnsureCtx: no AudioContext ctor available'); return null; }
      try { _mixCtx = new AC(); } catch (e) { dbg('clipMixEnsureCtx: new AC() threw', e); return null; }
      try { clipMarkOwn(_mixCtx); } catch {}
      try { _mixDest = _mixCtx.createMediaStreamDestination(); } catch (e) { dbg('clipMixEnsureCtx: createMediaStreamDestination threw', e); _mixDest = null; }
      try { _mixAn = _mixCtx.createAnalyser(); _mixAn.fftSize = 256; } catch { _mixAn = null; }
      dbg('clipMixEnsureCtx: created', { state: _mixCtx.state });
      return _mixCtx;
    } catch (e) { dbg('clipMixEnsureCtx: outer threw', e); return null; }
  }
  function clipMixLaneNodes(lane) {
    // Lazily create the persistent gain+meter pair for a lane. Independent
    // of any particular track, so sliders/meters keep working across swaps.
    try {
      const ctx = clipMixEnsureCtx();
      if (!ctx || !_mixDest) return null;
      const L = _mixLane[lane];
      if (!L) return null;
      if (!L.gain) {
        try { L.gain = ctx.createGain(); L.gain.gain.value = clipVol(lane); }
        catch (e) { dbg('clipMixLaneNodes: createGain threw', { lane, e }); return null; }
        try { L.gain.connect(_mixDest); } catch (e) { dbg('clipMixLaneNodes: gain->dest connect threw', { lane, e }); }
        try { if (_mixAn) L.gain.connect(_mixAn); } catch {}
      }
      if (!L.meter) {
        try { L.meter = ctx.createAnalyser(); L.meter.fftSize = 256; L.gain.connect(L.meter); }
        catch (e) { dbg('clipMixLaneNodes: meter setup threw', { lane, e }); }
      }
      return L;
    } catch (e) { dbg('clipMixLaneNodes: outer threw', { lane, e }); return null; }
  }
  function clipMixerAttach(lane, track) {
    // (Re)connect `track` into `lane`'s persistent gain node. Safe no-op if
    // the same live track is already attached. Returns true iff `lane` now
    // has a live, connected source.
    try {
      const live = (t) => { try { return t && t.readyState === 'live'; } catch { return false; } };
      const L = clipMixLaneNodes(lane);
      if (!L) { dbg('clipMixerAttach: no lane nodes (context failed?)', { lane }); return false; }
      if (!live(track)) {
        if (L.src) { try { L.src.disconnect(); } catch {} L.src = null; L.track = null; }
        return false;
      }
      if (L.track === track && L.src) return true; // already wired, nothing to do
      if (L.src) { try { L.src.disconnect(); } catch {} L.src = null; }
      let MS = null;
      try {
        const uw = (typeof unsafeWindow !== 'undefined' && unsafeWindow) || null;
        MS = (uw && uw.MediaStream) || (typeof window !== 'undefined' && window.MediaStream) || null;
      } catch {}
      if (!MS && typeof MediaStream !== 'undefined') { try { MS = MediaStream; } catch {} }
      if (!MS) { dbg('clipMixerAttach: no MediaStream ctor available', { lane }); return false; }
      let stream = null;
      try { stream = new MS([track]); } catch (e) { dbg('clipMixerAttach: new MediaStream threw', { lane, e }); return false; }
      if (!stream) return false;
      try {
        const src = _mixCtx.createMediaStreamSource(stream);
        src.connect(L.gain);
        L.src = src; L.track = track;
        dbg('clipMixerAttach: attached', { lane });
        return true;
      } catch (e) { dbg('clipMixerAttach: createMediaStreamSource/connect threw', { lane, e }); return false; }
    } catch (e) { dbg('clipMixerAttach: outer threw', { lane, e }); return false; }
  }
  function clipMixerOutputTrack() {
    // Reads whatever is currently flowing into _mixDest. Does NOT attach
    // anything - call clipMixerAttach() per lane first.
    try {
      const live = (t) => { try { return t && t.readyState === 'live'; } catch { return false; } };
      let anyLive = false;
      try { anyLive = live(_mixLane.game.track) || live(_mixLane.mic.track); } catch {}
      if (!anyLive || !_mixDest) return null;
      const trs = (_mixDest.stream && _mixDest.stream.getAudioTracks()) || [];
      for (const tr of trs) {
        if (live(tr)) {
          if (!_mixLogged) { _mixLogged = true; try { logInfo('audio mix active (per-lane gain)'); } catch {} }
          return tr;
        }
      }
    } catch (e) { dbg('clipMixerOutputTrack: threw', e); }
    return null;
  }
  function clipMixRefresh() {
    // Fold newly-appeared / newly-dead tracks into the live mix each tick.
    try {
      if (!_mixActive || rsState !== RS_RECORDING) return;
      let gt = null;
      try { const gs = clipGameAudioTracks(); gt = (gs && gs[0]) || null; } catch {}
      if (!gt) { try { const els = clipMediaTracks(); gt = (els && els[0]) || null; } catch {} }
      let mt = null;
      try { if (clipCfg().mic) mt = clipMicTrack(); } catch {}
      try { clipMixerAttach('game', gt); } catch {}
      try { clipMixerAttach('mic', mt); } catch {}
    } catch {}
  }
  function clipMixedAudioTrack() {
    // Single output track for the recorder. Routing priority:
    //  (1) game-only (no mic): tap MSD track DIRECT. Same-realm by
    //      construction (MSD created in the game's own context), slider
    //      applied via the tap's record-side gain. Proven to carry signal;
    //      cross-realm MediaStreamSource mixers have measured 0.00 here.
    //  (2) mic-only: mic track direct.
    //  (3) game+mic: per-lane-gain mixer (best effort); falls back to the
    //      game tap direct if the mixer yields nothing.
    try {
      const cfg = clipCfg();
      const live = (t) => { try { return t && t.readyState === 'live'; } catch { return false; } };
      let game = null, gameTap = null;
      if (cfg.audio) {
        try {
          const f = clipFirstGameTap();
          if (f && live(f.track)) { game = f.track; gameTap = f.tap; }
        } catch {}
        if (!game) { try { const els = clipMediaTracks(); game = (els && els.find(live)) || null; } catch {} }
      }
      let mic = null;
      if (cfg.mic) { try { mic = clipMicTrack(); mic = live(mic) ? mic : null; } catch {} }
      try {
        const gs = clipGameAudioTracks();
        const els = clipMediaTracks();
        logInfo(`AUDIO-DIAG sources hasGame=${!!game} hasMic=${!!mic} cfg.audio=${cfg.audio} cfg.mic=${cfg.mic} gameTracksFound=${gs.length} elTracksFound=${els.length} ctxCount=${_clipCtxs.size}`);
      } catch (e) { logInfo('AUDIO-DIAG threw: ' + e); }
      dbg('clipMixedAudioTrack: sources', { hasGame: !!game, hasMic: !!mic });
      if (game && !mic) {
        // Direct same-realm path. Slider -> tap recGain (speakers untouched).
        try {
          const v = clipVol('game');
          if (gameTap && gameTap.recGain) {
            try {
              if (gameTap.recGain.gain.setTargetAtTime && gameTap.recGain.context) gameTap.recGain.gain.setTargetAtTime(v, gameTap.recGain.context.currentTime, 0.01);
              else gameTap.recGain.gain.value = v;
            } catch { try { gameTap.recGain.gain.value = v; } catch {} }
          } else { try { clipTapSetGameVol(); } catch {} }
        } catch {}
        _mixActive = false;
        try { logInfo('audio route: game tap DIRECT (same-realm)'); } catch {}
        return game;
      }
      if (!game && !mic) { _mixActive = false; return null; }
      if (mic && !game) { _mixActive = false; return mic; }
      const gOk = clipMixerAttach('game', game);
      const mOk = clipMixerAttach('mic', mic);
      if (!gOk && !mOk) { _mixActive = false; dbg('clipMixedAudioTrack: attach failed for both lanes'); return game || mic || null; }
      const mixed = clipMixerOutputTrack();
      if (mixed) { _mixActive = true; return mixed; }
      _mixActive = false;
      dbg('clipMixedAudioTrack: mixer produced no output track, falling back to raw source');
      return game || mic || null;
    } catch (e) { dbg('clipMixedAudioTrack: threw', e); return null; }
  }
  // Phase 3: analyser buffer reuse - one cached Uint8Array per fftSize,
  // zero per-read allocation. clipAnLevel is hit by the meter loop, the
  // audio diag line and every save/boot log; allocating a fresh buffer per
  // read was pure GC pressure on the game thread.
  const _cnAnBufs = {};
  function clipAnLevel(an) {
    // 0.00 = digital silence .. ~0.5 = loud. Never throws.
    try {
      if (!an) return null;
      const n = (an.fftSize && an.fftSize > 0) ? an.fftSize : 256;
      let buf = _cnAnBufs[n];
      if (!buf) { buf = new Uint8Array(n); _cnAnBufs[n] = buf; }
      try { an.getByteTimeDomainData(buf); } catch { return null; }
      let peak = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = Math.abs(buf[i] - 128) / 128;
        if (v > peak) peak = v;
      }
      return peak;
    } catch { return null; }
  }
  function clipAudioLevels() {
    // One-line truth for the panel + logs: per-tap peak, mixer peak, states.
    try {
      const taps = [];
      try {
        _clipCtxs.forEach((ctx) => {
          try {
            if (clipIsOwnCtx(ctx) || clipIsOfflineCtx(ctx)) return;
            let t = null;
            try { t = _clipTaps ? _clipTaps.get(ctx) : ctx.__cnClipTap; } catch {}
            if (!t || !t.analyser) return;
            const lvl = clipAnLevel(t.analyser);
            let st = '?';
            try { st = ctx.state || '?'; } catch {}
            taps.push(`${st}:${lvl == null ? '?' : lvl.toFixed(2)}`);
          } catch {}
        });
      } catch {}
      let mixLvl = null, mixState = '?';
      try { if (_mixAn) mixLvl = clipAnLevel(_mixAn); } catch {}
      try { if (_mixCtx) mixState = _mixCtx.state || '?'; } catch {}
      let gameLvl = null, micLvl = null;
      try { gameLvl = _mixLane.game.meter ? clipAnLevel(_mixLane.game.meter) : null; } catch {}
      try { micLvl = _mixLane.mic.meter ? clipAnLevel(_mixLane.mic.meter) : null; } catch {}
      let mediaN = 0;
      try { mediaN = clipMediaTracks().length; } catch {}
      return `taps[${taps.length}]=${taps.slice(0, 4).join(',') || '-'} mix=${mixState}:${mixLvl == null ? '?' : mixLvl.toFixed(2)} game=${gameLvl == null ? '?' : gameLvl.toFixed(2)} mic=${micLvl == null ? '?' : micLvl.toFixed(2)} media=${mediaN}`;
    } catch { return ''; }
  }

  // ---- watermark composite (Medal-style, burned into the video) ----
  let _cnComp = { canvas: null, ctx: null, raf: 0, last: 0, label: null, labelKey: '' };
  function cnCompositeStop() {
    try {
      _cnComp.last = 0;
      if (_cnComp.raf) { try { cancelAnimationFrame(_cnComp.raf); } catch {} try { clearInterval(_cnComp.raf); } catch {} _cnComp.raf = 0; }
      _cnComp.canvas = null; _cnComp.ctx = null; _cnComp.label = null; _cnComp.labelKey = '';
    } catch {}
  }
  function cnLabelCanvas(name, mode, h) {
    // Watermark label: line1 = name/mode per toggles, line2 = brand tag.
    // Any part can be hidden; all hidden -> null (nothing drawn). Cache key
    // includes the toggles so flipping a checkbox rebuilds mid-roll.
    try {
      let showName = true, showMode = true, showBrand = true;
      try { const c = clipCfg(); showName = c.wmName !== false; showMode = c.wmMode !== false; showBrand = c.wmBrand !== false; } catch {}
      const key = (showName ? name : '') + '|' + (showMode ? mode : '') + '|' + (showBrand ? 'b' : '') + '|' + h;
      if (_cnComp.label && _cnComp.labelKey === key) return _cnComp.label;
      const lines = [];
      const line1 = ((showName && name) ? name + (showMode && mode ? ' - ' : '') : '') + ((showMode && mode) ? mode : '');
      if (line1) lines.push({ t: line1, big: true });
      if (showBrand) lines.push({ t: 'clipped with clippednow.gg', big: false });
      if (!lines.length) { _cnComp.label = null; _cnComp.labelKey = key; return null; }
      const fs = Math.max(12, Math.round(h * 0.032));
      const fs2 = Math.max(10, Math.round(h * 0.02));
      const pad = Math.max(6, Math.round(h * 0.02));
      const meas = document.createElement('canvas').getContext('2d');
      let wMax = 0, hTot = 0;
      for (const L of lines) {
        meas.font = (L.big ? '800 ' + fs : '700 ' + fs2) + 'px Inter,"Segoe UI",system-ui,-apple-system,sans-serif';
        wMax = Math.max(wMax, meas.measureText(L.t).width);
        hTot += L.big ? fs : fs2;
      }
      hTot += Math.round(pad * (lines.length > 1 ? 2.5 : 1.6));
      const c = document.createElement('canvas');
      c.width = Math.max(2, Math.ceil(wMax) + pad * 2);
      c.height = Math.max(2, Math.ceil(hTot));
      const g = c.getContext('2d');
      if (!g) return null;
      const stroke = Math.max(2, Math.round(2 * h / 540));
      g.textBaseline = 'top'; g.lineJoin = 'round';
      g.lineWidth = stroke; g.strokeStyle = '#000'; g.fillStyle = '#FFD400';
      let y = pad;
      for (const L of lines) {
        const f = L.big ? fs : fs2;
        g.font = (L.big ? '800 ' : '700 ') + f + 'px Inter,"Segoe UI",system-ui,-apple-system,sans-serif';
        g.strokeText(L.t, pad, y);
        g.fillText(L.t, pad, y);
        y += f + Math.round(pad * 0.5);
      }
      _cnComp.label = c; _cnComp.labelKey = key;
      return c;
    } catch { return null; }
  }
  function cnWatermarkXY(cw, ch, lw, lh, pad) {
    // Label anchor from cn_wm_pos: tl/tc/tr/bl/bc/br. Defaults bottom-right.
    try {
      let pos = 'br';
      try { pos = clipCfg().wmPos || 'br'; } catch {}
      const x = pos.endsWith('l') ? pad : (pos.endsWith('r') ? Math.max(pad, cw - lw - pad) : Math.max(pad, Math.round((cw - lw) / 2)));
      const y = pos[0] === 't' ? pad : Math.max(pad, ch - lh - pad);
      return [x, y];
    } catch { return [Math.max(6, cw - lw - 6), Math.max(6, ch - lh - 6)]; }
  }
  function cnCompositeStream(gameCanvas, fps, vertical) {
    try {
      cnCompositeStop();
      const gw0 = gameCanvas.width || 1280, gh0 = gameCanvas.height || 720;
      const c = document.createElement('canvas');
      const applySize = (gw, gh) => {
        if (vertical && gw > 0 && gh > 0) {
          if (gw / gh > 9 / 16) { c.width = Math.max(2, Math.round(gh * 9 / 16)); c.height = gh; }
          else { c.width = gw; c.height = Math.max(2, Math.round(gw * 16 / 9)); }
        } else { c.width = gw || 1280; c.height = gh || 720; }
      };
      applySize(gw0, gh0);
      let ctx = null;
      try { ctx = c.getContext('2d', { alpha: false }); } catch {}
      if (!ctx) { try { ctx = c.getContext('2d'); } catch {} }
      if (!ctx) return null;
      _cnComp.canvas = c; _cnComp.ctx = ctx; _cnComp.vert = !!vertical;
      try { _cnComp.pn = 0; _cnComp.pt0 = 0; _cnComp.pfps = 0; _cnComp.pema = 0; _cnComp.pwarn = false; } catch {}
      // Phase 2 throttle: paint on an interval at the TARGET fps, not on
      // every display refresh. rAF woke this callback ~60x/sec just to skip
      // 3 of 4 wakeups via the `last` gate - at 15fps capture that is 45
      // Cadence: rAF loop + interval gate, exactly like the proven 1.3.x
      // pipeline. A setInterval throttle was tried (fewer wakeups) but in
      // loaded game tabs timers get deprioritized while rAF stays
      // vsync-locked, so captures came out sparse. rAF wakes every vsync;
      // the gate paints at most at the target fps. 'unlimited' paints
      // every frame. Stats count gated paints = real capture fps.
      const uncapped = (fps === 'unlimited');
      const rate = uncapped ? 0 : Math.max(5, Math.min(120, Number(fps) || 15));
      const interval = uncapped ? 0 : 1000 / rate;
      const paint = () => {
        if (!_cnComp.canvas) return;
        try { if (typeof document !== 'undefined' && document.hidden) return; } catch {}
        let p0 = 0;
        try { p0 = performance.now(); } catch {}
        try {
          const gw = gameCanvas.width || 0, gh = gameCanvas.height || 0;
          if (vertical && gw > 0 && gh > 0) {
            let ew = c.width, eh = c.height;
            if (gw / gh > 9 / 16) { ew = Math.max(2, Math.round(gh * 9 / 16)); eh = gh; }
            else { ew = gw; eh = Math.max(2, Math.round(gw * 16 / 9)); }
            if (c.width !== ew || c.height !== eh) { c.width = ew; c.height = eh; _cnComp.labelKey = ''; }
            try {
              if (gw / gh > 9 / 16) { const cw = gh * 9 / 16, sx = (gw - cw) / 2; ctx.drawImage(gameCanvas, sx, 0, cw, gh, 0, 0, c.width, c.height); }
              else { const chh = gw * 16 / 9, sy = (gh - chh) / 2; ctx.drawImage(gameCanvas, 0, sy, gw, chh, 0, 0, c.width, c.height); }
            } catch {}
          } else {
            if (gw > 0 && gh > 0 && (c.width !== gw || c.height !== gh)) { c.width = gw; c.height = gh; _cnComp.labelKey = ''; }
            try { ctx.drawImage(gameCanvas, 0, 0, c.width, c.height); } catch {}
          }
          try {
            const nm = (cnMe.name || '').trim(), md = cnGmLabel(cnMode.code);
            const L = cnLabelCanvas(nm, md, c.height);
            if (L) {
              const pad = Math.max(6, Math.round(c.height * 0.02));
              const [lx, ly] = cnWatermarkXY(c.width, c.height, L.width, L.height, pad);
              ctx.drawImage(L, lx, ly);
            }
          } catch {}
          // Paint stats: cadence (paints/sec) + cost EMA. If paints cost
          // more than their slot, the composite IS the dropped-frames
          // culprit - log once per session so the user gets told, not
          // just a choppy file.
          try {
            if (p0) {
              const dt = performance.now() - p0;
              _cnComp.pema = _cnComp.pema ? _cnComp.pema * 0.9 + dt * 0.1 : dt;
              _cnComp.pn = (_cnComp.pn || 0) + 1;
              const nowMs = performance.now();
              if (!_cnComp.pt0) _cnComp.pt0 = nowMs;
              if (nowMs - _cnComp.pt0 >= 2000) {
                _cnComp.pfps = Math.round(_cnComp.pn * 1000 / Math.max(1, nowMs - _cnComp.pt0));
                _cnComp.pn = 0; _cnComp.pt0 = nowMs;
                if (!uncapped && !_cnComp.pwarn && interval > 0 && _cnComp.pema > interval * 1.5 && _cnComp.pfps > 0) {
                  _cnComp.pwarn = true;
                  try { logInfo(`composite overloaded: paint=${_cnComp.pema.toFixed(0)}ms vs ${interval.toFixed(0)}ms slot at ${rate}fps - lower FPS or disable watermark`); } catch {}
                }
              }
            }
          } catch {}
        } catch {}
      };
      try { paint(); } catch {} // first frame sync: recorder never starts blank
      try {
        const loop = () => {
          if (!_cnComp.canvas) return;
          try {
            let doPaint = uncapped;
            if (!doPaint) {
              try {
                const nowMs = performance.now();
                if (nowMs - _cnComp.last >= interval) { _cnComp.last = nowMs; doPaint = true; }
              } catch { doPaint = true; }
            }
            if (doPaint) paint();
          } catch {}
          try { _cnComp.raf = requestAnimationFrame(loop); } catch {}
        };
        _cnComp.raf = requestAnimationFrame(loop);
      } catch {}
      try { return uncapped ? c.captureStream() : c.captureStream(rate); } catch { return c.captureStream(); }
    } catch { try { cnCompositeStop(); } catch {} return null; }
  }
  let _clipRO = null;
  let _clipResizeT = 0;
  function clipWatchResize(canvas) {
    try {
      if (_clipRO) { try { _clipRO.disconnect(); } catch {} _clipRO = null; }
      if (!canvas || typeof ResizeObserver === 'undefined') return;
      let lastW = canvas.width || 0, lastH = canvas.height || 0;
      _clipRO = new ResizeObserver(() => {
        try {
          const w = canvas.width || 0, h = canvas.height || 0;
          if (!(w > 0 && h > 0)) return;
          if (w === lastW && h === lastH) return;
          lastW = w; lastH = h;
          if (rsState === RS_IDLE || rsState === RS_STARTING) return;
          if (rsState === RS_SAVING) { try { rsCutNow(); } catch {} return; }
          // Debounce: game canvases flap size during loads and transitions,
          // and every reboot wipes the rolling window. Reboot only if the
          // new size is still current after 1.5s of quiet AND differs from
          // what the session was booted with (_clip.cw/ch untouched here).
          try { if (_clipResizeT) clearTimeout(_clipResizeT); } catch {}
          _clipResizeT = setTimeout(() => {
            try {
              _clipResizeT = 0;
              const w2 = canvas.width || 0, h2 = canvas.height || 0;
              if (!(w2 > 0 && h2 > 0)) return;
              if (w2 !== lastW || h2 !== lastH) return; // still flapping
              if (w2 === (_clip.cw || 0) && h2 === (_clip.ch || 0)) {
                try { logInfo('canvas resize was transient - session continues'); } catch {}
                return;
              }
              logInfo('canvas resized - rebooting recorder');
              try { rsReboot('resize'); } catch {}
            } catch {}
          }, 1500);
        } catch {}
      });
      try { _clipRO.observe(canvas); } catch {}
    } catch {}
  }
  function clipProbeBlob(blob, ms) {
    // Single probe round: 'ok' (first frame decoded), 'timeout' (slow -
    // timeouts lie under load) or 'error' (decoder rejected it).
    return new Promise((res) => {
      let done = false, url = '', v = null;
      const finish = (how) => {
        if (done) return; done = true;
        try { if (v) { try { v.pause(); } catch {} try { v.removeAttribute('src'); } catch {} try { v.load(); } catch {} try { v.remove(); } catch {} } } catch {}
        try { if (url) URL.revokeObjectURL(url); } catch {}
        res(how);
      };
      try {
        url = URL.createObjectURL(blob);
        v = document.createElement('video');
        v.muted = true; v.preload = 'auto';
        v.onloadeddata = () => finish('ok');
        v.onerror = () => finish('error');
        v.src = url;
        setTimeout(() => finish('timeout'), Math.max(2000, Math.min(25000, Number(ms) || 6000)));
      } catch (e) { finish('error'); }
    });
  }
  function clipHeadSlice(blob) {
    try {
      if (blob && blob.size > 16 * 1024 * 1024) return new Blob([blob.slice(0, 16 * 1024 * 1024)], { type: blob.type || 'video/webm' });
    } catch {}
    return blob;
  }
  async function clipVerifyPlayable(blob) {
    // Two-stage verify. Fast path: 16MB head probe (loadeddata fires on the
    // first decodable frame, size-independent). Trap this fixed: a sliced
    // MP4 head can lack its moov box and ERROR while the full file is
    // perfectly playable - that false-error discarded ~90% of big MP4
    // clips as "corrupted". So a head ERROR falls back to one full-file
    // probe (longer timeout) before anything is discarded. Timeout at
    // either stage means slow, not broken.
    try {
      if (!blob || !blob.size) return 'error';
      const first = await clipProbeBlob(clipHeadSlice(blob), 6000);
      if (first === 'ok') return 'ok';
      if (first === 'timeout') return 'timeout';
      try { logInfo(`head probe rejected (${((blob.size || 0) / 1048576).toFixed(1)}MB) - retrying full file before discarding`); } catch {}
      return await clipProbeBlob(blob, 20000);
    } catch { return 'error'; }
  }
  function clipProbeDuration(blob) {
    return new Promise((res) => {
      let done = false, url = '', v = null;
      const finish = (d) => {
        if (done) return; done = true;
        try { if (v) { try { v.removeAttribute('src'); } catch {} try { v.load(); } catch {} try { v.remove(); } catch {} } } catch {}
        try { if (url) URL.revokeObjectURL(url); } catch {}
        res(d);
      };
      try {
        if (!blob || !blob.size) { finish(null); return; }
        url = URL.createObjectURL(blob);
        v = document.createElement('video');
        v.muted = true; v.preload = 'metadata';
        v.onloadedmetadata = () => {
          let d = null;
          try { d = v.duration; } catch {}
          if (d && isFinite(d)) { finish(d); return; }
          // Chrome quirk: live-recorded WebM reports Infinity until seeked.
          // Force the real duration via a far seek; 'durationchange' then
          // reports the finite value. Standard workaround, local only.
          try {
            const onDur = () => {
              try {
                const d2 = v.duration;
                if (d2 && isFinite(d2)) {
                  try { v.removeEventListener('durationchange', onDur); } catch {}
                  try { v.currentTime = 0; } catch {}
                  finish(d2);
                }
              } catch { finish(null); }
            };
            v.addEventListener('durationchange', onDur);
            v.currentTime = 1e9; // seek far beyond actual length, on purpose
          } catch { finish(null); }
        };
        v.onerror = () => finish(null);
        v.src = url;
        setTimeout(() => finish(null), 4000);
      } catch (e) { finish(null); }
    });
  }
  // ---- recorder FSM (one continuous session + rolling window) ----
  // Why no rotation/stitching anymore: every MediaRecorder restart resets
  // timestamps, so raw-concatenating a previous segment with the current
  // one produced files with overlapping timestamps - players showed them
  // as 2-3fps stutter even though 30fps was recorded. Now a single session
  // runs until reboot/save, chunks carry wall-clock times, old chunks are
  // pruned, and every saved clip is one continuous timestamp run.
  // BOUND: a session never outlives the rolling window (segment roll at
  // keepMs). Otherwise the pruned middle leaves a timestamp HOLE: the file
  // reports full span (76s) but only head+tail frames exist, so players
  // show a long timeline that stalls after seconds. Rolls restart cleanly
  // with no stitching and no carry-over - a kill right after a roll gets a
  // shorter clip, same honest tradeoff as 1.3.x.
  const RS_IDLE = 'idle', RS_STARTING = 'starting', RS_RECORDING = 'recording', RS_SAVING = 'saving';
  const RS_READY_BYTES = 48 * 1024, RS_READY_AGE = 1200, RS_IDLE_RETRY = 2500;
  const RS_WIN_MAX_BYTES = 160 * 1024 * 1024;
  let rsState = RS_IDLE, rsGen = 0, rsSaveQ = null, rsRetryT = null, rsCutT = null, rsSegT = 0;
  function rsKeepMs() {
    // Rolling window span: longest possible clip + margin. Sessions roll
    // at exactly this age, so the window always lies inside one session.
    try {
      const cfg = clipCfg();
      return (Math.max(8, Math.min(90, Number(cfg.len) || 30)) + Math.max(0, Math.min(10, Number(cfg.after) || 0)) + 15) * 1000;
    } catch { return 50000; }
  }
  function rsActive() { try { return rsState === RS_RECORDING || rsState === RS_SAVING; } catch { return false; } }
  function rsDataReady() {
    try {
      if (rsState !== RS_RECORDING && rsState !== RS_SAVING) return false;
      const r = _clip.rec;
      if (!r || r.state !== 'recording') return false;
      if ((_clip.chunkBytes || 0) < RS_READY_BYTES) return false;
      if ((Date.now() - (_clip.segStartedAt || 0)) < RS_READY_AGE) return false;
      return true;
    } catch { return false; }
  }
  function rsClearTimers() {
    try { if (_clipResizeT) { clearTimeout(_clipResizeT); _clipResizeT = 0; } } catch {}
    try { if (rsSegT) { clearTimeout(rsSegT); rsSegT = 0; } } catch {}
    try { if (_clip.pendingTimer) { clearTimeout(_clip.pendingTimer); _clip.pendingTimer = null; } } catch {}
    try { if (rsRetryT) { clearTimeout(rsRetryT); rsRetryT = null; } } catch {}
    try { if (rsCutT) { clearTimeout(rsCutT); rsCutT = null; } } catch {}
  }
  function rsCleanup() {
    try { cnCompositeStop(); } catch {}
    try { if (_clipRO) { _clipRO.disconnect(); _clipRO = null; } } catch {}
    rsClearTimers();
    try { if (_clip.rec && _clip.rec.state !== 'inactive') { try { _clip.rec.onstop = null; _clip.rec.stop(); } catch {} } } catch {}
    _clip.rec = null;
    try { if (_clip.stream) { _clip.stream.getVideoTracks().forEach(t => { try { t.stop(); } catch {} }); } } catch {}
    _clip.rec = null; _clip.stream = null; _clip.chunks = []; _clip.chunkBytes = 0;
    _clip.finalizing = false; _clip.withAudio = false; _clip.wmActive = false; _clip.started = false;
  }
  function rsEnter(next, arg) {
    try {
      rsClearTimers();
      rsState = next;
      _clip.started = (next === RS_RECORDING || next === RS_SAVING);
      _clip.finalizing = (next === RS_SAVING);
      if (next === RS_IDLE) {
        rsCleanup();
        _clip.pendingTrigger = null;
        try {
          if (clipCfg().enabled) rsRetryT = setTimeout(() => { try { rsRetryT = null; if (rsState === RS_IDLE) rsEnter(RS_STARTING); } catch {} }, RS_IDLE_RETRY);
        } catch {}
      } else if (next === RS_STARTING) {
        if (!rsBoot()) rsEnter(RS_IDLE);
        else rsEnter(RS_RECORDING);
      } else if (next === RS_RECORDING) {
        if (!rsRecordStart('enter')) rsEnter(RS_IDLE);
        else {
          try {
            if (rsSaveQ && (Date.now() - rsSaveQ.at) < 12000) {
              const q = rsSaveQ; rsSaveQ = null;
              rsEnter(RS_SAVING, q.trigger);
            } else rsSaveQ = null;
          } catch {}
        }
      } else if (next === RS_SAVING) {
        rsSaveStep(arg || 'manual');
      }
    } catch {}
    try { cnRefreshFab(); } catch {}
  }
  function rsWant() {
    try { if (rsState === RS_IDLE) rsEnter(RS_STARTING); } catch {}
  }
  function rsReboot(why) {
    // why names the trigger (settings/resize/watchdog/preset/mic-ready):
    // every reboot starts a fresh session (fresh rolling window), so a
    // chatty reason here is the reason clips come out shorter than the
    // buffer. Reboots are expected and cheap - the alternative (stitching
    // across sessions) produced timestamp-broken files.
    try { logInfo(`rec reboot why=${why || '?'}`); } catch {}
    try {
      const w = String(why || '');
      if ((w.indexOf('settings:') === 0 || w.indexOf('preset') === 0) && _rsWebmFallback) {
        _rsWebmFallback = false;
        try { logInfo('capture settings changed - WebM fallback cleared, re-evaluating'); } catch {}
      }
    } catch {}
    try { rsEnter(RS_STARTING); } catch {}
  }
  function rsBoot() {
    try {
      const cfg = clipCfg();
      if (!cfg.enabled) return false;
      if (typeof MediaRecorder === 'undefined') { _clip.lastErr = 'MediaRecorder unsupported'; return false; }
      if (_clip.started && _clip.stream) return true;
      const canvas = clipFindCanvas();
      if (!canvas) { _clip.lastErr = 'No game canvas found'; return false; }
      try { _clip.cw = canvas.width || 0; _clip.ch = canvas.height || 0; } catch {}
      try { clipWatchResize(canvas); } catch {}
      let stream = null, wmActive = false;
      const needComp = cfg.watermark || cfg.layout === 'vertical';
      try { if (needComp) { try { stream = cnCompositeStream(canvas, cfg.fps, cfg.layout === 'vertical') || null; } catch {} } } catch {}
      if (stream) wmActive = !!cfg.watermark;
      else {
        try { stream = (cfg.fps === 'unlimited') ? canvas.captureStream() : canvas.captureStream(Number(cfg.fps) || 15); }
        catch (e) { try { stream = canvas.captureStream(); } catch (e2) { _clip.lastErr = 'captureStream failed'; return false; } }
      }
      if (!stream) { _clip.lastErr = 'captureStream empty'; return false; }
      _clip.wmActive = wmActive;
      try { clipPatchAudio(); } catch {}
      try { if (cfg.mic) clipEnsureMic(); } catch {}
      let withAudio = false;
      try {
        const vt = (stream.getVideoTracks() || [])[0] || null;
        if (vt) {
          let at = null;
          try { at = clipMixedAudioTrack(); } catch {}
          if (at) {
            try {
              const attached = clipAttachAudio(stream, at);
              if (attached) { stream = attached; withAudio = true; }
            } catch {}
          }
        }
      } catch {}
      _clip.withAudio = withAudio;
      // Reboots (resize/settings) swap streams: stop orphaned tracks so a
      // dead captureStream stops burning encoder/GPU behind the new one.
      // NEVER stop borrowed audio: tap MSD tracks, mic tracks and media
      // capture tracks are owned by their source - stop()ing one ends it
      // for good (the tap keeps metering via its analyser, but its track
      // reads 'ended' forever and every later clip is video-only). Detach
      // borrowed audio with removeTrack; stop video + mixer-owned tracks.
      try {
        const old = _clip.stream;
        if (old && old !== stream) {
          let ownedMix = [];
          try { ownedMix = (_mixDest && _mixDest.stream && _mixDest.stream.getAudioTracks()) || []; } catch {}
          old.getTracks().forEach((t) => {
            try {
              if (!t) return;
              if (t.kind === 'video' || ownedMix.indexOf(t) >= 0) { try { t.stop(); } catch {} return; }
              try { old.removeTrack(t); } catch {}
            } catch {}
          });
        }
      } catch {}
      _clip.stream = stream; _clip.lastErr = '';
      try {
        let audioEls = -1;
        try { audioEls = document.querySelectorAll ? document.querySelectorAll('audio').length : -1; } catch {}
        let mixState = '?';
        try { mixState = _mixCtx ? _mixCtx.state : 'none'; } catch {}
        let mimeDbg = '';
        try { mimeDbg = clipPickMime(!!withAudio) || ''; } catch {}
        logInfo(`rec boot ${wmActive ? 'comp' : 'direct'} audio=${withAudio ? 'yes' : 'NO'} (${mimeDbg} mix=${mixState} ctxs=${_clipCtxs.size} <audio>=${audioEls} mic=${clipMicTrack() ? 'live' : (clipCfg().mic ? 'pending' : 'off')} ${clipAudioLevels()})`);
      } catch {}
      _clip.started = true; _clip.recStartedAt = Date.now();
      try {
        if (!rsBoot._visHooked) {
          rsBoot._visHooked = true;
          document.addEventListener('visibilitychange', () => { try { if (document.hidden) clipPause(true); else clipPause(false); } catch {} });
        }
      } catch {}
      return true;
    } catch { return false; }
  }
  function rsAudioCheck() {
    // Audio hot-path, RECORDING-only (never mid-save). Routing mirrors
    // clipMixedAudioTrack: game-only records the tap MSD direct (same-realm,
    // proven signal); mic-only records mic direct; game+mic uses the mixer.
    // Swaps restart the segment (tracks are immutable); pure lane additions
    // inside an already-mixed stream need no restart.
    try {
      if (rsState !== RS_RECORDING) return;
      if (!_clip.stream) return;
      let vt = null;
      try { vt = (_clip.stream.getVideoTracks() || [])[0] || null; } catch {}
      if (!vt || vt.readyState !== 'live') return;
      const cfg = clipCfg();
      const live = (t) => { try { return t && t.readyState === 'live'; } catch { return false; } };
      let game = null, gameTap = null;
      if (cfg.audio) {
        try {
          const f = (typeof clipFirstGameTap === 'function') ? clipFirstGameTap() : null;
          if (f && live(f.track)) { game = f.track; gameTap = f.tap; }
        } catch {}
        if (!game) { try { const els = clipMediaTracks(); game = (els && els.find(live)) || null; } catch {} }
      }
      let mic = null;
      if (cfg.mic) { try { mic = clipMicTrack(); mic = live(mic) ? mic : null; } catch {} }
      let curA = null;
      try { curA = (_clip.stream.getAudioTracks() || [])[0] || null; } catch {}
      const swapTo = (track, why) => {
        try {
          try { if (!rsSwapGate()) return false; } catch {}
          // Fresh tap may hold a stale recGain value: push the slider now.
          try { if (typeof clipTapSetGameVol === 'function') clipTapSetGameVol(); } catch {}
          _clip.withAudio = true;
          try { logInfo(`audio route: ${why} - session restart`); } catch {}
          try {
            const s = _clip.stream;
            if (curA && curA !== track) { try { s.removeTrack(curA); } catch {} }
            try {
              if ((s.getAudioTracks() || []).indexOf(track) < 0) s.addTrack(track);
            } catch { return false; }
          } catch { return false; }
          try { rsRecordRestart('swap:' + why); } catch {}
          return true;
        } catch { return false; }
      };
      if (game && !mic) {
        // Direct tap path (no mixer). Refresh slider gain on the tap.
        try {
          const v = clipVol('game');
          const tap = gameTap;
          if (tap && tap.recGain) {
            try {
              if (tap.recGain.gain.setTargetAtTime && tap.recGain.context) tap.recGain.gain.setTargetAtTime(v, tap.recGain.context.currentTime, 0.01);
              else tap.recGain.gain.value = v;
            } catch { try { tap.recGain.gain.value = v; } catch {} }
          }
        } catch {}
        if (curA === game) { if (!_clip.withAudio) _clip.withAudio = true; _mixActive = false; return; }
        if (!live(curA)) { _mixActive = false; swapTo(game, 'game tap direct'); return; }
        // Already recording some audio: only swap if the tap actually
        // feeding the recorder went cold/silent while a hotter tap exists
        // (e.g. latched onto a silent utility ctx, game on another).
        // NOTE: this must check the OWNER of curA specifically - max-across-
        // taps never fires when it should (a hot meter elsewhere masks a
        // cold recorded tap).
        try {
          // Which tap is actually feeding the recorder right now?
          let curTapLvl = null;
          try {
            _clipCtxs.forEach((c) => {
              try {
                if (clipIsOwnCtx(c) || clipIsOfflineCtx(c)) return;
                const t = _clipTaps ? _clipTaps.get(c) : c.__cnClipTap;
                if (!t || !t.analyser || !t.msd || !t.msd.stream) return;
                const trs = t.msd.stream.getAudioTracks() || [];
                if (trs.indexOf(curA) < 0) return;
                const l = clipAnLevel(t.analyser);
                if (l != null) curTapLvl = l;
              } catch {}
            });
          } catch {}
          // If that tap is silent (or unknown), hop to the hottest one.
          if (curTapLvl == null || curTapLvl < 0.005) {
            let hot = null;
            try {
              const f = clipFirstGameTap();
              if (f && live(f.track) && f.track !== curA) {
                const l = (f.tap && f.tap.analyser) ? clipAnLevel(f.tap.analyser) : null;
                if (l != null && l > 0.02) hot = f;
              }
            } catch {}
            if (hot) { _mixActive = false; swapTo(hot.track, 'cold tap -> hot tap'); return; }
          }
        } catch {}
        _mixActive = false;
        return;
      }
      if (!game && !mic) return;
      if (mic && !game) {
        if (curA === mic) { if (!_clip.withAudio) _clip.withAudio = true; _mixActive = false; return; }
        _mixActive = false;
        swapTo(mic, 'mic direct');
        return;
      }
      // game+mic: per-lane mixer (best effort).
      const wasAudio = _clip.withAudio;
      const gAttached = clipMixerAttach('game', game);
      const mAttached = clipMixerAttach('mic', mic);
      if (!gAttached && !mAttached) return;
      if (wasAudio && live(curA)) { _mixActive = true; return; }
      const mixed = clipMixerOutputTrack();
      if (!mixed) {
        // Mixer silent fallback: at least keep the hot game tap direct.
        if (game && (!live(curA) || curA !== game)) { _mixActive = false; swapTo(game, 'mixer silent - game tap direct'); }
        return;
      }
      let ns = null;
      try { ns = clipAttachAudio(_clip.stream, mixed); } catch { return; }
      if (!ns) return;
      try { if (!rsSwapGate()) return; } catch {}
      _clip.stream = ns; _clip.withAudio = true; _mixActive = true;
      try { logInfo('audio appeared mid-roll - new session with audio (mix)'); } catch {}
      try { rsRecordRestart('mix-attach'); } catch {}
    } catch (e) { dbg('rsAudioCheck: threw', e); }
  }
  function clipPause(p) {
    try {
      const r = _clip.rec;
      if (!r) return;
      if (p && r.state === 'recording') { try { r.pause(); } catch {} }
      else if (!p && r.state === 'paused') { try { r.resume(); } catch {} }
    } catch {}
  }
  // WebM fallback for starved encoders: set once we SEE a session deliver
  // <0.4 chunks/sec (software H264 while gaming emits so sparsely the MP4
  // is undecodable, not just choppy - WebM tolerates the gaps and still
  // plays). Cleared when the user touches capture settings.
  let _rsWebmFallback = false;
  function rsRecordStart(why) {
    // Starts ONE continuous session on the live stream. Chunks accumulate
    // with wall timestamps (1s timeslice); rsPruneWindow keeps the rolling
    // buffer. Restarts happen ONLY on reboot / audio-route change /
    // post-save - each restart begins a fresh session and fresh window.
    try {
      if (!_clip.stream) { _clip.lastErr = 'captureStream empty'; return false; }
      const cfg = clipCfg();
      const mime = _rsWebmFallback ? clipPickMimeExport(!!_clip.withAudio) : clipPickMime(!!_clip.withAudio);
      const opts = { videoBitsPerSecond: Number(cfg.bitrate) || 1500000 };
      if (_clip.withAudio) opts.audioBitsPerSecond = 128000;
      if (mime) opts.mimeType = mime;
      const g = ++rsGen;
      let rec = null;
      try { rec = new MediaRecorder(_clip.stream, opts); } catch (e) { try { rec = new MediaRecorder(_clip.stream); } catch (e2) { _clip.lastErr = 'Recorder init failed'; return false; } }
      _clip.rec = rec; _clip.chunks = []; _clip.chunkBytes = 0; _clip.mime = mime || ''; _clip.segStartedAt = Date.now();
      _clip.cadChecked = false; _clip.cadN = 0;
      _clip.sessHead = null; _clip.sessHeadBytes = 0; _clip.sessHeadDone = false;
      rec.ondataavailable = (e) => {
        try {
          if (e.data && e.data.size > 0 && g === rsGen) {
            const nowE = Date.now();
            _clip.chunks.push({ b: e.data, t: nowE });
            _clip.chunkBytes += e.data.size;
            // Pin the session head (first ~256KB carry the EBML/moov header
            // + track info). Long sessions prune it out of the rolling
            // window; without it the saved slice is undecodable. Pinned
            // refs survive pruning; finish() prepends the non-overlapping
            // part. 256KB/session is negligible.
            try {
              if (!_clip.sessHeadDone) {
                (_clip.sessHead = _clip.sessHead || []).push({ b: e.data, t: nowE });
                _clip.sessHeadBytes = (_clip.sessHeadBytes || 0) + e.data.size;
                if (_clip.sessHeadBytes >= 256 * 1024) _clip.sessHeadDone = true;
              }
            } catch {}
            try { rsPruneWindow(); } catch {}
            try { rsCadenceWatch(); } catch {}
          }
        } catch {}
      };
      rec.onerror = () => { try { _clip.lastErr = 'Recorder error'; } catch {} };
      try { rec.start(1000); } catch (e) { _clip.lastErr = 'Recorder start failed'; return false; }
      // Segment roll: bound the session to the window span so saves never
      // outlive their headers (timestamp holes). Skipped mid-save; the next
      // session starts fresh right after the store.
      try {
        if (rsSegT) clearTimeout(rsSegT);
        rsSegT = 0;
        const rollMs = rsKeepMs();
        rsSegT = setTimeout(() => {
          try {
            rsSegT = 0;
            if (rsState === RS_RECORDING) {
              try { logInfo('segment roll - fresh session, window stays headed'); } catch {}
              try { rsRecordRestart('segment-roll'); } catch {}
            }
          } catch {}
        }, rollMs);
      } catch {}
      // Assertion: proves the recorder actually holds an audio track. If
      // aTracks=0 here after a "yes audio" boot, the track object was
      // silently dropped (realm/container) - the file can never have sound.
      try {
        const vN = (_clip.stream.getVideoTracks() || []).length;
        const aT = _clip.stream.getAudioTracks() || [];
        logInfo(`rec session mime=${rec.mimeType || mime || '?'} vTracks=${vN} aTracks=${aT.length} aLive=${aT.map(t => t.readyState).join(',') || '-'} withAudioFlag=${!!_clip.withAudio} why=${why || '?'}`);
      } catch {}
      return true;
    } catch { return false; }
  }
  function rsCadenceWatch() {
    // Once per session, ~7s in: if the encoder delivered <0.4 chunks/sec on
    // MP4, it is starved and the file will be undecodable (not just choppy).
    // Flip this machine to WebM for reliability and restart the session.
    // Runs once per session; cleared settings re-arm it.
    try {
      if (_clip.cadChecked) return;
      const age = Date.now() - (_clip.segStartedAt || Date.now());
      if (age < 7000) return;
      _clip.cadChecked = true;
      if (_rsWebmFallback) return;
      const mime = String(_clip.mime || '');
      if (!mime.includes('mp4')) return;
      const n = (_clip.chunks || []).length;
      const rate = n / Math.max(1, age / 1000);
      if (rate >= 0.4) return;
      _rsWebmFallback = true;
      try { logInfo(`encoder starved (${rate.toFixed(2)} chunks/s on MP4) - WebM fallback for reliable files`); } catch {}
      try { toast('Encoder struggling - WebM for reliable clips (lower bitrate for MP4)', true); } catch {}
      try { rsRecordRestart('cadence-fallback-webm'); } catch {}
    } catch {}
  }
  function rsPruneWindow() {
    // Rolling window: keep enough for the longest possible clip (buffer +
    // aftermath + margin) and cap memory so Beast bitrates can't OOM the
    // tab (~160MB ~= 40s at 30Mbps, full 75s+ at Ultra and below).
    try {
      const keepMs = rsKeepMs();
      const now = Date.now();
      const ch = _clip.chunks || [];
      let cut = 0;
      while (cut < ch.length && (now - ch[cut].t > keepMs)) cut++;
      if (cut > 0) {
        let freed = 0;
        for (let i = 0; i < cut; i++) freed += ((ch[i].b && ch[i].b.size) || 0);
        ch.splice(0, cut);
        _clip.chunkBytes = Math.max(0, (_clip.chunkBytes || 0) - freed);
      }
      let total = _clip.chunkBytes || 0, drop = 0;
      while ((ch.length - drop) > 0 && total > RS_WIN_MAX_BYTES) { total -= (((ch[drop] || {}).b && ch[drop].b.size) || 0); drop++; }
      if (drop > 0) { ch.splice(0, drop); _clip.chunkBytes = Math.max(0, total); }
    } catch {}
  }
  function clipTrackFps() {
    // What the capture track actually delivers (0 = unknown/not flowing).
    // This is the number that settles "recorded at 30 but plays at 3":
    // if src reads 3, frames never reached the encoder.
    try {
      const vt = (_clip.stream && _clip.stream.getVideoTracks && _clip.stream.getVideoTracks()[0]) || null;
      if (!vt) return 0;
      const st = vt.getSettings ? vt.getSettings() : null;
      const f = st && st.frameRate;
      return (f && isFinite(f)) ? Math.round(f) : 0;
    } catch { return 0; }
  }
  function rsRecordRestart(why) {
    // Audio route changed (tracks are immutable) or save-grab: stop this
    // session dead (no save, onstop detached) and start a fresh one. The
    // window resets - the new session covers from here with correct audio.
    try { logInfo(`rec session restart why=${why || '?'}`); } catch {}
    try { if (_clip.rec) { try { _clip.rec.onstop = null; } catch {} try { if (_clip.rec.state !== 'inactive') _clip.rec.stop(); } catch {} } } catch {}
    _clip.rec = null; _clip.chunks = []; _clip.chunkBytes = 0;
    _clip.sessHead = null; _clip.sessHeadBytes = 0; _clip.sessHeadDone = false;
    try { rsRecordStart(why); } catch {}
  }
  let _rsStorm = [];
  function rsSwapGate() {
    // Audio taps flap (game recreates contexts on scene changes). Without a
    // gate, every flap restarts the session and clears the window, so the
    // buffer never accumulates AND every clip comes from a seconds-old
    // session. Cap: 3 swaps per 10s; excess waits for the next ticker pass.
    try {
      const now = Date.now();
      _rsStorm = _rsStorm.filter((t) => now - t < 10000);
      if (_rsStorm.length >= 3) {
        try { logInfo('audio swap throttled (storm) - retrying on next pass'); } catch {}
        return false;
      }
      _rsStorm.push(now);
      return true;
    } catch { return true; }
  }
  function rsWindowSlice(ms) {
    // Newest entries covering ~ms of wall time. Single session means
    // continuous timestamps, so the assembled blob plays buttery smooth.
    try {
      const ch = _clip.chunks || [];
      if (!ch.length) return [];
      const endT = ch[ch.length - 1].t;
      let i = ch.length;
      while (i > 0 && endT - ch[i - 1].t <= ms) i--;
      return ch.slice(i);
    } catch { return []; }
  }
  function rsSaveStep(trigger) {
    try {
      const cfg = clipCfg();
      _clip.pendingTrigger = trigger;
      if (trigger !== 'manual') { try { cnSfx('tick'); } catch {} }
      // Last-chance audio grab: rolling window is video-only but a tap/mic
      // is available NOW - restart the session BEFORE the aftermath wait so
      // the aftermath carries real audio. Pre-roll stays video-only in this
      // rare case; an audible aftermath beats a silent full clip.
      try {
        if (!_clip.withAudio && trigger !== 'manual' && rsState === RS_SAVING && _clip.stream) {
          let at = null;
          try { at = clipMixedAudioTrack(); } catch {}
          if (at) {
            const ns = clipAttachAudio(_clip.stream, at);
            if (ns) {
              _clip.stream = ns; _clip.withAudio = true;
              try { logInfo('save-time audio grab - aftermath records with audio (new session)'); } catch {}
              try { rsRecordRestart('save-grab'); } catch {}
            }
          } else {
            try { logInfo(`save-time: no audio source (cfgA=${cfg.audio ? 1 : 0} cfgM=${cfg.mic ? 1 : 0}) - clip will be video-only`); } catch {}
          }
        }
      } catch {}
      const wait = Math.max(0, Math.min(10, Number(cfg.after) || 0));
      if (trigger === 'manual' || wait <= 0) { rsCutStep(trigger); return; }
      try {
        _clip.pendingTimer = setTimeout(() => { try { _clip.pendingTimer = null; } catch {} try { if (rsState === RS_SAVING) rsCutStep(_clip.pendingTrigger || trigger); } catch {} }, wait * 1000);
      } catch { rsCutStep(trigger); return; }
      try { if (cnOpen) cnRenderCurrent(); } catch {}
    } catch { try { rsCutStep(trigger); } catch {} }
  }
  function rsCutNow() {
    try {
      if (rsState !== RS_SAVING) return;
      try { if (_clip.pendingTimer) { clearTimeout(_clip.pendingTimer); _clip.pendingTimer = null; } } catch {}
      logInfo('resize during aftermath - finalizing early');
      rsCutStep(_clip.pendingTrigger || 'kill');
    } catch {}
  }
  function rsCutStep(trigger) {
    try {
      const cfg = clipCfg();
      try { if (_clip.pendingTimer) { clearTimeout(_clip.pendingTimer); _clip.pendingTimer = null; } } catch {}
      const r = _clip.rec, g = rsGen;
      const fail = (msg) => {
        try { if (msg) { _clip.lastErr = msg; try { toast(msg, true); } catch {} } } catch {}
        try { rsEnter(RS_RECORDING); } catch {}
        return null;
      };
      if (!r || r.state === 'inactive') return fail('');
      const mime = (r.mimeType) || _clip.mime || clipPickMime(!!_clip.withAudio) || 'video/webm';
      let done = false;
      const finish = () => {
        if (done) return; done = true;
        const cfg2 = clipCfg();
        const wantMs = (Math.max(8, Math.min(90, Number(cfg2.len) || 30)) + Math.max(0, Math.min(10, Number(cfg2.after) || 0)) + 2) * 1000;
        const win = rsWindowSlice(wantMs);
        // Prepend pinned session-head chunks the window pruned away (but
        // only the part strictly older than the window - overlapping
        // clusters would rewind timestamps and corrupt the file again).
        let sel = win;
        try {
          const head = _clip.sessHead || [];
          if (head.length && win.length && head[0].t < win[0].t) {
            const pre = head.filter((h) => h && h.t < win[0].t);
            if (pre.length) {
              sel = [...pre, ...win];
              try { logInfo(`clip prepended ${pre.length} header chunk(s) for decodability`); } catch {}
            }
          }
        } catch {}
        try {
          if (g !== rsGen || _clip.rec !== r) { rsEnter(RS_RECORDING); return null; }
          _clip.rec = null;
          _clip.chunks = []; _clip.chunkBytes = 0;
          _clip.sessHead = null; _clip.sessHeadBytes = 0; _clip.sessHeadDone = false;
        } catch {}
        const spanMs = sel.length > 1 ? Math.max(0, sel[sel.length - 1].t - sel[0].t) : 0;
        rsStoreClip(trigger, sel, mime, spanMs, cfg);
        return sel;
      };
      try {
        r.onstop = () => {
          try {
            if (g !== rsGen) return;
            finish();
          } catch { try { finish(); } catch {} }
        };
      } catch { return fail(''); }
      try { r.requestData(); } catch {}
      setTimeout(() => { try { if (g === rsGen && rsState === RS_SAVING) r.stop(); } catch { finish(); } }, 250);
      try { rsCutT = setTimeout(() => { try { if (!done) finish(); } catch {} }, 5000); } catch {}
    } catch { try { rsEnter(RS_RECORDING); } catch {} return null; }
  }
  async function rsStoreClip(trigger, entries, mime, spanMs, cfg) {
    try {
      entries = entries || [];
      cfg = cfg || clipCfg();
      const parts = [];
      for (const e of entries) { try { if (e && e.b && e.b.size > 0) parts.push(e.b); } catch {} }
      let blob = parts.length ? new Blob(parts, { type: mime }) : null;
      const secs = Math.max(0, (Number(spanMs) || 0) / 1000);
      // Chunk cadence: ~1 chunk/sec is healthy (1s timeslice). Far below
      // that, the encoder was starved - the file will play choppy no
      // matter what the fps setting says.
      let cadBit = '';
      try {
        if (secs > 2 && parts.length) {
          const cad = parts.length / secs;
          cadBit = ` cad=${cad.toFixed(1)}/s`;
          if (cad < 0.5) {
            try { toast('Clip recorded with dropped frames - lower FPS/quality if it repeats', true); } catch {}
          }
        }
      } catch {}
      try {
        let srcFps = 0;
        try { srcFps = clipTrackFps(); } catch {}
        logInfo(`clip cut ${trigger} mime=${mime} withAudio=${!!_clip.withAudio} window=${secs.toFixed(1)}s of ${cfg.len}s buffer${srcFps ? ' src=' + srcFps + 'fps' : ''}${cadBit} ${clipAudioLevels()}`);
      } catch {}
      if (!blob || !blob.size) {
        try { toast('Clip not ready - try again in a few seconds', true); } catch {}
        try { cnSfx('err'); } catch {}
        try { rsEnter(RS_RECORDING); } catch {}
        return null;
      }
      const plays = await clipVerifyPlayable(blob);
      // Policy: missing headers are now prepended at cut time, so a probe
      // ERROR means the bytes genuinely don't decode. But verdicts have
      // lied before (slow machines, decoder exhaustion), and a discarded
      // clip is unrecoverable while a kept one can still play in VLC or
      // after re-download. So: SAVE it, flagged suspect, toast honestly.
      const suspect = plays !== 'ok';
      if (plays === 'error') {
        try { logInfo(`clip failed decode probe (${((blob.size || 0) / 1048576).toFixed(1)}MB) - saving flagged SUSPECT, check playback`); } catch {}
      } else if (suspect) { try { logInfo('decode probe timed out under load - saving anyway (file is likely fine)'); } catch {} }
      // Persist who the clip is about (kill/death only, fresh feed only) so
      // the library can search by victim/killer name later.
      let kvKiller = null, kvVictim = null;
      try {
        const lk = _clip.lastKill;
        if (lk && trigger !== 'manual' && (Date.now() - (lk.at || 0)) < 30000) {
          kvKiller = String(lk.killer || '').slice(0, 32) || null;
          kvVictim = String(lk.victim || '').slice(0, 32) || null;
        }
      } catch {}
      const row = await clipStore({ trigger, mode: cnMode.code || null, layout: (cfg || clipCfg()).layout || null, killer: kvKiller, victim: kvVictim, suspect: !!suspect }, blob);
      if (!row) {
        // Storage write failed (quota/full) - say so LOUDLY instead of a
        // fake "saved" toast over a missing clip.
        try { logInfo('clip library write failed (storage full?)'); } catch {}
        try { toast('Could not save clip - browser storage full? Free space and retry', true); } catch {}
        try { cnSfx('err'); } catch {}
        try { rsEnter(RS_RECORDING); } catch {}
        try { if (cnOpen) fetchCnClips(); } catch {}
        return null;
      }
      try { toast(suspect ? `Clip saved (check playback, ${secs ? Math.round(secs) + 's' : 'clip'})` : `Clip saved (${secs ? Math.round(secs) + 's' : 'clip'})`, suspect, { label: 'Watch', fn: () => { try { cnShow('lib'); } catch {} } }); } catch {}
      try { if (suspect) cnSfx('err'); else cnSfx('ok'); } catch {}
      try { if (row && _clip.lastKill && trigger !== 'manual') _clip.lastKill.decision = suspect ? 'saved-suspect' : 'saved'; } catch {}
      try { rsEnter(RS_RECORDING); } catch {}
      try { if (cnOpen) fetchCnClips(); } catch {}
      try { if (row) cnWhMaybeAuto(row, blob); } catch {}
      return row;
    } catch { try { rsEnter(RS_RECORDING); } catch {} return null; }
  }
  function rsSaveReq(trigger) {
    // Entry for kills/deaths/wins/manual: queue when busy, otherwise save.
    // Forces one last synchronous audio-attach check right before cutting -
    // rsAudioCheck() otherwise only runs on the 2.5s ticker, which can miss
    // a tap/mic that became live moments before this trigger fired, leaving
    // the clip that's about to be cut permanently silent.
    try {
      const cfg = clipCfg();
      if (!cfg.enabled) {
        if (trigger === 'manual') { try { toast('Clipping is off - enable it in the panel', true); } catch {} }
        return Promise.resolve(null);
      }
      if (rsState === RS_SAVING) {
        if (trigger === 'manual') { try { toast('Finishing current clip - one sec…', true); } catch {} return Promise.resolve(null); }
        try { rsSaveQ = { trigger, at: Date.now() }; } catch {}
        try { if (cnOpen) cnRenderCurrent(); } catch {}
        return Promise.resolve(null);
      }
      if (rsState !== RS_RECORDING) {
        try { rsWant(); } catch {}
        if (trigger !== 'manual') { try { rsSaveQ = { trigger, at: Date.now() }; } catch {} }
        else { try { toast('Clip not ready yet - ' + (_clip.lastErr || 'recording… try again in a few seconds'), true); } catch {} }
        return Promise.resolve(null);
      }
      try {
        const before = !!_clip.withAudio;
        rsAudioCheck();
        dbg('rsSaveReq: pre-cut audio check', { trigger, before, after: !!_clip.withAudio, levels: clipAudioLevels() });
      } catch (e) { dbg('rsSaveReq: rsAudioCheck threw', e); }
      rsEnter(RS_SAVING, trigger);
      return Promise.resolve(null);
    } catch { return Promise.resolve(null); }
  }
  function clipReady() { try { return rsDataReady(); } catch { return false; } }

  // ---- local library (IndexedDB, cap 20) ----
  function clipDb() {
    return new Promise((res) => {
      try {
        const rq = indexedDB.open('cn-clips', 1);
        rq.onupgradeneeded = () => { try { rq.result.createObjectStore('clips', { keyPath: 'id' }); } catch {} };
        rq.onsuccess = () => res(rq.result);
        rq.onerror = () => res(null);
      } catch { res(null); }
    });
  }
  async function clipStore(meta, blob) {
    try {
      const db = await clipDb();
      if (!db) return null;
      const row = { id: 'c' + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36), ts: Date.now(), trigger: meta.trigger || 'manual', mode: meta.mode || null, layout: meta.layout || null, fav: false, mime: blob.type || 'video/webm', size: blob.size || 0, killer: meta.killer || null, victim: meta.victim || null, suspect: !!meta.suspect, blob };
      await new Promise((ok, no) => { try { const tx = db.transaction('clips', 'readwrite'); tx.objectStore('clips').put(row); tx.oncomplete = ok; tx.onerror = no; } catch (e) { no(e); } });
      try {
        const all = await new Promise((rs) => { try { const tx = db.transaction('clips', 'readonly'); const q = tx.objectStore('clips').getAll(); q.onsuccess = () => rs(q.result || []); q.onerror = () => rs([]); } catch { rs([]); } });
        // Evict oldest-first while over EITHER cap: 20 clips or ~600MB.
        // Beast bitrates (30Mbps ≈ 225MB per minute) would otherwise blow
        // the browser storage quota and every later save would fail.
        all.sort((a, b) => (a.ts || 0) - (b.ts || 0));
        let total = 0;
        for (const r of all) total += Number(r.size) || 0;
        const drop = [];
        while ((all.length - drop.length > 20 || total > 600 * 1024 * 1024) && drop.length < all.length) {
          const v = all[drop.length];
          // Never evict the clip we just saved, nor favorites, unless the
          // count cap forces it (count cap ignores fav to bound growth).
          if (v.id !== row.id && v.fav && (all.length - drop.length <= 20)) break;
          if (v.id === row.id) break;
          total -= Number(v.size) || 0;
          drop.push(v);
        }
        if (drop.length) {
          await new Promise((rs) => { try { const tx = db.transaction('clips', 'readwrite'); const st = tx.objectStore('clips'); drop.forEach(r => { try { st.delete(r.id); } catch {} }); tx.oncomplete = rs; tx.onerror = rs; } catch { rs(); } });
          try { logInfo(`library evicted ${drop.length} old clip(s)`); } catch {}
        }
      } catch {}
      try { db.close(); } catch {}
      return row;
    } catch { return null; }
  }
  async function clipList() {
    try {
      const db = await clipDb();
      if (!db) return [];
      const rows = await new Promise((res) => { try { const tx = db.transaction('clips', 'readonly'); const q = tx.objectStore('clips').getAll(); q.onsuccess = () => res(q.result || []); q.onerror = () => res([]); } catch { res([]); } });
      try { db.close(); } catch {}
      rows.sort((a, b) => (b.ts || 0) - (a.ts || 0));
      return rows.slice(0, 20);
    } catch { return []; }
  }
  async function clipDelete(id) {
    try {
      const db = await clipDb();
      if (!db) return;
      await new Promise((res) => { try { const tx = db.transaction('clips', 'readwrite'); tx.objectStore('clips').delete(id); tx.oncomplete = res; tx.onerror = res; } catch { res(); } });
      try { db.close(); } catch {}
    } catch {}
  }
  async function clipGet(id) {
    try {
      const db = await clipDb();
      if (!db) return null;
      const row = await new Promise((res) => { try { const tx = db.transaction('clips', 'readonly'); const q = tx.objectStore('clips').get(id); q.onsuccess = () => res(q.result || null); q.onerror = () => res(null); } catch { res(null); } });
      try { db.close(); } catch {}
      return row;
    } catch { return null; }
  }
  async function clipSetFav(id, fav) {
    try {
      const db = await clipDb();
      if (!db) return null;
      const row = await new Promise((res) => { try { const tx = db.transaction('clips', 'readonly'); const q = tx.objectStore('clips').get(id); q.onsuccess = () => res(q.result || null); q.onerror = () => res(null); } catch { res(null); } });
      if (!row) { try { db.close(); } catch {} return null; }
      row.fav = !!fav;
      await new Promise((res) => { try { const tx = db.transaction('clips', 'readwrite'); tx.objectStore('clips').put(row); tx.oncomplete = res; tx.onerror = res; } catch { res(); } });
      try { db.close(); } catch {}
      return row;
    } catch { return null; }
  }
  async function clipClearAll() {
    try {
      const db = await clipDb();
      if (!db) return;
      await new Promise((res) => { try { const tx = db.transaction('clips', 'readwrite'); tx.objectStore('clips').clear(); tx.oncomplete = res; tx.onerror = res; } catch { res(); } });
      try { db.close(); } catch {}
    } catch {}
  }

  // ---- triggers ----
  function clipScheduleAuto(trigger) {
    try { rsSaveReq(trigger); } catch {}
  }
  async function saveClip(trigger) {
    try { return await rsSaveReq(trigger); } catch { return null; }
  }
  const CN_STREAK_WINDOW = 12000, CN_CONFIRM_WINDOW = 15000;
  function cnStreakName(n) {
    try {
      if (n === 2) return 'DOUBLE KILL';
      if (n === 3) return 'TRIPLE KILL';
      if (n === 4) return 'QUAD KILL';
      return n + 'x RAMPAGE';
    } catch { return 'MULTI KILL'; }
  }
  function clipOnKill(killer, victim, isKill) {
    try {
      const stored = (cnMe.name || '').trim();
      const note = (decision) => { try { _clip.lastKill = { killer: String(killer || '?'), victim: String(victim || '?'), me: stored || '?', at: Date.now(), decision }; } catch {} };
      const rawMe = stored.toLowerCase();
      const rawK = String(killer || '').replace(/<[^>]*>/g, '').trim().toLowerCase();
      const rawV = String(victim || '').replace(/<[^>]*>/g, '').trim().toLowerCase();
      const meN = cnNormName(stored).trim().toLowerCase();
      const kN = cnNormName(killer || '').trim().toLowerCase();
      const vN = cnNormName(victim || '').trim().toLowerCase();
      const iDid = (xN, rawX) => !!((meN && xN && meN === xN) || (rawMe && rawX && rawMe === rawX));
      if (!isKill) {
        // Knock: stash mine so a later confirm (anyone finishes my victim)
        // still clips. Previously knocks were pure-ignored: knocking two
        // players and having a teammate finish both saved nothing.
        try {
          if (iDid(kN, rawK) && vN) {
            const ks = (_clip.knocks || []).filter((k) => Date.now() - k.at < CN_CONFIRM_WINDOW);
            ks.push({ victim: vN, at: Date.now() });
            _clip.knocks = ks.slice(-5);
            note('knock-stashed');
            return;
          }
        } catch {}
        note('knock-ignored'); return;
      }
      const cfg = clipCfg();
      if (!cfg.enabled) { note('clipping-off'); return; }
      const amKiller = iDid(kN, rawK) || !stored;
      const amVictim = iDid(vN, rawV);
      const now = Date.now();
      if (amKiller) {
        if (!cfg.autoKill) { note('auto-off'); return; }
        // Streak: 2+ of MY kills inside the window share one celebration.
        // Bypass for streaks skips the 8s debounce (a double 3s apart is
        // exactly what we want two clips of) and saves trigger 'streak'.
        let n = 1;
        try {
          const st = _clip.streak || { n: 0, firstAt: 0 };
          n = (now - st.firstAt < CN_STREAK_WINDOW) ? st.n + 1 : 1;
          _clip.streak = { n, firstAt: n === 1 ? now : st.firstAt };
        } catch {}
        const trig = n >= 2 ? 'streak' : 'kill';
        if (trig === 'kill' && now - _clip.killAt < 8000) { note('debounced'); return; }
        _clip.killAt = now;
        note('queued');
        if (n >= 2) { try { toast(`${cnStreakName(n)} - clip saving`, false); } catch {} }
        clipScheduleAuto(trig);
        return;
      }
      if (amVictim) {
        if (!cfg.autoDeath) { note('auto-off'); return; }
        if (now - _clip.deathAt < 8000) { note('debounced'); return; }
        _clip.deathAt = now;
        note('queued');
        clipScheduleAuto('death');
        return;
      }
      // Confirm: someone finished a player I knocked within the window.
      // Consume the stash entry so one knock confirms exactly one clip.
      try {
        const ks = (_clip.knocks || []).filter((k) => now - k.at < CN_CONFIRM_WINDOW);
        const hit = vN ? ks.findIndex((k) => k.victim === vN) : -1;
        _clip.knocks = hit >= 0 ? ks.slice(0, hit).concat(ks.slice(hit + 1)) : ks;
        if (hit >= 0) {
          if (!cfg.autoKill) { note('auto-off'); return; }
          if (now - _clip.killAt < 8000) { note('debounced'); return; }
          _clip.killAt = now;
          note('confirm-queued');
          try { toast('Confirmed your knock - clip saving', false); } catch {}
          clipScheduleAuto('kill');
          return;
        }
      } catch {}
      note('not-your-kill');
    } catch {}
  }
  function clipOnWin() {
    try {
      const cfg = clipCfg();
      if (!cfg.enabled || !cfg.autoWin) return;
      const now = Date.now();
      if (now - _clip.winAt < 30000) return;
      _clip.winAt = now;
      clipScheduleAuto('win');
    } catch {}
  }

  // ---- minimal UI: floating button + compact panel ----
  let cnOpen = false, cnFab = null, cnPanel = null, cnClips = [], cnClipsLoading = false;
  const _cnObjUrls = new Set();
  function cnTrackUrl(u) { try { if (u) _cnObjUrls.add(u); } catch {} return u; }
  function cnRevokeUrls() {
    try {
      _cnObjUrls.forEach((u) => { try { URL.revokeObjectURL(u); } catch {} });
      _cnObjUrls.clear();
    } catch {}
  }
  // Inter, loaded once from Google Fonts with display=swap: if it can't
  // load (offline, blocked), every stack falls back to Segoe UI / system-ui
  // with zero breakage. One stylesheet request, cached by the browser.
  function cnEnsureFont() {
    try {
      if (document.getElementById('cn-inter-font')) return;
      const head = document.head || document.documentElement;
      if (!head) return;
      try {
        const pre = document.createElement('link');
        pre.rel = 'preconnect';
        pre.href = 'https://fonts.gstatic.com';
        pre.crossOrigin = 'anonymous';
        head.appendChild(pre);
      } catch {}
      const link = document.createElement('link');
      link.id = 'cn-inter-font';
      link.rel = 'stylesheet';
      link.href = 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;700;800&display=swap';
      head.appendChild(link);
      // Warm the faces early so the watermark canvas picks up real Inter
      // instead of fallback on its first paint.
      try { if (document.fonts && document.fonts.load) { document.fonts.load('800 20px Inter'); document.fonts.load('700 14px Inter'); } } catch {}
    } catch {}
  }
  function cnEnsureStyle() {
    try {
      const ex = document.getElementById('cn-style');
      if (ex) {
        // Rebuild once per version so new rules (timeline, editor) actually
        // apply for updaters stuck with the old cached stylesheet.
        try { if (ex.dataset && ex.dataset.v === CN_VERSION) return; } catch {}
        try { ex.remove(); } catch {}
      }
      const st = document.createElement('style');
      st.id = 'cn-style';
      try { st.dataset.v = CN_VERSION; } catch {}
      st.textContent = '#cn-fab{position:fixed;right:14px;bottom:14px;width:34px;height:34px;border-radius:50%;border:2px solid rgba(255,255,255,.25);background:#111;color:#fff;font-size:15px;cursor:pointer;z-index:2147483645;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 16px rgba(0,0,0,.5);font-family:Inter,"Segoe UI",system-ui,-apple-system,sans-serif;transition:transform 120ms ease-out}'
        + '#cn-fab:hover{transform:scale(1.1)}'
        + '#cn-panel{position:fixed;right:14px;bottom:58px;width:400px;max-width:94vw;max-height:82vh;display:none;flex-direction:column;background:#111;border:1px solid rgba(255,255,255,.12);border-radius:14px;z-index:2147483646;overflow:hidden;font-family:Inter,"Segoe UI",system-ui,-apple-system,sans-serif;box-shadow:0 12px 48px rgba(0,0,0,.6);opacity:0;transform:scale(.94);transform-origin:bottom right;transition:opacity 160ms ease-out,transform 160ms ease-out}'
        + '#cn-panel.cn-show{opacity:1;transform:scale(1)}'
        + '#cn-body{overflow:auto;padding:12px 14px}'
        + '.cn-rowbtn{flex:1;min-width:120px;background:#fff;color:#000;border:0;border-radius:8px;padding:8px 0;font-size:12px;font-weight:800;cursor:pointer}'
        + '.cn-ghost{background:none;border:1px solid rgba(255,255,255,.12);color:#9ca3af;border-radius:6px;padding:6px 12px;font-size:11px;cursor:pointer}'
        + '.cn-card{background:#171717;border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:10px 12px;margin-bottom:8px}'
        + '.cn-set{background:#171717;border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:8px}'
        + '.cn-sel{background:#0f0f0f;color:#e5e7eb;border:1px solid rgba(255,255,255,.12);border-radius:6px;font-size:11px;padding:4px 6px}'
        + '#cn-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:2147483646;display:none;opacity:0;transition:opacity 160ms ease-out}'
        + '#cn-backdrop.cn-show{opacity:1}'
        + '.cn-modal{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%) scale(.96);width:min(720px,96vw);aspect-ratio:4/3;max-height:88vh;display:none;flex-direction:column;background:#111;border:1px solid rgba(255,255,255,.12);border-radius:16px;z-index:2147483647;overflow:hidden;font-family:Inter,"Segoe UI",system-ui,-apple-system,sans-serif;box-shadow:0 12px 48px rgba(0,0,0,.6);opacity:0;transition:opacity 160ms ease-out,transform 160ms ease-out}'
        + '#cn-edit{width:min(780px,96vw);aspect-ratio:auto;max-height:92vh}'
        + '#cn-set{width:min(560px,94vw);aspect-ratio:auto;max-height:90vh}'
        + '.cn-tl{position:relative;height:84px;background:#0a0a0a;border:1px solid rgba(255,255,255,.1);border-radius:8px;overflow:hidden;touch-action:none;cursor:crosshair}'
        + '#cn-tl-film{position:absolute;inset:0;display:flex;gap:1px;opacity:.85;pointer-events:none}'
        + '#cn-tl-sel{position:absolute;top:0;bottom:0;background:rgb(34 211 238 / .18);border-left:2px solid #22d3ee;border-right:2px solid #22d3ee;pointer-events:none}'
        + '.cn-tl-h{position:absolute;top:0;bottom:0;width:16px;margin-left:-8px;cursor:ew-resize;touch-action:none;z-index:2}'
        + '.cn-tl-h:after{content:"";position:absolute;left:6px;top:0;bottom:0;width:4px;border-radius:2px;background:#22d3ee}'
        + '#cn-tl-ph{position:absolute;top:0;bottom:0;width:2px;background:#fff;pointer-events:none;box-shadow:0 0 4px #000;z-index:1}'
        + '.cn-modal.cn-show{opacity:1;transform:translate(-50%,-50%) scale(1)}'
        + '.cn-mbody{overflow:auto;padding:12px 14px;flex:1;min-height:0}'
        + '.cn-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}'
        + '.cn-thumb{width:100%;aspect-ratio:4/3;background:#000;border-radius:6px;object-fit:cover}'
        + '.cn-vert .cn-thumb{aspect-ratio:9/16}'
        + '.cn-search{background:#0f0f0f;color:#e5e7eb;border:1px solid rgba(255,255,255,.12);border-radius:8px;font-size:12px;padding:7px 10px;width:100%;box-sizing:border-box;outline:none}'
        + '.cn-star{background:none;border:0;color:#FFD400;font-size:15px;cursor:pointer;padding:2px 4px}'
        + '.cn-keybtn{background:#0f0f0f;color:#e5e7eb;border:1px solid rgba(255,255,255,.12);border-radius:6px;font-size:11px;padding:4px 10px;cursor:pointer;min-width:64px}'
        + '.cn-meter{height:14px;background:#0a0a0a;border:1px solid rgba(255,255,255,.08);border-radius:4px;overflow:hidden;position:relative}'
        + '.cn-meter-fill{position:absolute;left:0;top:0;bottom:0;width:0%;background:linear-gradient(90deg,#22d3ee,#10b981);transition:width 60ms linear}'
        + '.cn-vol-row{display:flex;flex-direction:column;gap:3px}'
        + '.cn-vol-row input[type=range]{width:100%;accent-color:#fff;cursor:pointer}';
      (document.head || document.documentElement).appendChild(st);
    } catch {}
  }
  // ---- views: main (slim) / clips list / preview / onboarding ----
  let _cnAnimT = null;
  let cnView = null, cnPrevId = null;
  let cnLibQ = '', cnLibSort = null, cnLibMode = null, cnKeyCap = null;
  function cnEls() {
    try {
      return {
        bd: document.getElementById('cn-backdrop'),
        main: document.getElementById('cn-panel'),
        lib: document.getElementById('cn-lib'),
        prev: document.getElementById('cn-prev'),
        edit: document.getElementById('cn-edit'),
        set: document.getElementById('cn-set'),
        onb: document.getElementById('cn-onb'),
      };
    } catch { return {}; }
  }
  function cnShow(view) {
    try {
      cnEnsureFab();
      const e = cnEls();
      try { if (_cnAnimT) clearTimeout(_cnAnimT); } catch {}
      cnView = view;
      cnOpen = !!view;
      const modal = view === 'lib' || view === 'prev' || view === 'edit' || view === 'set' || view === 'onb';
      try {
        if (e.bd) {
          if (modal) {
            e.bd.style.display = 'block';
            requestAnimationFrame(() => { requestAnimationFrame(() => { try { e.bd.classList.add('cn-show'); } catch {} }); });
          } else { try { e.bd.classList.remove('cn-show'); } catch {} e.bd.style.display = 'none'; }
        }
      } catch {}
      const pairs = [[e.main, 'main'], [e.lib, 'lib'], [e.prev, 'prev'], [e.edit, 'edit'], [e.set, 'set'], [e.onb, 'onb']];
      for (const [el, name] of pairs) {
        try {
          if (!el) continue;
          if (name === view) {
            el.style.display = 'flex';
            requestAnimationFrame(() => { requestAnimationFrame(() => { try { el.classList.add('cn-show'); } catch {} }); });
          } else {
            try { el.classList.remove('cn-show'); } catch {}
            el.style.display = 'none';
          }
        } catch {}
      }
      try { if (view !== 'main' && view !== 'set') cnStopMeters(); } catch {}
      try { if (view === 'prev' || view === 'edit') cnThumbStop(); } catch {}
      try { if (view !== 'edit') cnEditLeave(); } catch {}
      try { cnRenderCurrent(); } catch {}
      try { if (view === 'lib') fetchCnClips(); } catch {}
    } catch {}
  }
  function cnHideAll() {
    // Animated close: drop cn-show so panels fade + zoom out via CSS, then
    // display:none after the transition. Guarded so a reopen within the
    // window cancels the hide instead of blanking the fresh view.
    try {
      const e = cnEls();
      cnView = null; cnOpen = false; cnPrevId = null;
      try { if (typeof cnUntrapRelease === 'function') cnUntrapRelease(); } catch {}
      try { if (e.bd) e.bd.classList.remove('cn-show'); } catch {}
      for (const el of [e.main, e.lib, e.prev, e.edit, e.set, e.onb]) {
        try { if (!el) continue; el.classList.remove('cn-show'); } catch {}
      }
      try { if (_cnAnimT) clearTimeout(_cnAnimT); } catch {}
      try {
        _cnAnimT = setTimeout(() => {
          try {
            _cnAnimT = null;
            if (cnView !== null) return; // reopened meanwhile: leave it alone
            const x = cnEls();
            try { if (x.bd) x.bd.style.display = 'none'; } catch {}
            for (const el of [x.main, x.lib, x.prev, x.edit, x.set, x.onb]) {
              try { if (!el) continue; el.style.display = 'none'; } catch {}
            }
          } catch {}
        }, 180);
      } catch {}
      try { cnStopMeters(); } catch {}
      try { cnEditLeave(); } catch {}
      try { cnThumbStop(); } catch {}
      try { cnRevokeUrls(); } catch {}
    } catch {}
  }
  function cnBack() {
    try {
      if (cnView === 'prev') cnShow('lib');
      else if (cnView === 'edit') cnShow('prev');
      else if (cnView === 'set') cnShow('main');
      else cnHideAll();
    } catch {}
  }
  function cnRenderCurrent() {
    try {
      if (cnView === 'main') renderMain();
      else if (cnView === 'lib') renderLib();
      else if (cnView === 'prev') renderPrev();
      else if (cnView === 'edit') renderEdit();
      else if (cnView === 'set') renderSet();
      else if (cnView === 'onb') renderOnb();
      try { cnRefreshFab(); } catch {}
    } catch {}
  }
  function cnToggle(force) {
    try {
      const show = typeof force === 'boolean' ? force : cnView !== 'main';
      if (show) cnShow('main');
      else cnHideAll();
    } catch {}
  }
  function cnApplyFab() {
    try { if (cnFab) cnFab.style.display = clipCfg().fab ? 'flex' : 'none'; } catch {}
  }
  function cnApplyFabPos() {
    try {
      if (!cnFab) return;
      const x = Number(GMGet('cn_fab_x', NaN)), y = Number(GMGet('cn_fab_y', NaN));
      if (isNaN(x) || isNaN(y)) {
        cnFab.style.left = ''; cnFab.style.top = '';
        cnFab.style.right = '14px'; cnFab.style.bottom = '14px';
        return;
      }
      const w = window.innerWidth || 1280, h = window.innerHeight || 720;
      const cx = Math.max(0, Math.min(w - 40, x)), cy = Math.max(0, Math.min(h - 40, y));
      cnFab.style.right = 'auto'; cnFab.style.bottom = 'auto';
      cnFab.style.left = cx + 'px'; cnFab.style.top = cy + 'px';
    } catch {}
  }
  function cnMakeFabDraggable() {
    try {
      if (!cnFab || cnFab.__cnDrag) return;
      cnFab.__cnDrag = true;
      let sx = 0, sy = 0, ox = 0, oy = 0, moved = false, pid = null;
      const down = (e) => {
        try {
          if (e.button !== undefined && e.button !== 0) return;
          moved = false; pid = e.pointerId;
          const r = cnFab.getBoundingClientRect();
          sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top;
          try { cnFab.setPointerCapture(e.pointerId); } catch {}
          e.preventDefault();
        } catch {}
      };
      const move = (e) => {
        try {
          if (pid === null || e.pointerId !== pid) return;
          const dx = e.clientX - sx, dy = e.clientY - sy;
          if (!moved && Math.hypot(dx, dy) < 5) return;
          moved = true;
          const w = window.innerWidth || 1280, h = window.innerHeight || 720;
          const nx = Math.max(0, Math.min(w - 40, ox + dx)), ny = Math.max(0, Math.min(h - 40, oy + dy));
          cnFab.style.right = 'auto'; cnFab.style.bottom = 'auto';
          cnFab.style.left = nx + 'px'; cnFab.style.top = ny + 'px';
        } catch {}
      };
      const up = (e) => {
        try {
          if (pid === null || (e.pointerId !== undefined && e.pointerId !== pid)) return;
          pid = null;
          if (!moved) { try { e.stopPropagation(); } catch {} cnToggle(); return; }
          try {
            const r = cnFab.getBoundingClientRect();
            GMSet('cn_fab_x', Math.round(r.left)); GMSet('cn_fab_y', Math.round(r.top));
          } catch {}
        } catch {}
      };
      cnFab.addEventListener('pointerdown', down);
      cnFab.addEventListener('pointermove', move);
      cnFab.addEventListener('pointerup', up);
      cnFab.addEventListener('pointercancel', () => { pid = null; });
      cnFab.addEventListener('dblclick', (e) => {
        try { e.stopPropagation(); } catch {}
        try { GMSet('cn_fab_x', NaN); GMSet('cn_fab_y', NaN); } catch {}
        cnApplyFabPos();
      });
    } catch {}
  }
  function cnEnsureModal(id) {
    try {
      let el = document.getElementById(id);
      if (!el) {
        el = document.createElement('div');
        el.id = id;
        el.className = 'cn-modal';
        el.style.display = 'none';
        ['mousedown', 'click', 'keydown'].forEach(ev => el.addEventListener(ev, e => e.stopPropagation()));
        el.addEventListener('wheel', e => e.stopPropagation(), { passive: false });
        (document.body || document.documentElement).appendChild(el);
      }
      return el;
    } catch { return null; }
  }
  function cnEnsureFabWhenReady() {
    try {
      if (document.body) { try { cnEnsureFab(); } catch {} return; }
      let n = 0;
      const t = setInterval(() => {
        try {
          n++;
          if (document.body || n > 20) {
            try { clearInterval(t); } catch {}
            try { cnEnsureFab(); } catch {}
            try { if (GMGet('cn_onboarded', false) !== true) cnShow('onb'); } catch {}
          }
        } catch {}
      }, 500);
    } catch { try { cnEnsureFab(); } catch {} }
  }
  function cnEnsureFab() {
    try {
      cnEnsureStyle();
      try { cnEnsureFont(); } catch {}
      if (!cnFab) {
        cnFab = document.createElement('button');
        cnFab.id = 'cn-fab';
        cnFab.title = 'ClippedNow.gg - drag to move · click to open · hide in Settings';
        cnFab.textContent = '●';
        try { cnFab.style.touchAction = 'none'; cnFab.style.userSelect = 'none'; } catch {}
        (document.body || document.documentElement).appendChild(cnFab);
        cnMakeFabDraggable();
      }
      if (!cnPanel) {
        cnPanel = document.createElement('div');
        cnPanel.id = 'cn-panel';
        ['mousedown', 'click', 'keydown'].forEach(ev => cnPanel.addEventListener(ev, e => e.stopPropagation()));
        cnPanel.addEventListener('wheel', e => e.stopPropagation(), { passive: false });
        (document.body || document.documentElement).appendChild(cnPanel);
      }
      try {
        if (!document.getElementById('cn-backdrop')) {
          const bd = document.createElement('div');
          bd.id = 'cn-backdrop';
          bd.onclick = () => { try { cnBack(); } catch {} };
          (document.body || document.documentElement).appendChild(bd);
        }
      } catch {}
      try { cnEnsureModal('cn-lib'); } catch {}
      try { cnEnsureModal('cn-prev'); } catch {}
      try { cnEnsureModal('cn-edit'); } catch {}
      try { cnEnsureModal('cn-set'); } catch {}
      try { cnEnsureModal('cn-onb'); } catch {}
      try {
        const cfg = clipCfg();
        if (cnFab) cnFab.title = `ClippedNow.gg - drag to move · click to open (${cfg.keyPanel}) · quick clip (${cfg.keyClip}) · hide in Settings`;
      } catch {}
      cnApplyFabPos();
      cnRefreshFab();
      cnApplyFab();
    } catch {}
  }
  function cnRefreshFab() {
    try {
      if (!cnFab) return;
      cnFab.style.color = rsActive() || _clip.pendingTimer ? '#ef4444' : '#6b7280';
    } catch {}
  }
  function fetchCnClips() {
    if (!cnOpen) return;
    cnClipsLoading = true; cnRenderCurrent();
    clipList().then((rows) => {
      cnClipsLoading = false; cnClips = rows || [];
      if (cnOpen) cnRenderCurrent();
    }).catch(() => { cnClipsLoading = false; cnClips = []; if (cnOpen) cnRenderCurrent(); });
  }
  // ---- shared view helpers ----
  function cnSel(id, val, opts) { try { return `<select id="${id}" class="cn-sel">` + opts.map(([v, l]) => `<option value="${v}"${String(v) === String(val) ? ' selected' : ''}>${l}</option>`).join('') + `</select>`; } catch { return ''; } }
  function cnChk(id, on, label) { try { return `<label style="display:flex;align-items:center;gap:6px;font-size:11px;color:#e5e7eb;cursor:pointer"><input type="checkbox" id="${id}"${on ? ' checked' : ''} style="accent-color:#fff"> ${label}</label>`; } catch { return ''; } }
  function cnBadge(t) { try { return { kill: '#ef4444', death: '#f59e0b', win: '#10b981', manual: '#6b7280', trim: '#22d3ee', streak: '#a855f7' }[t] || '#6b7280'; } catch { return '#6b7280'; } }
  function cnWhen(ts) { try { return new Date(ts || Date.now()).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }); } catch { return ''; } }
  function cnKB(n) { try { return ((Number(n) || 0) / 1024).toFixed(0) + ' KB'; } catch { return ''; } }
  function cnMimeShort() {
    try {
      const m = (_clip.rec && _clip.rec.mimeType) || _clip.mime || '';
      let s = '?';
      if (m.includes('avc1') || m.includes('mp4')) s = 'mp4/h264';
      else if (m.includes('vp8')) s = 'vp8';
      else if (m.includes('vp9')) s = 'vp9';
      else s = m.split(';')[0] || '?';
      if (m.includes('mp4a')) s += '+aac'; else if (m.includes('opus')) s += '+opus';
      return s;
    } catch { return '?'; }
  }
  function cnAudioDiag() {
    try {
      let els = -1;
      try { els = document.querySelectorAll ? document.querySelectorAll('audio').length : -1; } catch {}
      let lv = '';
      try { lv = clipAudioLevels(); } catch {}
      return `ctxs=${_clipCtxs.size} els=${els}${lv ? ' ' + lv : ''}`;
    } catch { return ''; }
  }
  function cnRecState(cfg) {
    try {
      const segAge = Math.max(0, Math.round((Date.now() - (_clip.segStartedAt || Date.now())) / 1000));
      let micBit = '';
      try { if (cfg.mic) micBit = clipMicTrack() ? '+mic' : '(no-mic)'; } catch {}
      const audioBit = _clip.withAudio ? ` · +audio${micBit}` : ((cfg.audio || cfg.mic) ? ` · no-audio (${cnAudioDiag()})` : '');
      const wmBit = _clip.wmActive ? ' · WM' : (cfg.watermark ? ' · no-WM' : '');
      const layBit = cfg.layout === 'vertical' ? ' · 9:16' : '';
      if (rsState === RS_SAVING) return `Saving ${(_clip.pendingTrigger || 'clip')} clip…`;
      if (rsState === RS_RECORDING) {
        if (rsDataReady()) {
          let srcBit = '';
          try { const sf = clipTrackFps(); if (sf > 0) srcBit = ` · src ${sf}fps`; } catch {}
          let paintBit = '';
          try { if (_clip.wmActive && _cnComp.pfps > 0) paintBit = ` · paint ${_cnComp.pfps}fps`; } catch {}
          let gameBit = '';
          try { if (_cnFpsAvg > 0) gameBit = ` · game ${Math.round(_cnFpsAvg)}fps`; } catch {}
          return `REC ${segAge}/${cfg.len}s window · ${cnFpsLabel(cfg.fps)}fps${srcBit}${paintBit}${gameBit} ${cnMimeShort()}${audioBit}${wmBit}${layBit}`;
        }
        return 'buffering…';
      }
      return (_clip.lastErr || 'starting…');
    } catch { return 'starting…'; }
  }
  function cnLibRows() {
    try {
      const sort = cnLibSort || GMGet('cn_lib_sort', 'new') || 'new';
      const q = String(cnLibQ || '').trim().toLowerCase();
      let rows = (cnClips || []).slice();
      if (q) {
        rows = rows.filter((r) => {
          try {
            if ((r.trigger || '').toLowerCase().includes(q)) return true;
            if ((r.killer || '').toLowerCase().includes(q)) return true;
            if ((r.victim || '').toLowerCase().includes(q)) return true;
            if (cnGmLabel(r.mode).toLowerCase().includes(q)) return true;
            if (cnWhen(r.ts).toLowerCase().includes(q)) return true;
            if ((q === 'fav' || q === 'favorite' || q === '★') && r.fav) return true;
            if ((q === '9:16' || q === 'vertical') && r.layout === 'vertical') return true;
            return false;
          } catch { return false; }
        });
      }
      const by = {
        new: (a, b) => (b.ts || 0) - (a.ts || 0),
        old: (a, b) => (a.ts || 0) - (b.ts || 0),
        big: (a, b) => (b.size || 0) - (a.size || 0),
        small: (a, b) => (a.size || 0) - (b.size || 0),
        fav: (a, b) => ((b.fav ? 1 : 0) - (a.fav ? 1 : 0)) || ((b.ts || 0) - (a.ts || 0)),
      }[sort] || ((a, b) => (b.ts || 0) - (a.ts || 0));
      rows.sort(by);
      return rows;
    } catch { return (cnClips || []).slice(); }
  }
  function cnDownload(r) {
    try {
      if (!r || !r.blob) return;
      const u = cnTrackUrl(URL.createObjectURL(r.blob));
      const a = document.createElement('a');
      a.href = u;
      a.download = `clippednow-${r.trigger || 'clip'}-${r.ts || Date.now()}.${clipExt(r.mime || (r.blob && r.blob.type))}`;
      (document.body || document.documentElement).appendChild(a); a.click(); a.remove();
      try { toast(`Downloading ${(r.trigger || 'clip')} clip`, false); } catch {}
      try { cnSfx('ok'); } catch {} // 'ok' chime on the real output (never the record tap): hearable mid-game
      setTimeout(() => { try { URL.revokeObjectURL(u); _cnObjUrls.delete(u); } catch {} }, 30000);
    } catch {}
  }
  function cnBindSettings(prefix) {
    try {
      const g = (id) => document.getElementById(`${prefix}-${id}`);
      const on = (id, fn) => { const el = g(id); if (el) el.onchange = fn; };
      on('clip-enabled', (e) => clipSet('cn_clip_enabled', e.target.checked));
      on('clip-autokill', (e) => clipSet('cn_clip_autokill', e.target.checked));
      on('clip-autodeath', (e) => clipSet('cn_clip_autodeath', e.target.checked));
      on('clip-autowin', (e) => clipSet('cn_clip_autowin', e.target.checked));
      on('clip-audio', (e) => clipSet('cn_clip_audio', e.target.checked));
      on('clip-mic', (e) => clipSet('cn_clip_mic', e.target.checked));
      on('clip-watermark', (e) => clipSet('cn_clip_watermark', e.target.checked));
      on('fab-visible', (e) => clipSet('cn_fab_visible', e.target.checked));
      on('sounds', (e) => clipSet('cn_sounds', e.target.checked));
      on('clip-len', (e) => clipSet('cn_clip_len', Number(e.target.value) || 30));
      on('clip-fps', (e) => { const v = e.target.value; clipSet('cn_clip_fps', v === 'unlimited' ? 'unlimited' : (Number(v) || 15)); });
      on('clip-bitrate', (e) => clipSet('cn_clip_bitrate', Number(e.target.value) || 1500000));
      on('clip-after', (e) => { const v = Number(e.target.value); clipSet('cn_clip_after', isNaN(v) ? 5 : Math.max(0, Math.min(10, v))); });
      on('clip-layout', (e) => clipSet('cn_clip_layout', e.target.value === 'vertical' ? 'vertical' : 'landscape'));
      on('clip-format', (e) => clipSet('cn_clip_format', e.target.value === 'webm' ? 'webm' : 'mp4'));
      on('clip-preset', (e) => { try { cnApplyPreset(e.target.value); } catch {} });
      const wm = (id, key) => { const el = g(id); if (el) el.onchange = (e) => { try { GMSet(key, e.target.checked); } catch {} try { clipCfgApplyKey(key, e.target.checked); } catch {} try { _cnComp.labelKey = ''; } catch {} try { if (typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {} }; };
      wm('wm-name', 'cn_wm_name'); wm('wm-mode', 'cn_wm_mode'); wm('wm-brand', 'cn_wm_brand');
      on('wm-pos', (e) => { const v = String(e.target.value || 'br'); const vv = ['tl', 'tc', 'tr', 'bl', 'bc', 'br'].includes(v) ? v : 'br'; try { GMSet('cn_wm_pos', vv); } catch {} try { clipCfgApplyKey('cn_wm_pos', vv); } catch {} try { _cnComp.labelKey = ''; } catch {} try { if (typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {} });
    } catch {}
  }
  // ---- hardware auto-detect: recommend (never force) a preset ----
  // Cores via hardwareConcurrency, RAM via deviceMemory (Chrome caps it at
  // 8, so 8 means "8 or more"), GPU via the WebGL debug renderer string.
  // SwiftShader/llvmpipe = no hardware acceleration = software encode, so
  // lowend is the only sane pick and we say why. Detection is cached per
  // page load (one throwaway canvas) and the recommendation is one click -
  // user settings are never overwritten silently.
  let _cnHw = null;
  function cnHwDetect() {
    try {
      if (_cnHw) return _cnHw;
      const hw = { cores: 0, ram: 0, gpu: '', sw: false };
      try { hw.cores = Number(navigator.hardwareConcurrency) || 0; } catch {}
      try { hw.ram = Number(navigator.deviceMemory) || 0; } catch {}
      try {
        const cv = document.createElement('canvas');
        const gl = cv.getContext('webgl2') || cv.getContext('webgl');
        if (!gl) { hw.sw = true; }
        else {
          let dbg = null;
          try { dbg = gl.getExtension('WEBGL_debug_renderer_info'); } catch {}
          let r = '';
          try { r = dbg ? String(gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) || '') : String(gl.getParameter(gl.RENDERER) || ''); } catch {}
          hw.gpu = r.slice(0, 96);
          if (!r || /swiftshader|llvmpipe|software|basic render/i.test(r)) hw.sw = true;
        }
      } catch { hw.sw = true; }
      _cnHw = hw;
      return hw;
    } catch { return { cores: 0, ram: 0, gpu: '', sw: false }; }
  }
  function cnHwShortGpu(g) {
    try {
      const m = String(g || '').match(/(GeForce[^,(]*|Radeon[^,(]*|Arc[^,(]*|Iris[^,(]*|UHD Graphics[^,(]*|Apple M\d[^,(]*)/i);
      if (m) return m[1].trim().slice(0, 40);
      return String(g || '').slice(0, 40) || 'unknown GPU';
    } catch { return 'unknown GPU'; }
  }
  function cnHwTier() {
    try {
      const hw = cnHwDetect();
      const g = hw.gpu || '';
      const m = (re) => { try { return re.test(g); } catch { return false; } };
      let key = 'medium', why = 'balanced default';
      if (hw.sw) { key = 'lowend'; why = 'no GPU acceleration (software rendering)'; }
      else if (m(/rtx\s*(50|40)/i)) { key = 'beast'; why = 'RTX 40/50-class GPU'; }
      else if (m(/rx\s*(9070|9060|7900|7800|7700)/i)) { key = 'beast'; why = 'RX 7000/9000-class GPU'; }
      else if (m(/rtx\s*30/i) || m(/rx\s*6[789]00/i) || m(/arc\s*a7/i)) { key = (hw.cores >= 10) ? 'beast' : 'max'; why = 'strong GPU' + (hw.cores >= 10 ? ' + 10+ CPU threads' : ''); }
      else if (m(/rtx\s*20/i) || m(/gtx\s*16/i) || m(/rx\s*6[046]00/i) || m(/arc\s*a[35]/i) || m(/apple\s*m[1-4]/i)) { key = 'max'; why = 'mid-range GPU'; }
      else if (m(/nvidia/i)) { key = 'max'; why = 'NVIDIA GPU'; }
      else if (m(/radeon|amd/i)) { key = 'medium'; why = 'older AMD GPU'; }
      else if (m(/intel/i)) { key = /iris|arc/i.test(g) ? 'max' : 'medium'; why = 'integrated Intel GPU'; }
      if (!hw.sw) {
        if (hw.cores > 0 && hw.cores <= 4 && key !== 'lowend') { key = 'medium'; why += ' · ≤4 CPU threads'; }
        if (hw.ram > 0 && hw.ram <= 2) { key = 'lowend'; why += ' · ≤2GB RAM'; }
      }
      try { if (!CN_PRESETS.find(([k]) => k === key)) key = 'medium'; } catch {}
      return { key, why, hw };
    } catch { return { key: 'medium', why: 'detection failed', hw: {} }; }
  }
  function cnHwPresetLabel(key) {
    try {
      const p = CN_PRESETS.find(([k]) => k === key);
      return p ? p[1] : key;
    } catch { return key; }
  }
  function cnHwRowHtml() {
    try {
      const t = cnHwTier();
      const hw = t.hw || {};
      const bits = [];
      if (hw.cores) bits.push(hw.cores + '-thread CPU');
      if (hw.ram) bits.push((hw.ram >= 8 ? '8+GB' : hw.ram + 'GB') + ' RAM');
      bits.push(cnHwShortGpu(hw.gpu));
      let cur = 'custom';
      try { cur = cnPresetOf(clipCfg().fps, clipCfg().bitrate); } catch {}
      const already = cur === t.key;
      const warn = t.hw && t.hw.sw ? `<div style="font-size:10px;color:#eab308">Software rendering detected - enable hardware acceleration in the browser for smooth recording.</div>` : '';
      return `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>This PC</span><span style="color:#e5e7eb">${esc(bits.join(' · '))}</span><span>→ recommended: <b style="color:#fff">${esc(cnHwPresetLabel(t.key))}</b> <span style="color:#6b7280">(${esc(t.why)})</span></span><button id="cn-hw-apply" class="cn-keybtn">${already ? '✓ Using it' : 'Apply'}</button></div>`
        + warn;
    } catch { return ''; }
  }
  function cnHwBind() {
    try {
      const b = document.getElementById('cn-hw-apply');
      if (b) b.onclick = (e) => {
        try { if (e && e.stopPropagation) e.stopPropagation(); } catch {}
        try {
          const t = cnHwTier();
          cnApplyPreset(t.key);
          try { toast(`Applied ${cnHwPresetLabel(t.key)} for this PC`, false); } catch {}
        } catch {}
      };
    } catch {}
  }
  // ---- capture presets: named fps+bitrate bundles ----
  // Low-end PC (light encoder load), Medium (balanced default), Discord
  // clips (smooth 30fps under Discord's free upload limits), Max quality,
  // Beast (120fps + 30Mbps for i9 + 4080/5070-class rigs: Chrome encodes
  // H264/VP9 on the GPU when hardware acceleration is on, so a strong GPU
  // directly buys headroom here), Source (uncapped fps + ~20Mbps: as close
  // to the game feed as a browser encoder gets - big files).
  // Applying a preset writes fps+bitrate and reboots the recorder once.
  const CN_PRESETS = [
    ['lowend', 'Low-end PC', 10, 1000000],
    ['medium', 'Medium', 15, 2500000],
    ['discord', 'Discord clips', 30, 2500000],
    ['max', 'Max quality', 60, 8000000],
    ['beast', 'Beast mode (high-end)', 120, 30000000],
    ['source', 'Source quality', 'unlimited', 20000000],
  ];
  function cnFpsLabel(fps) {
    try { return fps === 'unlimited' ? 'max' : String(Number(fps) || CN_DEF.fps); } catch { return '15'; }
  }
  function cnPresetOf(fps, bitrate) {
    try {
      for (const [k, , f, b] of CN_PRESETS) { if (String(fps) === String(f) && Number(bitrate) === b) return k; }
    } catch {}
    return 'custom';
  }
  function cnApplyPreset(key) {
    try {
      const p = CN_PRESETS.find(([k]) => k === key);
      if (!p) return;
      try { GMSet('cn_clip_fps', p[2]); } catch {}
      try { GMSet('cn_clip_bitrate', p[3]); } catch {}
      try { clipCfgApplyKey('cn_clip_fps', p[2]); } catch {}
      try { clipCfgApplyKey('cn_clip_bitrate', p[3]); } catch {}
      try { clipEnsureMic(); } catch {}
      try { rsReboot('preset'); } catch {}
      try { logInfo(`preset -> ${p[1]} (${cnFpsLabel(p[2])}fps, ${(p[3] / 1000000).toFixed(1)}Mbps)`); } catch {}
      try { if (typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {}
    } catch {}
  }
  function cnSettingsHtml(cfg, prefix) {
    try {
      return `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:10px 0 4px">SETTINGS</div>`
        + `<div class="cn-set">`
        + `<div style="display:flex;gap:12px;flex-wrap:wrap">${cnChk(prefix + '-clip-enabled', cfg.enabled, 'Enable')}${cnChk(prefix + '-clip-autokill', cfg.autoKill, 'My kills')}${cnChk(prefix + '-clip-autodeath', cfg.autoDeath, 'My deaths')}${cnChk(prefix + '-clip-autowin', cfg.autoWin, 'Wins')}${cnChk(prefix + '-clip-audio', cfg.audio, 'Game audio')}${cnChk(prefix + '-clip-mic', cfg.mic, 'Mic')}${cnChk(prefix + '-clip-watermark', cfg.watermark, 'Watermark')}${cnChk(prefix + '-fab-visible', cfg.fab, 'Show button')}${cnChk(prefix + '-sounds', cfg.sounds, 'Sounds')}</div>`
        + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Buffer</span>${cnSel(prefix + '-clip-len', cfg.len, [[8, '8s'], [15, '15s'], [30, '30s ✓'], [45, '45s'], [60, '60s']])}<span>FPS</span>${cnSel(prefix + '-clip-fps', cfg.fps, [[10, '10'], [15, '15 ✓'], [30, '30'], [60, '60'], [120, '120'], ['unlimited', 'Unlimited']])}</div>`
        + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Quality</span>${cnSel(prefix + '-clip-bitrate', cfg.bitrate, [[1500000, 'Low ✓'], [2500000, 'Medium'], [5000000, 'High'], [8000000, 'Ultra'], [20000000, 'Source'], [30000000, 'Pro']])}<span>Aftermath</span>${cnSel(prefix + '-clip-after', cfg.after, [[0, '0s'], [3, '3s'], [5, '5s ✓'], [8, '8s']])}</div>`
        + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Layout</span>${cnSel(prefix + '-clip-layout', cfg.layout, [['landscape', 'Landscape 16:9'], ['vertical', 'Vertical 9:16']])}<span>Format</span>${cnSel(prefix + '-clip-format', cfg.format || 'mp4', [['mp4', 'MP4 (smooth)'], ['webm', 'WebM (seeking)']])}<span>Preset</span>${cnSel(prefix + '-clip-preset', cnPresetOf(cfg.fps, cfg.bitrate), [['custom', 'Custom'], ...CN_PRESETS.map(([k, l]) => [k, l])])}</div>`
        + (cfg.format === 'webm' ? '' : `<div style="font-size:10px;color:#6b7280">MP4 plays smoothest but seeking can show blocky frames - switch to WebM if you scrub a lot.</div>`)
        + cnHwRowHtml()
        + `<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:11px;color:#e5e7eb"><span style="color:#9ca3af">Watermark</span>${cnChk(prefix + '-wm-name', cfg.wmName, 'Name')}${cnChk(prefix + '-wm-mode', cfg.wmMode, 'Mode')}${cnChk(prefix + '-wm-brand', cfg.wmBrand, 'Brand')}</div>`
        + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Watermark spot</span>${cnSel(prefix + '-wm-pos', cfg.wmPos, [['tl', 'Top left'], ['tc', 'Top center'], ['tr', 'Top right'], ['bl', 'Bottom left'], ['bc', 'Bottom center'], ['br', 'Bottom right']])}</div>`
        + ((cfg.fps === 'unlimited' || cfg.fps >= 60 || (cfg.fps >= 30 && cfg.bitrate >= 2500000)) ? `<div style="font-size:11px;color:#eab308">High FPS + quality can drop game FPS - try 15 FPS / Low if the game stutters. Unlimited captures at the game's own frame rate.</div>` : '')
        + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Clip key</span><button id="${prefix}-key-clip" class="cn-keybtn">${esc(cfg.keyClip)}</button><span>Panel key</span><button id="${prefix}-key-panel" class="cn-keybtn">${esc(cfg.keyPanel)}</button><span>Clips key</span><button id="${prefix}-key-lib" class="cn-keybtn">${esc(cfg.keyLib)}</button></div>`
        + `</div>`;
    } catch { return ''; }
  }
  function cnBindKeys(prefix) {
    try {
      const c = document.getElementById(`${prefix}-key-clip`), p = document.getElementById(`${prefix}-key-panel`), l = document.getElementById(`${prefix}-key-lib`);
      if (c) c.onclick = (e) => { try { e.stopPropagation(); } catch {} cnKeyCap = 'clip'; c.textContent = 'press key…'; };
      if (p) p.onclick = (e) => { try { e.stopPropagation(); } catch {} cnKeyCap = 'panel'; p.textContent = 'press key…'; };
      if (l) l.onclick = (e) => { try { e.stopPropagation(); } catch {} cnKeyCap = 'lib'; l.textContent = 'press key…'; };
    } catch {}
  }
  // ---- audio mix section: waveform meters + gain sliders ----
  function cnMicStateLabel() {
    // Human-readable mic status for the meter row label.
    try {
      const cfg = clipCfg();
      if (!cfg.mic) return 'off';
      if (clipMicTrack()) return 'live';
      if (_micAsking) return 'connecting…';
      if (_micErr) return _micErr;
      return 'pending';
    } catch { return ''; }
  }
  function cnMeterRow(lane, label) {
    // One row: label + live status, meter bar, gain slider (0-200%).
    // The mic row also gets an input picker (default mic vs virtual cable).
    try {
      const vol = clipVol(lane);
      const statusTxt = lane === 'mic' ? ` · ${esc(cnMicStateLabel())}` : (clipCfg().audio ? '' : ' · off');
      let devSel = '';
      if (lane === 'mic') {
        try {
          const cur = clipCfg().micDevice || 'default';
          const opts = [[
            'default', (_micDevices.length ? 'Default mic' : 'Default mic (enable Mic to list inputs)'),
          ]].concat(_micDevices.filter((d) => d.id && d.id !== 'default').map((d) => [d.id, esc(d.label || 'Microphone').slice(0, 60)]));
          devSel = `<div style="display:flex;gap:6px;align-items:center;margin-top:2px"><span style="font-size:10px;color:#6b7280">Input</span>${cnSel('cn-mic-dev', cur, opts)}</div>`;
        } catch {}
      }
      return `<div class="cn-vol-row">`
        + `<div style="display:flex;justify-content:space-between;font-size:10px;color:#9ca3af"><span>${esc(label)}${statusTxt}</span><span id="cn-vol-${lane}-val">${Math.round(vol * 100)}%</span></div>`
        + `<div class="cn-meter"><div id="cn-meter-${lane}" class="cn-meter-fill"></div></div>`
        + `<input id="cn-vol-${lane}" type="range" min="0" max="200" step="1" value="${Math.round(vol * 100)}">`
        + devSel
        + `</div>`;
    } catch { return ''; }
  }
  function cnAudioMixHtml() {
    try {
      return `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:6px 0 4px">AUDIO MIX</div>`
        + `<div class="cn-set">${cnMeterRow('game', 'Game audio')}${cnMeterRow('mic', 'Microphone')}</div>`;
    } catch { return ''; }
  }
  function cnBindMeters(prefix) {
    try {
      ['game', 'mic'].forEach((lane) => {
        const sl = document.getElementById(`cn-vol-${lane}`);
        if (!sl) return;
        sl.oninput = () => {
          try {
            const v = Number(sl.value) / 100;
            clipSetVol(lane, v);
            const val = document.getElementById(`cn-vol-${lane}-val`);
            if (val) val.textContent = Math.round(v * 100) + '%';
          } catch {}
        };
      });
      const dev = document.getElementById('cn-mic-dev');
      if (dev) dev.onchange = () => { try { cnSetMicDevice(dev.value); } catch {} };
      // Refresh the input list while the panel is open (labels appear only
      // after mic permission; cheap, throttled to one call per 10s).
      try {
        if (clipCfg().mic && Date.now() - _micDevicesAt > 10000) clipListMicDevices();
      } catch {}
    } catch {}
  }
  // Phase 3 throttle: meters paint on a 10Hz interval, not rAF. Two bars
  // don't need 60fps - the CSS width transition already smooths motion -
  // and each tick used to cost 2 DOM lookups + up to N analyser reads with
  // a fresh buffer alloc each. Now: cached elements, shared buffers, and
  // 6x fewer ticks. Loop runs only while the main panel is visible.
  let _cnMeterT = 0;
  let _cnMeterEls = { game: null, mic: null };
  function cnMeterEls() {
    // Bar nodes die on every panel re-render (innerHTML rebuild), so the
    // cache is validated with document.contains and refreshed on miss.
    try {
      const g = _cnMeterEls.game, m = _cnMeterEls.mic;
      if (g && m && document.contains(g) && document.contains(m)) return _cnMeterEls;
      _cnMeterEls = { game: document.getElementById('cn-meter-game'), mic: document.getElementById('cn-meter-mic') };
      return _cnMeterEls;
    } catch { return _cnMeterEls; }
  }
  function cnTickMeters() {
    // Reads the per-lane POST-GAIN AnalyserNodes so the bars reflect the
    // current slider position, not just raw source level.
    try {
      if (cnView !== 'main' && cnView !== 'set') { try { cnStopMeters(); } catch {} return; }
      try { if (typeof document !== 'undefined' && document.hidden) return; } catch {}
      const els = cnMeterEls();
      ['game', 'mic'].forEach((lane) => {
        try {
          const el = els[lane];
          if (!el) return;
          const L = _mixLane[lane];
          let lvl = (L && L.meter) ? clipAnLevel(L.meter) : null;
          // Game-only clips record the tap direct (mixer idle): fall back to
          // the tap analyser so the bar keeps reflecting real game output.
          if (lane === 'game' && (lvl == null || lvl < 0.005)) {
            try {
              _clipCtxs.forEach((ctx) => {
                try {
                  if (lvl != null && lvl >= 0.005) return;
                  if (clipIsOwnCtx(ctx) || clipIsOfflineCtx(ctx)) return;
                  const t = _clipTaps ? _clipTaps.get(ctx) : ctx.__cnClipTap;
                  if (t && t.analyser) {
                    const l2 = clipAnLevel(t.analyser);
                    if (l2 != null && l2 > (lvl || 0)) lvl = l2;
                  }
                } catch {}
              });
            } catch {}
          }
          el.style.width = Math.max(0, Math.min(100, Math.round((lvl || 0) * 220))) + '%';
        } catch {}
      });
    } catch {}
  }
  function cnStartMeters() {
    try {
      _cnMeterEls = { game: null, mic: null }; // fresh render = fresh nodes
      if (!_cnMeterT) _cnMeterT = setInterval(cnTickMeters, 100);
      try { cnTickMeters(); } catch {} // first paint sync, no 100ms lag
    } catch {}
  }
  function cnStopMeters() {
    try { if (_cnMeterT) { clearInterval(_cnMeterT); _cnMeterT = 0; } } catch {}
    try { _cnMeterEls = { game: null, mic: null }; } catch {}
  }
  // ---- Discord webhook delivery (opt-in: these bytes DO leave the PC) ----
  // Paste a channel webhook URL -> Save checks the format, then GETs the
  // webhook (name/avatar/channel) for the status row: green = online,
  // red = unreachable. Auto-send watches freshly saved clips through the
  // trigger filter; the ⇪ button in preview sends one clip on demand.
  // Upload cap is 20MB (Discord allows >10MB only on boosted servers, so
  // oversize-for-the-server posts fail with the HTTP status shown).
  // Ordering guarantee: the clip is stored in the local library FIRST -
  // a failed upload never loses the clip.
  const CN_WH_MAX = 20 * 1024 * 1024;
  const CN_WH_TRIGGERS = [['kill', 'Kills'], ['death', 'Deaths'], ['win', 'Wins'], ['streak', 'Streaks'], ['trim', 'Trims'], ['manual', 'Manual']];
  let _whBusy = false;
  const _whQueue = [];
  function cnWhUrl() {
    try { return String(GMGet('cn_wh_url', '') || '').trim(); } catch { return ''; }
  }
  function cnWhValid(u) {
    try { return /^https:\/\/(discord\.com|discordapp\.com)\/api\/webhooks\/\d+\/[\w-]+/.test(String(u || '')); } catch { return false; }
  }
  function cnWhUser() {
    // Custom name wins; falls back to the learned player name.
    try {
      const c = String(GMGet('cn_wh_user', '') || '').trim().slice(0, 32);
      if (c) return c;
    } catch {}
    try { return String((cnMe.name || '').trim() || 'clippednow').slice(0, 32); } catch {}
    return 'clippednow';
  }
  function cnWhAuto() {
    try { return GMGet('cn_wh_auto', false) === true; } catch { return false; }
  }
  function cnWhTrigs() {
    try {
      const raw = String(GMGet('cn_wh_trigs', 'kill,streak,win') || '');
      return raw.split(',').map((s) => s.trim()).filter(Boolean);
    } catch { return ['kill', 'streak', 'win']; }
  }
  function cnWhFileName(row, blob) {
    try {
      const user = cnWhUser().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 24) || 'clip';
      const ext = String((blob && blob.type) || '').includes('gif') ? 'gif' : clipExt((row && row.mime) || (blob && blob.type));
      return `clip-by-${user}-${(row && row.trigger) || 'clip'}-${(row && row.ts) || Date.now()}.${ext}`;
    } catch { return `clip-${Date.now()}.webm`; }
  }
  function cnWhContent(row) {
    // Plain and simple: "clip from Username - Date". No username override
    // in the payload, so Discord shows the webhook's own created name.
    try {
      const user = cnWhUser();
      let date = '';
      try { date = cnWhen((row && row.ts) || Date.now()); } catch {}
      return `clip from ${user}${date ? ' - ' + date : ''}`.slice(0, 1500);
    } catch { return 'clip from clippednow'; }
  }
  function cnWhFetch(url, ms) {
    // fetch with a hard timeout (AbortController may not exist everywhere).
    try {
      let timer = null;
      let ctrl = null;
      try { if (typeof AbortController !== 'undefined') { ctrl = new AbortController(); timer = setTimeout(() => { try { ctrl.abort(); } catch {} }, ms || 12000); } } catch {}
      const opts = {};
      try { if (ctrl) opts.signal = ctrl.signal; } catch {}
      return fetch(url, opts).then((r) => { try { if (timer) clearTimeout(timer); } catch {} return r; })
        .catch((e) => { try { if (timer) clearTimeout(timer); } catch {} throw e; });
    } catch (e) { return Promise.reject(e); }
  }
  async function cnWhInfo(url) {
    // { ok, name, avatarUrl } or { ok:false, err }. Never throws.
    try {
      if (!cnWhValid(url)) return { ok: false, err: 'bad-url' };
      const r = await cnWhFetch(url, 12000);
      if (!r || !r.ok) return { ok: false, err: 'http-' + ((r && r.status) || '?') };
      const j = await r.json();
      if (!j || !j.id) return { ok: false, err: 'bad-resp' };
      const av = j.avatar ? `https://cdn.discordapp.com/avatars/${j.id}/${j.avatar}.png` : 'https://cdn.discordapp.com/embed/avatars/0.png';
      return { ok: true, name: j.name || 'webhook', avatarUrl: av, channel: j.channel_id ? '#' + String(j.channel_id).slice(-4) : '' };
    } catch { return { ok: false, err: 'network' }; }
  }
  function cnWhUpload(blob, filename, content) {
    // Multipart POST via XHR (fetch can't report upload progress).
    return new Promise((res) => {
      const done = (o) => { try { res(o); } catch {} };
      try {
        const url = cnWhUrl();
        if (!cnWhValid(url)) { done({ ok: false, err: 'no-webhook' }); return; }
        const fd = new FormData();
        try { fd.append('payload_json', JSON.stringify({ content })); } catch {}
        try { fd.append('file', blob, filename); } catch { done({ ok: false, err: 'form' }); return; }
        const x = new XMLHttpRequest();
        try { x.timeout = 90000; } catch {}
        x.open('POST', url);
        x.onload = () => { try { done({ ok: x.status >= 200 && x.status < 300, status: x.status }); } catch { done({ ok: false, err: 'load' }); } };
        x.onerror = () => done({ ok: false, err: 'network' });
        x.ontimeout = () => done({ ok: false, err: 'timeout' });
        try { x.send(fd); } catch { done({ ok: false, err: 'send' }); }
      } catch { done({ ok: false, err: 'throw' }); }
    });
  }
  function cnWhMaybeAuto(row, blob) {
    // Called once per saved clip. Cheap gate, then queue.
    try {
      if (!cnWhAuto()) return;
      if (!row || !blob || !blob.size) return;
      if (!cnWhValid(cnWhUrl())) return;
      if (!cnWhTrigs().includes(row.trigger)) return;
      if (blob.size > CN_WH_MAX) { try { toast('Clip too big for Discord (20MB) - trim it shorter', true); } catch {} return; }
      _whQueue.push({ row, blob });
      if (_whQueue.length > 3) _whQueue.splice(0, _whQueue.length - 3); // don't spam a backlog
      cnWhPump();
    } catch {}
  }
  async function cnWhPump() {
    if (_whBusy) return;
    _whBusy = true;
    try {
      while (_whQueue.length) {
        const j = _whQueue.shift();
        try {
          try { toast(`Sending ${j.row.trigger} clip to Discord…`); } catch {}
          const r = await cnWhUpload(j.blob, cnWhFileName(j.row, j.blob), cnWhContent(j.row));
          if (r.ok) { try { toast('Clip sent to Discord', false); } catch {} try { cnSfx('ok'); } catch {} }
          else { try { toast(`Discord send failed (${r.status || r.err || '?'}) - clip kept in library`, true); } catch {} try { cnSfx('err'); } catch {} }
        } catch {}
      }
    } catch {}
    _whBusy = false;
  }
  async function cnWhSendOne(row, blob) {
    // Manual ⇪ send from preview. Never throws.
    try {
      if (!cnWhValid(cnWhUrl())) { try { toast('Set a Discord webhook first (panel settings)', true); } catch {} return; }
      if (!blob || !blob.size) { try { toast('Nothing to send', true); } catch {} return; }
      if (blob.size > CN_WH_MAX) { try { toast('Too big for Discord (20MB) - trim it shorter first', true); } catch {} return; }
      try { toast('Sending clip to Discord…'); } catch {}
      const r = await cnWhUpload(blob, cnWhFileName(row, blob), cnWhContent(row));
      if (r.ok) { try { toast('Clip sent to Discord', false); } catch {} try { cnSfx('ok'); } catch {} }
      else { try { toast(`Discord send failed (${r.status || r.err || '?'}) - clip kept in library, check webhook`, true); } catch {} try { cnSfx('err'); } catch {} }
    } catch {}
  }
  function cnWhHtml() {
    try {
      const has = cnWhValid(cnWhUrl());
      // Keep unsaved drafts: renderMain rebuilds on any toggle, which would
      // otherwise wipe a half-pasted URL.
      let draftUrl = '', draftUser = '';
      try { const cur = document.getElementById('cn-wh-url'); if (cur && document.contains(cur) && cur.value) draftUrl = cur.value; } catch {}
      try { const cu = document.getElementById('cn-wh-user'); if (cu && document.contains(cu) && cu.value) draftUser = cu.value; } catch {}
      return `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:10px 0 4px">DISCORD WEBHOOK</div>`
        + `<div class="cn-set">`
        + `<div id="cn-wh-status" style="display:flex;align-items:center;gap:8px;font-size:11px;color:#9ca3af"><span id="cn-wh-dot" style="width:8px;height:8px;border-radius:50%;background:#4b5563;flex-shrink:0"></span><span id="cn-wh-info">${has ? 'checking…' : 'not set'}</span></div>`
        + `<input id="cn-wh-url" class="cn-search" placeholder="https://discord.com/api/webhooks/…  (paste here)" value="${esc(draftUrl || cnWhUrl())}" autocomplete="off" spellcheck="false">`
        + `<div style="display:flex;gap:8px;flex-wrap:wrap"><button id="cn-wh-save" class="cn-rowbtn">Save webhook</button><button id="cn-wh-test" class="cn-ghost">Test</button><button id="cn-wh-clear" class="cn-ghost">Remove</button></div>`
        + `<label style="display:flex;align-items:center;gap:6px;font-size:11px;color:#e5e7eb;cursor:pointer"><input type="checkbox" id="cn-wh-auto"${cnWhAuto() ? ' checked' : ''} style="accent-color:#fff"> Auto-send new clips</label>`
        + `<div id="cn-wh-trigs" style="display:flex;gap:12px;flex-wrap:wrap">${CN_WH_TRIGGERS.map(([v, l]) => `<label style="display:flex;align-items:center;gap:6px;font-size:11px;color:#e5e7eb;cursor:pointer"><input type="checkbox" data-cn-wh-trig="${v}"${cnWhTrigs().includes(v) ? ' checked' : ''} style="accent-color:#fff"> ${l}</label>`).join('')}</div>`
        + `<div style="display:flex;gap:8px;align-items:center;font-size:11px;color:#9ca3af"><span>Post as</span><input id="cn-wh-user" class="cn-search" style="flex:1" placeholder="${esc(String((cnMe.name || 'player').slice(0, 32)))}" value="${esc(draftUser || String(GMGet('cn_wh_user', '') || ''))}" autocomplete="off" spellcheck="false"></div>`
        + `<div style="font-size:10px;color:#6b7280">Sent clips leave this PC through YOUR webhook. 20MB cap (Discord allows >10MB only on boosted servers) - oversize clips are skipped with a hint. Failed uploads keep the clip in your library.</div>`
        + `</div>`;
    } catch { return ''; }
  }
  function cnWhBind() {
    try {
      const url = document.getElementById('cn-wh-url');
      if (url) { try { cnUntrapInput(url); } catch {} }
      const user = document.getElementById('cn-wh-user');
      if (user) {
        try { cnUntrapInput(user); } catch {}
        user.onchange = () => { try { GMSet('cn_wh_user', String(user.value || '').trim().slice(0, 32)); } catch {} };
      }
      const save = document.getElementById('cn-wh-save');
      if (save) save.onclick = () => {
        try {
          const u = String((document.getElementById('cn-wh-url') || {}).value || '').trim();
          if (!cnWhValid(u)) { try { toast('That does not look like a webhook URL', true); } catch {} return; }
          try { GMSet('cn_wh_url', u); } catch {}
          try { toast('Webhook saved - checking…'); } catch {}
          cnWhRefresh();
        } catch {}
      };
      const clear = document.getElementById('cn-wh-clear');
      if (clear) clear.onclick = () => {
        try { GMSet('cn_wh_url', ''); } catch {}
        try { GMSet('cn_wh_auto', false); } catch {}
        try { cnRenderCurrent(); } catch {}
      };
      const test = document.getElementById('cn-wh-test');
      if (test) test.onclick = async () => {
        try {
          if (!cnWhValid(cnWhUrl())) { try { toast('Save a webhook first', true); } catch {} return; }
          try { toast('Sending test message…'); } catch {}
          const fd = new FormData();
          try { fd.append('payload_json', JSON.stringify({ content: `clip from ${cnWhUser()} - test message, webhook is live` })); } catch {}
          try {
            const rr = await fetch(cnWhUrl(), { method: 'POST', body: fd });
            if (rr && rr.ok) { try { toast('Test posted - check the channel', false); } catch {} try { cnSfx('ok'); } catch {} }
            else { try { toast(`Test failed (${(rr && rr.status) || '?'})`, true); } catch {} }
          } catch { try { toast('Test failed - network?', true); } catch {} }
          cnWhRefresh();
        } catch {}
      };
      const auto = document.getElementById('cn-wh-auto');
      if (auto) auto.onchange = () => {
        try {
          const on = !!auto.checked;
          if (on && !cnWhValid(cnWhUrl())) { try { toast('Save a webhook first', true); } catch {} auto.checked = false; return; }
          try { GMSet('cn_wh_auto', on); } catch {}
          try { toast(on ? 'Auto-send on' : 'Auto-send off'); } catch {}
        } catch {}
      };
      try {
        document.querySelectorAll('[data-cn-wh-trig]').forEach((box) => {
          box.onchange = () => {
            try {
              const all = Array.from(document.querySelectorAll('[data-cn-wh-trig]')).filter((b) => b.checked).map((b) => b.dataset.cnWhTrig);
              try { GMSet('cn_wh_trigs', all.join(',')); } catch {}
            } catch {}
          };
        });
      } catch {}
      cnWhRefresh();
    } catch {}
  }
  async function cnWhRefresh() {
    // Re-read the webhook for the status row. Stale renders bail via id.
    try {
      const dot = document.getElementById('cn-wh-dot'), info = document.getElementById('cn-wh-info');
      const url = cnWhUrl();
      if (!cnWhValid(url)) {
        try { if (dot) dot.style.background = '#4b5563'; } catch {}
        try { if (info) info.textContent = url ? 'invalid URL' : 'not set'; } catch {}
        return;
      }
      try { if (info) info.textContent = 'checking…'; } catch {}
      const w = await cnWhInfo(url);
      try {
        if (!document.getElementById('cn-wh-info')) return; // panel moved on
        if (w.ok) {
          if (dot) dot.style.background = '#10b981';
          if (info) {
            info.textContent = '';
            try {
              const img = document.createElement('img');
              img.src = w.avatarUrl; img.alt = '';
              Object.assign(img.style, { width: '16px', height: '16px', borderRadius: '50%', verticalAlign: '-3px', marginRight: '4px' });
              img.onerror = () => { try { img.remove(); } catch {} };
              info.appendChild(img);
            } catch {}
            try { info.appendChild(document.createTextNode(`${w.name} ● online`)); } catch {}
          }
        } else {
          if (dot) dot.style.background = '#ef4444';
          if (info) info.textContent = `offline (${w.err || '?'}) - check URL / channel`;
        }
      } catch {}
    } catch {}
  }
  // ---- main panel: recorder essentials only (settings live in ⚙ view) ----
  function renderMain() {
    try {
      if (!cnPanel) return;
      const cfg = clipCfg();
      const meLabel = cnMe.name ? esc(cnMe.name) : 'learning…';
      const modeLabel = esc(cnGmLabel(cnMode.code));
      const layLabel = cfg.layout === 'vertical' ? '9:16' : '16:9';
      let html = `<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.08)">`
        + `<div><b style="font-size:15px;color:#fff">ClippedNow.gg v${CN_VERSION}</b><div id="cn-rec-state" style="font-size:11px;color:#9ca3af;margin-top:2px">${esc(cnRecState(cfg))}</div>`
        + `<div style="font-size:11px;color:#6b7280;margin-top:2px">you: ${meLabel} · ${modeLabel} · ${layLabel}</div></div>`
        + `<button id="cn-close" class="cn-ghost">Close</button></div>`
        + `<div id="cn-body">`
        + `<button id="cn-clip-now" class="cn-rowbtn" style="width:100%;margin-bottom:8px">● Clip now (${esc(cfg.keyClip)})</button>`
        + `<button id="cn-open-lib" class="cn-rowbtn" style="width:100%;margin-bottom:8px;background:#171717;color:#fff;border:1px solid rgba(255,255,255,.12)">▦ Open clips (<span id="cn-open-count">…</span>)</button>`
        + `<button id="cn-open-set" class="cn-ghost" style="width:100%;margin-bottom:10px">⚙ Settings & audio</button>`;
      try {
        const lk = _clip.lastKill;
        if (lk && lk.at) {
          const ago = Math.max(0, Math.round((Date.now() - lk.at) / 1000));
          const why = { queued: 'clip queued', saved: 'clip saved', 'saved-suspect': 'clip saved - check playback', 'not-your-kill': 'skipped (not your kill)', 'knock-ignored': 'skipped (knock)', 'knock-stashed': 'knock noted - clips if confirmed', 'confirm-queued': 'confirm clip queued', debounced: 'skipped (too soon)', 'auto-off': 'auto-clip off', 'clipping-off': 'clipping off', corrupted: 'discarded (corrupted)' }[lk.decision] || lk.decision || '';
          html += `<div style="font-size:11px;color:#6b7280;margin:-2px 0 10px">Last killfeed: ${esc(lk.killer)} → ${esc(lk.victim)} · ${ago}s ago · ${esc(why)}</div>`;
        }
      } catch {}
      html += `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Preset</span>${cnSel('cn-main-preset', cnPresetOf(cfg.fps, cfg.bitrate), [['custom', 'Custom'], ...CN_PRESETS.map(([k, l]) => [k, l])])}<span>${cnFpsLabel(cfg.fps)}fps · ${(cfg.bitrate / 1000000).toFixed(1)}Mbps · ${cfg.len}s</span></div>`
        + `<div style="font-size:11px;color:#6b7280;padding:10px 2px 2px;line-height:1.6">100% local - clips never leave this PC.<br>made with love by @cisled</div>`
        + `</div>`;
      cnRevokeUrls();
      cnPanel.innerHTML = html;
      const close = document.getElementById('cn-close');
      if (close) close.onclick = () => cnToggle(false);
      const now = document.getElementById('cn-clip-now');
      if (now) now.onclick = () => { try { cnSfx('tap'); } catch {} try { saveClip('manual'); } catch {} };
      const open = document.getElementById('cn-open-lib');
      if (open) open.onclick = () => { try { cnShow('lib'); } catch {} };
      const set = document.getElementById('cn-open-set');
      if (set) set.onclick = () => { try { cnShow('set'); } catch {} };
      const pre = document.getElementById('cn-main-preset');
      if (pre) pre.onchange = () => { try { cnApplyPreset(pre.value); } catch {} };
      try {
        clipList().then((rows) => {
          try {
            if (cnView !== 'main') return;
            const el = document.getElementById('cn-open-count');
            if (el) el.textContent = String((rows || []).length);
            try { cnClips = rows || []; } catch {}
          } catch {}
        }).catch(() => {});
      } catch {}
      cnRefreshFab();
    } catch {}
  }
  // ---- settings view: full capture/audio/discord settings, away from record --
  function renderSet() {
    try {
      const e = cnEls();
      if (!e.set) return;
      const cfg = clipCfg();
      e.set.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.08)">`
        + `<div><b style="font-size:15px;color:#fff">⚙ Settings</b><div style="font-size:11px;color:#6b7280;margin-top:2px">applies live · recorder keeps running</div></div>`
        + `<button id="cn-set-back" class="cn-ghost">← Recorder</button></div>`
        + `<div class="cn-mbody">`
        + cnAudioMixHtml()
        + cnSettingsHtml(cfg, 'cn')
        + cnWhHtml()
        + `<div style="font-size:11px;color:#6b7280;padding:10px 2px 2px;line-height:1.6">Capture stays 100% local - only clips you send leave via your webhook.</div>`
        + `</div>`;
      const back = document.getElementById('cn-set-back');
      if (back) back.onclick = () => { try { cnShow('main'); } catch {} };
      try { cnBindSettings('cn'); } catch {}
      try { cnBindKeys('cn'); } catch {}
      try { cnBindMeters('cn'); } catch {}
      try { cnWhBind(); } catch {}
      try { cnHwBind(); } catch {}
      try { cnStartMeters(); } catch {}
      try { cnRefreshFab(); } catch {}
    } catch {}
  }

  // ---- shooter-proof text inputs (mirrors buildnow-companion untrap) ----
  // The game eats keystrokes at window/document capture (registered before
  // us), so a blinking cursor still gets no characters: insertion dies even
  // though focus works. Element-level stopPropagation is too late in that
  // flow. Fix: capture trap on the input itself (bubble-phase game listeners)
  // PLUS a document-capture key forwarder that re-injects printable keys +
  // Backspace as input value while the search box is engaged. The forwarder
  // does not need to beat the game: preventDefault does not stop
  // propagation, so our listener still fires and restores the character.
  let _cnUntrapEl = null, _cnUntrapUntil = 0, _cnUntrapTimer = null, _cnUntrapFwdOn = false;
  function cnUntrapActive() {
    try {
      return !!(_cnUntrapEl && document.contains(_cnUntrapEl) && Date.now() <= _cnUntrapUntil && (cnView === 'lib' || cnView === 'set'));
    } catch { return false; }
  }
  function cnUntrapRelease() { try { _cnUntrapEl = null; _cnUntrapUntil = 0; } catch {} }
  function cnUntrapForward(e) {
    try {
      if (!cnUntrapActive()) return;
      const el = _cnUntrapEl;
      if (!el) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      if (e.isComposing) return;
      const k = e.key;
      const put = (fn) => {
        try { e.preventDefault(); } catch {}
        try { e.stopPropagation(); } catch {}
        try { if (e.stopImmediatePropagation) e.stopImmediatePropagation(); } catch {}
        try { fn(); } catch {}
      };
      if (k === 'Backspace') {
        put(() => {
          const v = el.value || '';
          let s = (document.activeElement === el && el.selectionStart != null) ? el.selectionStart : v.length;
          let t = (document.activeElement === el && el.selectionEnd != null) ? el.selectionEnd : v.length;
          s = Math.max(0, Math.min(s, v.length)); t = Math.max(0, Math.min(t, v.length));
          if (s === t) s = Math.max(0, s - 1);
          el.value = v.slice(0, s) + v.slice(t);
          try { el.focus(); } catch {}
          try { el.setSelectionRange(s, s); } catch {}
          el.dispatchEvent(new Event('input', { bubbles: true }));
        });
        return;
      }
      if (k && k.length === 1) {
        put(() => {
          const v = el.value || '';
          let s = (document.activeElement === el && el.selectionStart != null) ? el.selectionStart : v.length;
          let t = (document.activeElement === el && el.selectionEnd != null) ? el.selectionEnd : v.length;
          s = Math.max(0, Math.min(s, v.length)); t = Math.max(0, Math.min(t, v.length));
          el.value = v.slice(0, s) + k + v.slice(t);
          try { el.focus(); } catch {}
          try { el.setSelectionRange(s + 1, s + 1); } catch {}
          el.dispatchEvent(new Event('input', { bubbles: true }));
        });
        return;
      }
    } catch {}
  }
  function cnUntrapInput(el) {
    // Trap + engage for one panel text field. Never throws.
    try {
      if (!el || el.dataset.cnUntrapped) return;
      try { el.dataset.cnUntrapped = '1'; } catch {}
      try { el.setAttribute('autocomplete', 'off'); } catch {}
      try { el.setAttribute('autocapitalize', 'off'); } catch {}
      try { el.setAttribute('spellcheck', 'false'); } catch {}
      const trap = (e) => {
        try { e.stopPropagation(); } catch {}
        try { if (e.stopImmediatePropagation) e.stopImmediatePropagation(); } catch {}
      };
      ['keydown', 'keyup', 'keypress'].forEach((ev) => { try { el.addEventListener(ev, trap, true); } catch {} });
      const engage = () => {
        try {
          _cnUntrapEl = el; _cnUntrapUntil = Date.now() + 60000;
          if (!_cnUntrapFwdOn) {
            _cnUntrapFwdOn = true;
            try { document.addEventListener('keydown', cnUntrapForward, true); } catch {}
          }
          if (_cnUntrapTimer) return;
          _cnUntrapTimer = setInterval(() => {
            try {
              const cur = _cnUntrapEl;
              if (!cur || !cnUntrapActive()) {
                // Releasing here would drop mid-typing after 60s; the active
                // gate already expired, just stop polling until re-engaged.
                if (!cur || !document.contains(cur) || (cnView !== 'lib' && cnView !== 'set')) { try { clearInterval(_cnUntrapTimer); } catch {} _cnUntrapTimer = null; }
                return;
              }
              if (document.activeElement === cur) return;
              // Legit in-panel navigation releases (don't trap the user).
              try {
                const e = cnEls();
                const ae = document.activeElement;
                const inPanel = (p) => { try { return ae && ae !== document.body && p && p.contains(ae); } catch { return false; } };
                if (inPanel(e.lib) || inPanel(e.set)) { cnUntrapRelease(); return; }
              } catch {}
              try { cur.focus(); } catch {}
            } catch {}
          }, 250);
        } catch {}
      };
      try {
        el.addEventListener('mousedown', (e) => { try { e.stopPropagation(); } catch {} try { setTimeout(() => { try { el.focus(); } catch {} }, 0); } catch {} engage(); }, true);
        el.addEventListener('click', (e) => { try { e.stopPropagation(); } catch {} engage(); }, true);
        el.addEventListener('focus', () => { try { engage(); } catch {} }, true);
      } catch {}
    } catch {}
  }
  // ---- clips list panel: centered 3:4 modal, search + sort + grid/list ----
  function renderLib() {
    try {
      const e = cnEls();
      if (!e.lib) return;
      const sort = cnLibSort || GMGet('cn_lib_sort', 'new') || 'new';
      const mode = cnLibMode || GMGet('cn_lib_view', 'grid') || 'grid';
      const html = `<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.08)">`
        + `<div><b style="font-size:15px;color:#fff">My clips</b><div id="cn-lib-sub" style="font-size:11px;color:#6b7280;margin-top:2px">…</div></div>`
        + `<button id="cn-lib-close" class="cn-ghost">Close</button></div>`
        + `<div class="cn-mbody">`
        + `<div style="display:flex;gap:8px;margin-bottom:8px"><input id="cn-lib-q" class="cn-search" placeholder="Search kill, victim, mode, date, ★…" value="${esc(cnLibQ)}">`
        + `<button id="cn-lib-view" class="cn-ghost" title="Toggle grid/list">${mode === 'grid' ? 'List' : 'Grid'}</button></div>`
        + `<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;font-size:11px;color:#9ca3af"><span>Sort</span>${cnSel('cn-lib-sort', sort, [['new', 'Newest'], ['old', 'Oldest'], ['big', 'Largest'], ['small', 'Smallest'], ['fav', 'Favorites']])}`
        + `<span style="flex:1"></span><button id="cn-lib-clear" style="background:none;border:0;color:#6b7280;font-size:11px;cursor:pointer;text-decoration:underline">Clear all</button></div>`
        + `<div id="cn-lib-body"></div>`
        + `</div>`;
      cnRevokeUrls();
      e.lib.innerHTML = html;
      const close = document.getElementById('cn-lib-close');
      if (close) close.onclick = () => cnHideAll();
      const q = document.getElementById('cn-lib-q');
      if (q) {
        q.oninput = () => { try { cnLibQ = q.value; cnLibBody(); } catch {} };
        q.onkeydown = (ev) => { try { ev.stopPropagation(); } catch {} };
        try { cnUntrapInput(q); } catch {} // shooter-proof typing (see above)
      }
      const vw = document.getElementById('cn-lib-view');
      if (vw) vw.onclick = () => {
        try {
          cnLibMode = (cnLibMode || GMGet('cn_lib_view', 'grid') || 'grid') === 'grid' ? 'list' : 'grid';
          GMSet('cn_lib_view', cnLibMode);
          renderLib();
        } catch {}
      };
      const s = document.getElementById('cn-lib-sort');
      if (s) s.onchange = () => { try { cnLibSort = s.value; GMSet('cn_lib_sort', cnLibSort); cnLibBody(); } catch {} };
      const clr = document.getElementById('cn-lib-clear');
      if (clr) clr.onclick = () => { if (!confirm('Delete all clips?')) return; clipClearAll().then(() => { try { cnClips = []; } catch {} fetchCnClips(); }); };
      if (cnClipsLoading) {
        const b = document.getElementById('cn-lib-body');
        if (b) b.innerHTML = `<div style="color:#9ca3af;font-size:12px;text-align:center;padding:20px 0">Loading clips…</div>`;
      } else cnLibBody();
    } catch {}
  }
  // ---- lazy thumbnails (library grid used to decode all 20 videos at once) ----
  // Old path assigned src to every grid video on render: up to 20 blob URLs
  // decoding simultaneously = the library lag. Now: preload="none", load on
  // visibility with a 240px prefetch margin, cap 8 live decoders FIFO
  // (preferring to evict off-screen thumbs so scroll-back stays instant).
  let _cnThumbObs = null;
  const _cnThumbLive = [];
  function cnThumbUnload(v) {
    try {
      const u = v.__cnUrl || '';
      try { v.removeAttribute('src'); } catch {}
      try { v.load(); } catch {}
      if (u) { try { URL.revokeObjectURL(u); } catch {} try { _cnObjUrls.delete(u); } catch {} }
      try { v.__cnUrl = ''; } catch {}
      const ix = _cnThumbLive.indexOf(v);
      if (ix >= 0) _cnThumbLive.splice(ix, 1);
    } catch {}
  }
  function cnThumbLoad(v, r) {
    try {
      if (!v || !r || !r.blob || v.__cnUrl) return;
      while (_cnThumbLive.length >= 8) {
        let victim = -1;
        for (let i = 0; i < _cnThumbLive.length; i++) {
          if (_cnThumbLive[i] === v) continue;
          if (!_cnThumbLive[i].__cnVis) { victim = i; break; }
          if (victim < 0) victim = i;
        }
        if (victim < 0) break;
        cnThumbUnload(_cnThumbLive[victim]);
      }
      const u = cnTrackUrl(URL.createObjectURL(r.blob));
      try { v.__cnUrl = u; } catch {}
      try { v.preload = 'metadata'; } catch {}
      v.src = u;
      _cnThumbLive.push(v);
    } catch {}
  }
  function cnThumbObserve(body, byId) {
    try {
      try { if (_cnThumbObs) _cnThumbObs.disconnect(); } catch {}
      _cnThumbObs = null;
      _cnThumbLive.length = 0; // new render = new nodes; old URLs die with renderLib's revoke
      const loadFor = (v) => { try { cnThumbLoad(v, byId[v.dataset.cnVid]); } catch {} };
      let scroller = null;
      try { scroller = (body.closest && body.closest('.cn-mbody')) || null; } catch {}
      if (typeof IntersectionObserver === 'undefined' || !scroller) {
        body.querySelectorAll('video[data-cn-vid]').forEach(loadFor);
        return;
      }
      _cnThumbObs = new IntersectionObserver((ents) => {
        try {
          for (const en of ents) {
            try { en.target.__cnVis = !!en.isIntersecting; } catch {}
            if (en.isIntersecting) loadFor(en.target);
          }
        } catch {}
      }, { root: scroller, rootMargin: '240px 0px', threshold: 0 });
      body.querySelectorAll('video[data-cn-vid]').forEach((v) => { try { _cnThumbObs.observe(v); } catch {} });
    } catch {}
  }
  function cnThumbStop() {
    try { if (_cnThumbObs) _cnThumbObs.disconnect(); } catch {}
    _cnThumbObs = null;
    try { _cnThumbLive.length = 0; } catch {}
  }
  function cnLibBody() {
    try {
      const body = document.getElementById('cn-lib-body');
      const sub = document.getElementById('cn-lib-sub');
      const rows = cnLibRows();
      const mode = cnLibMode || GMGet('cn_lib_view', 'grid') || 'grid';
      const byId = {};
      rows.forEach((r) => { byId[r.id] = r; });
      if (sub) {
        try {
          const mb = ((cnClips || []).reduce((a, r) => a + (r.size || 0), 0) / 1048576).toFixed(1);
          sub.textContent = `${rows.length} of ${(cnClips || []).length} · ${mb} MB`;
          try {
            if (navigator.storage && navigator.storage.estimate) {
              navigator.storage.estimate().then((est) => {
                try {
                  if (!est || !est.quota) return;
                  const el = document.getElementById('cn-lib-sub');
                  if (!el) return;
                  el.textContent += ` / ~${(est.quota / 1048576).toFixed(0)} MB`;
                } catch {}
              }).catch(() => {});
            }
          } catch {}
        } catch {}
      }
      if (!body) return;
      if (!rows.length) {
        body.innerHTML = `<div style="color:#9ca3af;font-size:12px;text-align:center;padding:20px 0">No clips match - press ${esc(clipCfg().keyClip)} or get a kill.</div>`;
        return;
      }
      const vert = clipCfg().layout === 'vertical';
      const card = (r) => {
        const star = r.fav ? '★' : '☆';
        const lay = r.layout === 'vertical' ? ' · 9:16' : '';
        const vs = (r.trigger !== 'manual' && (r.killer || r.victim)) ? ` · ${esc(r.killer || '?')} → ${esc(r.victim || '?')}` : '';
        const head = `<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px"><span style="font-size:10px;font-weight:800;color:#fff;background:${cnBadge(r.trigger)};border-radius:4px;padding:2px 6px">${esc((r.trigger || 'manual').toUpperCase())}</span>`
          + (r.suspect ? `<span style="font-size:10px;font-weight:800;color:#000;background:#eab308;border-radius:4px;padding:2px 6px" title="Decode check failed - may not play in browser, try VLC">⚠ CHECK</span>` : '')
          + `<span style="font-size:11px;color:#9ca3af;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(cnWhen(r.ts))} · ${cnKB(r.size)}${r.mode ? ' · ' + esc(cnGmLabel(r.mode)) : ''}${lay}${vs}</span>`
          + `<button data-cn-fav="${r.id}" class="cn-star" title="Favorite">${star}</button></div>`;
        if (mode === 'grid') {
          return `<div class="cn-card" data-cn-open="${r.id}" style="cursor:pointer;margin-bottom:0;${vert ? 'grid-column:span 1' : ''}">${head}<video data-cn-vid="${r.id}" preload="none" muted playsinline class="cn-thumb"></video></div>`;
        }
        return `<div class="cn-card" data-cn-open="${r.id}" style="cursor:pointer;display:flex;gap:10px;align-items:center">${head.split('</div>')[0]}</div><div style="flex:1;min-width:0"><video data-cn-vid="${r.id}" preload="none" muted playsinline style="width:100%;aspect-ratio:16/9;background:#000;border-radius:6px;object-fit:cover"></video></div><button data-cn-fav="${r.id}" class="cn-star" title="Favorite" style="font-size:18px">${star}</button></div>`;
      };
      body.innerHTML = (mode === 'grid' ? `<div class="cn-grid${vert ? ' cn-vert' : ''}">` : `<div style="display:flex;flex-direction:column;gap:8px">`)
        + rows.map(card).join('') + `</div>`;
      try {
        cnThumbObserve(body, byId);
      } catch {}
      try {
        body.querySelectorAll('[data-cn-open]').forEach(el => {
          el.onclick = () => { try { cnPrevId = el.dataset.cnOpen; cnShow('prev'); } catch {} };
        });
      } catch {}
      try {
        body.querySelectorAll('[data-cn-fav]').forEach(b => {
          b.onclick = (ev) => {
            try { ev.stopPropagation(); } catch {}
            try { cnSfx('tap'); } catch {}
            const id = b.dataset.cnFav;
            const cur = byId[id];
            clipSetFav(id, !(cur && cur.fav)).then((row) => {
              try {
                if (row) { const ix = (cnClips || []).findIndex(x => x.id === id); if (ix >= 0) cnClips[ix] = row; }
                if (cnView === 'lib') cnLibBody();
              } catch {}
            });
          };
        });
      } catch {}
    } catch {}
  }
  // ---- trim & GIF export (preview panel, 100% local) ----
  // Trimming re-records the selected range through a hidden video element
  // (captureStream + MediaRecorder, same mime picker as the recorder), so
  // no muxer/transcoder is needed and it works for webm + mp4 alike. GIF
  // export seeks through the range at 10fps/320px, median-cut quantizes
  // each frame to 256 colors and encodes animated GIF89a with an inline
  // LZW compressor - zero libraries, zero network, all on-device.
  const CN_TRIM_GIF_MAX = 6, CN_GIF_FPS = 10, CN_GIF_W = 320, CN_TRIM_MIN = 0.3;
  let _cnTrim = { id: null, dur: 0, start: 0, end: 0, busy: false, seq: 0 };
  let _cnExportErr = '';
  const _cnTrimDurCache = {};
  function cnTrimStatus(txt) {
    try { const el = document.getElementById('cn-trim-status'); if (el) el.textContent = txt; } catch {}
  }
  function cnDownloadBlob(blob, filename, label) {
    try {
      if (!blob || !blob.size) { try { toast('Export failed - try again', true); } catch {} return; }
      const u = cnTrackUrl(URL.createObjectURL(blob));
      const a = document.createElement('a');
      a.href = u; a.download = filename;
      (document.body || document.documentElement).appendChild(a); a.click(); a.remove();
      try { toast(`Downloading ${label || 'clip'}`, false); } catch {}
      try { cnSfx('ok'); } catch {}
      setTimeout(() => { try { URL.revokeObjectURL(u); _cnObjUrls.delete(u); } catch {} }, 30000);
    } catch {}
  }
  function cnTrimHtml() {
    try {
      return `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:10px 0 4px">TRIM & EXPORT</div>`
        + `<div class="cn-set" id="cn-trim-box">`
        + `<div id="cn-trim-status" style="font-size:11px;color:#9ca3af">Loading duration…</div>`
        + `<div class="cn-vol-row"><div style="display:flex;justify-content:space-between;font-size:10px;color:#9ca3af"><span>Start</span><span id="cn-trim-start-val">0.0s</span></div>`
        + `<input id="cn-trim-start" type="range" min="0" max="1" step="0.1" value="0" disabled></div>`
        + `<div class="cn-vol-row"><div style="display:flex;justify-content:space-between;font-size:10px;color:#9ca3af"><span>End</span><span id="cn-trim-end-val">…</span></div>`
        + `<input id="cn-trim-end" type="range" min="0" max="1" step="0.1" value="1" disabled></div>`
        + `<div style="display:flex;gap:8px;flex-wrap:wrap"><button id="cn-trim-dl" class="cn-rowbtn" disabled>⤓ Trimmed video</button>`
        + `<button id="cn-trim-copy" class="cn-rowbtn" disabled title="Copy the trimmed clip, paste straight into Discord">⧉ Copy</button>`
        + `<button id="cn-trim-gif" class="cn-rowbtn" disabled title="Best for 1-5s moments">⤓ GIF (≤${CN_TRIM_GIF_MAX}s)</button></div>`
        + `<button id="cn-trim-save" class="cn-ghost" style="width:100%" disabled>＋ Save trim to library</button>`
        + `</div>`;
    } catch { return ''; }
  }
  function cnTrimRefresh() {
    // Push state into the sliders + labels. Never throws.
    try {
      const t = _cnTrim;
      const sS = document.getElementById('cn-trim-start'), sE = document.getElementById('cn-trim-end');
      const vS = document.getElementById('cn-trim-start-val'), vE = document.getElementById('cn-trim-end-val');
      if (sS) { sS.max = String(t.dur); sS.value = String(t.start); }
      if (sE) { sE.max = String(t.dur); sE.value = String(t.end); }
      if (vS) vS.textContent = Number(t.start).toFixed(1) + 's';
      if (vE) vE.textContent = Number(t.end).toFixed(1) + 's';
      if (!t.busy && t.dur > 0) {
        const span = Math.max(0, t.end - t.start);
        cnTrimStatus(`Selected ${span.toFixed(1)}s of ${Number(t.dur).toFixed(1)}s`);
      }
    } catch {}
  }
  function cnTrimBind(r) {
    // (Re)wire the trim box for this clip; resolves duration async, then
    // enables the controls. seq-guarded so a fav-toggle re-render can't let
    // a stale probe enable another clip's buttons.
    try {
      const seq = ++_cnTrim.seq;
      _cnTrim = { id: r.id, dur: 0, start: 0, end: 0, busy: false, seq };
      const enable = (on) => {
        try {
          ['cn-trim-start', 'cn-trim-end', 'cn-trim-dl', 'cn-trim-copy', 'cn-trim-gif', 'cn-trim-save'].forEach((id) => {
            const el = document.getElementById(id);
            if (el) el.disabled = !on;
          });
        } catch {}
      };
      const paint = () => { try { if (_cnTrim.seq === seq) cnTrimRefresh(); } catch {} };
      const sS = document.getElementById('cn-trim-start'), sE = document.getElementById('cn-trim-end');
      if (sS) sS.oninput = () => {
        try {
          if (_cnTrim.seq !== seq || _cnTrim.busy) return;
          _cnTrim.start = Math.max(0, Math.min(Number(sS.value) || 0, _cnTrim.end - CN_TRIM_MIN));
          paint();
          try { const v = document.getElementById('cn-edit-vid') || document.getElementById('cn-prev-vid'); if (v) v.currentTime = _cnTrim.start; } catch {}
        } catch {}
      };
      if (sE) sE.oninput = () => {
        try {
          if (_cnTrim.seq !== seq || _cnTrim.busy) return;
          _cnTrim.end = Math.min(_cnTrim.dur, Math.max(Number(sE.value) || 0, _cnTrim.start + CN_TRIM_MIN));
          paint();
          try { const v = document.getElementById('cn-edit-vid') || document.getElementById('cn-prev-vid'); if (v) v.currentTime = Math.max(0, _cnTrim.end - 0.5); } catch {}
        } catch {}
      };
      const dl = document.getElementById('cn-trim-dl');
      if (dl) dl.onclick = () => { try { cnTrimAction(r, seq, 'video'); } catch {} };
      const cp = document.getElementById('cn-trim-copy');
      if (cp) cp.onclick = () => { try { cnTrimAction(r, seq, 'copy'); } catch {} };
      const gf = document.getElementById('cn-trim-gif');
      if (gf) gf.onclick = () => { try { cnTrimAction(r, seq, 'gif'); } catch {} };
      const sv = document.getElementById('cn-trim-save');
      if (sv) sv.onclick = () => { try { cnTrimAction(r, seq, 'save'); } catch {} };
      const cached = _cnTrimDurCache[r.id];
      const gotDur = (d) => {
        try {
          if (_cnTrim.seq !== seq) return;
          if (!(d && isFinite(d) && d > CN_TRIM_MIN)) { cnTrimStatus('Duration unknown - trim unavailable'); return; }
          try { _cnTrimDurCache[r.id] = d; } catch {}
          _cnTrim.dur = d; _cnTrim.start = 0; _cnTrim.end = d;
          enable(true); paint();
        } catch {}
      };
      if (cached && isFinite(cached) && cached > 0) gotDur(cached);
      else if (r.blob) { try { clipProbeDuration(r.blob).then(gotDur).catch(() => { try { if (_cnTrim.seq === seq) cnTrimStatus('Duration unknown - trim unavailable'); } catch {} }); } catch {} }
      else cnTrimStatus('No video data');
    } catch {}
  }
  // ---- export progress overlay (floats above everything, cancellable) ----
  let _cnExpOn = false, _cnExpCancel = false;
  function cnExportShow(title) {
    try {
      _cnExpOn = true; _cnExpCancel = false;
      let ov = document.getElementById('cn-export');
      if (!ov) {
        ov = document.createElement('div');
        ov.id = 'cn-export';
        Object.assign(ov.style, { position: 'fixed', left: '50%', top: '18px', transform: 'translateX(-50%)', zIndex: '2147483647', display: 'none', flexDirection: 'column', gap: '8px', background: '#111', color: '#fff', border: '1px solid rgb(255 255 255 / 14%)', borderRadius: '12px', boxShadow: '0 12px 48px rgb(0 0 0 / 60%)', fontFamily: 'Inter,"Segoe UI",system-ui,-apple-system,sans-serif', padding: '12px 14px', width: '300px', maxWidth: '90vw' });
        ov.innerHTML = `<b id="cn-exp-title" style="font-size:13px">Exporting…</b>`
          + `<div class="cn-meter" style="height:10px"><div id="cn-exp-fill" class="cn-meter-fill"></div></div>`
          + `<div id="cn-exp-label" style="font-size:11px;color:#9ca3af">starting…</div>`
          + `<button id="cn-exp-cancel" class="cn-ghost" style="align-self:flex-end">Cancel</button>`;
        ['mousedown', 'click', 'keydown'].forEach((ev) => { try { ov.addEventListener(ev, (e) => e.stopPropagation()); } catch {} });
        (document.body || document.documentElement).appendChild(ov);
        const c = ov.querySelector('#cn-exp-cancel');
        if (c) c.onclick = (e) => { try { if (e && e.stopPropagation) e.stopPropagation(); } catch {} try { _cnExpCancel = true; cnExportProg(null, 'Cancelling…'); } catch {} };
      }
      ov.style.display = 'flex';
      cnExportProg(0, title || 'Exporting…');
      try { const t = document.getElementById('cn-exp-title'); if (t) t.textContent = title || 'Exporting…'; } catch {}
    } catch {}
  }
  function cnExportProg(pct, label) {
    try {
      if (!_cnExpOn) return;
      const f = document.getElementById('cn-exp-fill');
      if (f && pct != null && isFinite(pct)) f.style.width = Math.max(0, Math.min(100, Math.round(pct * 100))) + '%';
      const l = document.getElementById('cn-exp-label');
      if (l && label != null) l.textContent = String(label);
    } catch {}
  }
  function cnExportHide() {
    try {
      _cnExpOn = false;
      const ov = document.getElementById('cn-export');
      if (ov) ov.style.display = 'none';
    } catch {}
  }
  async function cnTrimAction(r, seq, kind) {
    // Shared export flow: lock UI, produce blob via the chosen path, then
    // download or store. Stale renders (seq mismatch) abort silently.
    try {
      if (_cnTrim.seq !== seq || _cnTrim.busy || _cnTrim.id !== r.id) return;
      const start = Math.max(0, Number(_cnTrim.start) || 0);
      const end = Math.min(_cnTrim.dur, Math.max(Number(_cnTrim.end) || 0, start + CN_TRIM_MIN));
      if (!(end - start >= CN_TRIM_MIN)) { try { toast('Trim range too short', true); } catch {} return; }
      if (kind === 'gif' && end - start > CN_TRIM_GIF_MAX) { try { toast(`GIF limited to ${CN_TRIM_GIF_MAX}s - shorten the range`, true); } catch {} return; }
      _cnTrim.busy = true;
      try { _cnExportErr = ''; } catch {}
      try { cnExportShow(kind === 'gif' ? 'Encoding GIF' : (kind === 'copy' ? 'Copying clip' : 'Exporting video')); cnExportProg(0, 'starting…'); } catch {}
      try {
        ['cn-trim-start', 'cn-trim-end', 'cn-trim-dl', 'cn-trim-copy', 'cn-trim-gif', 'cn-trim-save'].forEach((id) => {
          const el = document.getElementById(id);
          if (el) el.disabled = true;
        });
      } catch {}
      try { cnSfx('tap'); } catch {}
      let blob = null;
      try {
        if (kind === 'gif') {
          cnTrimStatus(`Encoding GIF… 0/${Math.round((end - start) * CN_GIF_FPS)} frames`);
          blob = await cnTrimExportGif(r, start, end, seq);
        } else {
          cnTrimStatus('Exporting video - keep this tab audible, you will hear the clip…');
          blob = await cnTrimExportVideo(r, start, end);
        }
      } catch { blob = null; }
      try { cnExportHide(); } catch {}
      try { if (_cnExpCancel) { try { toast('Export cancelled'); } catch {} paint0(); return; } } catch { paint0(); return; }
      try { if (_cnTrim.seq !== seq) return; } catch { return; }
      _cnTrim.busy = false;
      try {
        ['cn-trim-start', 'cn-trim-end', 'cn-trim-dl', 'cn-trim-copy', 'cn-trim-gif', 'cn-trim-save'].forEach((id) => {
          const el = document.getElementById(id);
          if (el) el.disabled = false;
        });
      } catch {}
      if (!blob || !blob.size) { try { toast(_cnExportErr || 'Export failed - try again', true); } catch {} paint0(); return; }
      const stamp = r.ts || Date.now();
      if (kind === 'copy') {
        try {
          if (typeof ClipboardItem !== 'undefined' && navigator.clipboard && navigator.clipboard.write) {
            await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'video/webm']: blob })]);
            try { toast('Copied - paste it into Discord', false); } catch {}
            try { cnSfx('ok'); } catch {}
          } else { try { toast('Copy not supported here - download instead', true); } catch {} }
        } catch { try { toast('Copy blocked by the browser - download instead', true); } catch {} }
        paint0();
        return;
      }
      if (kind === 'save') {
        try {
          const row = await clipStore({ trigger: 'trim', mode: r.mode || null, layout: r.layout || null }, blob);
          if (row) {
            try { cnClips.unshift(row); } catch {}
            try { toast('Trim saved to library', false); } catch {}
            try { cnSfx('ok'); } catch {}
            try { if (cnView === 'lib') fetchCnClips(); } catch {}
          } else { try { toast('Could not save trim', true); } catch {} }
        } catch { try { toast('Could not save trim', true); } catch {} }
        paint0();
        return;
      }
      const secs = Number(end - start).toFixed(1);
      if (kind === 'gif') cnDownloadBlob(blob, `clippednow-trim-${stamp}.gif`, `GIF (${secs}s)`);
      else cnDownloadBlob(blob, `clippednow-trim-${stamp}.${clipExt(blob.type)}`, `trimmed clip (${secs}s)`);
      paint0();
      function paint0() { try { if (_cnTrim.seq === seq) cnTrimRefresh(); } catch {} }
    } catch {}
  }
  function cnTrimExportVideo(r, start, end) {
    // Realtime re-record of [start, end) through a hidden media element.
    // Audible by design: muting the element would mute the captured track
    // in Chromium, so we warn and let the clip play out loud. Always 1x
    // speed. Play/stall failures fail fast with a reason instead of a
    // silent frozen-frame clip.
    return new Promise((res) => {
      const done = (b) => { try { res(b); } catch {} };
      try {
        const url = URL.createObjectURL(r.blob);
        const v = document.createElement('video');
        v.preload = 'auto';
        try { v.playsInline = true; } catch {}
        try { v.style.cssText = 'position:fixed;left:-9999px;top:0;width:320px;height:240px;'; } catch {}
        (document.body || document.documentElement).appendChild(v);
        let rec = null;
        const chunks = [];
        let finished = false;
        const cleanup = () => {
          try { v.pause(); } catch {}
          try { v.removeAttribute('src'); } catch {}
          try { v.load(); } catch {}
          try { v.remove(); } catch {}
          try { URL.revokeObjectURL(url); } catch {}
        };
        const fail = (msg) => { if (finished) return; finished = true; try { if (msg) _cnExportErr = String(msg).slice(0, 120); } catch {} try { if (rec && rec.state !== 'inactive') rec.stop(); } catch {} cleanup(); done(null); };
        let safetyT = null, watchT = null;
        try {
          v.onloadedmetadata = () => {
            try {
              const stream = v.captureStream ? v.captureStream() : (v.mozCaptureStream ? v.mozCaptureStream() : null);
              if (!stream) { fail(); return; }
              let mime = clipPickMimeExport((stream.getAudioTracks() || []).length > 0);
              const recStream = stream;
              const opts = { videoBitsPerSecond: 5000000 };
              if ((recStream.getAudioTracks() || []).length > 0) opts.audioBitsPerSecond = 128000;
              if (mime) opts.mimeType = mime;
              try { rec = new MediaRecorder(recStream, opts); } catch (e) { try { rec = new MediaRecorder(recStream); } catch (e2) { fail(); return; } }
              rec.ondataavailable = (e) => { try { if (e.data && e.data.size) chunks.push(e.data); } catch {} };
              rec.onstop = () => {
                if (finished) return; finished = true;
                try { if (watchT) clearInterval(watchT); } catch {}
                try { if (safetyT) clearTimeout(safetyT); } catch {}
                try {
                  const type = rec.mimeType || mime || 'video/webm';
                  const blob = chunks.length ? new Blob(chunks, { type }) : null;
                  // A few KB means the encoder never got real frames (play
                  // blocked, black track, muxer hiccup) - say so instead of
                  // handing over a file that glitches in VLC/Chrome.
                  if (!blob || blob.size < 4096) { cleanup(); done(null); _cnExportErr = _cnExportErr || 'Export came out empty - retry'; return; }
                  done(blob);
                } catch { done(null); }
                try { stream.getTracks().forEach((t) => { try { t.stop(); } catch {} }); } catch {}
                cleanup();
              };
              const begin = () => {
                // Order matters: play FIRST, start the recorder only once
                // frames actually flow ('playing'). Starting the recorder on
                // a not-yet-playing element writes leader garbage that some
                // players never recover from. No timeslice either: one blob
                // on stop, no chunk-boundary tails on short clips.
                let started = false;
                const startRec = () => {
                  try {
                    if (finished || started) return;
                    started = true;
                    if (!rec || rec.state !== 'inactive') { fail(); return; }
                    rec.start();
                  } catch { fail(); return; }
                };
                try {
                  const onPlaying = () => { try { v.removeEventListener('playing', onPlaying); } catch {} startRec(); };
                  v.addEventListener('playing', onPlaying);
                } catch {}
                try {
                  const pr = v.play();
                  if (pr && pr.then) {
                    pr.then(() => { /* 'playing' event starts the recorder */ }).catch(() => { fail('Playback blocked - click the game page once, then retry'); });
                    setTimeout(() => { try { if (!finished && !started && !v.paused) startRec(); } catch {} }, 2500);
                  } else { startRec(); }
                } catch { fail('Playback blocked - click the game page once, then retry'); return; }
                try {
                  let lastT = -1, stallN = 0, ticks = 0;
                  const t0 = Date.now();
                  const expectMs = (end - start) * 1000;
                  watchT = setInterval(() => {
                    try {
                      if (finished || _cnExpCancel) { try { clearInterval(watchT); } catch {} if (_cnExpCancel && !finished) fail(); return; }
                      ticks++;
                      if (v.paused && !v.ended && ticks > 30) { fail('Playback blocked - click the game page once, then retry'); return; }
                      if (!v.paused && !v.ended) {
                        if (lastT >= 0 && Math.abs(v.currentTime - lastT) < 0.0005) {
                          if (++stallN >= 25) { fail('Export stalled - retry'); return; }
                        } else { stallN = 0; lastT = v.currentTime; }
                      }
                      try {
                        if (cnView === 'edit' || _cnExpOn) cnExportProg(Math.min(0.99, (Date.now() - t0) / Math.max(1, expectMs)), 'Recording…');
                      } catch {}
                      if (started && (v.currentTime >= end || v.ended)) {
                        try { clearInterval(watchT); } catch {}
                        try { v.pause(); } catch {}
                        // Audio tail: give the encoder a beat to flush or
                        // the last ~200ms goes silent/choppy in some players.
                        setTimeout(() => { try { if (!finished && rec && rec.state === 'recording') rec.stop(); } catch {} }, 300);
                      }
                    } catch {}
                  }, 100);
                } catch {}
                try { safetyT = setTimeout(() => { try { if (!finished && rec && rec.state !== 'inactive') rec.stop(); } catch {} }, Math.max(8000, (end - start) * 1000 + 10000)); } catch {}
              };
              try {
                let seeked = false;
                const onSeek = () => { if (seeked) return; seeked = true; try { v.removeEventListener('seeked', onSeek); } catch {} begin(); };
                v.addEventListener('seeked', onSeek);
                try { v.currentTime = Math.max(0, start); } catch { begin(); return; }
                setTimeout(() => { if (!seeked) { seeked = true; try { v.removeEventListener('seeked', onSeek); } catch {} begin(); } }, 3000);
              } catch { begin(); }
            } catch { fail(); }
          };
        } catch { fail(); return; }
        v.onerror = () => fail();
        v.src = url;
        setTimeout(() => fail(), 120000);
      } catch { done(null); }
    });
  }
  function cnSeekTo(v, t) {
    // Resolve once the media element lands on t (or after a timeout, with
    // whatever frame is showing - a slightly-off frame beats a stuck export).
    return new Promise((res) => {
      let out = false;
      const fin = () => { if (out) return; out = true; try { v.removeEventListener('seeked', fin); } catch {} res(); };
      try {
        v.addEventListener('seeked', fin);
        try { v.currentTime = t; } catch { fin(); return; }
        setTimeout(fin, 2000);
      } catch { fin(); }
    });
  }
  function cnGifQuantize(px, npix) {
    // Median-cut to <=256 colors on a pixel sample, then nearest-color map
    // with an exact-hit cache (game footage has big flat areas: high hits).
    // Returns { pal, idx, nPal } or null. Pure math, no DOM.
    try {
      const step = Math.max(1, Math.floor(npix / 9000));
      const samples = [];
      for (let i = 0; i < npix; i += step) samples.push([px[i * 4], px[i * 4 + 1], px[i * 4 + 2]]);
      if (!samples.length) return null;
      let boxes = [samples];
      let guard = 0;
      while (boxes.length < 256 && guard++ < 300) {
        let bi = -1, bc = 0, br = -1;
        for (let b = 0; b < boxes.length; b++) {
          const s = boxes[b];
          if (s.length < 2) continue;
          const mn = [255, 255, 255], mx = [0, 0, 0];
          for (let k = 0; k < s.length; k++) {
            const p = s[k];
            for (let c = 0; c < 3; c++) { if (p[c] < mn[c]) mn[c] = p[c]; if (p[c] > mx[c]) mx[c] = p[c]; }
          }
          for (let c = 0; c < 3; c++) { const rg = mx[c] - mn[c]; if (rg > br) { br = rg; bi = b; bc = c; } }
        }
        if (bi < 0 || br <= 0) break;
        const s = boxes[bi].slice().sort((a, b) => a[bc] - b[bc]);
        const mid = Math.max(1, s.length >> 1);
        boxes[bi] = s.slice(0, mid);
        boxes.push(s.slice(mid));
      }
      const pal = [];
      for (const s of boxes) {
        if (!s.length) continue;
        let R = 0, G = 0, B = 0;
        for (let k = 0; k < s.length; k++) { R += s[k][0]; G += s[k][1]; B += s[k][2]; }
        pal.push([Math.round(R / s.length), Math.round(G / s.length), Math.round(B / s.length)]);
      }
      if (!pal.length) return null;
      const nPal = pal.length;
      while (pal.length < 256) pal.push([0, 0, 0]);
      const cache = new Map();
      const idx = new Uint8Array(npix);
      for (let i = 0; i < npix; i++) {
        const R = px[i * 4], G = px[i * 4 + 1], B = px[i * 4 + 2];
        const key = (R << 16) | (G << 8) | B;
        let m = cache.get(key);
        if (m === undefined) {
          let best = 0, bd = 1073741824;
          for (let p = 0; p < nPal; p++) {
            const c = pal[p];
            const dr = R - c[0], dg = G - c[1], db = B - c[2];
            const d = dr * dr + dg * dg + db * db;
            if (d < bd) { bd = d; best = p; if (!d) break; }
          }
          m = best;
          if (cache.size < 20000) cache.set(key, m);
        }
        idx[i] = m;
      }
      return { pal, idx, nPal };
    } catch { return null; }
  }
  function cnGifLzw(minSize, idx) {
    // GIF-spec LZW compressor: clear at start, DEFERRED code-size growth
    // (bump after next passes 2^size, not when it reaches it - the decoder
    // only learns each new table entry one code later than the encoder, so
    // bumping early emits a wide code the decoder still reads narrow),
    // clear + reset on table-full. Returns a plain byte array.
    try {
      const clear = 1 << minSize, eoi = clear + 1;
      let codeSize = minSize + 1;
      let next = eoi + 1;
      const dict = new Map();
      const out = [];
      let acc = 0, bits = 0;
      const emit = (code) => {
        acc |= code << bits; bits += codeSize;
        while (bits >= 8) { out.push(acc & 255); acc >>= 8; bits -= 8; }
      };
      emit(clear);
      let prefix = idx[0];
      for (let i = 1; i < idx.length; i++) {
        const k = idx[i];
        const key = prefix * 256 + k;
        const hit = dict.get(key);
        if (hit !== undefined) { prefix = hit; continue; }
        emit(prefix);
        if (next >= 4096) {
          emit(clear);
          dict.clear(); next = eoi + 1; codeSize = minSize + 1;
        } else {
          dict.set(key, next); next++;
          if (next === (1 << codeSize) + 1 && codeSize < 12) codeSize++;
        }
        prefix = k;
      }
      emit(prefix); emit(eoi);
      if (bits > 0) out.push(acc & 255);
      return out;
    } catch { return null; }
  }
  function cnGifEncode(frames, w, h, delayCs) {
    // frames: [{ pal (256 entries), idx }]. Global table = frame 0 palette,
    // each frame also carries its exact local table. Loop forever.
    try {
      if (!frames || !frames.length) return null;
      const out = [];
      const wstr = (v) => { out.push(v & 255, (v >> 8) & 255); };
      out.push(0x47, 0x49, 0x46, 0x38, 0x39, 0x61); // GIF89a
      wstr(w); wstr(h);
      out.push(0xF7, 0x00, 0x00); // GCT flag, 8-bit color, 256-entry table
      const g0 = frames[0].pal;
      for (let i = 0; i < 256; i++) { const c = g0[i] || [0, 0, 0]; out.push(c[0] & 255, c[1] & 255, c[2] & 255); }
      out.push(0x21, 0xFF, 0x0B); // NETSCAPE2.0 loop extension
      const app = 'NETSCAPE2.0';
      for (let i = 0; i < app.length; i++) out.push(app.charCodeAt(i));
      out.push(0x03, 0x01, 0x00, 0x00, 0x00);
      for (const f of frames) {
        out.push(0x21, 0xF9, 0x04, 0x04); // GCE: no disposal
        wstr(delayCs);
        out.push(0x00, 0x00);
        out.push(0x2C); // image descriptor, full frame
        wstr(0); wstr(0); wstr(w); wstr(h);
        out.push(0x87); // local table flag + 256 entries
        for (let i = 0; i < 256; i++) { const c = f.pal[i] || [0, 0, 0]; out.push(c[0] & 255, c[1] & 255, c[2] & 255); }
        out.push(0x08); // LZW min code size (8-bit indices)
        const comp = cnGifLzw(8, f.idx);
        if (!comp) return null;
        for (let i = 0; i < comp.length; i += 255) {
          const n = Math.min(255, comp.length - i);
          out.push(n);
          for (let j = 0; j < n; j++) out.push(comp[i + j]);
        }
        out.push(0x00);
      }
      out.push(0x3B);
      return new Blob([new Uint8Array(out)], { type: 'image/gif' });
    } catch { return null; }
  }
  async function cnTrimExportGif(r, start, end, seq) {
    // Seek-step the range at CN_GIF_FPS, downscale to CN_GIF_W, quantize +
    // encode. Aborts (null) if the preview moved on to another clip.
    try {
      const fps = CN_GIF_FPS;
      const span = Math.min(end - start, CN_TRIM_GIF_MAX);
      if (!(span > 0)) return null;
      const url = URL.createObjectURL(r.blob);
      const v = document.createElement('video');
      v.muted = true; v.preload = 'auto';
      try { v.playsInline = true; } catch {}
      (document.body || document.documentElement).appendChild(v);
      const cleanup = () => {
        try { v.pause(); } catch {}
        try { v.removeAttribute('src'); } catch {}
        try { v.load(); } catch {}
        try { v.remove(); } catch {}
        try { URL.revokeObjectURL(url); } catch {}
      };
      const meta = await new Promise((ok) => {
        let done = false;
        const fin = (good) => { if (done) return; done = true; ok(good); };
        try {
          v.onloadedmetadata = () => fin(true);
          v.onerror = () => fin(false);
          v.src = url;
          setTimeout(() => fin(false), 15000);
        } catch { fin(false); }
      });
      if (!meta) { cleanup(); return null; }
      const vw = v.videoWidth || 640, vh = v.videoHeight || 360;
      const w = Math.max(2, Math.min(CN_GIF_W, vw));
      const h = Math.max(2, Math.round((vh * w) / vw));
      const cv = document.createElement('canvas');
      cv.width = w; cv.height = h;
      let gx = null;
      try { gx = cv.getContext('2d', { willReadFrequently: true }); } catch {}
      if (!gx) { try { gx = cv.getContext('2d'); } catch {} }
      if (!gx) { cleanup(); return null; }
      const n = Math.max(1, Math.round(span * fps));
      const frames = [];
      for (let k = 0; k < n; k++) {
        try { if (_cnTrim.seq !== seq) { cleanup(); return null; } } catch { cleanup(); return null; }
        try { if (_cnExpCancel) { cleanup(); return null; } } catch { cleanup(); return null; }
        const t = Math.min(start + k / fps, start + span - 0.05);
        try { await cnSeekTo(v, Math.max(0, t)); } catch {}
        try { gx.drawImage(v, 0, 0, w, h); } catch { continue; }
        let d = null;
        try { d = gx.getImageData(0, 0, w, h); } catch { continue; }
        if (!d) continue;
        const q = cnGifQuantize(d.data, w * h);
        if (q) frames.push(q);
        try {
          if (k % 5 === 4 || k === n - 1) cnTrimStatus(`Encoding GIF… ${k + 1}/${n} frames`);
          try { cnExportProg((k + 1) / n, `Encoding GIF… ${k + 1}/${n} frames`); } catch {}
          await new Promise((rr) => setTimeout(rr, 0)); // let the panel breathe
        } catch {}
      }
      cleanup();
      if (!frames.length) return null;
      return cnGifEncode(frames, w, h, Math.round(100 / fps));
    } catch { return null; }
  }
  // ---- editor view (timeline + filmstrip + trim + export) ----
  // Timeline with draggable handles over a seek-built filmstrip, the same
  // start/end sliders as fallback, and all four export actions (video,
  // copy, GIF, save to library). Trim state (_cnTrim) is shared, so
  // cnTrimBind/cnTrimAction work here unchanged.
  let _cnEditId = null, _cnFilmSeq = 0, _cnTlT = 0;
  const _cnFilmCache = new Map();
  function cnFilmPaint(box, thumbs) {
    try {
      if (!box) return;
      box.innerHTML = (thumbs || []).map((t) => `<img src="${t}" style="flex:1;min-width:0;height:100%;object-fit:cover" draggable="false">`).join('');
    } catch {}
  }
  async function cnFilmBuild(r, dur, seq) {
    // 10 thumbnails via a hidden video: seek, draw small, JPEG dataURL.
    // Cached per clip (max 3) so reopening is instant.
    try {
      const box = document.getElementById('cn-tl-film');
      if (!box) return;
      const key = r.id + ':' + Math.round(dur);
      if (_cnFilmCache.has(key)) { cnFilmPaint(box, _cnFilmCache.get(key)); return; }
      box.innerHTML = `<div style="font-size:10px;color:#6b7280;margin:auto">building filmstrip…</div>`;
      const url = URL.createObjectURL(r.blob);
      const v = document.createElement('video');
      v.muted = true; v.preload = 'auto';
      try { v.style.cssText = 'position:fixed;left:-9999px;top:0;width:160px;height:90px;'; } catch {}
      (document.body || document.documentElement).appendChild(v);
      const cleanup = () => {
        try { v.pause(); } catch {}
        try { v.removeAttribute('src'); } catch {}
        try { v.load(); } catch {}
        try { v.remove(); } catch {}
        try { URL.revokeObjectURL(url); } catch {}
      };
      const metaOk = await new Promise((ok) => {
        let done = false;
        const fin = (g) => { if (done) return; done = true; ok(g); };
        try {
          v.onloadedmetadata = () => fin(true);
          v.onerror = () => fin(false);
          v.src = url;
          setTimeout(() => fin(false), 12000);
        } catch { fin(false); }
      });
      if (!metaOk || seq !== _cnFilmSeq) { cleanup(); return; }
      const N = 10, tw = 96, th = 54;
      const cv = document.createElement('canvas');
      cv.width = tw; cv.height = th;
      const gx = cv.getContext('2d');
      if (!gx) { cleanup(); return; }
      const thumbs = [];
      for (let k = 0; k < N; k++) {
        if (seq !== _cnFilmSeq) { cleanup(); return; }
        try { await cnSeekTo(v, Math.min(Math.max(0, (dur * (k + 0.5)) / N), Math.max(0, dur - 0.1))); } catch {}
        try { gx.drawImage(v, 0, 0, tw, th); } catch { continue; }
        try { thumbs.push(cv.toDataURL('image/jpeg', 0.55)); } catch {}
        try { await new Promise((rr) => setTimeout(rr, 0)); } catch {}
      }
      cleanup();
      if (seq !== _cnFilmSeq || !thumbs.length) return;
      try {
        _cnFilmCache.set(key, thumbs);
        if (_cnFilmCache.size > 3) { try { _cnFilmCache.delete(_cnFilmCache.keys().next().value); } catch {} }
      } catch {}
      try { const b2 = document.getElementById('cn-tl-film'); if (b2) cnFilmPaint(b2, thumbs); } catch {}
    } catch {}
  }
  function cnTlPaint() {
    // Handles + selection + playhead from _cnTrim + editor video time.
    try {
      const t = _cnTrim, dur = t.dur;
      if (!(dur > 0)) return;
      const bar = document.getElementById('cn-tl-bar');
      if (!bar || !document.contains(bar)) return;
      const pct = (x) => Math.max(0, Math.min(100, (x / dur) * 100));
      const s = pct(t.start), e = pct(t.end);
      const h0 = document.getElementById('cn-tl-h0'), h1 = document.getElementById('cn-tl-h1');
      const sel = document.getElementById('cn-tl-sel'), ph = document.getElementById('cn-tl-ph');
      if (h0) h0.style.left = s + '%';
      if (h1) h1.style.left = e + '%';
      if (sel) { sel.style.left = s + '%'; sel.style.width = Math.max(0, e - s) + '%'; }
      const tm = document.getElementById('cn-tl-times');
      if (tm) tm.textContent = `${Number(t.start).toFixed(1)}s → ${Number(t.end).toFixed(1)}s (${Math.max(0, t.end - t.start).toFixed(1)}s)`;
      try {
        const v = document.getElementById('cn-edit-vid');
        if (v && ph && isFinite(v.currentTime)) ph.style.left = pct(v.currentTime) + '%';
      } catch {}
    } catch {}
  }
  function cnTlBind() {
    try {
      try { if (_cnTlT) clearInterval(_cnTlT); } catch {}
      _cnTlT = 0;
      const bar = document.getElementById('cn-tl-bar');
      if (!bar) return;
      const tOf = (clientX) => {
        try {
          const rc = bar.getBoundingClientRect();
          const f = Math.max(0, Math.min(1, (clientX - rc.left) / Math.max(1, rc.width)));
          return f * (_cnTrim.dur || 0);
        } catch { return 0; }
      };
      const seekV = (t) => { try { const v = document.getElementById('cn-edit-vid'); if (v) { try { v.pause(); } catch {} v.currentTime = Math.max(0, t); } } catch {} };
      const dragWire = (id, isStart) => {
        const h = document.getElementById(id);
        if (!h || h.__cnTl) return;
        try { h.__cnTl = true; } catch {}
        h.addEventListener('pointerdown', (e) => {
          try { e.stopPropagation(); e.preventDefault(); } catch {}
          try { h.setPointerCapture(e.pointerId); } catch {}
          const move = (ev) => {
            try {
              const t = tOf(ev.clientX);
              if (!(_cnTrim.dur > 0) || _cnTrim.busy) return;
              if (isStart) _cnTrim.start = Math.max(0, Math.min(t, _cnTrim.end - CN_TRIM_MIN));
              else _cnTrim.end = Math.min(_cnTrim.dur, Math.max(t, _cnTrim.start + CN_TRIM_MIN));
              try { cnTrimRefresh(); } catch {}
              try { cnTlPaint(); } catch {}
              seekV(isStart ? _cnTrim.start : Math.max(0, _cnTrim.end - 0.3));
            } catch {}
          };
          const up = () => {
            try { h.removeEventListener('pointermove', move); } catch {}
            try { h.removeEventListener('pointerup', up); } catch {}
            try { h.removeEventListener('pointercancel', up); } catch {}
          };
          try { h.addEventListener('pointermove', move); } catch {}
          try { h.addEventListener('pointerup', up); } catch {}
          try { h.addEventListener('pointercancel', up); } catch {}
        });
      };
      dragWire('cn-tl-h0', true);
      dragWire('cn-tl-h1', false);
      if (!bar.__cnTl) {
        try { bar.__cnTl = true; } catch {}
        bar.addEventListener('pointerdown', (e) => {
          try {
            if (e.target && (e.target.id === 'cn-tl-h0' || e.target.id === 'cn-tl-h1')) return;
            seekV(tOf(e.clientX));
          } catch {}
        });
      }
      _cnTlT = setInterval(() => { try { if (cnView !== 'edit') return; cnTlPaint(); } catch {} }, 250);
    } catch {}
  }
  function cnEditLeave() {
    try { if (_cnTlT) { clearInterval(_cnTlT); _cnTlT = 0; } } catch {}
    try { _cnFilmSeq++; } catch {}
  }
  function renderEdit() {
    try {
      const e = cnEls();
      if (!e.edit) return;
      const r = (cnClips || []).find((x) => x.id === cnPrevId) || null;
      if (!r) { cnShow('lib'); return; }
      _cnEditId = r.id;
      void _cnEditId;
      e.edit.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.08)">`
        + `<div style="min-width:0"><b style="font-size:15px;color:#fff">✂ Editor</b><div style="font-size:11px;color:#6b7280;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc((r.trigger || 'clip').toUpperCase())} · ${esc(cnWhen(r.ts))} · ${cnKB(r.size)}</div></div>`
        + `<button id="cn-edit-close" class="cn-ghost">← Preview</button></div>`
        + `<div class="cn-mbody">`
        + `<video id="cn-edit-vid" controls muted playsinline style="background:#000;border-radius:8px;${r.layout === 'vertical' ? 'aspect-ratio:9/16;max-height:34vh;width:auto;max-width:100%;margin:0 auto;display:block;' : 'width:100%;aspect-ratio:16/9;'}"></video>`
        + `<div style="display:flex;gap:8px;align-items:center;margin-top:6px"><button id="cn-edit-mute" class="cn-ghost" style="flex:0 0 auto">🔇 muted</button><span style="font-size:10px;color:#6b7280">unmute to audition game audio</span></div>`
        + `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:10px 0 4px">TIMELINE</div>`
        + `<div id="cn-tl-bar" class="cn-tl"><div id="cn-tl-film"></div><div id="cn-tl-sel"></div><div id="cn-tl-h0" class="cn-tl-h" title="Trim start"></div><div id="cn-tl-h1" class="cn-tl-h" title="Trim end"></div><div id="cn-tl-ph"></div></div>`
        + `<div id="cn-tl-times" style="font-size:11px;color:#9ca3af;margin-top:4px">…</div>`
        + `<div style="font-size:10px;color:#6b7280;margin-top:2px">drag handles · click bar to seek</div>`
        + cnTrimHtml()
        + `</div>`;
      cnRevokeUrls();
      try {
        const v = document.getElementById('cn-edit-vid');
        if (v && r.blob) v.src = cnTrackUrl(URL.createObjectURL(r.blob));
        if (v) v.addEventListener('timeupdate', () => { try { if (cnView === 'edit') cnTlPaint(); } catch {} });
      } catch {}
      const mute = document.getElementById('cn-edit-mute');
      if (mute) mute.onclick = (e) => {
        try {
          if (e && e.stopPropagation) e.stopPropagation();
          const v = document.getElementById('cn-edit-vid');
          if (!v) return;
          v.muted = !v.muted;
          mute.textContent = v.muted ? '🔇 muted' : '🔊 sound on';
        } catch {}
      };
      const close = document.getElementById('cn-edit-close');
      if (close) close.onclick = () => { try { cnShow('prev'); } catch {} };
      try { cnTrimBind(r); } catch {}
      try { cnTlBind(); } catch {}
      try { cnTlPaint(); } catch {}
      const fs = ++_cnFilmSeq;
      const waitDur = setInterval(() => {
        try {
          if (fs !== _cnFilmSeq || cnView !== 'edit') { try { clearInterval(waitDur); } catch {} return; }
          if (_cnTrim.dur > 0 && _cnTrim.id === r.id) { try { clearInterval(waitDur); } catch {} cnFilmBuild(r, _cnTrim.dur, fs); }
        } catch { try { clearInterval(waitDur); } catch {} }
      }, 400);
    } catch {}
  }
  // ---- preview panel: big preview + favorite / download / delete ----
  function renderPrev() {
    try {
      const e = cnEls();
      if (!e.prev) return;
      const r = (cnClips || []).find(x => x.id === cnPrevId) || null;
      if (!r) { cnShow('lib'); return; }
      const star = r.fav ? '★' : '☆';
      const lay = r.layout === 'vertical' ? ' · 9:16' : '';
      const html = `<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.08)">`
        + `<div style="display:flex;align-items:center;gap:8px;min-width:0"><span style="font-size:10px;font-weight:800;color:#fff;background:${cnBadge(r.trigger)};border-radius:4px;padding:2px 6px;flex-shrink:0">${esc((r.trigger || 'manual').toUpperCase())}</span>`
        + `<span style="font-size:12px;color:#9ca3af;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(cnWhen(r.ts))}</span></div>`
        + `<button id="cn-prev-fav" class="cn-star" title="Favorite" style="font-size:18px">${star}</button></div>`
        + `<div class="cn-mbody">`
        + `<video id="cn-prev-vid" controls autoplay muted playsinline style="background:#000;border-radius:8px;${r.layout === 'vertical' ? 'aspect-ratio:9/16;max-height:46vh;width:auto;max-width:100%;margin:0 auto;display:block;' : 'width:100%;'}"></video>`
        + `<div style="font-size:11px;color:#9ca3af;margin:8px 0">${r.mode ? esc(cnGmLabel(r.mode)) + ' · ' : ''}${cnKB(r.size)}${lay}${(r.trigger !== 'manual' && (r.killer || r.victim)) ? `<br>${esc(r.killer || '?')} → ${esc(r.victim || '?')}` : ''}${r.suspect ? `<br><span style="color:#eab308">⚠ decode check failed - try VLC / download</span>` : ''}</div>`
        + `<div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap"><button id="cn-prev-edit" class="cn-rowbtn">✂ Edit clip</button></div>`
        + `<div style="display:flex;gap:8px;margin-top:8px"><button id="cn-prev-dl" class="cn-rowbtn">Download</button><button id="cn-prev-wh" class="cn-rowbtn" title="Send this clip to Discord">⇪ Discord</button><button id="cn-prev-del" class="cn-ghost">Delete</button></div>`
        + `<button id="cn-prev-back" class="cn-ghost" style="width:100%;margin-top:8px">← Back to clips</button>`
        + `</div>`;
      cnRevokeUrls();
      e.prev.innerHTML = html;
      try {
        const v = document.getElementById('cn-prev-vid');
        if (v && r.blob) v.src = cnTrackUrl(URL.createObjectURL(r.blob));
      } catch {}
      const edt = document.getElementById('cn-prev-edit');
      if (edt) edt.onclick = () => { try { cnSfx('tap'); } catch {} try { cnShow('edit'); } catch {} };
      const fav = document.getElementById('cn-prev-fav');
      if (fav) fav.onclick = () => {
        try { cnSfx('tap'); } catch {}
        clipSetFav(r.id, !r.fav).then((row) => {
          try {
            if (row) { const ix = (cnClips || []).findIndex(x => x.id === r.id); if (ix >= 0) cnClips[ix] = row; }
            if (cnView === 'prev') renderPrev();
          } catch {}
        });
      };
      const dl = document.getElementById('cn-prev-dl');
      if (dl) dl.onclick = () => { try { cnSfx('tap'); } catch {} cnDownload(r); };
      const wh = document.getElementById('cn-prev-wh');
      if (wh) wh.onclick = () => { try { cnSfx('tap'); } catch {} try { cnWhSendOne(r, r.blob); } catch {} };
      const del = document.getElementById('cn-prev-del');
      if (del) del.onclick = () => {
        if (!confirm('Delete this clip?')) return;
        clipDelete(r.id).then(() => {
          try { cnClips = (cnClips || []).filter(x => x.id !== r.id); } catch {}
          cnShow('lib');
        });
      };
      const back = document.getElementById('cn-prev-back');
      if (back) back.onclick = () => { try { cnShow('lib'); } catch {} };
    } catch {}
  }
  // ---- first-start panel: pick capture device settings once ----
  function renderOnb() {
    try {
      const e = cnEls();
      if (!e.onb) return;
      const cfg = clipCfg();
      e.onb.innerHTML = `<div style="padding:16px 16px 0"><b style="font-size:17px;color:#fff">Welcome to ClippedNow.gg</b>`
        + `<div style="font-size:12px;color:#9ca3af;margin-top:4px;line-height:1.6">Pick your capture settings once - recording starts automatically when the game connects. 100% local, nothing leaves this PC.</div></div>`
        + `<div class="cn-mbody">`
        + cnSettingsHtml(cfg, 'ob')
        + `<button id="cn-onb-go" class="cn-rowbtn" style="width:100%;margin-top:12px">● Start clipping</button>`
        + `<button id="cn-onb-skip" style="width:100%;margin-top:8px;background:none;border:0;color:#6b7280;font-size:11px;cursor:pointer;text-decoration:underline">Skip with defaults</button>`
        + `</div>`;
      try { cnBindSettings('ob'); } catch {}
      try { cnBindKeys('ob'); } catch {}
      try { cnHwBind(); } catch {}
      const go = document.getElementById('cn-onb-go');
      if (go) go.onclick = () => { try { cnOnboardDone(false); } catch {} };
      const skip = document.getElementById('cn-onb-skip');
      if (skip) skip.onclick = () => { try { cnOnboardDone(true); } catch {} };
    } catch {}
  }
  function cnOnboardDone(skipped) {
    try { GMSet('cn_onboarded', true); } catch {}
    try { logInfo(skipped ? 'onboarding skipped - defaults active' : 'capture settings saved'); } catch {}
    try { toast(skipped ? 'Defaults active - change anytime in the panel' : 'Settings saved - happy clipping', false); } catch {}
    try { if (!skipped) cnSfx('ok'); } catch {}
    try { cnHideAll(); } catch {}
    try { clipKickSoon(); } catch {}
  }

  // ---- boot ----
  try { cnHookWS(window); } catch {}
  try { if (typeof unsafeWindow !== 'undefined' && unsafeWindow !== window) cnHookWS(unsafeWindow); } catch {}
  try { clipPatchAudio(); } catch {}
  document.addEventListener('keydown', (e) => {
    try {
      if (cnKeyCap) {
        try { e.preventDefault(); } catch {}
        try { e.stopPropagation(); } catch {}
        try { if (e.stopImmediatePropagation) e.stopImmediatePropagation(); } catch {}
        const k = String(e.key || '');
        if (k && k !== 'Escape') {
          try { const gk = cnKeyCap === 'clip' ? 'cn_key_clip' : (cnKeyCap === 'lib' ? 'cn_key_lib' : 'cn_key_panel'); GMSet(gk, k); try { clipCfgApplyKey(gk, k); } catch {} } catch {}
          try { logInfo(`keybind ${cnKeyCap} -> ${k}`); } catch {}
        }
        try { cnKeyCap = null; } catch {}
        try { cnRenderCurrent(); } catch {}
        return;
      }
      try {
        const t = e.target;
        const tag = t && t.tagName ? String(t.tagName).toLowerCase() : '';
        if (tag === 'input' || tag === 'select' || tag === 'textarea' || (t && t.isContentEditable)) return;
      } catch {}
      const cfg = clipCfg();
      if (e.key === cfg.keyClip && !e.ctrlKey && !e.metaKey) { e.preventDefault(); try { cnSfx('tap'); } catch {} saveClip('manual'); return; }
      if (e.key === cfg.keyPanel && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        try { if (!clipCfg().fab) clipSet('cn_fab_visible', true); } catch {} // hidden button: panel key brings it back
        cnToggle(); return;
      }
      if (e.key === cfg.keyLib && !e.ctrlKey && !e.metaKey && !e.altKey) {
        e.preventDefault();
        try { if (!clipCfg().fab) clipSet('cn_fab_visible', true); } catch {}
        try { if (cnView === 'lib') cnHideAll(); else cnShow('lib'); } catch {}
        return;
      }
      if (e.key === 'Escape' && cnOpen) {
        if (cnView === 'onb') { try { cnOnboardDone(true); } catch {} return; }
        cnBack();
      }
    } catch {}
  }, true);
  setTimeout(() => {
    try { clipCfg(); } catch {} // warm config cache off the hot path
    try { clipVolLoad(); } catch {} // warm volume cache off the hot path
    try { cnEnsureFabWhenReady(); } catch {}
    try { clipEnsureMic(); } catch {}
    try { rsWant(); } catch {}
    try {
      if (document.body && GMGet('cn_onboarded', false) !== true) cnShow('onb');
    } catch {}
    logInfo(`ClippedNow.gg v${CN_VERSION} ready - no account, all local`);
  }, 700);
  setInterval(() => {
    try {
      try { cnHookWS(window); } catch {}
      try { if (typeof unsafeWindow !== 'undefined' && unsafeWindow !== window) cnHookWS(unsafeWindow); } catch {}
      if (rsState === RS_IDLE) { try { rsWant(); } catch {} }
      else if (rsState === RS_RECORDING && !_clip.rec) { try { rsReboot('watchdog-no-rec'); } catch {} }
      else if (rsState === RS_RECORDING && clipCanvasChanged()) { try { rsReboot('watchdog-canvas'); } catch {} }
    } catch {}
  }, 15000);
  // ---- game-FPS governor: notice sustained jank, suggest once ----
  // One chained rAF callback (~5 lines per vsync, negligible): rAF slips
  // exactly when the tab can't keep up, so its deltas ARE the visible
  // framerate. Folded into an average every 2.5s tick; if the game holds
  // under ~25fps for ~30s while capture settings are heavy, toast exactly
  // what to lower - once per page load, never auto-changes settings.
  let _cnFpsLast = 0, _cnFpsAcc = 0, _cnFpsN = 0, _cnFpsAvg = 0, _cnFpsLowWindows = 0, _cnFpsWarned = false;
  function cnFpsTick() {
    try {
      let now = 0;
      try { now = performance.now(); } catch {}
      if (now > 0 && _cnFpsLast > 0 && now > _cnFpsLast && now - _cnFpsLast < 1000) {
        _cnFpsAcc += now - _cnFpsLast; _cnFpsN++;
      }
      if (now > 0) _cnFpsLast = now;
    } catch {}
    try { requestAnimationFrame(cnFpsTick); } catch {}
  }
  function cnFpsGovern() {
    try {
      if (_cnFpsN > 0) {
        _cnFpsAvg = _cnFpsN / Math.max(0.001, _cnFpsAcc / 1000);
        _cnFpsAcc = 0; _cnFpsN = 0;
      } else return;
      if (rsState !== RS_RECORDING) { _cnFpsLowWindows = 0; return; }
      if (!(_cnFpsAvg > 0) || _cnFpsAvg >= 25) { _cnFpsLowWindows = 0; return; }
      _cnFpsLowWindows++;
      if (_cnFpsLowWindows < 12 || _cnFpsWarned) return; // ~30s sustained
      _cnFpsWarned = true;
      let hint = 'lower FPS or Quality a notch';
      try {
        const c = clipCfg();
        if (c.bitrate >= 20000000) hint = 'drop Quality to Ultra or lower';
        else if (c.fps === 'unlimited' || c.fps >= 60) hint = 'drop FPS to 30';
        else if (c.watermark) hint = 'turn off the watermark';
      } catch {}
      try { logInfo(`game running at ~${Math.round(_cnFpsAvg)}fps while recording - ${hint}`); } catch {}
      try { toast(`Game at ~${Math.round(_cnFpsAvg)}fps - ${hint}`, true); } catch {}
    } catch {}
  }
  ['pointerdown', 'keydown', 'touchend'].forEach((ev) => {
    try { document.addEventListener(ev, () => { try { _mixGestureAt = Date.now(); } catch {} try { clipMixResume(); } catch {} }, true); } catch {}
  });
  try { requestAnimationFrame(cnFpsTick); } catch {} // game-FPS governor sampler
  // Live indicator ticker: the dot + status line + audio hot-path track the
  // recorder without opening anything (full re-render would steal focus
  // from open inputs/sliders).
  setInterval(() => {
    try { cnRefreshFab(); } catch {}
    try { cnFpsGovern(); } catch {}
    try { rsAudioCheck(); } catch {}
    try { clipMixRefresh(); } catch {}
    try { clipMixResume(); } catch {}
    try {
      if (cnView === 'main') {
        const el = document.getElementById('cn-rec-state');
        if (el) el.textContent = cnRecState(clipCfg());
      }
    } catch {}
  }, 2500);
})();