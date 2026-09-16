// ==UserScript==
// @name         ClippedNow.gg - BuildNow GG Clipper
// @namespace    https://github.com/jaredv2
// @version      1.3.8
// @description  ClippedNow.gg v1.3.2 - auto-clip your BuildNow GG kills, deaths and wins. 100% local: no account, no server, nothing leaves your PC.
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
  const CN_VERSION = '1.3.8';
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
  function toast(text, isError) {
    try {
      document.querySelectorAll('.cn-toast').forEach(el => el.remove());
      const d = document.createElement('div');
      d.className = 'cn-toast';
      d.textContent = text;
      Object.assign(d.style, { position: 'fixed', top: '16px', right: '16px', zIndex: '2147483647', background: isError ? '#991b1b' : '#000', color: '#fff', borderRadius: '8px', border: '1px solid rgb(255 255 255 / 12%)', boxShadow: '0 4px 16px rgb(0 0 0 / 40%)', fontFamily: 'Inter, system-ui, sans-serif', fontSize: '14px', fontWeight: '500', padding: '12px 16px', maxWidth: '320px' });
      (document.body || document.documentElement).appendChild(d);
      setTimeout(() => { try { d.remove(); } catch {} }, 3000);
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
      if (inferred && inferred !== 'DF' && inferred !== 'unknown') { cnMode.code = inferred; cnMode.at = now; return; }
      if (inferred && !cnMode.code) { cnMode.code = inferred; cnMode.at = now; return; }
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
      const killer = parts[0].replace(/<[^>]*>/g, '').trim();
      const victim = parts[1].replace(/<[^>]*>/g, '').trim();
      if (!killer || !victim) return;
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
  function clipCfg() {
    try {
      const lenRaw = Number(GMGet('cn_clip_len', CN_DEF.len));
      const layoutRaw = String(GMGet('cn_clip_layout', CN_DEF.layout) || 'landscape');
      return {
        enabled: GMGet('cn_clip_enabled', CN_DEF.enabled),
        len: [8, 15, 30, 45, 60].includes(lenRaw) ? lenRaw : CN_DEF.len,
        fps: Number(GMGet('cn_clip_fps', CN_DEF.fps)) || CN_DEF.fps,
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
        sounds: GMGet('cn_sounds', CN_DEF.sounds) !== false,
        keyClip: String(GMGet('cn_key_clip', CN_DEF.keyClip) || 'F8'),
        keyPanel: String(GMGet('cn_key_panel', CN_DEF.keyPanel) || 'F4'),
        keyLib: String(GMGet('cn_key_lib', CN_DEF.keyLib) || 'F9'),
        debug: GMGet('cn_debug', false) === true,
      };
    } catch { return Object.assign({}, CN_DEF); }
  }
  function clipSet(k, v) {
    try { GMSet(k, v); } catch {}
    try { if (k === 'cn_clip_len' || k === 'cn_clip_fps' || k === 'cn_clip_bitrate' || k === 'cn_clip_enabled' || k === 'cn_clip_audio' || k === 'cn_clip_mic' || k === 'cn_clip_watermark' || k === 'cn_clip_layout') { try { clipEnsureMic(); } catch {} rsReboot('settings:' + k); } } catch {}
    try { if (k === 'cn_fab_visible') cnApplyFab(); } catch {}
    try { if (typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {}
  }
  function clipKickSoon() {
    try { if (rsState === RS_IDLE) rsEnter(RS_STARTING); } catch {}
  }
  function clipPickMime(withAudio) {
    try {
      const cands = withAudio
        ? ['video/mp4;codecs="avc1.42E01E,mp4a.40.2"', 'video/webm;codecs=vp8,opus', 'video/webm;codecs=vp9,opus', 'video/webm', 'video/mp4']
        : ['video/mp4;codecs="avc1.42E01E"', 'video/mp4', 'video/webm;codecs=vp8', 'video/webm', 'video/webm;codecs=vp9'];
      for (const m of cands) { try { if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m)) return m; } catch {} }
    } catch {}
    return '';
  }
  function clipExt(mime) { try { if (String(mime || '').includes('mp4')) return 'mp4'; } catch {} return 'webm'; }
  let _clip = { rec: null, stream: null, chunks: [], prev: null, segTimer: null, segStartedAt: 0, finalizing: false, started: false, lastErr: '', mime: '', withAudio: false, wmActive: false, recStartedAt: 0, killAt: 0, deathAt: 0, winAt: 0, pendingTimer: null, pendingTrigger: null, lastKill: null, cw: 0, ch: 0 };
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
  function clipCanvasChanged() {
    try {
      const c = clipFindCanvas();
      if (!c || !_clip.cw || !_clip.ch) return false;
      const w = c.width || 0, h = c.height || 0;
      return w > 0 && h > 0 && (w !== _clip.cw || h !== _clip.ch);
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
  function clipStashForUpgrade(reason) {
    try {
      const chunks = (_clip.chunks || []).slice();
      let bytes = 0;
      for (const c of chunks) bytes += (c && c.size) || 0;
      if (chunks.length && bytes > 0) {
        try { clipStashPrev(chunks, _clip.mime, Date.now() - (_clip.segStartedAt || Date.now())); } catch {}
        try { logInfo(`audio upgrade (${reason}) - stashed ${(bytes / 1024).toFixed(0)}KB for stitch`); } catch {}
      }
    } catch {}
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
  function clipVol(lane) {
    try {
      const v = Number(GMGet(lane === 'mic' ? 'cn_vol_mic' : 'cn_vol_game', 1));
      if (isNaN(v) || v < 0) return 1;
      return Math.max(0, Math.min(2, v));
    } catch { return 1; }
  }
  function clipSetVol(lane, v) {
    try {
      v = Math.max(0, Math.min(2, Number(v)));
      if (isNaN(v)) return;
      GMSet(lane === 'mic' ? 'cn_vol_mic' : 'cn_vol_game', v);
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
  function clipAnLevel(an) {
    // 0.00 = digital silence .. ~0.5 = loud. Never throws.
    try {
      if (!an) return null;
      const n = (an.fftSize && an.fftSize > 0) ? an.fftSize : 256;
      const buf = new Uint8Array(n);
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
      if (_cnComp.raf) { try { cancelAnimationFrame(_cnComp.raf); } catch {} _cnComp.raf = 0; }
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
        meas.font = (L.big ? '800 ' + fs : '700 ' + fs2) + 'px Inter,system-ui,sans-serif';
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
        g.font = (L.big ? '800 ' : '700 ') + f + 'px Inter,system-ui,sans-serif';
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
      const interval = 1000 / Math.max(5, Math.min(60, Number(fps) || 15));
      const loop = () => {
        if (!_cnComp.canvas) return;
        try {
          const gw = gameCanvas.width || 0, gh = gameCanvas.height || 0;
          if (vertical && gw > 0 && gh > 0) {
            let ew = c.width, eh = c.height;
            if (gw / gh > 9 / 16) { ew = Math.max(2, Math.round(gh * 9 / 16)); eh = gh; }
            else { ew = gw; eh = Math.max(2, Math.round(gw * 16 / 9)); }
            if (c.width !== ew || c.height !== eh) { c.width = ew; c.height = eh; _cnComp.labelKey = ''; }
            const now = performance.now();
            if (now - _cnComp.last >= interval) {
              _cnComp.last = now;
              try {
                if (gw / gh > 9 / 16) { const cw = gh * 9 / 16, sx = (gw - cw) / 2; ctx.drawImage(gameCanvas, sx, 0, cw, gh, 0, 0, c.width, c.height); }
                else { const chh = gw * 16 / 9, sy = (gh - chh) / 2; ctx.drawImage(gameCanvas, 0, sy, gw, chh, 0, 0, c.width, c.height); }
              } catch {}
              try {
                const nm = (cnMe.name || '').trim(), md = cnGmLabel(cnMode.code);
                const L = cnLabelCanvas(nm, md, c.height);
                if (L) {
                  const pad = Math.max(6, Math.round(c.height * 0.02));
                  const [lx, ly] = cnWatermarkXY(c.width, c.height, L.width, L.height, pad);
                  ctx.drawImage(L, lx, ly);
                }
              } catch {}
            }
          } else {
            if (gw > 0 && gh > 0 && (c.width !== gw || c.height !== gh)) { c.width = gw; c.height = gh; _cnComp.labelKey = ''; }
            const now = performance.now();
            if (now - _cnComp.last >= interval) {
              _cnComp.last = now;
              try { ctx.drawImage(gameCanvas, 0, 0, c.width, c.height); } catch {}
              try {
                const nm = (cnMe.name || '').trim(), md = cnGmLabel(cnMode.code);
                const L = cnLabelCanvas(nm, md, c.height);
                if (L) {
                  const pad = Math.max(6, Math.round(c.height * 0.02));
                  const [lx, ly] = cnWatermarkXY(c.width, c.height, L.width, L.height, pad);
                  ctx.drawImage(L, lx, ly);
                }
              } catch {}
            }
          }
        } catch {}
        _cnComp.raf = requestAnimationFrame(loop);
      };
      _cnComp.raf = requestAnimationFrame(loop);
      return c.captureStream(Number(fps) || 15);
    } catch { try { cnCompositeStop(); } catch {} return null; }
  }
  let _clipRO = null;
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
          try { _clip.cw = w; _clip.ch = h; } catch {}
          if (rsState === RS_IDLE || rsState === RS_STARTING) return;
          if (rsState === RS_SAVING) { try { rsCutNow(); } catch {} return; }
          logInfo('canvas resized - rebooting recorder');
          try { rsReboot('resize'); } catch {}
        } catch {}
      });
      try { _clipRO.observe(canvas); } catch {}
    } catch {}
  }
  function clipVerifyPlayable(blob) {
    return new Promise((res) => {
      let done = false, url = '', v = null;
      const finish = (ok) => {
        if (done) return; done = true;
        try { if (v) { try { v.pause(); } catch {} try { v.removeAttribute('src'); } catch {} try { v.load(); } catch {} try { v.remove(); } catch {} } } catch {}
        try { if (url) URL.revokeObjectURL(url); } catch {}
        res(ok);
      };
      try {
        url = URL.createObjectURL(blob);
        v = document.createElement('video');
        v.muted = true; v.preload = 'auto';
        v.onloadeddata = () => finish(true);
        v.onerror = () => finish(false);
        v.src = url;
        setTimeout(() => finish(false), 6000);
      } catch (e) { finish(false); }
    });
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
  const PREV_MAX_BYTES = 48 * 1024 * 1024, STITCH_MAX_BYTES = 80 * 1024 * 1024;
  function clipStashPrev(chunks, mime, durMs) {
    try {
      chunks = (chunks || []).filter(c => c && c.size > 0);
      if (!chunks.length) return;
      let bytes = 0;
      for (const c of chunks) bytes += c.size || 0;
      if (bytes <= 0 || bytes > PREV_MAX_BYTES) return;
      _clip.prev = { chunks: chunks.slice(), bytes, mime: mime || 'video/webm', durMs: Math.max(0, Number(durMs) || 0), withAudio: !!_clip.withAudio, layout: (function () { try { return clipCfg().layout || 'landscape'; } catch { return 'landscape'; } })(), w: _clip.cw || 0, h: _clip.ch || 0, at: Date.now() };
    } catch {}
  }
  function clipPickStitch(sDur, cDur, pDurMs, needMs) {
    try {
      if (sDur == null || !(sDur > 0)) return false;
      const c = Number(cDur) || 0;
      const want = Math.max(c, c + Math.max(0, Math.min(Number(pDurMs) || 0, Number(needMs) || 0)) / 1000);
      return sDur + 1.0 >= want;
    } catch { return false; }
  }
  async function clipStitchedBlob(curChunks, mime, curDurMs, cfg) {
    try {
      // MP4 concat is attempted, not banned: Chrome writes fragmented MP4 so
      // init+tail can play, and acceptance below is purely duration-PROBED -
      // a bad stitch falls back to current alone. Zero risk either way.
      const isMp4 = /mp4/i.test(String(mime || ''));
      const lenMs = Math.max(8000, Math.min(90000, (Number((cfg || {}).len) || 30) * 1000));
      curDurMs = Math.max(0, Number(curDurMs) || 0);
      if (curDurMs >= lenMs) return null;
      const needMs = lenMs - curDurMs;
      if (needMs <= 500) return null;
      const p = _clip.prev;
      if (!p || !p.chunks || !p.chunks.length) return null;
      if (p.mime !== mime) return null;
      if (p.withAudio !== !!_clip.withAudio) return null;
      if (p.w !== (_clip.cw || 0) || p.h !== (_clip.ch || 0)) return null;
      try { if (p.layout !== (clipCfg().layout || 'landscape')) return null; } catch {}
      if ((Date.now() - (p.at || 0)) > lenMs + 30000) return null;
      let curBytes = 0;
      for (const c of curChunks) curBytes += (c && c.size) || 0;
      if ((p.bytes || 0) + curBytes > STITCH_MAX_BYTES) return null;
      const curBlob = new Blob(curChunks, { type: mime });
      const stitched = new Blob([...p.chunks, ...curChunks], { type: mime });
      const [sDur, cDur] = await Promise.all([clipProbeDuration(stitched), clipProbeDuration(curBlob)]);
      if (clipPickStitch(sDur, cDur, p.durMs, needMs)) {
        try { if (isMp4) logInfo(`clip stitched MP4 cur=${cDur == null ? '?' : cDur.toFixed(1)}s -> ${sDur == null ? '?' : sDur.toFixed(1)}s`); } catch {}
        return { blob: stitched, sDur, cDur };
      }
      try { logInfo(`stitch rejected (stitched=${sDur == null ? '?' : sDur.toFixed(1)}s cur=${cDur == null ? '?' : cDur.toFixed(1)}s) - saving current`); } catch {}
    } catch {}
    return null;
  }
  // ---- recorder FSM ----
  const RS_IDLE = 'idle', RS_STARTING = 'starting', RS_RECORDING = 'recording', RS_ROTATING = 'rotating', RS_SAVING = 'saving';
  const RS_READY_BYTES = 48 * 1024, RS_READY_AGE = 1200, RS_IDLE_RETRY = 2500;
  let rsState = RS_IDLE, rsGen = 0, rsSaveQ = null, rsRetryT = null, rsRotT = null, rsCutT = null;
  function rsActive() { try { return rsState === RS_RECORDING || rsState === RS_ROTATING || rsState === RS_SAVING; } catch { return false; } }
  function rsDataReady() {
    try {
      if (rsState !== RS_RECORDING && rsState !== RS_ROTATING && rsState !== RS_SAVING) return false;
      const r = _clip.rec;
      if (!r || r.state !== 'recording') return false;
      if ((_clip.chunkBytes || 0) < RS_READY_BYTES) return false;
      if ((Date.now() - (_clip.segStartedAt || 0)) < RS_READY_AGE) return false;
      return true;
    } catch { return false; }
  }
  function rsClearTimers() {
    try { if (_clip.segTimer) { clearTimeout(_clip.segTimer); _clip.segTimer = null; } } catch {}
    try { if (_clip.pendingTimer) { clearTimeout(_clip.pendingTimer); _clip.pendingTimer = null; } } catch {}
    try { if (rsRetryT) { clearTimeout(rsRetryT); rsRetryT = null; } } catch {}
    try { if (rsRotT) { clearTimeout(rsRotT); rsRotT = null; } } catch {}
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
      _clip.started = (next === RS_RECORDING || next === RS_ROTATING || next === RS_SAVING);
      _clip.finalizing = (next === RS_ROTATING || next === RS_SAVING);
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
        if (!rsSegment('enter')) rsEnter(RS_IDLE);
        else {
          try {
            const cfg = clipCfg();
            _clip.segTimer = setTimeout(() => { try { if (rsState === RS_RECORDING) rsEnter(RS_ROTATING); } catch {} }, Math.max(8, Math.min(90, Number(cfg.len) || 30)) * 1000);
          } catch {}
          try {
            if (rsSaveQ && (Date.now() - rsSaveQ.at) < 12000) {
              const q = rsSaveQ; rsSaveQ = null;
              rsEnter(RS_SAVING, q.trigger);
            } else rsSaveQ = null;
          } catch {}
        }
      } else if (next === RS_ROTATING) {
        rsRotateStep();
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
    // every reboot resets segment age, so a chatty reason here is the
    // reason clips come out shorter than the buffer.
    try { logInfo(`rec reboot why=${why || '?'}`); } catch {}
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
      try { if (needComp) { try { stream = cnCompositeStream(canvas, Number(cfg.fps) || 15, cfg.layout === 'vertical') || null; } catch {} } } catch {}
      if (stream) wmActive = !!cfg.watermark;
      else {
        try { stream = canvas.captureStream(Number(cfg.fps) || 15); }
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
          const s = _clip.stream;
          if (curA && curA !== track) { try { s.removeTrack(curA); } catch {} }
          try {
            if ((s.getAudioTracks() || []).indexOf(track) < 0) s.addTrack(track);
          } catch { return false; }
          // Fresh tap may hold a stale recGain value: push the slider now.
          try { if (typeof clipTapSetGameVol === 'function') clipTapSetGameVol(); } catch {}
          try { clipStashForUpgrade(why); } catch {}
          try { if (_clip.rec) { try { _clip.rec.onstop = null; } catch {} try { if (_clip.rec.state !== 'inactive') _clip.rec.stop(); } catch {} } } catch {}
          _clip.withAudio = true;
          try { logInfo(`audio route: ${why}`); } catch {}
          try { rsSegment('swap:' + why); } catch {}
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
      try { clipStashForUpgrade('game+mic mix'); } catch {}
      try { if (_clip.rec) { try { _clip.rec.onstop = null; } catch {} try { if (_clip.rec.state !== 'inactive') _clip.rec.stop(); } catch {} } } catch {}
      _clip.stream = ns; _clip.withAudio = true; _mixActive = true;
      try { logInfo('audio appeared mid-roll - continuing with audio (mix)'); } catch {}
      try { rsSegment('mix-attach'); } catch {}
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
  function rsSegment(why) {
    // why = short restart reason for the log (rotation, audio swap, reboot…).
    // Every restart resets segment age, so frequent reasons here explain
    // clips shorter than the buffer.
    try {
      if (!_clip.stream) { _clip.lastErr = 'captureStream empty'; return false; }
      const cfg = clipCfg();
      const mime = clipPickMime(!!_clip.withAudio);
      const opts = { videoBitsPerSecond: Number(cfg.bitrate) || 1500000 };
      if (_clip.withAudio) opts.audioBitsPerSecond = 128000;
      if (mime) opts.mimeType = mime;
      const g = ++rsGen;
      let rec = null;
      try { rec = new MediaRecorder(_clip.stream, opts); } catch (e) { try { rec = new MediaRecorder(_clip.stream); } catch (e2) { _clip.lastErr = 'Recorder init failed'; return false; } }
      _clip.rec = rec; _clip.chunks = []; _clip.chunkBytes = 0; _clip.mime = mime || ''; _clip.segStartedAt = Date.now();
      rec.ondataavailable = (e) => { try { if (e.data && e.data.size > 0 && g === rsGen) { _clip.chunks.push(e.data); _clip.chunkBytes += e.data.size; } } catch {} };
      rec.onerror = () => { try { _clip.lastErr = 'Recorder error'; } catch {} };
      try { rec.start(1000); } catch (e) { _clip.lastErr = 'Recorder start failed'; return false; }
      // Assertion: proves the recorder actually holds an audio track. If
      // aTracks=0 here after a "yes audio" boot, the track object was
      // silently dropped (realm/container) - the file can never have sound.
      try {
        const vN = (_clip.stream.getVideoTracks() || []).length;
        const aT = _clip.stream.getAudioTracks() || [];
        logInfo(`rec seg mime=${rec.mimeType || mime || '?'} vTracks=${vN} aTracks=${aT.length} aLive=${aT.map(t => t.readyState).join(',') || '-'} withAudioFlag=${!!_clip.withAudio} why=${why || '?'}`);
      } catch {}
      return true;
    } catch { return false; }
  }
  function rsRotateStep() {
    try {
      const r = _clip.rec, g = rsGen;
      if (!r || r.state === 'inactive') { rsEnter(RS_RECORDING); return; }
      try {
        r.onstop = () => {
          try { if (g !== rsGen || rsState !== RS_ROTATING) return; } catch { return; }
          try { clipStashPrev(_clip.chunks.slice(), _clip.mime, Date.now() - (_clip.segStartedAt || Date.now())); } catch {}
          try { _clip.rec = null; _clip.chunks = []; _clip.chunkBytes = 0; } catch {}
          rsEnter(RS_RECORDING);
        };
      } catch { rsEnter(RS_RECORDING); return; }
      try { r.requestData(); } catch {}
      try { r.stop(); } catch { rsEnter(RS_RECORDING); return; }
      try { rsRotT = setTimeout(() => { try { if (rsState === RS_ROTATING) rsEnter(RS_RECORDING); } catch {} }, 3000); } catch {}
    } catch { try { rsEnter(RS_RECORDING); } catch {} }
  }
  function rsSaveStep(trigger) {
    try {
      const cfg = clipCfg();
      _clip.pendingTrigger = trigger;
      if (trigger !== 'manual') { try { cnSfx('tick'); } catch {} }
      // Last-chance audio grab: segment rolled video-only but a tap/mic is
      // available NOW - restart with audio BEFORE the aftermath wait, so the
      // saved file has a real audio track instead of silent video-only.
      // (Pre-roll video is stashed as prev; MP4 can't stitch it back, but an
      // audible aftermath beats a silent full clip.)
      try {
        if (!_clip.withAudio && trigger !== 'manual' && rsState === RS_SAVING && _clip.stream) {
          let at = null;
          try { at = clipMixedAudioTrack(); } catch {}
          if (at) {
            const ns = clipAttachAudio(_clip.stream, at);
            if (ns) {
              try { clipStashForUpgrade('save-time audio grab'); } catch {}
              try { if (_clip.rec) { try { _clip.rec.onstop = null; } catch {} try { if (_clip.rec.state !== 'inactive') _clip.rec.stop(); } catch {} } } catch {}
              _clip.stream = ns; _clip.withAudio = true;
              try { logInfo('save-time audio grab - aftermath will carry audio'); } catch {}
              try { rsSegment('save-grab'); } catch {}
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
      const finish = (curChunks, curDurMs) => {
        if (done) return; done = true;
        const cur = (curChunks || []).slice();
        try {
          if (g !== rsGen || _clip.rec !== r) { rsEnter(RS_RECORDING); return null; }
          _clip.rec = null;
          try { clipStashPrev(cur, mime, curDurMs); } catch {}
          _clip.chunks = []; _clip.chunkBytes = 0;
        } catch {}
        rsStoreClip(trigger, cur, mime, curDurMs, cfg);
        return cur;
      };
      try {
        r.onstop = () => {
          try {
            if (g !== rsGen) return;
            finish(_clip.chunks.slice(), Date.now() - (_clip.segStartedAt || Date.now()));
          } catch { try { finish(null, 0); } catch {} }
        };
      } catch { return fail(''); }
      try { r.requestData(); } catch {}
      setTimeout(() => { try { if (g === rsGen && rsState === RS_SAVING) r.stop(); } catch { finish(null, 0); } }, 250);
      try { rsCutT = setTimeout(() => { try { if (!done) finish(null, 0); } catch {} }, 5000); } catch {}
    } catch { try { rsEnter(RS_RECORDING); } catch {} return null; }
  }
  async function rsStoreClip(trigger, curChunks, mime, curDurMs, cfg) {
    try {
      curChunks = curChunks || [];
      cfg = cfg || clipCfg();
      let blob = curChunks.length ? new Blob(curChunks, { type: mime }) : null;
      let how = 'current', stitchedDur = null;
      try { logInfo(`clip cut ${trigger} mime=${mime} withAudio=${!!_clip.withAudio} ${clipAudioLevels()}`); } catch {}
      try {
        const st = await clipStitchedBlob(curChunks, mime, curDurMs, cfg);
        if (st && st.blob && st.blob.size) {
          blob = st.blob; how = 'stitched'; stitchedDur = st.sDur;
          try { logInfo(`clip stitched cur=${st.cDur == null ? '?' : st.cDur.toFixed(1)}s -> ${st.sDur == null ? '?' : st.sDur.toFixed(1)}s`); } catch {}
        } else {
          // No stitch (rejected by probe, or no usable prev): clip length =
          // current segment age + aftermath. Short of the buffer usually
          // means the segment restarted recently - see 'rec seg ... why='.
          try { logInfo(`clip unstitched: seg was ${(Number(curDurMs) / 1000).toFixed(1)}s of ${cfg.len}s buffer`); } catch {}
        }
      } catch {}
      if (!blob || !blob.size) {
        try { toast('Clip not ready - try again in a few seconds', true); } catch {}
        try { cnSfx('err'); } catch {}
        try { rsEnter(RS_RECORDING); } catch {}
        return null;
      }
      const plays = await clipVerifyPlayable(blob);
      if (!plays) {        try { toast('Clip corrupted - discarded, keep playing', true); } catch {}
        try { cnSfx('err'); } catch {}
        try { if (_clip.lastKill && trigger !== 'manual') _clip.lastKill.decision = 'corrupted'; } catch {}
        try { rsEnter(RS_RECORDING); } catch {}
        try { if (cnOpen) fetchCnClips(); } catch {}
        return null;
      }
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
      const row = await clipStore({ trigger, mode: cnMode.code || null, layout: (cfg || clipCfg()).layout || null, killer: kvKiller, victim: kvVictim }, blob);
      let secs = null;
      try { secs = (how === 'stitched' && stitchedDur) ? stitchedDur : (Number(curDurMs) || 0) / 1000; } catch {}
      try { toast(`Clip saved (${how}${secs ? ', ' + Math.round(secs) + 's' : ''}) - open My clips to watch`, false); } catch {}
      try { cnSfx('ok'); } catch {}
      try { if (row && _clip.lastKill && trigger !== 'manual') _clip.lastKill.decision = 'saved'; } catch {}
      try { rsEnter(RS_RECORDING); } catch {}
      try { if (cnOpen) fetchCnClips(); } catch {}
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
      if (rsState === RS_SAVING || rsState === RS_ROTATING) {
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
      const row = { id: 'c' + Date.now().toString(36) + Math.floor(Math.random() * 1e6).toString(36), ts: Date.now(), trigger: meta.trigger || 'manual', mode: meta.mode || null, layout: meta.layout || null, fav: false, mime: blob.type || 'video/webm', size: blob.size || 0, killer: meta.killer || null, victim: meta.victim || null, blob };
      await new Promise((ok, no) => { try { const tx = db.transaction('clips', 'readwrite'); tx.objectStore('clips').put(row); tx.oncomplete = ok; tx.onerror = no; } catch (e) { no(e); } });
      try {
        const all = await new Promise((rs) => { try { const tx = db.transaction('clips', 'readonly'); const q = tx.objectStore('clips').getAll(); q.onsuccess = () => rs(q.result || []); q.onerror = () => rs([]); } catch { rs([]); } });
        if (all.length > 20) {
          all.sort((a, b) => (a.ts || 0) - (b.ts || 0));
          const drop = all.slice(0, all.length - 20);
          await new Promise((rs) => { try { const tx = db.transaction('clips', 'readwrite'); const st = tx.objectStore('clips'); drop.forEach(r => { try { st.delete(r.id); } catch {} }); tx.oncomplete = rs; tx.onerror = rs; } catch { rs(); } });
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
  function clipOnKill(killer, victim, isKill) {
    try {
      const stored = (cnMe.name || '').trim();
      const note = (decision) => { try { _clip.lastKill = { killer: String(killer || '?'), victim: String(victim || '?'), me: stored || '?', at: Date.now(), decision }; } catch {} };
      if (!isKill) { note('knock-ignored'); return; }
      const cfg = clipCfg();
      if (!cfg.enabled) { note('clipping-off'); return; }
      const rawMe = stored.toLowerCase();
      const rawK = String(killer || '').replace(/<[^>]*>/g, '').trim().toLowerCase();
      const rawV = String(victim || '').replace(/<[^>]*>/g, '').trim().toLowerCase();
      const meN = cnNormName(stored).trim().toLowerCase();
      const kN = cnNormName(killer || '').trim().toLowerCase();
      const vN = cnNormName(victim || '').trim().toLowerCase();
      const amKiller = ((meN && kN && meN === kN) || (rawMe && rawK && rawMe === rawK)) || !stored;
      const amVictim = ((meN && vN && meN === vN) || (rawMe && rawV && rawMe === rawV));
      const now = Date.now();
      if (amKiller) {
        if (!cfg.autoKill) { note('auto-off'); return; }
        if (now - _clip.killAt < 8000) { note('debounced'); return; }
        _clip.killAt = now;
        note('queued');
        clipScheduleAuto('kill');
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
  function cnEnsureStyle() {
    try {
      if (document.getElementById('cn-style')) return;
      const st = document.createElement('style');
      st.id = 'cn-style';
      st.textContent = '#cn-fab{position:fixed;right:14px;bottom:14px;width:34px;height:34px;border-radius:50%;border:2px solid rgba(255,255,255,.25);background:#111;color:#fff;font-size:15px;cursor:pointer;z-index:2147483645;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 16px rgba(0,0,0,.5);font-family:Inter,system-ui,sans-serif;transition:transform 120ms ease-out}'
        + '#cn-fab:hover{transform:scale(1.1)}'
        + '#cn-panel{position:fixed;right:14px;bottom:58px;width:360px;max-width:92vw;max-height:80vh;display:none;flex-direction:column;background:#111;border:1px solid rgba(255,255,255,.12);border-radius:14px;z-index:2147483646;overflow:hidden;font-family:Inter,system-ui,sans-serif;box-shadow:0 12px 48px rgba(0,0,0,.6);opacity:0;transform:scale(.94);transform-origin:bottom right;transition:opacity 160ms ease-out,transform 160ms ease-out}'
        + '#cn-panel.cn-show{opacity:1;transform:scale(1)}'
        + '#cn-body{overflow:auto;padding:12px 14px}'
        + '.cn-rowbtn{flex:1;min-width:120px;background:#fff;color:#000;border:0;border-radius:8px;padding:8px 0;font-size:12px;font-weight:800;cursor:pointer}'
        + '.cn-ghost{background:none;border:1px solid rgba(255,255,255,.12);color:#9ca3af;border-radius:6px;padding:6px 12px;font-size:11px;cursor:pointer}'
        + '.cn-card{background:#171717;border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:10px 12px;margin-bottom:8px}'
        + '.cn-set{background:#171717;border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:8px}'
        + '.cn-sel{background:#0f0f0f;color:#e5e7eb;border:1px solid rgba(255,255,255,.12);border-radius:6px;font-size:11px;padding:4px 6px}'
        + '#cn-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:2147483646;display:none;opacity:0;transition:opacity 160ms ease-out}'
        + '#cn-backdrop.cn-show{opacity:1}'
        + '.cn-modal{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%) scale(.96);width:min(640px,94vw);aspect-ratio:4/3;max-height:86vh;display:none;flex-direction:column;background:#111;border:1px solid rgba(255,255,255,.12);border-radius:16px;z-index:2147483647;overflow:hidden;font-family:Inter,system-ui,sans-serif;box-shadow:0 12px 48px rgba(0,0,0,.6);opacity:0;transition:opacity 160ms ease-out,transform 160ms ease-out}'
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
      const modal = view === 'lib' || view === 'prev' || view === 'onb';
      try {
        if (e.bd) {
          if (modal) {
            e.bd.style.display = 'block';
            requestAnimationFrame(() => { requestAnimationFrame(() => { try { e.bd.classList.add('cn-show'); } catch {} }); });
          } else { try { e.bd.classList.remove('cn-show'); } catch {} e.bd.style.display = 'none'; }
        }
      } catch {}
      const pairs = [[e.main, 'main'], [e.lib, 'lib'], [e.prev, 'prev'], [e.onb, 'onb']];
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
      try { if (view !== 'main' && _cnMeterRaf) { cancelAnimationFrame(_cnMeterRaf); _cnMeterRaf = 0; } } catch {}
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
      for (const el of [e.main, e.lib, e.prev, e.onb]) {
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
            for (const el of [x.main, x.lib, x.prev, x.onb]) {
              try { if (!el) continue; el.style.display = 'none'; } catch {}
            }
          } catch {}
        }, 180);
      } catch {}
      try { if (_cnMeterRaf) { cancelAnimationFrame(_cnMeterRaf); _cnMeterRaf = 0; } } catch {}
      try { cnRevokeUrls(); } catch {}
    } catch {}
  }
  function cnBack() {
    try {
      if (cnView === 'prev') cnShow('lib');
      else cnHideAll();
    } catch {}
  }
  function cnRenderCurrent() {
    try {
      if (cnView === 'main') renderMain();
      else if (cnView === 'lib') renderLib();
      else if (cnView === 'prev') renderPrev();
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
  function cnBadge(t) { try { return { kill: '#ef4444', death: '#f59e0b', win: '#10b981', manual: '#6b7280' }[t] || '#6b7280'; } catch { return '#6b7280'; } }
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
      if (rsState === RS_ROTATING) return 'switching segment…';
      if (rsState === RS_RECORDING) {
        if (rsDataReady()) return `REC seg ${segAge}/${cfg.len}s · ${cfg.fps}fps ${cnMimeShort()}${audioBit}${wmBit}${layBit}`;
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
      on('clip-fps', (e) => clipSet('cn_clip_fps', Number(e.target.value) || 15));
      on('clip-bitrate', (e) => clipSet('cn_clip_bitrate', Number(e.target.value) || 1500000));
      on('clip-after', (e) => { const v = Number(e.target.value); clipSet('cn_clip_after', isNaN(v) ? 5 : Math.max(0, Math.min(10, v))); });
      on('clip-layout', (e) => clipSet('cn_clip_layout', e.target.value === 'vertical' ? 'vertical' : 'landscape'));
      on('clip-preset', (e) => { try { cnApplyPreset(e.target.value); } catch {} });
      const wm = (id, key) => { const el = g(id); if (el) el.onchange = (e) => { try { GMSet(key, e.target.checked); } catch {} try { _cnComp.labelKey = ''; } catch {} try { if (typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {} }; };
      wm('wm-name', 'cn_wm_name'); wm('wm-mode', 'cn_wm_mode'); wm('wm-brand', 'cn_wm_brand');
      on('wm-pos', (e) => { const v = String(e.target.value || 'br'); try { GMSet('cn_wm_pos', ['tl', 'tc', 'tr', 'bl', 'bc', 'br'].includes(v) ? v : 'br'); } catch {} try { if (typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {} });
    } catch {}
  }
  // ---- capture presets: named fps+bitrate bundles ----
  // Low-end PC (light encoder load), Medium (balanced default), Discord
  // clips (smooth 30fps under Discord's free upload limits), Max quality.
  // Applying a preset writes fps+bitrate and reboots the recorder once.
  const CN_PRESETS = [
    ['lowend', 'Low-end PC', 10, 1000000],
    ['medium', 'Medium', 15, 2500000],
    ['discord', 'Discord clips', 30, 2500000],
    ['max', 'Max quality', 60, 8000000],
  ];
  function cnPresetOf(fps, bitrate) {
    try {
      for (const [k, , f, b] of CN_PRESETS) { if (Number(fps) === f && Number(bitrate) === b) return k; }
    } catch {}
    return 'custom';
  }
  function cnApplyPreset(key) {
    try {
      const p = CN_PRESETS.find(([k]) => k === key);
      if (!p) return;
      try { GMSet('cn_clip_fps', p[2]); } catch {}
      try { GMSet('cn_clip_bitrate', p[3]); } catch {}
      try { clipEnsureMic(); } catch {}
      try { rsReboot('preset'); } catch {}
      try { logInfo(`preset -> ${p[1]} (${p[2]}fps, ${(p[3] / 1000000).toFixed(1)}Mbps)`); } catch {}
      try { if (typeof cnRenderCurrent === 'function') cnRenderCurrent(); } catch {}
    } catch {}
  }
  function cnSettingsHtml(cfg, prefix) {
    try {
      return `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:10px 0 4px">SETTINGS</div>`
        + `<div class="cn-set">`
        + `<div style="display:flex;gap:12px;flex-wrap:wrap">${cnChk(prefix + '-clip-enabled', cfg.enabled, 'Enable')}${cnChk(prefix + '-clip-autokill', cfg.autoKill, 'My kills')}${cnChk(prefix + '-clip-autodeath', cfg.autoDeath, 'My deaths')}${cnChk(prefix + '-clip-autowin', cfg.autoWin, 'Wins')}${cnChk(prefix + '-clip-audio', cfg.audio, 'Game audio')}${cnChk(prefix + '-clip-mic', cfg.mic, 'Mic')}${cnChk(prefix + '-clip-watermark', cfg.watermark, 'Watermark')}${cnChk(prefix + '-fab-visible', cfg.fab, 'Show button')}${cnChk(prefix + '-sounds', cfg.sounds, 'Sounds')}</div>`
        + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Buffer</span>${cnSel(prefix + '-clip-len', cfg.len, [[8, '8s'], [15, '15s'], [30, '30s ✓'], [45, '45s'], [60, '60s']])}<span>FPS</span>${cnSel(prefix + '-clip-fps', cfg.fps, [[10, '10'], [15, '15 ✓'], [30, '30'], [60, '60']])}</div>`
        + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Quality</span>${cnSel(prefix + '-clip-bitrate', cfg.bitrate, [[1500000, 'Low ✓'], [2500000, 'Medium'], [5000000, 'High'], [8000000, 'Ultra']])}<span>Aftermath</span>${cnSel(prefix + '-clip-after', cfg.after, [[0, '0s'], [3, '3s'], [5, '5s ✓'], [8, '8s']])}</div>`
        + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Layout</span>${cnSel(prefix + '-clip-layout', cfg.layout, [['landscape', 'Landscape 16:9'], ['vertical', 'Vertical 9:16']])}<span>Preset</span>${cnSel(prefix + '-clip-preset', cnPresetOf(cfg.fps, cfg.bitrate), [['custom', 'Custom'], ...CN_PRESETS.map(([k, l]) => [k, l])])}</div>`
        + `<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;font-size:11px;color:#e5e7eb"><span style="color:#9ca3af">Watermark</span>${cnChk(prefix + '-wm-name', cfg.wmName, 'Name')}${cnChk(prefix + '-wm-mode', cfg.wmMode, 'Mode')}${cnChk(prefix + '-wm-brand', cfg.wmBrand, 'Brand')}</div>`
        + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Watermark spot</span>${cnSel(prefix + '-wm-pos', cfg.wmPos, [['tl', 'Top left'], ['tc', 'Top center'], ['tr', 'Top right'], ['bl', 'Bottom left'], ['bc', 'Bottom center'], ['br', 'Bottom right']])}</div>`
        + ((cfg.fps >= 30 && cfg.bitrate >= 2500000) ? `<div style="font-size:11px;color:#eab308">High FPS + quality can drop game FPS - try 15 FPS / Low if the game stutters.</div>` : '')
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
  let _cnMeterRaf = 0;
  function cnTickMeters() {
    // rAF loop, only while the main panel is visible. Reads the per-lane
    // POST-GAIN AnalyserNodes so the bars reflect the current slider
    // position, not just raw source level.
    try {
      if (cnView !== 'main') { _cnMeterRaf = 0; return; }
      ['game', 'mic'].forEach((lane) => {
        try {
          const el = document.getElementById(`cn-meter-${lane}`);
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
    try { _cnMeterRaf = requestAnimationFrame(cnTickMeters); } catch { _cnMeterRaf = 0; }
  }
  function cnStartMeters() {
    try { if (!_cnMeterRaf) _cnMeterRaf = requestAnimationFrame(cnTickMeters); } catch {}
  }
  // ---- main panel: slim - status + audio mix + settings + entry points ----
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
        + `<button id="cn-open-lib" class="cn-rowbtn" style="width:100%;margin-bottom:10px;background:#171717;color:#fff;border:1px solid rgba(255,255,255,.12)">▦ Open clips (<span id="cn-open-count">…</span>)</button>`;
      try {
        const lk = _clip.lastKill;
        if (lk && lk.at) {
          const ago = Math.max(0, Math.round((Date.now() - lk.at) / 1000));
          const why = { queued: 'clip queued', saved: 'clip saved', 'not-your-kill': 'skipped (not your kill)', 'knock-ignored': 'skipped (knock)', debounced: 'skipped (too soon)', 'auto-off': 'auto-clip off', 'clipping-off': 'clipping off', corrupted: 'discarded (corrupted)' }[lk.decision] || lk.decision || '';
          html += `<div style="font-size:11px;color:#6b7280;margin:-2px 0 10px">Last killfeed: ${esc(lk.killer)} → ${esc(lk.victim)} · ${ago}s ago · ${esc(why)}</div>`;
        }
      } catch {}
      html += cnAudioMixHtml()
        + cnSettingsHtml(cfg, 'cn')
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
      try { cnBindSettings('cn'); } catch {}
      try { cnBindKeys('cn'); } catch {}
      try { cnBindMeters('cn'); } catch {}
      try { cnStartMeters(); } catch {}
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
      return !!(_cnUntrapEl && document.contains(_cnUntrapEl) && Date.now() <= _cnUntrapUntil && cnView === 'lib');
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
                if (!cur || !document.contains(cur) || cnView !== 'lib') { try { clearInterval(_cnUntrapTimer); } catch {} _cnUntrapTimer = null; }
                return;
              }
              if (document.activeElement === cur) return;
              // Legit in-panel navigation releases (don't trap the user).
              try {
                const e = cnEls();
                const ae = document.activeElement;
                if (ae && ae !== document.body && e.lib && e.lib.contains(ae)) { cnUntrapRelease(); return; }
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
          + `<span style="font-size:11px;color:#9ca3af;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(cnWhen(r.ts))} · ${cnKB(r.size)}${r.mode ? ' · ' + esc(cnGmLabel(r.mode)) : ''}${lay}${vs}</span>`
          + `<button data-cn-fav="${r.id}" class="cn-star" title="Favorite">${star}</button></div>`;
        if (mode === 'grid') {
          return `<div class="cn-card" data-cn-open="${r.id}" style="cursor:pointer;margin-bottom:0;${vert ? 'grid-column:span 1' : ''}">${head}<video data-cn-vid="${r.id}" preload="metadata" muted playsinline class="cn-thumb"></video></div>`;
        }
        return `<div class="cn-card" data-cn-open="${r.id}" style="cursor:pointer;display:flex;gap:10px;align-items:center">${head.split('</div>')[0]}</div><div style="flex:1;min-width:0"><video data-cn-vid="${r.id}" preload="metadata" muted playsinline style="width:100%;aspect-ratio:16/9;background:#000;border-radius:6px;object-fit:cover"></video></div><button data-cn-fav="${r.id}" class="cn-star" title="Favorite" style="font-size:18px">${star}</button></div>`;
      };
      body.innerHTML = (mode === 'grid' ? `<div class="cn-grid${vert ? ' cn-vert' : ''}">` : `<div style="display:flex;flex-direction:column;gap:8px">`)
        + rows.map(card).join('') + `</div>`;
      try {
        body.querySelectorAll('video[data-cn-vid]').forEach(v => {
          const r = byId[v.dataset.cnVid];
          if (r && r.blob) { try { v.src = cnTrackUrl(URL.createObjectURL(r.blob)); } catch {} }
        });
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
        + `<div style="font-size:11px;color:#9ca3af;margin:8px 0">${r.mode ? esc(cnGmLabel(r.mode)) + ' · ' : ''}${cnKB(r.size)}${lay}${(r.trigger !== 'manual' && (r.killer || r.victim)) ? `<br>${esc(r.killer || '?')} → ${esc(r.victim || '?')}` : ''}</div>`
        + `<div style="display:flex;gap:8px"><button id="cn-prev-dl" class="cn-rowbtn">Download</button><button id="cn-prev-del" class="cn-ghost">Delete</button></div>`
        + `<button id="cn-prev-back" class="cn-ghost" style="width:100%;margin-top:8px">← Back to clips</button>`
        + `</div>`;
      cnRevokeUrls();
      e.prev.innerHTML = html;
      try {
        const v = document.getElementById('cn-prev-vid');
        if (v && r.blob) v.src = cnTrackUrl(URL.createObjectURL(r.blob));
      } catch {}
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
          try { GMSet(cnKeyCap === 'clip' ? 'cn_key_clip' : (cnKeyCap === 'lib' ? 'cn_key_lib' : 'cn_key_panel'), k); } catch {}
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
  ['pointerdown', 'keydown', 'touchend'].forEach((ev) => {
    try { document.addEventListener(ev, () => { try { _mixGestureAt = Date.now(); } catch {} try { clipMixResume(); } catch {} }, true); } catch {}
  });
  // Live indicator ticker: the dot + status line + audio hot-path track the
  // recorder without opening anything (full re-render would steal focus
  // from open inputs/sliders).
  setInterval(() => {
    try { cnRefreshFab(); } catch {}
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