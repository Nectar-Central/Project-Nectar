// ==UserScript==
// @name         Nectar Buildnow Companion LITE
// @namespace    https://github.com/Nectar-Central
// @version      2.35.0
// @description  Nectar Buildnow Companion LITE v2.35.0-lite (no player list, no FPS chip, slowed polls) — leaderboard panel, graphics presets, Discord rich presence, FPS chip, session refresh modal.
// @author       Nectar Labs
// @match        https://buildnow-gg.game-files.crazygames.com/*
// @match        https://*.crazygames.com/*
// @all-frames   true
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_deleteValue
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// @connect      exitgames.com
// @connect      cdn.jsdelivr.net
// @connect      api-compbng.vercel.app
// @connect      project-nectar-6n70.onrender.com
// @connect      vercel.app
// @connect      *.vercel.app
// @connect      supabase.co
// @connect      *.supabase.co
// @connect      devtunnels.ms
// @connect      *.devtunnels.ms
// @updateURL    disabled-for-lite
// @downloadURL  disabled-for-lite
// ==/UserScript==

(function () {
  'use strict';
  const BNC_VERSION = '2.35.0-lite';
  const BNC_LITE = true; // lite: skips FPS chip, players poll, chat poll, gfx loop
  const HREF = (() => { try { return location.href; } catch { return ''; } })();
  const IS_GAME_FRAME = HREF.includes('buildnow-gg.game-files.crazygames.com');
  // Loading shell lives inside an intermediate iframe (games.crazygames.com),
  // not on the top page. Gate by CrazyGames hostname only.
  let IS_CRAZYGAMES=false;
  try{ IS_CRAZYGAMES = /(^|\.)crazygames\.com$/.test(location.hostname||''); }catch{}
  if (!IS_GAME_FRAME && !IS_CRAZYGAMES) return;

  const GMGet = (k,d=null)=>{ try{ return typeof GM_getValue!=='undefined'? GM_getValue(k,d) : d; }catch{ return d; } };
  const GMSet = (k,v)=>{ try{ if(typeof GM_setValue!=='undefined') GM_setValue(k,v); }catch{} };
  const GMDel = (k)=>{ try{ if(typeof GM_deleteValue!=='undefined') GM_deleteValue(k); else if(typeof GM_setValue!=='undefined') GM_setValue(k, undefined); }catch{} };
  // Enforce login: if user sets bnc_disabled, script is inert - play normally
  if (GMGet('bnc_disabled', false)) {
    console.log('%c[BNC] disabled - play normally (reenable via GMSet bnc_disabled false)', 'color:#6b7280');
    return;
  }

  // Production API default. Override order: stored bnc_api_base (user-set,
  // e.g. http://127.0.0.1:8000 for a local backend) > ?api=local > production.
  // Without the stored override the drawer talks to production even when a
  // local backend runs — everything reads Offline there. Never throws.
  const SERVER_API = 'https://project-nectar-6n70.onrender.com';
  const LOCAL_API  = 'http://127.0.0.1:8000';
  function normalizeApiBase(raw){
    try{
      let s=String(raw==null?'':raw).trim();
      if(!s) return null;
      if(!/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) s='http://'+s;
      const u=new URL(s);
      if(u.protocol!=='http:' && u.protocol!=='https:') return null;
      return u.origin;
    }catch{ return null; }
  }
  function resolveApiBase(){
    try{
      const stored=normalizeApiBase(GMGet('bnc_api_base', null));
      if(stored) return stored;
    }catch{}
    // allow localhost override via ?api=local (dev only, invisible in UI)
    try { if (location.search.includes('api=local')) return LOCAL_API; } catch {}
    return SERVER_API;
  }
  function cleanTokenStorage(){
    ['bnc_token','bn_token','bnc_guest','bn_guest','bnc_discord_username','bn_discord_username','bnc_discord_id','bn_discord_id','bnc_first_seen','bn_first_seen','bnc_game_username','bn_game_username','bnc_game_id','bn_game_id','bnc_welcomed','bn_welcomed','bnc_session_gm','bn_session_gm','bnc_session_id','bn_session_id','bnc_req_secret','bn_req_secret','bnc_req_token','bn_req_token','bnc_req_counter','bn_req_counter'].forEach(GMDel);
    loginToken=null; discordUsername=null; hasWelcomed=false;
    sessionId=null; currentReqToken=null; reqCounter=0; discordId=null;
    try{ if(typeof stopRevalidate==='function') stopRevalidate(); }catch{}
    try{ localStorage.removeItem('bnc_token'); localStorage.removeItem('bn_token'); sessionStorage.removeItem('bnc_token'); sessionStorage.removeItem('bn_token'); }catch{}
  }
  let loginToken = GMGet('bnc_token', GMGet('bn_token', null));
  // guest removed: login is mandatory, otherwise play without tracker by disabling script
  let isGuest = false;
  let isLocked = false;
  let discordUsername = GMGet('bnc_discord_username', GMGet('bn_discord_username', null));
  let hasWelcomed = !!GMGet('bnc_welcomed', GMGet('bn_welcomed', false));
  let sessionId = GMGet('bnc_session_id', GMGet('bn_session_id', null));
  let reqCounter = Number(GMGet('bnc_req_counter', GMGet('bn_req_counter', 0))||0);
  let currentReqToken = GMGet('bnc_req_token', GMGet('bn_req_token', null));
  let discordId = GMGet('bnc_discord_id', GMGet('bn_discord_id', null));
  let authToastShown = false;

  function storeSession(j){
    if(j.session_id) { sessionId=j.session_id; GMSet('bnc_session_id', sessionId); }
    if(j.req_secret) { GMDel('bnc_req_secret'); GMDel('bn_req_secret'); }
    if(j.initial_token) { currentReqToken=j.initial_token; GMSet('bnc_req_token', currentReqToken); reqCounter=0; GMSet('bnc_req_counter', 0); }
    else if(j.new_req_token){ currentReqToken=j.new_req_token; GMSet('bnc_req_token', currentReqToken); reqCounter=Number(j.new_counter??reqCounter+1); GMSet('bnc_req_counter', reqCounter); }
    else if(j.new_counter!=null){ reqCounter=j.new_counter; GMSet('bnc_req_counter', reqCounter); }
    if(j.discord_id){ discordId=j.discord_id; GMSet('bnc_discord_id', discordId); }
    // NOTE: the login token is deliberately KEPT (never deleted here): the
    // server's used-token path returns the still-active session, so a lost
    // session can re-validate silently after restarts instead of asking the
    // player to link again. A truly dead token is wiped on explicit reject.
    if(j.session_id) { try{ stopRevalidate(); }catch{} }
    GMDel('bnc_req_secret'); GMDel('bn_req_secret');
  }
  // Session-only clear: drops the session, keeps login token + identity so
  // silentRevalidate can restore without the player. Never prompts by itself.
  function clearSessionOnly(){
    sessionId=null; currentReqToken=null; reqCounter=0;
    try{ GMDel('bnc_session_id'); GMDel('bn_session_id'); GMDel('bnc_req_token'); GMDel('bn_req_token'); GMDel('bnc_req_counter'); GMDel('bn_req_counter'); }catch{}
  }
  // Silent re-validation with the kept login token. Explicit rejection =
  // token truly dead (full wipe + prompt). Anything else (timeout, 500,
  // 503) = transient: keep retrying quietly every 60s, never wipe.
  let _revalTimer=null;
  function stopRevalidate(){ try{ if(_revalTimer){ clearInterval(_revalTimer); _revalTimer=null; } }catch{} }
  function scheduleRevalidate(){
    try{
      if(_revalTimer || !loginToken) return;
      _revalTimer=setInterval(()=>{ try{ silentRevalidate('periodic'); }catch{} }, 60000);
    }catch{}
  }
  // `opts.cb(ok, transient)` lets a caller follow the refresh: ok=true means a
  // session came back, transient=true means the API just could not be reached
  // (we keep retrying). `opts.quiet` suppresses the link prompt - used when the
  // session-expired modal is already doing the talking.
  function silentRevalidate(why, opts){
    const done=(opts && typeof opts.cb==='function') ? opts.cb : null;
    const quiet=!!(opts && opts.quiet);
    try{
      if(!loginToken){ if(done) done(false, false); return; }
      if(sessionId) { try{ stopRevalidate(); }catch{} if(done) done(true, false); return; } // already restored
      apiPost('/api/validate-token', {token: loginToken, _quiet:true}, (ok, txt)=>{
        try{
          if(ok){
            const j=JSON.parse(txt);
            if(j.discord_username) { discordUsername=j.discord_username; GMSet('bnc_discord_username', discordUsername); }
            if(j.game_username) GMSet('bnc_game_username', j.game_username);
            if(j.game_id) GMSet('bnc_game_id', j.game_id);
            if(j.session_id){ storeSession(j); try{ logInfo(`[AUTH] session restored (${why})`); }catch{} try{ showToast('Session restored', false); }catch{} if(done) done(true, false); return; }
            if(done) done(false, false);
            return;
          }
          let reason=''; try{ reason=(JSON.parse(txt||'{}').error)||''; }catch{}
          const rl=String(reason).toLowerCase();
          if(rl.includes('locked')){
            try{ showLockedPanel(reason || 'Account locked', 'default'); }catch{}
            try{ stopRevalidate(); }catch{}
            if(done) done(false, false);
            return;
          }
          const dead=rl.includes('invalid')||rl.includes('expired')||rl.includes('not found')||rl.includes('no account')||rl.includes('incorrect');
          if(dead){
            // token truly dead: full wipe (+ the link prompt, unless a caller
            // is already showing the session-expired modal)
            cleanTokenStorage(); hasWelcomed=false;
            try{ stopRevalidate(); }catch{}
            if(quiet){ if(done) done(false, false); }
            else setTimeout(()=>{ showTokenPrompt(); showToast('Session expired - enter new token', true); }, 800);
          } else {
            try{ logInfo(`[AUTH] revalidate failed (${reason||'network'}) - retrying quietly`); }catch{}
            scheduleRevalidate();
            if(done) done(false, true);
          }
        }catch{}
      });
    }catch{}
  }

  const PAL_EARLY = { bgInfo: 'background:#1f2937;color:#e5e7eb;padding:1px 6px;border-radius:3px' };
  function logInfoEarly(msg, ...rest) { console.log(`%c[BNC]%c %c[INF]%c ${msg}`, 'color:#6b7280;font-weight:800', '', PAL_EARLY.bgInfo, '', ...rest); }
  const PAL = { bgSys: 'background:#7c3aed;color:#fff;padding:1px 6px;border-radius:3px;font-weight:900', bgOk: 'background:#10b981;color:#002018;padding:1px 6px;border-radius:3px;font-weight:800', bgErr: 'background:#ef4444;color:#fff;padding:1px 6px;border-radius:3px;font-weight:900', bgInfo: 'background:#1f2937;color:#e5e7eb;padding:1px 6px;border-radius:3px' };
  function _ts(){ return new Date().toLocaleTimeString('en-GB',{hour12:false})+'.'+String(Date.now()%1000).padStart(3,'0'); }
  function logSys(msg, ...rest) { console.log(`%c[BNC]%c %c[SYS]%c ${_ts()} ${msg}`, 'color:#7c3aed;font-weight:900', '', PAL.bgSys, '', ...rest); }
  function logOk(msg, ...rest) { console.log(`%c[BNC]%c %c[OK]%c ${_ts()} ${msg}`, 'color:#10b981;font-weight:900', '', PAL.bgOk, '', ...rest); }
  function logErr(msg, ...rest) { console.warn(`%c[BNC]%c %c[ERR]%c ${_ts()} ${msg}`, 'color:#ef4444;font-weight:900', '', PAL.bgErr, '', ...rest); }
  function logInfo(msg, ...rest) { console.log(`%c[BNC]%c %c[INF]%c ${_ts()} ${msg}`, 'color:#6b7280;font-weight:800', '', PAL.bgInfo, '', ...rest); }
  function logGroup(label, fn){
    try{ console.groupCollapsed(`%c[BNC] ${label}`, 'color:#7c3aed;font-weight:700'); fn(); console.groupEnd(); }catch{ fn(); }
  }

  // ==== Loading-screen branding (Nectar rebrand of the CrazyGames loading shell) ====
  // The loading shell lives inside an intermediate CrazyGames iframe (not the
  // top page, not the game iframe). This module runs in ALL CrazyGames frames
  // and overwrites the shell elements every tick — idempotent, safe everywhere.
  const BNC_REBRAND_BANNER = 'https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/lobby-music/nectar_banner.png';
  const BNC_REBRAND_TITLE = 'Nectar - Buildnow Enhanced';
  // Always on. The toggle that used to gate this was removed, so it is not a
  // setting any more - and a stale bnc_loading_rebrand=false left behind must
  // not keep the shell unbranded with no way to turn it back on.
  function bncRebrandOn(){ return true; }
  function bncRebrandSetText(el,txt){
    if(!el) return false;
    try{
      const cur=el.textContent;
      if(cur===txt) return true;
      el.textContent=txt;
      try{ if(el.innerText!==undefined&&el.innerText!==txt) el.innerText=txt; }catch{}
      return true;
    }catch{ return false; }
  }
  function bncRebrandFindInDoc(doc){
    let found=false, changed=false;
    const launching=`Launching With Nectar v${BNC_VERSION}`;
    try{
      const title=doc.getElementById('overlayThumbnailTitle');
      if(title){ found=true; const before=title.textContent; if(bncRebrandSetText(title,BNC_REBRAND_TITLE)&&before!==BNC_REBRAND_TITLE) changed=true; }
    }catch{}
    try{
      const msgs=doc.querySelectorAll('._messages_fbnci_122 div')||[];
      for(const m of msgs){ found=true; const before=m.textContent; if(bncRebrandSetText(m,launching)&&before!==launching) changed=true; }
    }catch{}
    try{
      const head1=doc.querySelector('.css-vuljoq');
      const t1=head1&&(head1.children&&head1.children[0]);
      if(t1){ found=true; const before=t1.textContent; if(bncRebrandSetText(t1,launching)&&before!==launching) changed=true; }
    }catch{}
    try{
      const head2=doc.querySelector('.css-inwm4l');
      const t2=head2&&(head2.children&&head2.children[0]);
      if(t2){ found=true; const before=t2.textContent; if(bncRebrandSetText(t2,BNC_REBRAND_TITLE)&&before!==BNC_REBRAND_TITLE) changed=true; }
    }catch{}
    try{
      const imgs=[...(doc.querySelectorAll('img')||[])];
      for(const im of imgs){
        let isCover=false;
        try{
          const s=(im.currentSrc||im.src||'')+'';
          const c=(im.className&&im.className.baseVal!==undefined?im.className.baseVal:im.className||'')+'';
          const ss=(im.getAttribute&&im.getAttribute('srcset'))||im.srcset||'';
          isCover=s.includes('buildnow-gg_16x9-cover')||String(ss).includes('buildnow-gg_16x9-cover')||String(c).includes('_root_gg1yh_');
        }catch{}
        if(!isCover) continue;
        found=true;
        try{ if(im.src!==BNC_REBRAND_BANNER){ im.src=BNC_REBRAND_BANNER; changed=true; } }catch{}
        try{ if(im.srcset){ im.removeAttribute('srcset'); changed=true; } }catch{}
        try{ if(im.getAttribute&&im.getAttribute('srcset')){ im.removeAttribute('srcset'); changed=true; } }catch{}
      }
    }catch{}
    return {found, changed};
  }
  function bncRebrandOnce(){
    if(!bncRebrandOn()) return 'disabled';
    const r=bncRebrandFindInDoc(document);
    return !r.found?'waiting':(r.changed?'applied':'enforced');
  }
  let bncRebrandTimer=null, bncRebrandLogged=false;
  function bncRebrandBoot(){
    if(bncRebrandTimer) return;
    if(!bncRebrandOn()) return;
    logInfo('[REBRAND] loading-screen branding on');
    // persistent: the shell rotates loading messages on its own timer, so
    // re-enforce while the loading screen is present (writes are idempotent)
    bncRebrandTimer=setInterval(()=>{
      let st='waiting';
      try{ st=bncRebrandOnce(); }catch{}
      if(st!=='waiting'&&!bncRebrandLogged){ bncRebrandLogged=true; logInfo('[REBRAND] loading screen branded'); }
      if(st==='waiting') bncRebrandLogged=false;
    },800);
  }

  // ---- CrazyGames frames: brand the loading shell ----
  // The loading shell lives inside an intermediate iframe (games.crazygames.com),
  // NOT on the top page. Let the rebrand run in every frame — it's idempotent.
  // Only the full tracker is gated to the game frame.
  try{ bncRebrandBoot(); bncRebrandOnce(); }catch(e){}

  // ---- Only the full tracker runs inside the game frame ----
  if (!IS_GAME_FRAME) return;

  // session restore - login required, no guest capture
  if(sessionId && currentReqToken){
    logInfoEarly(`session restored ${String(sessionId).slice(0,8)}… counter=${reqCounter}`);
  } else if(loginToken){
    // session-less boot with a kept login token: silent re-validate restores
    // the still-active session (server reuse path) with no prompt. Retries
    // quietly on network failure; only an explicit rejection wipes + prompts.
    setTimeout(()=>{ try{ silentRevalidate('boot'); }catch{} }, 600);
  }

  // queue: one at a time to avoid req_token reuse
  let apiQueue = [];
  let apiBusy = false;
  function apiPost(path, body, cb){
    const clean={...body}; delete clean._retried;
    const quiet=!!clean._quiet; delete clean._quiet;
    const noLive=!!clean._noLiveness; delete clean._noLiveness;
    const noTok=!!clean._noToken; delete clean._noToken;
    const noWipe=!!clean._noWipe; delete clean._noWipe;
    apiQueue.push({path, body: clean, cb, retried: !!body._retried, quiet, noLive, noTok, noWipe});
    processApiQueue();
  }
  function processApiQueue(){
    if(apiBusy || !apiQueue.length) return;
    apiBusy = true;
    const {path, body: origBody, cb, retried, quiet, noLive, noTok, noWipe} = apiQueue.shift();
    let body = {...origBody};
    if(path!=='/api/validate-token' && sessionId && (currentReqToken || noTok)){
      if(path==='/api/capture'){
        body = { ...body, authSessionId: sessionId, discord_id: discordId };
        // _noToken (heartbeat): touch the session without rotating/burning
        // a single-use req_token. Server proceeds, skips rotation, 204s.
        if(!noTok && currentReqToken) body.req_token = currentReqToken;
        // heartbeats opt out: they warm the server session but must never
        // stamp client liveness (that would resurrect ghost blocks / dot).
        if(!noLive){ try{ lastCaptureSentAt=Date.now(); }catch{} }
      } else {
        body = { ...body, session_id: sessionId, discord_id: discordId, req_token: currentReqToken };
      }
    } else if(path!=='/api/validate-token' && !sessionId){
      logErr(`apiPost ${path} skipped - no session`);
      try{cb(false,'no session');}catch{}
      apiBusy = false; setTimeout(processApiQueue, 10); return;
    }
    const base = resolveApiBase();
    let target = path;
    if(base.includes('vercel.app') && path==='/api/validate-token') target = '/api/auth/verify';
    const url = base + target;
    const GM = typeof GM_xmlhttpRequest !== 'undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow !== 'undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
    const hasGM = !!GM;
    const doFetch = (u, useGM)=>{
      const wrapOk = (ok, txt)=>{
        if(ok){
          try{
            const j=JSON.parse(txt);
            if(j.new_req_token) { storeSession({new_req_token:j.new_req_token, new_counter:j.new_counter}); logInfo(`req_token rotated ${String(j.new_req_token).slice(0,4)}… counter ${j.new_counter}`); }
            else if(j.initial_token) storeSession(j);
            else if(j.new_counter!=null) { reqCounter=j.new_counter; GMSet('bnc_req_counter', reqCounter); }
            if(j.game_username){ GMSet('bnc_game_username', j.game_username); logInfo(`game_username updated → ${j.game_username}`); }
            if(j.game_id){ GMSet('bnc_game_id', j.game_id); logInfo(`game_id updated → ${j.game_id}`); }
          }catch{}
        } else {
          try{
            const j=JSON.parse(txt);
            const lowerErr = String(j.error||'').toLowerCase();
            if(lowerErr.includes('locked') || lowerErr.includes('account locked')){
              isLocked = true; queue=[]; flushTimer=null;
              logErr(`locked via ${path}: ${j.error}`);
              showLockedPanel(j.error, j.lock_scope);
            } else if(j.error && String(j.error).toLowerCase().includes('replay')){
              if(j.current) { currentReqToken=j.current; GMSet('bnc_req_token', currentReqToken); logInfo(`replay sync token → ${String(currentReqToken).slice(0,4)}…`); }
              else if(j.new_req_token) storeSession(j);
              if(!retried){
                logInfo(`replay detected for ${path} - retry`);
                apiQueue.unshift({path, body: {...origBody, _retried:true}, cb, retried:true, quiet, noLive, noTok, noWipe});
                apiBusy = false; setTimeout(processApiQueue, 30); return;
              }
            }
            // A Nectar session lives 2 days; when it runs out the API says
            // "Session expired - please re-link" and every call stops working.
            // Handled in one place, whichever endpoint noticed it first.
            if(lowerErr.includes('session expired')){
              try{ onSessionExpired(path); }catch{}
            }
            if(j.error && String(j.error).toLowerCase().includes('invalid')){
              if(j.current){ currentReqToken=j.current; GMSet('bnc_req_token', currentReqToken); logInfo(`mismatch sync token → ${String(currentReqToken).slice(0,4)}…`); if(!retried){ logInfo(`mismatch retry ${path}`); apiQueue.unshift({path, body: {...origBody, _retried:true}, cb, retried:true, quiet, noLive, noTok, noWipe}); apiBusy=false; setTimeout(processApiQueue,30); return; } }
              // rejected with no resync token: historically wiped the session
              // on first sight — but that fires on transient DB read misses
              // ("Invalid auth session") too. Retry once, then clear the
              // SESSION only (login token kept) and re-validate silently;
              // full wipe + prompt happens only on explicit rejection
              // inside silentRevalidate. _noWipe requests never touch this.
              else if(noWipe){ try{ logErr(`auth rejected (${path}) - no-wipe request, session kept`); }catch{} }
              else if(!retried){ try{ logInfo(`auth rejected (${path}) - retry once before clearing`); }catch{} apiQueue.unshift({path, body: {...origBody, _retried:true}, cb, retried:true, quiet, noLive, noTok, noWipe}); apiBusy=false; setTimeout(processApiQueue,1500); return; }
              else { logErr(`auth rejected twice - clearing session, re-validating`); try{ clearSessionOnly(); }catch{} try{ silentRevalidate('invalid'); }catch{} }
            }
          }catch{}
        }
        try{ cb(ok, txt); }catch{}
        apiBusy = false; setTimeout(processApiQueue, 10);
      };
      const wrapFail = (msg)=>{ logErr(msg); try{ cb(false, msg); }catch{} apiBusy=false; setTimeout(processApiQueue,20); };
      if(useGM && hasGM){
        try{
          // Captures carry engine feed + full DB processing server-side, so
          // match-start bursts legitimately take longer — give them 30s.
          // A 15s timeout here resends a batch the server is still working
          // on, doubling its load (the timed-out work still completes).
          const postTimeout = String(u).includes('/api/capture') ? 30000 : 15000;
          GM({ method:'POST', url:u, headers:{'Content-Type':'application/json'}, data: JSON.stringify(body), timeout:postTimeout,
            onload: r=> { const ok=r.status>=200&&r.status<300; const txt=r.responseText||''; const isZone=txt.includes('"zone"'); if(!quiet||!ok){ (ok?logOk:logErr)(`API ${ok?'OK':'ERR'} ${u} → ${r.status} ${String(txt).slice(0, isZone?1200:600)}`); } if(isZone){ try{ const j=JSON.parse(txt); const zps=(j.packets||[]).filter(p=> (p.packet||p.packet_type)==='zone'); if(zps.length){ logGroup(`API zone batch ${zps.length} packets`, ()=>{ zps.forEach(zp=> console.log('zone packet', zp, 'decrypted', zp.decrypted_hex||zp.raw)); }); } }catch{} } wrapOk(ok, r.responseText); },
            onerror: (e)=>{ wrapFail(`API GM_xhr error → ${u} ${e&&e.error||''}`); },
            ontimeout: ()=>{ wrapFail(`API timeout (${postTimeout/1000}s) → ${u}`); }});
          return true;
        }catch(e){ wrapFail(`API GM throw ${e.message}`); return false; }
      }
      try{
        fetch(u,{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(r=>r.text().then(t=>{ const ok=r.ok; const isZone=t.includes('"zone"'); if(!quiet||!ok){ (ok?logOk:logErr)(`API fetch ${ok?'OK':'ERR'} ${u} → ${r.status} ${t.slice(0, isZone?1200:600)}`); } if(isZone){ try{ const j=JSON.parse(t); const zps=(j.packets||[]).filter(p=> (p.packet||p.packet_type)==='zone'); if(zps.length){ logGroup(`API zone batch ${zps.length} packets`, ()=>{ zps.forEach(zp=> console.log('zone packet', zp, 'decrypted', zp.decrypted_hex||zp.raw)); }); } }catch{} } wrapOk(ok,t); }).catch(()=>wrapFail(`fetch catch ${u}`))).catch((e)=>{ wrapFail(`API fetch fail ${u} ${e.message}`); });
        return true;
      }catch(e){ wrapFail(`API fetch throw ${e.message}`); return false; }
    };
    if(!doFetch(url, true)) doFetch(url, false);
  }

  // toast helpers + welcome toast per page load (once per session, deduped 30 min)
  let lastToastAt=0, lastToastText='', toastReady=false, toastLoading=false;
  let lastWelcomeAt = Number(GMGet('bnc_welcome_at', 0)||0);
  function showWelcomeToast(){
    const now = Date.now();
    if(now - lastWelcomeAt < 30*60*1000) return;
    lastWelcomeAt = now; GMSet('bnc_welcome_at', now);
    // delay to let toastReady load
    setTimeout(()=>{
      if(sessionId) showToast(`BuildNow Companion - Linked${discordUsername? ' as '+discordUsername:''}`, false);
      else if(loginToken) showToast('BuildNow Companion - Ready', false);
      else showToast('BuildNow Companion - Login required', true);
    }, 900);
  }
  // fire welcome once per load
  setTimeout(showWelcomeToast, 700);
  function showToast(text, isError){
    const now=Date.now(); if(text===lastToastText && now-lastToastAt<3000) return;
    lastToastText=text; lastToastAt=now;
    const style={background:isError?'#991b1b':'#000',color:'#fff',borderRadius:'8px',border:'1px solid rgb(255 255 255 / 12%)',boxShadow:'0 4px 16px rgb(0 0 0 / 12%)',fontFamily:'Inter, system-ui, sans-serif',fontSize:'14px',fontWeight:'500',padding:'12px 16px'};
    const doShow=()=>{
      try{
        if(typeof Toastify!=='undefined'){ Toastify({text,duration:3200,gravity:'top',position:'right',stopOnFocus:true,style}).showToast(); }
        else { const d=document.createElement('div'); d.textContent=text; Object.assign(d.style,{position:'fixed',top:'16px',right:'16px',zIndex:'2147483647',background:style.background,color:style.color,borderRadius:style.borderRadius,padding:style.padding}); (document.body||document.documentElement).appendChild(d); setTimeout(()=>d.remove(),3200); }
      }catch{}
    };
    if(toastReady){ doShow(); return; }
    if(toastLoading) return;
    toastLoading=true;
    try{
      const link=document.createElement('link'); link.rel='stylesheet'; link.href='https://cdn.jsdelivr.net/npm/toastify-js@1.12.0/src/toastify.min.css'; document.documentElement.appendChild(link);
      const s=document.createElement('script'); s.src='https://cdn.jsdelivr.net/npm/toastify-js@1.12.0/src/toastify.min.js';
      s.onload=()=>{ toastReady=true; toastLoading=false; doShow(); };
      s.onerror=()=>{ toastReady=true; toastLoading=false; doShow(); };
      document.documentElement.appendChild(s);
      setTimeout(()=>{ if(!toastReady){ toastReady=true; toastLoading=false; doShow(); } },2000);
    }catch{ toastReady=true; toastLoading=false; doShow(); }
  }
  // The game frame is sandboxed without allow-top-navigation, so the script can
  // never reload anything itself (not even its own frame reliably). Instead tell
  // the user to reload with Ctrl+R.
  function promptReload(msg){
    try{ showToast(msg || 'Press Ctrl+R to reload the page', false); }catch{}
    try{ logInfo('[UI] prompt reload: ' + (msg || 'Press Ctrl+R')); }catch{}
  }
  // ---- PARTY helpers ----
  // sess shape: /api/stats session {party:{is_party,code}, short_code, ...}
  function getPartyInfo(sess){
    if(!sess) return {isParty:false, code:null};
    const p = sess.party || null;
    const isParty = !!(p && p.is_party);
    const code = sess.short_code || (p && p.code) || null;
    return {isParty, code};
  }
  function showLockedPanel(reason, scope){
    if(document.getElementById('bnc-locked')) return;
    const isAdmin = String(scope||'').toLowerCase()==='admin';
    const title = isAdmin ? 'Admin-locked' : 'Account locked';
    const help = isAdmin ? 'This is an <b style="color:#fff">admin lock</b> - only an admin can run <b style="color:#fff">/adminunlock</b> on you. Disable the tracker to play normally.' : 'Use <b style="color:#fff">/unlock</b> in Discord or ask an admin. Disable the tracker to play normally.';
    const wrap=document.createElement('div');
    wrap.id='bnc-locked';
    Object.assign(wrap.style,{position:'fixed',inset:'0',zIndex:'2147483647',background:'rgba(0,0,0,0.82)',display:'flex',alignItems:'center',justifyContent:'center',padding:'24px',fontFamily:'Inter, system-ui, sans-serif'});
    const card=document.createElement('div');
    Object.assign(card.style,{background:'#0a0a0a',border:'1px solid #991b1b',borderRadius:'14px',padding:'28px',width:'420px',textAlign:'center',boxShadow:'0 8px 32px rgba(239,68,68,0.25)'});
    card.innerHTML = `<div style="font-size:44px;margin-bottom:8px">${isAdmin?'⛔':'🔒'}</div><div style="font-size:18px;font-weight:800;color:#fff;margin-bottom:6px">${title}</div><div style="font-size:13px;color:#fca5a5;margin-bottom:6px;word-break:break-word">${String(reason||'Token reuse or sharing detected - friend account use is blocked.').replace(/</g,'&lt;')}</div><div style="font-size:12px;color:#9ca3af;margin-bottom:16px">${help}</div><div style="display:flex;gap:8px;justify-content:center"><button id="bnc-locked-disable" style="flex:1;height:40px;border:0;border-radius:8px;background:#fff;color:#000;font-weight:700;cursor:pointer">Disable & Reload</button><button id="bnc-locked-retry" style="flex:1;height:40px;border:1px solid #374151;border-radius:8px;background:#1f2937;color:#e5e7eb;font-weight:600;cursor:pointer">Retry</button></div><div style="margin-top:10px;font-size:11px;color:#6b7280">BNC is disabled while locked - no capture or API calls.</div>`;
    wrap.appendChild(card);
    // block game input
    wrap.addEventListener('click', e=> e.stopPropagation());
    wrap.addEventListener('keydown', e=> e.stopPropagation(), true);
    // freeze capture
    try{ queue=[]; flushTimer=null; }catch{}
    (document.body||document.documentElement).appendChild(wrap);
    // prevent game hotkeys
    const blocker=(e)=>{ e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation(); };
    ['keydown','keyup','keypress'].forEach(ev=> window.addEventListener(ev, blocker, true));
    document.getElementById('bnc-locked-disable').onclick=()=>{
      GMSet('bnc_disabled', true);
      try{ GMDel('bnc_session_id'); GMDel('bnc_req_token'); }catch{}
      promptReload('BNC disabled - press Ctrl+R to reload and play normally');
    };
    document.getElementById('bnc-locked-retry').onclick=()=>{
      wrap.remove();
      ['keydown','keyup','keypress'].forEach(ev=> window.removeEventListener(ev, blocker, true));
      promptReload('Press Ctrl+R to reload the page');
    };
  }
  // ---- SESSION EXPIRED -------------------------------------------------
  // A Nectar session lives 2 days (server: SESSION_TTL_DAYS). When it runs out
  // the API answers 401 "Session expired - please re-link" and everything the
  // companion does goes quiet: no stats, no points, no alerts. The saved login
  // token usually refreshes it silently, so we try that first - and when that
  // fails the player needs a fresh token, which is exactly what
  // /refresh-token in the Discord server hands out.
  const SESSION_INVITE = 'https://discord.gg/GDSW9p39J5';
  let _sessionFixAt=0, _sessionFixBusy=false;
  function sessionModalStatus(msg, tone){
    try{
      const el=document.getElementById('bnc-session-status');
      if(!el) return;
      el.textContent=msg||'';
      el.style.color = tone==='bad' ? '#fca5a5' : (tone==='ok' ? '#6ee7b7' : '#9ca3af');
    }catch{}
  }
  function hideSessionExpiredModal(){
    try{ const w=document.getElementById('bnc-session-expired'); if(w && w._bncCleanup) w._bncCleanup(); if(w) w.remove(); }catch{}
  }
  function showSessionExpiredModal(){
    if(document.getElementById('bnc-session-expired')) return;
    const wrap=document.createElement('div');
    wrap.id='bnc-session-expired';
    Object.assign(wrap.style,{position:'fixed',inset:'0',zIndex:'2147483647',background:'rgba(0,0,0,0.82)',display:'flex',alignItems:'center',justifyContent:'center',padding:'24px',fontFamily:'Inter, system-ui, sans-serif'});
    const card=document.createElement('div');
    Object.assign(card.style,{background:'#0a0a0a',border:'1px solid rgba(255,255,255,0.16)',borderRadius:'14px',padding:'26px',width:'440px',boxSizing:'border-box',boxShadow:'0 8px 32px rgba(0,0,0,0.55)'});
    card.innerHTML='<div style="font-size:30px;margin-bottom:6px">🔁</div>'
      + '<div style="font-size:18px;font-weight:800;color:#fff;margin-bottom:6px">Session expired</div>'
      + '<div style="font-size:13px;color:#9ca3af;line-height:1.6;margin-bottom:14px">Nectar sessions last 2 days. Until a new one is linked, your stats, points and alerts stop updating.</div>'
      + '<div style="font-size:12px;color:#e5e7eb;line-height:1.8;margin-bottom:10px">'
      +   '<div>1. In the Nectar Discord server run <code style="background:#1f2937;border-radius:4px;padding:1px 6px;color:#fff;font-family:monospace">/refresh-token</code> - it DMs you a fresh token.</div>'
      +   '<div>2. Paste it below and hit <b style="color:#fff">Save &amp; reload</b>.</div>'
      + '</div>'
      + '<input id="bnc-session-token" type="text" placeholder="Paste token (A1B2C3D4)" autocomplete="off" spellcheck="false" maxlength="32" style="width:100%;height:40px;padding:0 12px;border:1px solid #374151;border-radius:8px;background:#111;color:#fff;font-size:15px;font-family:monospace;letter-spacing:1.5px;box-sizing:border-box;outline:none">'
      + '<div style="display:flex;gap:8px;margin-top:12px">'
      +   '<button id="bnc-session-save" style="flex:1;height:40px;border:0;border-radius:8px;background:#fff;color:#000;font-weight:700;cursor:pointer">Save &amp; reload</button>'
      +   '<button id="bnc-session-reload" style="flex:1;height:40px;border:1px solid #374151;border-radius:8px;background:#1f2937;color:#e5e7eb;font-weight:600;cursor:pointer">Reload page</button>'
      + '</div>'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">'
      +   '<a href="'+SESSION_INVITE+'" target="_blank" rel="noopener" style="font-size:12px;color:#a5b4fc;text-decoration:none">Open the Nectar Discord server</a>'
      +   '<button id="bnc-session-later" style="background:none;border:0;color:#6b7280;font-size:12px;cursor:pointer">Not now</button>'
      + '</div>'
      + '<div id="bnc-session-status" style="margin-top:10px;font-size:12px;color:#9ca3af;min-height:16px"></div>';
    wrap.appendChild(card);
    (document.body||document.documentElement).appendChild(wrap);
    wrap.addEventListener('click', e=> e.stopPropagation());
    const input=card.querySelector('#bnc-session-token');
    // The game installs capture-phase key listeners before we do, so typing in
    // this box needs the same guard the link prompt uses: kill the event for the
    // game, but never preventDefault (the input needs every key).
    const guard=(e)=>{ try{ const ae=document.activeElement; const t=e.target; if(input && (input===t || input.contains(t) || card.contains(t) || (ae && card.contains(ae)))) { e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation(); } }catch{} };
    const events=['keydown','keyup','keypress','input','paste','cut','compositionstart','compositionupdate','compositionend'];
    const targets=[window, document, document.documentElement];
    try{ if(typeof unsafeWindow!=='undefined' && unsafeWindow!==window) targets.push(unsafeWindow); }catch{}
    events.forEach(ev=> targets.forEach(t=>{ try{ t.addEventListener(ev, guard, true); }catch{} }));
    wrap._bncCleanup=()=>{ events.forEach(ev=> targets.forEach(t=>{ try{ t.removeEventListener(ev, guard, true); }catch{} })); };
    let linking=false;
    const save=card.querySelector('#bnc-session-save');
    const submit=()=>{
      if(linking) return;
      const tok=String(input && input.value || '').trim().toUpperCase();
      if(!tok){ sessionModalStatus('Enter the token from /refresh-token', 'bad'); return; }
      if(tok.length<8){ sessionModalStatus('Token too short', 'bad'); return; }
      linking=true; save.textContent='Linking…'; sessionModalStatus('Checking the token…');
      apiPost('/api/validate-token', {token: tok}, (ok, txt)=>{
        let reason='', scope='default';
        try{ const j=JSON.parse(txt||'{}'); reason=j.error||''; scope=j.lock_scope||(j.is_admin_locked?'admin':'default'); }catch{}
        if(!ok && String(reason).toLowerCase().includes('locked')){ hideSessionExpiredModal(); showLockedPanel(reason, scope); return; }
        if(!ok){ linking=false; save.textContent='Save & reload'; sessionModalStatus('Token incorrect - run /refresh-token again', 'bad'); return; }
        try{
          const j=JSON.parse(txt);
          if(j.discord_username) GMSet('bnc_discord_username', j.discord_username);
          if(j.game_username) GMSet('bnc_game_username', j.game_username);
          if(j.session_id || j.initial_token) storeSession(j);
          else { GMSet('bnc_token', tok); loginToken=tok; }
        }catch{ GMSet('bnc_token', tok); loginToken=tok; }
        sessionModalStatus('Linked - reloading…', 'ok');
        setTimeout(()=>{ try{ location.reload(); }catch{} }, 600);
      });
    };
    if(save) save.onclick=submit;
    const reload=card.querySelector('#bnc-session-reload');
    if(reload) reload.onclick=()=>{ try{ location.reload(); }catch{} };
    const later=card.querySelector('#bnc-session-later');
    if(later) later.onclick=()=>{ hideSessionExpiredModal(); try{ showToast('Session expired - run /refresh-token to fix it', true); }catch{} };
    if(input) input.addEventListener('keydown', (e)=>{ if(e.key==='Enter'){ e.preventDefault(); submit(); } });
    setTimeout(()=>{ try{ if(input) input.focus(); }catch{} }, 80);
  }
  // One entry point for every "your session is gone" signal (capture 401 or any
  // other API reply). Tries the silent refresh first, keeps the player informed
  // through the modal, and never stacks a second modal on top.
  function onSessionExpired(why){
    try{
      const now=Date.now();
      if(_sessionFixBusy) return;                       // a refresh is already in flight
      if(document.getElementById('bnc-session-expired') && now-_sessionFixAt<30000) return;
      _sessionFixAt=now; _sessionFixBusy=true;
      try{ logInfo(`[AUTH] session expired (${why||'api'}) - refreshing`); }catch{}
      showSessionExpiredModal();
      sessionModalStatus('Refreshing automatically…');
      let had=false;
      try{ if(sessionId){ clearSessionOnly(); had=true; } }catch{}
      if(!loginToken){
        sessionModalStatus('No saved token to refresh with - run /refresh-token above.', 'bad');
        _sessionFixBusy=false;
        return;
      }
      try{
        silentRevalidate('expired', {quiet:true, cb:(ok, transient)=>{
          try{
            _sessionFixBusy=false;
            if(ok){ sessionModalStatus(had?'Session refreshed - you can keep playing (reload whenever).':'Session refreshed.', 'ok'); }
            else if(transient){ sessionModalStatus('Nectar is unreachable right now - still retrying in the background.', null); }
            else sessionModalStatus('Could not refresh with the saved token - run /refresh-token and paste the new one.', 'bad');
          }catch{}
        }});
      }catch{ _sessionFixBusy=false; }
    }catch{ _sessionFixBusy=false; }
  }
  function showTokenPrompt(){
    if (loginToken || sessionId) return;
    const old=document.getElementById('bnc-login-wrap'); if(old) old.remove();

    const wrap=document.createElement('div');
    wrap.id='bnc-login-wrap';
    Object.assign(wrap.style,{position:'fixed',inset:'0',zIndex:'2147483647',background:'rgba(0,0,0,0.44)',display:'flex',alignItems:'center',justifyContent:'center',padding:'24px',fontFamily:'Inter, system-ui, sans-serif'});

    const card=document.createElement('div');
    Object.assign(card.style,{background:'#fff',border:'1px solid rgba(0,0,0,0.1)',borderRadius:'12px',padding:'32px',width:'360px',boxShadow:'0 4px 24px rgba(0,0,0,0.08)',textAlign:'left'});

    const title=document.createElement('div');
    title.textContent='Link Discord';
    Object.assign(title.style,{fontSize:'18px',fontWeight:'600',color:'#000',margin:'0 0 8px 0'});

    const sub=document.createElement('div');
    sub.innerHTML='Enter token from <span style="color:#000;font-weight:500">/register</span> in Discord.';
    Object.assign(sub.style,{fontSize:'14px',color:'rgba(0,0,0,0.64)',margin:'0 0 20px 0'});

    const input=document.createElement('input');
    input.type='text';
    input.placeholder='A1B2C3D4';
    input.autocomplete='off';
    input.spellcheck=false;
    input.maxLength=32;
    input.id='bnc-token-field';
    input.setAttribute('inputmode','text');
    Object.assign(input.style,{width:'100%',height:'42px',padding:'0 12px',border:'1px solid rgba(0,0,0,0.15)',borderRadius:'8px',fontSize:'16px',fontFamily:'monospace',letterSpacing:'1.5px',outline:'none',boxSizing:'border-box',color:'#000',background:'#fafafa',caretColor:'#000',WebkitTextFillColor:'#000',opacity:'1',userSelect:'text',MozUserSelect:'text',WebkitUserSelect:'text'});

    const btnRow=document.createElement('div');
    Object.assign(btnRow.style,{display:'flex',gap:'8px',marginTop:'16px'});

    const submitBtn=document.createElement('button');
    submitBtn.textContent='Link';
    Object.assign(submitBtn.style,{flex:'1',height:'42px',border:'0',borderRadius:'8px',background:'#000',color:'#fff',cursor:'pointer',fontSize:'14px',fontWeight:'600'});

    const disableBtn=document.createElement('button');
    disableBtn.textContent='Disable';
    Object.assign(disableBtn.style,{flex:'1',height:'42px',border:'1px solid rgba(0,0,0,0.1)',borderRadius:'8px',background:'#fff',cursor:'pointer',fontSize:'14px',fontWeight:'500'});
    disableBtn.title='Disable tracker and play normally';

    const msg=document.createElement('div');
    Object.assign(msg.style,{marginTop:'12px',fontSize:'13px',color:'rgba(0,0,0,0.64)',minHeight:'18px'});
    const hint=document.createElement('div');
    hint.innerHTML='No token? Disable the userscript in Tampermonkey to play normally.';
    Object.assign(hint.style,{marginTop:'8px',fontSize:'11px',color:'#6b7280',textAlign:'center'});

    btnRow.appendChild(submitBtn);
    btnRow.appendChild(disableBtn);
    card.appendChild(title);
    card.appendChild(sub);
    card.appendChild(input);
    card.appendChild(btnRow);
    card.appendChild(msg);
    card.appendChild(hint);
    wrap.appendChild(card);
    (document.body||document.documentElement).appendChild(wrap);

    // --- Fix: game canvas steals keyboard (Backspace/Enter blocked) ---
    // The game installs window-level capture listeners before we do, so
    // stopping propagation at the input element is too late (capture goes top-down).
    // We install a higher-priority guard on window/document in capture phase
    // that checks if focus/target is inside the modal and then kills the event
    // before the game sees it. We do NOT preventDefault - let the input handle it.
    const isInModal = (evTarget) => {
      const ae = document.activeElement;
      try {
        if (input.contains(evTarget)) return true;
        if (wrap.contains(evTarget)) return true;
        if (ae && (ae === input || wrap.contains(ae))) return true;
        if (evTarget && evTarget.closest && evTarget.closest('#bnc-login-wrap')) return true;
      } catch {}
      return false;
    };
    const modalGuard = (e) => {
      if (!document.getElementById('bnc-login-wrap')) return;
      if (!isInModal(e.target)) return;
      // allow Tab navigation inside modal, but block game from seeing the key
      e.stopPropagation();
      if (e.stopImmediatePropagation) e.stopImmediatePropagation();
      // do not call preventDefault - input needs Backspace/Delete/Enter/Arrows
    };
    const guardEvents = ['keydown','keyup','keypress','beforeinput','input','paste','cut','compositionstart','compositionupdate','compositionend'];
    const guardTargets = [window, document, document.documentElement];
    try { if (typeof unsafeWindow !== 'undefined' && unsafeWindow !== window) guardTargets.push(unsafeWindow); } catch {}
    guardEvents.forEach(ev => {
      guardTargets.forEach(t => {
        try { t.addEventListener(ev, modalGuard, true); } catch {}
      });
    });

    // Also guard clipboard + focus stealing
    input.addEventListener('paste', (e) => { e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation(); });
    input.addEventListener('cut', (e) => { e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation(); });

    // Cleanup when modal closes
    const cleanupGuard = () => {
      guardEvents.forEach(ev => {
        guardTargets.forEach(t => {
          try { t.removeEventListener(ev, modalGuard, true); } catch {}
        });
      });
    };

    const closeModal = () => {
      cleanupGuard();
      try { wrap.remove(); } catch {}
    };

    input.addEventListener('focus',()=>{ input.style.borderColor='#000'; });
    input.addEventListener('blur',()=>{ input.style.borderColor='rgba(0,0,0,0.15)'; });

    // Backdrop click closes - but not input/card clicks
    wrap.addEventListener('click',(e)=>{ if(e.target===wrap) closeModal(); });
    // Escape closes
    const escHandler = (e) => { if (e.key === 'Escape') closeModal(); };
    document.addEventListener('keydown', escHandler, true);

    let linking=false;
    const doSubmit = () => {
      if(linking) return;
      const tok=(input.value||'').trim().toUpperCase();
      if(!tok){ msg.textContent='Enter a token'; return; }
      if(tok.length<8){ msg.textContent='Token too short'; return; }
      linking=true;
      submitBtn.textContent='Linking…';
      submitBtn.style.opacity='0.6';
      msg.textContent='Checking…';
      apiPost('/api/validate-token', {token: tok}, (ok, txt)=>{
          let lockedReason=''; let lockedScope='default'; try{ const _p=JSON.parse(txt||'{}'); lockedReason=_p.error||''; lockedScope=_p.lock_scope||(_p.is_admin_locked?'admin':'default'); }catch{}
        if(!ok && String(lockedReason).toLowerCase().includes('locked')){ document.removeEventListener('keydown', escHandler, true); closeModal(); showLockedPanel(lockedReason, lockedScope); return; }
        if(!ok){ msg.textContent='Token incorrect - try again'; submitBtn.textContent='Link'; submitBtn.style.opacity='1'; linking=false; showToast('Token incorrect',true); return; }
        try{
          const j=JSON.parse(txt);
          if(j.discord_username) GMSet('bnc_discord_username', j.discord_username);
          if(j.game_username) GMSet('bnc_game_username', j.game_username);
          if(j.session_id || j.initial_token) storeSession(j);
          else { GMSet('bnc_token', tok); loginToken=tok; }
        }catch{
          GMSet('bnc_token', tok); loginToken=tok;
        }
        document.removeEventListener('keydown', escHandler, true);
        closeModal();
        promptReload('Linked! Press Ctrl+R to reload the page');
      });
    };

    disableBtn.onclick=()=>{
      GMSet('bnc_disabled', true);
      document.removeEventListener('keydown', escHandler, true); closeModal();
      promptReload('BNC disabled - press Ctrl+R to reload and play normally');
    };
    submitBtn.onclick=doSubmit;
    // Enter submits, Backspace/Delete must work - handled by guard not blocking default
    input.addEventListener('keydown',(e)=>{
      if(e.key==='Enter'){ e.preventDefault(); doSubmit(); }
      // prevent game hotkeys while typing (Ctrl+Shift combos are handled globally, ignore here)
      if (e.key !== 'Enter') {
        e.stopPropagation();
        if(e.stopImmediatePropagation) e.stopImmediatePropagation();
      }
    });
    // keep focus inside modal even if game tries to steal it
    const focusTrap = setInterval(()=>{ if(document.getElementById('bnc-login-wrap') && document.activeElement !== input && !wrap.contains(document.activeElement)) { try{ input.focus(); }catch{} } }, 800);
    const obs = new MutationObserver(()=>{ if(!document.getElementById('bnc-login-wrap')){ clearInterval(focusTrap); obs.disconnect(); document.removeEventListener('keydown', escHandler, true); cleanupGuard(); } });
    try{ obs.observe(document.documentElement, {childList:true}); }catch{}

    setTimeout(()=>{ try{ input.focus(); input.select(); }catch{} },80);
  }
  if (!loginToken && !sessionId) {
    if (document.readyState==='loading') document.addEventListener('DOMContentLoaded', showTokenPrompt, {once:true});
    else setTimeout(showTokenPrompt, 600);
    setTimeout(()=>{ if(!loginToken && !sessionId && !document.getElementById('bnc-login-wrap')) showTokenPrompt(); }, 1500);
  }

  // --- Packet sender only ---
  const APP_ID='30773790-f9fe-487c-92c2-19517c5c39ad';
  const PHOTON_HOSTS=['exitgames.com','photon'];
  const ESSENTIAL_OPS=new Set([225,226,227,228,230,231]); // 228 = LeaveRoom/queue-cancel candidate (semantics unconfirmed — watch op#228 lines in server logs before wiring removal)
  const ESSENTIAL_EVS=new Set([101,253,254,255]);
  const DROP_EVS_NOISY=new Set([200,201,202,204,206]);
  const BATCH_MAX=40, FLUSH_MS=250;

  // ---- OPTIMISTIC STATE ----
  let _optimisticState = {
    curScn: null,        // current scene from last packet
    inMatch: false,      // derived: true if the scene is not a lobby scene
    lastUpdateAt: 0,     // timestamp of last optimistic update
    // Set whenever the packets declare LOBBY (auth / my leave / MainLobby
    // scene) and cleared by the next non-lobby scene. While it is up nothing
    // else may claim match: the captured session state trails the game and
    // keeps saying 'match' after the player is already back in the lobby, which
    // is exactly what left the client stuck on "In Match" there.
    lobbyGuard: false,
  };
  let _curScnFlipAt=0;   // timestamp when curScn flipped to match (3s grace before ranked UI)
  // Victory-breather hold: match end looks like a lobby scene while the
  // player is still in the same ranked room (multi-match rooms hit this
  // every seal). Packet evidence of live match refreshes this; the server
  // reconcile below may only flip match->lobby once it goes stale, so a
  // slow seal never swaps the points counter for the lobby card. Leaves
  // clear it (disableRanked) for an instant flip. Server fallback reads
  // never stamp it (a trailing server 'match' must not hold itself).
  let _lastMatchEvidenceAt=0;
  const BREATHER_HOLD_MS=120000;

  // --- LOBBY <-> MATCH ------------------------------------------------
  // LOBBY is authoritative from MY Authenticate packets (op 230/231 out - the
  // login and every re-auth) and MY LeaveRoom packet (op 254 out): those are
  // the moments the game itself says "no room". Nothing else may claim lobby.
  // MATCH comes from a curScn (PropertiesChanged) that is not a lobby scene -
  // and the value is compared normalised, because it arrives as raw bytes and
  // a stray/echoed scene must never flip a quiet lobby into "in match".
  // EXCEPTION (between-matches hold, below): a lobby scene while still in the
  // same coded ranked room is the victory breather, not a leave - multi-match
  // rooms hit this every match end and the session must survive it.
  function _normScn(v){ return String(v==null?'':v).trim().toLowerCase(); }
  function _isLobbyScene(scn){ const s=_normScn(scn); return s==='mainlobby' || s==='lobby'; }
  function _isSceneLike(scn){ return /[a-z]/i.test(String(scn==null?'':scn).trim()); }
  // Endless-series hold (private zw_lg/wwzw): those rooms run infinite matches
  // back-to-back under one room code — the victory/intermission screen at every
  // stG=3 reads as a MainLobby scene while the player never left. Flipping to
  // lobby there swaps every details line (drawer, presence, Discord card, music)
  // mid-series. While the live session still holds the same private code in one
  // of those modes, a lobby scene is a breather, not a leave. Auth/leave stamps
  // bypass via opts.via (authenticate is the series end). Never throws.
  function _privateEndlessHold(){
    try{
      const sess=statsCache&&statsCache.session;
      const code=sess&&sess.short_code?String(sess.short_code).trim().toUpperCase():'';
      if(!/^[A-Z0-9]{5}$/.test(code)) return false;
      const gm=sess&&sess.game_mode?String(sess.game_mode).trim().toLowerCase():'';
      return gm==='zw_lg'||gm==='wwzw';
    }catch{ return false; }
  }

  function _applyOptimisticCurScn(curScn, opts) {
    if (!curScn) return;
    if (!_isSceneLike(curScn)) { logSys(`[OPTIMISTIC] curScn ignored (not a scene): ${JSON.stringify(String(curScn))}`); return; }
    const inMatch = !_isLobbyScene(curScn);
    if (String(curScn) === String(_optimisticState.curScn||'') && inMatch === _optimisticState.inMatch) return;
    const wasInMatch = _optimisticState.inMatch;
    // Between-matches hold: lobby scene + we never left (still in the same
    // coded ranked room, proven by liveRanked()'s block+session agreement
    // right now) = victory breather, not a leave. Leave/auth have their own
    // paths that clear intent first, so they can never take this branch.
    // Private endless-series rooms (zw_lg/wwzw under one room code) hold the
    // same way via _privateEndlessHold — except auth/leave stamps (opts.via),
    // which always end the series. Holding match mode keeps the overlay (with
    // its pending-seal honesty), music and Discord steady; the 25s server
    // reconcile still flips a genuinely abandoned victory screen, and the
    // next match scene is a noop.
    let _holdMatch = false;
    try{
      if(!inMatch && wasInMatch){
        if(typeof liveRanked==='function' && liveRanked()) _holdMatch = true;
        else if(!(opts&&(opts.via==='auth'||opts.via==='leave')) && typeof _privateEndlessHold==='function' && _privateEndlessHold()) _holdMatch = true;
      }
    }catch{}
    _optimisticState.curScn = curScn;
    _optimisticState.inMatch = _holdMatch ? true : inMatch;
    _optimisticState.lastUpdateAt = Date.now();
    // Live-match evidence (packet scene, hold included: the hold itself is
    // proof we were in a ranked match moments ago). Lobby scenes never stamp.
    if(_optimisticState.inMatch){ try{ _lastMatchEvidenceAt=Date.now(); }catch{} }
    // Lobby is a statement, not a hope: hold it until a real scene change says
    // match (see effInMatch). Match clears it. Auth/leave stamps land here too
    // (they call this with 'MainLobby'), so leaving a match is final.
    // The between-matches hold never raises the guard (nothing was left).
    _optimisticState.lobbyGuard = _holdMatch ? false : !inMatch;
    logSys(`[OPTIMISTIC] curScn=${curScn} inMatch=${_optimisticState.inMatch} lobbyGuard=${_optimisticState.lobbyGuard}${_holdMatch?' HOLD':''}`);
    // Breather = a seal is about to land: poll fast until it does instead
    // of waiting for the steady 5s cadence.
    if(_holdMatch){ try{ startSealWatch('breather'); }catch{} }
    // RANKED-tagged mirror: scene flips are the lobby<->match transition —
    // keep them visible under a [RANKED] console filter too. (This point is
    // only reached on actual change — unchanged scenes return early above.)
    try{ logInfo(`[SCENE] scene -> ${curScn} inMatch=${_optimisticState.inMatch}`); }catch{}
    const silent = opts && opts.silent;
    // Audio rule: pause ONLY on scene flip (lobby<->match). Same-side scene
    // changes and silent sets never touch audio (prevents stop+restart cycles).
    if (!silent && wasInMatch !== _optimisticState.inMatch) {
      try { updateLobbyMusic(); } catch {}
    }
    // fast drawer + ranked update on flip (all three: the match/lobby
    // split is now optimistic-first, so this renders in ms, not polls)
    if (wasInMatch !== _optimisticState.inMatch) {
      try{ renderInfoPanel(); }catch{}
      // The flip outranks any older server echo: drop the capture shadow
      // (a match-era shadow would lie for 45s in a quiet lobby) and stamp
      // the lobby flip so pre-flip batches can't re-poison it (see
      // noteCaptureSnap). Cleared on the next match-ward flip.
      try{ _capSnap=null; }catch{}
      if(_optimisticState.inMatch && !wasInMatch){
        _curScnFlipAt=Date.now();
        try{ _lobbyFlipAt=0; }catch{}
        // scene entered a match => a real room exists even if the join op
        // was missed: flip the local room flag so the Players tab appears
        // on the curScn change, not on the next stats poll.
        try{ noteLocalRoom(true); }catch{}
      } else {
        _curScnFlipAt=0;
        try{ _lobbyFlipAt=Date.now(); }catch{}
      }
      try{ updateRankedOverlay(); updateRankedLobby(); updateRankedMatchUi(); }catch{}
      try{ startTransitionBurst('curScn'); }catch{}
    }
  }

  function _normName(n) {
    if (!n) return '';
    let s = String(n).replace(/<[^>]*>/g, '').trim();
    if (s.toLowerCase().startsWith('dc:')) s = s.slice(3).trim();
    return s;
  }

  // Server-state reconciliation (enter/exit driver for audio + optimistic state).
  // Derives server in-match from EITHER shape: capture POST (full rooms map) or
  // stats GET (flat current-room fields). Flips optimistic state on disagreement
  // once optimistic is stale (>OPTIMISTIC_TRUST_MS — server trails live packets
  // by seconds), which heals stuck flags even when idle (no capture batches
  // flowing). Returns true on flip.
  function _serverInMatch(sess) {
    if(!sess) return null;
    try {
      const rooms = sess.rooms;
      const curRoom = rooms ? rooms[sess.current_room_id] : null;
      const serverCurScn = (curRoom && curRoom.cur_scn) || sess.cur_scn || null;
      // A lobby scene is the lobby, whoever put it there last - and it outranks
      // the session state, which trails the game: leaving via leave/auth clears
      // state late, so a stale 'match' here is what pinned the client in match
      // for a whole lobby (and kept the Discord card on "In Match").
      if(serverCurScn && _isLobbyScene(serverCurScn)) return false;
      if(sess.state) return String(sess.state).toLowerCase() === 'match';
      if(!serverCurScn) return null;
      // A match scene proves nothing without a room to play it in: the stale
      // match scene parked on the lobby pseudo-room is the classic false
      // "in match" (the room we are actually in is named 'lobby'), so that
      // reads as unknown here instead of match.
      const rid = String((curRoom && curRoom.room_id) || sess.current_room_id || '');
      if(!rid || _isLobbyScene(rid)) return null;
      return true;
    } catch(e) {}
    return null;
  }
  function _reconcileServerState(sess) {
    try {
      const serverInMatch = _serverInMatch(sess);
      if(serverInMatch === null || serverInMatch === _optimisticState.inMatch) return false;
      // A packet-declared lobby is not up for debate: the server trails the
      // game, so "server says match" while we sit in the lobby is exactly the
      // stale read that used to flip the client back into match. Only the next
      // scene change clears the guard.
      if(serverInMatch === true && _optimisticState.lobbyGuard) {
        logSys('[OPTIMISTIC] server match ignored (packets said lobby)');
        return false;
      }
      // Victory-breather hold: still in the same ranked room with fresh
      // match evidence = between matches, not a leave. The seal lands in
      // seconds; an abandoned screen ages out of the hold and flips.
      if(serverInMatch === false && _optimisticState.inMatch){
        try{
          const _lr=(typeof liveRanked==='function') ? liveRanked() : null;
          if(_lr && (Date.now()-_lastMatchEvidenceAt)<BREATHER_HOLD_MS){
            logSys('[OPTIMISTIC] server lobby ignored (victory breather hold)');
            try{ startSealWatch('breather-hold'); }catch{}
            return false;
          }
        }catch{}
      }
      const optimisticFresh = (Date.now() - (_optimisticState.lastUpdateAt || 0)) < OPTIMISTIC_TRUST_MS;
      if(optimisticFresh) {
        logSys(`[OPTIMISTIC] server reconcile ignored (fresh optimistic inMatch=${_optimisticState.inMatch}, server=${serverInMatch})`);
        return false;
      }
      logSys(`[OPTIMISTIC] server reconcile inMatch=${serverInMatch} (was ${_optimisticState.inMatch})`);
      try {
        const rooms = sess.rooms;
        const curRoom = rooms ? rooms[sess.current_room_id] : null;
        const serverCurScn = (curRoom && curRoom.cur_scn) || sess.cur_scn || null;
        if(serverCurScn) _optimisticState.curScn = serverCurScn;
      } catch(e) {}
      _optimisticState.inMatch = serverInMatch;
      _optimisticState.lastUpdateAt = Date.now();
      // Flip the guard with the flip: a server-confirmed lobby holds like a
      // packet-stamped one, and a server-confirmed match releases it.
      _optimisticState.lobbyGuard = !serverInMatch;
      try { updateLobbyMusic(); } catch {}  // enter-match pauses, exit-to-lobby resumes
      return true;
    } catch(e) { return false; }
  }

  // Optimistic killfeed dedup: the broadcast + its inbound retransmit arrive
  // as separate sightings ~0.5s apart — without this every kill shows +2
  // then "flips back" on the next stats poll. Same pair inside 5s bumps once.
  let _optKfLast = { key: '', at: 0 };
  function _applyOptimisticKillfeed(killer, victim) {
    const myName = GMGet('bnc_game_username', '') || '';
    if (!myName) return;
    try{ _lastMatchEvidenceAt=Date.now(); }catch{}  // killfeed = live match
    const myNorm = _normName(myName);
    const killerNorm = _normName(killer);
    const victimNorm = _normName(victim);
    if (!statsCache) return;
    try {
      const key = `${killerNorm || ''}>${victimNorm || ''}`;
      const now = Date.now();
      if (key && key === _optKfLast.key && now - _optKfLast.at < 5000) {
        logSys(`[OPTIMISTIC] killfeed dup ignored ${killer}→${victim}`);
        return;
      }
      _optKfLast = { key, at: now };
    } catch {}
    if (myNorm && killerNorm && myNorm === killerNorm) {
      statsCache.kills = (statsCache.kills || 0) + 1;
      logSys(`[OPTIMISTIC] KILL +1 → ${statsCache.kills} victim=${victim}`);
      try { renderInfoPanel(); } catch {}
      // Instant counter: ask the server to price this kill at seal-time
      // economics and layer it over the counter until the seal lands (the
      // layer is discarded the moment the server total moves — never added
      // to it). Silent when unresolvable; kill-switch below.
      try{ _priceOptimisticKill(killer, victim); }catch{}
    } else if (myNorm && victimNorm && myNorm === victimNorm) {
      statsCache.deaths = (statsCache.deaths || 0) + 1;
      logSys(`[OPTIMISTIC] DEATH +1 → ${statsCache.deaths} killer=${killer}`);
      try { renderInfoPanel(); } catch {}
      // My death usually ends my match (solo) — a seal lands seconds
      // later but the steady poll only runs every 15-30s, which is the
      // "toast shows up after the next match started" lag. Chase it.
      try{ startSealWatch('self-death'); }catch{}
    }
  }

  // ---- COLLECT MODE ----
  let collectMode = false;
  let collectedPackets = [];
  const COLLECT_MAX = 5000;

  function toggleCollectMode(){
    collectMode = !collectMode;
    if(collectMode){
      collectedPackets = [];
      showToast('Collect mode ON, packets buffering', false);
      logSys('COLLECT mode ON');
    }else{
      showToast(`Collect mode OFF, ${collectedPackets.length} packets buffered`, false);
      logSys('COLLECT mode OFF');
    }
    try{ updateCollectBadge(); }catch{}
  }

  function downloadCollected(){
    if(!collectedPackets.length){
      showToast('No packets collected', true);
      return;
    }
    const cleanPackets = collectedPackets.map(({_bytes, ...rest}) => rest);
    const data = {
      meta: {
        capturedAt: new Date().toISOString(),
        game: 'BuildNow GG',
        totalPackets: cleanPackets.length,
        sessionId: captureSessionId
      },
      packets: cleanPackets
    };
    const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `bnc-capture-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    showToast(`Saved ${cleanPackets.length} packets`, false);
    logSys(`DOWNLOAD ${cleanPackets.length} packets`);
  }

  function clearCollected(){
    collectedPackets = [];
    showToast('Collected packets cleared', false);
    logSys('COLLECT cleared');
    try{ updateCollectBadge(); }catch{}
  }

  function replayCollected(){
    if(!collectedPackets.length){
      showToast('No packets to replay', true);
      return;
    }
    if(!sessionId || !currentReqToken){
      showToast('Need valid session to replay', true);
      return;
    }
    showToast(`Replaying ${collectedPackets.length} packets…`, false);
    logSys(`REPLAY start ${collectedPackets.length} packets`);
    const batchSize = 50;
    let sent = 0;
    function sendBatch(start){
      const batch = collectedPackets.slice(start, start + batchSize);
      if(!batch.length){
        showToast(`Replay done, ${sent} packets sent`, false);
        logSys(`REPLAY done ${sent} packets`);
        return;
      }
      const records = batch.map(r => ({
        seq: r.seq,
        t: r.t,
        dir: r.dir,
        url: r.url,
        fullUrl: r.fullUrl,
        label: r.label,
        rawHex: r.rawHex
      }));
      apiPost('/api/capture', { sessionId: captureSessionId, records }, (ok)=>{
        if(ok) sent += batch.length;
        else logErr(`Replay batch failed at ${start}`);
        setTimeout(()=>sendBatch(start + batchSize), 50);
      });
    }
    sendBatch(0);
  }

  function handleFileLoad(file){
    const reader = new FileReader();
    reader.onload = (e)=>{
      try{
        const data = JSON.parse(e.target.result);
        if(data.packets && Array.isArray(data.packets)){
          collectedPackets = data.packets.slice(-COLLECT_MAX);
          showToast(`Loaded ${collectedPackets.length} packets from file`, false);
          logSys(`LOADED ${collectedPackets.length} packets from ${file.name}`);
          try{ updateCollectBadge(); }catch{}
        }else{
          showToast('Invalid file format', true);
        }
      }catch(err){
        showToast('Failed to parse file', true);
        logErr(`File load error: ${err.message}`);
      }
    };
    reader.readAsText(file);
  }

  // Hotkeys: Ctrl+Shift+C = toggle collect, etc - disable when modal/input focused
  // Mac support: Cmd (metaKey) counts as Ctrl, so Cmd+Shift+<key> works like Ctrl+Shift+<key>
  function modKey(e){ return !!(e.ctrlKey||e.metaKey); }
  function isModalOrInputFocused(){
    const ae = document.activeElement;
    return !!(ae && (ae.id === 'bnc-token-field' || ae.closest && ae.closest('#bnc-login-wrap')) || document.getElementById('bnc-login-wrap')?.contains(ae));
  }
  // --- KEYBIND REGISTRY: every rebindable action lives here. The Ctrl+Shift+K
  // panel renders one row per bind (dev binds hidden unless isDev()); the
  // dispatcher below matches presses against the registry. Drawer (I) and
  // ranked panel (F9) keep their legacy GM keys; the K menu itself is fixed.
  //
  // Defaults use Alt+Shift: Chrome/Edge/Firefox swallow Ctrl+Shift+C/I/J/R/T
  // (DevTools, reload, tabs) before page JS ever sees them, so Ctrl+Shift
  // letter defaults silently never fire. Alt+Shift reaches the page.
  // Stored binds win over defaults — existing users keep what they set.
  // Defaults are single keys (one press, no combos): panels on F2/F4/F9,
  // this menu on Backquote — kid-simple on Windows AND Mac (60% boards have
  // Backquote but no F-keys), dodging the Alt+Shift layout-switch trap plus
  // every Ctrl+Shift browser reservation. Stored binds always win, so
  // changing defaults only affects fresh installs. F1/F3/F5 stay
  // off-limits (browser takes them — see bindWarning). F11/F12 are host
  // panels (rebindable; bare presses may lose to fullscreen/DevTools).
  const BNC_BINDS=[
    {id:'keys', label:'Shortcuts menu', key:'bnc_bind_keys', dev:false, def:{ctrl:false,shift:false,alt:false,meta:false,key:'`'}, run:()=>toggleKeybinds()},
    {id:'collect', label:'Collect toggle', key:'bnc_bind_collect', dev:false, def:{ctrl:false,shift:true,alt:true,meta:false,key:'C'}, run:()=>toggleCollectMode()},
    {id:'save', label:'Save collected', key:'bnc_bind_save', dev:false, def:{ctrl:false,shift:true,alt:true,meta:false,key:'S'}, run:()=>downloadCollected()},
    {id:'load', label:'Load file', key:'bnc_bind_load', dev:false, def:{ctrl:false,shift:true,alt:true,meta:false,key:'L'}, run:()=>{ try{ document.getElementById('bnc-file-input')?.click(); }catch{} }},
    {id:'replay', label:'Replay', key:'bnc_bind_replay', dev:false, def:{ctrl:false,shift:true,alt:true,meta:false,key:'R'}, run:()=>replayCollected()},
    {id:'feed', label:'Feed toggle (dev)', key:'bnc_bind_feed', dev:true, def:{ctrl:false,shift:true,alt:true,meta:false,key:'F'}, run:()=>toggleFeed()},
    {id:'pause', label:'Pause feed (dev)', key:'bnc_bind_pause', dev:true, def:{ctrl:false,shift:true,alt:true,meta:false,key:'P'}, run:()=>togglePauseFeed()},
    {id:'drawer', label:'Player info', key:'bnc_drawer_keybind', dev:false, def:{ctrl:false,shift:false,alt:false,meta:false,key:'F2'}, run:()=>toggleInfoPanel()},
    {id:'ranked', label:'Ranked panel', key:'bnc_ranked_keybind', dev:false, def:{ctrl:false,shift:false,alt:false,meta:false,key:'F9'}, run:()=>toggleRankedPanel()},
    {id:'leaderboard', label:'Leaderboard', key:'bnc_lb_keybind', dev:false, def:{ctrl:false,shift:false,alt:false,meta:false,key:'F4'}, run:()=>toggleLbPanel()},
    {id:'host11', label:'Host roster (F11)', key:'bnc_host11_keybind', dev:false, def:{ctrl:false,shift:false,alt:false,meta:false,key:'F11'}, run:()=>toggleHost11Panel()},
    {id:'host12', label:'Host controls (F12)', key:'bnc_host12_keybind', dev:false, def:{ctrl:false,shift:false,alt:false,meta:false,key:'F12'}, run:()=>toggleHost12Panel()},
  ];
  // Combos the browser takes before the page (per registration order they
  // can't be intercepted no matter the capture phase). Capture warns instead
  // of saving a dead bind. Key: "ctrl+shift+C" style + bare function keys.
  function bindWarning(kb, selfId){
    const k=String(kb.key||'').toUpperCase();
    const mods=[];
    if(kb.ctrl||kb.meta) mods.push('ctrl');
    if(kb.shift) mods.push('shift');
    if(kb.alt) mods.push('alt');
    const sig=mods.join('+')+(mods.length?'+':'')+k;
    const reserved={
      'ctrl+shift+C':'DevTools inspect', 'ctrl+shift+I':'DevTools', 'ctrl+shift+J':'DevTools console',
      'ctrl+shift+R':'hard reload', 'ctrl+shift+T':'reopen tab', 'ctrl+shift+W':'close window',
      'ctrl+shift+N':'incognito window', 'ctrl+shift+O':'bookmark manager', 'ctrl+shift+B':'bookmark bar',
      'ctrl+shift+D':'bookmark tabs', 'ctrl+shift+Q':'quit browser', 'ctrl+shift+DELETE':'clear browsing data',
      'ctrl+shift+G':'find previous', 'ctrl+shift+M':'switch profile', 'ctrl+shift+K':'keybinds menu',
      'F5':'reload', 'F1':'help', 'F3':'find next',
    };
    if(reserved[sig]) return `Browser takes ${formatKeybind(kb)} (${reserved[sig]}) — pick another.`;
    for(const b of BNC_BINDS){
      if(b.id===selfId) continue;
      const cur=getBindKb(b);
      const sameMod=!!(cur.ctrl||cur.meta)===!!(kb.ctrl||kb.meta) && !!cur.shift===!!kb.shift && !!cur.alt===!!kb.alt;
      if(!sameMod) continue;
      const ck=String(cur.code||'').toUpperCase(), nk=String(kb.code||'').toUpperCase();
      const sameKey=(ck && nk) ? ck===nk : String(cur.key||'').toUpperCase()===k;
      if(sameKey)
        return `${formatKeybind(kb)} is already ${b.label} — pick another.`;
    }
    return null;
  }
  function isDev(){ try{ return !!(statsCache && statsCache.is_dev); }catch{ return false; } }
  // Capture liveness: stamped on every /api/capture send (live + replay).
  // A ghost ranked block (dead session served from server cache after a
  // kick/reload) must not render when WE haven't captured in minutes.
  let lastCaptureSentAt=0;
  const CAPTURE_FRESH_MS=120000;
  function captureFresh(){ try{ return Date.now()-lastCaptureSentAt<=CAPTURE_FRESH_MS; }catch{ return false; } }
  // WS liveness: stamped on EVERY websocket packet seen by the hook (even
  // non-essential keepalives that never reach /api/capture). An idle lobby
  // sends almost no essential packets for minutes — WS alive + captures
  // stale = idle, not dead. Only WS dead + captures dead = hook killed.
  let lastWsSeenAt=0;
  const WS_FRESH_MS=120000;
  function wsFresh(){ try{ return lastWsSeenAt>0 && Date.now()-lastWsSeenAt<=WS_FRESH_MS; }catch{ return false; } }
  // Instant-state shadow: every successful /api/capture carries a full
  // session (+ ranked_room in matches) and arrives far more often than the
  // stats poll during play. Stash just enough to render ranked UI instantly;
  // statsCache stays the points authority (never overwritten from here).
  let _capSnap=null; // {at, inMatch, code, mver}
  const CAP_SNAP_FRESH_MS=45000;
  // Timestamp of the last local flip TO lobby (scene says MainLobby). A
  // capture response built from packets older than this is a stale echo —
  // noteCaptureSnap ignores its inMatch=true so an in-flight batch can't
  // resurrect match UI after a real lobby flip. Cleared on match-ward flips.
  let _lobbyFlipAt=0;
  function noteCaptureSnap(j){
    try{
      if(!j || !j.session) return;
      const im=_serverInMatch(j.session);
      let code=null, mver=0;
      try{
        const rooms=j.session.rooms||{};
        const cur=rooms[j.session.current_room_id]||null;
        if(cur){
          code=String(cur.short_code||'').toUpperCase()||null;
          mver=Number(cur.match_version)||0;
        }
      }catch{}
      if(im===true && _lobbyFlipAt){
        // Stale echo? The batch's newest packet predates the lobby flip.
        try{
          let newest=0;
          for(const p of (j.packets||[])){ const t=Number(p && p.t)||0; if(t>newest) newest=t; }
          if(newest && newest<_lobbyFlipAt) return;
        }catch{}
      }
      _capSnap={at:Date.now(), inMatch:im===true, code, mver};
      // Live-match evidence for the victory-breather hold (server fallback
      // reads never stamp — only this packet-derived shadow does).
      if(im===true){ try{ _lastMatchEvidenceAt=Date.now(); }catch{} }
    }catch{}
  }
  // Local-signal trust window: server trails live packets (~8s observed),
  // so optimistic/capture signals stay authoritative this long before the
  // server is allowed to override them. One constant, three users.
  const OPTIMISTIC_TRUST_MS=25000;
  // Effective match state, freshest-first: optimistic sniff (<10s) →
  // capture shadow (<15s) → server session. Returns true/false/null
  // (null = no signal at all). Renderers use this instead of reading
  // statsCache.session directly so flips show in ms, not polls.
  // A fresh packet-derived LOBBY is an answer too, not just a fresh match:
  // this used to only be able to say "true", so a stale server 'match' won
  // every time the player was sitting in the lobby (wrong card, no music).
  function effInMatch(){
    try{
      const now=Date.now();
      // The packets win. A scene/auth/leave stamp is the freshest thing there
      // is, and a LOBBY stamp holds until a new scene says otherwise - no
      // 25s window, no capture shadow, no stale server 'match' underneath it.
      if(_optimisticState.lobbyGuard) return false;
      if(_optimisticState.inMatch) return true;
      if(_capSnap && now-_capSnap.at<=CAP_SNAP_FRESH_MS) return !!_capSnap.inMatch;
      const sess=statsCache && statsCache.session;
      if(sess && sess.state) return String(sess.state).toLowerCase()==='match';
      return null;
    }catch{ return null; }
  }
  // Transition burst polling: after a locally-observed transition (scene
  // flip, stG edge, join-track), poll stats fast until the server confirms
  // the new state (or 12s). Fast perception without a permanently hot poll:
  // steady cadence is 5s tracked / 15s idle, bursts only bridge the gap.
  let _burstUntil=0, _burstTimer=null;
  function startTransitionBurst(reason){
    try{
      _burstUntil=Date.now()+12000;
      if(_burstTimer) return;
      const tick=()=>{
        try{
          _burstTimer=null;
          if(Date.now()>_burstUntil) return;
          const sess=statsCache && statsCache.session;
          const srv=sess && sess.state ? String(sess.state).toLowerCase()==='match' : null;
          const eff=effInMatch();
          if(srv!==null && eff!==null && srv===eff){
            _burstUntil=0;
            return; // server confirmed — back to normal cadence
          }
          try{ fetchStats(false); }catch{}
          _burstTimer=setTimeout(tick, 3000);
        }catch{}
      };
      _burstTimer=setTimeout(tick, 1500);
      try{ logInfo(`[RANKED] transition burst start (${reason||'?'})`); }catch{}
    }catch{}
  }
  let _lastCapUiAt=0;
  // Optimistic kill layer: priced kills land on the counter in ~300ms
  // instead of at the seal (~2-4s). Display-only and disposable: shown =
  // server_total + pending, and pending resets to zero the moment the
  // server total moves (seal landed), on room change, or on leave — so a
  // priced kill the seal never banks can never stick. Kill-switch below.
  const OPTIMISTIC_KILLS=true;
  let _optPending=0, _optBase=null, _optRid=null, _optVer=null, _optAt=0, _optN=0;
  // Priced layer older than this is discarded: a match that never seals
  // must not leave phantom points on later matches with the same total.
  const OPT_STALE_MS=120000;
  function _optServerPts(){
    try{
      const rk=(typeof liveRanked==='function') ? liveRanked() : null;
      if(!rk || rk.points==null) return null;
      return {rid:String(rk.rid||''), pts:Number(rk.points)||0,
              ver:Number(rk.match_version)||0};
    }catch{ return null; }
  }
  function _optReset(sv){
    _optPending=0;
    _optBase=sv ? sv.pts : null;
    _optRid=sv ? sv.rid : null;
    _optVer=sv ? sv.ver : null;
    _optAt=Date.now();
  }
  function _optLive(){
    // The layer is spendable only while the server total, room and match
    // version all still match the pricing moment — and only while fresh.
    try{
      if(_optBase==null || _optPending<=0) return 0;
      if(Date.now()-_optAt>OPT_STALE_MS) return 0;
      const sv=_optServerPts();
      if(!sv || sv.rid!==_optRid || sv.pts!==_optBase || sv.ver!==_optVer) return 0;
      return _optPending;
    }catch{ return 0; }
  }
  function _priceOptimisticKill(killer, victim){
    if(!OPTIMISTIC_KILLS || !IS_GAME_FRAME) return;
    let rk=null;
    try{ rk=(typeof liveRanked==='function') ? liveRanked() : null; }catch{}
    if(!rk) return;
    try{ if(effInMatch()!==true) return; }catch{ return; }
    let code=null, did=null;
    try{ code=(statsCache&&statsCache.session&&statsCache.session.short_code)||null; }catch{}
    try{ did=myDiscordId(); }catch{}
    if(!code || !did) return;
    code=String(code).trim().toUpperCase();
    if(!/^[A-Z0-9]{5}$/.test(code)) return;
    // Base captured at request time: a response that arrives after the
    // server total moved (seal landed mid-flight) is dropped, never stacked.
    let reqBase=null;
    try{ reqBase=_optServerPts(); }catch{}
    if(!reqBase) return;
    try{
      apiPostJson('/api/ranked/kill-price',
        {code, discord_id:String(did), killer_name:String(killer||''), victim_name:String(victim||'')},
        (j)=>{
          try{
            if(!j || j.ok!==true) return;
            const award=Number(j.award)||0;
            if(!(award>0)) return;
            const sv=_optServerPts();
            if(!sv || sv.rid!==reqBase.rid || sv.pts!==reqBase.pts || sv.ver!==reqBase.ver) return;
            if(_optBase==null || _optRid==null || sv.rid!==_optRid || sv.pts!==_optBase || sv.ver!==_optVer){ _optReset(sv); }
            _optPending+=award;
            try{ _optAt=Date.now(); }catch{}
            _optN++;
            try{ logInfo(`[RANKED] opt-kill +${award} (pending=${_optPending})`); }catch{}
            try{
              if(typeof MINI_TOASTS!=='undefined' && MINI_TOASTS){
                queuePtsToast('ELIMINATION', award, {key:`opt:${sv.rid}:${sv.ver}:${_optN}`, noCoalesce:true, dwell:2000});
              }
            }catch{}
            try{ updateRankedOverlay(); }catch{}
          }catch{}
        });
    }catch{}
  }
  // Seal-watch: fast stats polling after a match ends until its seal lands.
  // Seals pay seconds after match end but the steady poll only runs every
  // 15-30s, which is the "toast shows up after the next match started"
  // lag. Armed on the victory breather, on self-death (solo: match over
  // for me), and re-armed per match end; polls every 1.5s for up to 30s
  // and stops the moment a new seal is observed (either toast path), on
  // leave, or on timeout. Deliberately NOT on next-match start: a slow
  // seal (host-gated) can land after stG=1, and quitting there strands
  // the toast on the 30s steady poll — the exact lag above. Every poll
  // runs the normal render, so toasts queue the instant the seal is visible.
  let _sealWatchUntil=0, _sealWatchTimer=null, _sealWatchBase=null;
  function _sealWatchSnap(){
    try{
      const sc=statsCache||{};
      const rk=sc.ranked||null, rt=sc.ranked_tourney||null;
      return {
        rkv: rk ? (Number(rk.match_version)||0) : 0,
        rlr: (rk && rk.last_result) ? (Number(rk.last_result.match_version)||0) : 0,
        rpts: rk ? (Number(rk.points)||0) : 0,
        tlr: (rt && rt.last_result) ? (Number(rt.last_result.match_version)||0) : 0,
        tpts: rt ? (Number(rt.points)||0) : 0,
      };
    }catch{ return {rkv:0, rlr:0, rpts:0, tlr:0, tpts:0}; }
  }
  function _sealWatchAdvanced(){
    try{
      const b=_sealWatchBase, n=_sealWatchSnap();
      if(!b) return false;
      return n.rlr>b.rlr || n.tlr>b.tlr || n.tpts>b.tpts || n.rpts>b.rpts;
    }catch{ return false; }
  }
  function startSealWatch(reason){
    try{
      _sealWatchUntil=Date.now()+30000;
      // Always re-baseline: a re-arm means a newer match just ended, so the
      // seal awaited is its own, not the previous watch's.
      try{ _sealWatchBase=_sealWatchSnap(); }catch{ _sealWatchBase=null; }
      try{ logInfo(`[RANKED] seal-watch start (${reason||'?'})`); }catch{}
      if(_sealWatchTimer) return;
      const tick=()=>{
        try{
          _sealWatchTimer=null;
          if(Date.now()>_sealWatchUntil){ _sealWatchBase=null; return; }
          let stopWhy=null;
          try{
            if(typeof liveRanked==='function' && !liveRanked()) stopWhy='leave';
            else if(_sealWatchAdvanced()) stopWhy='seal';
          }catch{}
          if(stopWhy){
            _sealWatchUntil=0; _sealWatchBase=null;
            try{ logInfo(`[RANKED] seal-watch stop (${stopWhy})`); }catch{}
            return;
          }
          try{ fetchStats(false); }catch{}
          _sealWatchTimer=setTimeout(tick, 1500);
        }catch{}
      };
      _sealWatchTimer=setTimeout(tick, 1200);
    }catch{}
  }
  // Ranked verdict (comp-copy): local _rankedRoom intent + server stats
  // `ranked` block confirmer (liveRanked). No endpoint polls, no roster
  // checks. Leave guard + dead captures hide locally and never re-show
  // from a stale block.
  // Comp-tourney rooms score manually: every ranked surface (toasts,
  // overlays, lobby card, seal queue) stays dark while holding one. Gates:
  // server comp_tourney block, local _compRoom code match, or tourney/scrim
  // room tag — the same trio the tourney HUD uses. Never throws.
  function _inCompRoom(code){
    try{
      const sess=statsCache&&statsCache.session;
      const cur=sess&&sess.short_code?String(sess.short_code).toUpperCase():null;
      if(!cur) return false;
      if(code && String(code).toUpperCase()!==cur) return false;
      if(statsCache&&statsCache.comp_tourney) return true;
      try{ if(typeof _compRoom!=='undefined'&&_compRoom&&String(_compRoom.code||'').toUpperCase()===cur) return true; }catch{}
      try{ const tg=sess&&sess.tag; if(tg&&(tg.kind==='tourney'||tg.kind==='scrim')) return true; }catch{}
      return false;
    }catch{ return false; }
  }
  // ---- RANKED DETECTION (comp-system copy adapted for ranked) ----
  // Stable pattern copied from comp: join-time pID sniff -> local intent
  // {_rankedRoom:{code,at}} (10-min freshness, like _compRoom) + server
  // stats `ranked` block as the only confirmer. No roster checks, no
  // optimistic tables, no 90s expiry, no presence ping, no stG hook.
  // Comp-tourney rooms stay comp: _inCompRoom() mutes every ranked surface.
  // ---- RANKED DETECTION (comp-system copy adapted for ranked) ----
  // Same gating contract as the comp HUD (updateTourneyHud): no live session
  // short_code -> hidden, always. The server still serves the ranked block
  // from its preserved short_code with no live session (boot, idle-stale),
  // and that block alone must never paint UI — exactly like comp_tourney
  // without a session code never paints the tourney HUD. Local _rankedRoom
  // intent only drives fast refresh (burst + instant re-render on join),
  // never substitutes for the session code. Never throws.
  function liveRanked(){
    try{
      if(typeof _inCompRoom==='function' && _inCompRoom()) return null;
      if(!rankedEnabled()) return null;
      const sc=statsCache;
      const rk=sc&&sc.ranked?sc.ranked:null;
      if(!rk || !rk.active || !rk.rid) return null;
      try{ if(!captureFresh() && !wsFresh()) return null; }catch{}
      try{ if(_rankedLeaveGuard) return null; }catch{}
      const sess=sc&&sc.session?sc.session:null;
      let sessCode=null, rkCode=null;
      try{ if(sess&&sess.short_code) sessCode=String(sess.short_code).toUpperCase(); }catch{}
      try{ if(rk.short_code) rkCode=String(rk.short_code).toUpperCase(); }catch{}
      // Comp parity: no live session code -> hidden (boot, first auth,
      // idle-stale lobby). Stale-code blocks can arrive either way.
      if(!sessCode) return null;
      // Block must belong to the live room (server keyed it by session code,
      // same agreement the comp HUD relies on for comp_tourney).
      if(rkCode && rkCode!==sessCode) return null;
      // Hygiene: drop stale local intent so a later session can't agree
      // with a dead join. Fresh intent needs no check — the session+block
      // agreement above already rules.
      try{
        if(typeof _rankedRoom!=='undefined' && _rankedRoom && _rankedRoom.code){
          if((Date.now()-(_rankedRoom.at||0))>=600000){ try{ _rankedRoom=null; }catch{} }
        }
      }catch{}
      return rk;
    }catch{ return null; }
  }
  // ---- RANKED v2 (response-only) ----
  // One-way flow, no conversations: client sends packets via /api/capture,
  // server answers with {packets, session, ranked_room}; idle UI refreshes
  // via /api/stats {ranked}. No by-code/mine polls, no room poll, no
  // endpoint verdicts, no fee/placement math.
  //   lobby UI  = server confirmed the create/join bind (ranked block live)
  //   match UI  = lobby UI + curScn flipped to match (local, instant)
  //   leave     = hide locally, never re-show from a stale block
  //   Awards (both sides share the lobby table when known, else the
  //   dynamic ranges): kills 4-18 by opponent strength with taper, win and
  //   placement scale with lobby strength and size, loss -5 to -32 by tier.
  // Ranked rebuilt on the comp scoring core: seals bank comp-style events and
  // score kill/placement/win through compute_match_points (ranked economy
  // kept). Surfaces are live again.
  const RANKED_SLEEPING = false;
  // Ranked master switch (server parity): RANKED_ENABLED here must agree
  // with server/lib/ranked.py's env flag. The server publishes its own
  // state every stats poll (arena.ranked_sleeping); on disagreement the
  // SERVER wins (conservative: any sleeping signal mutes ranked UI) and
  // the console says so once, so a drifted build gets fixed.
  const RANKED_ENABLED = !RANKED_SLEEPING;
  let _rankedSleepRemote=null;   // last server-said sleeping state
  let _rankedParityWarned=false;
  // Effective gate: local flag on AND server not sleeping. Never throws.
  function rankedEnabled(){
    try{
      const sc=statsCache;
      const remote=(sc && sc.arena) ? !!sc.arena.ranked_sleeping : null;
      if(remote!=null && remote!==_rankedSleepRemote){
        _rankedSleepRemote=remote;
        if(remote && !_rankedParityWarned && RANKED_ENABLED){
          _rankedParityWarned=true;
          try{ logInfo('[RANKED] server says ranked is SLEEPING while local RANKED_ENABLED=true - server wins, ranked UI muted (fix flag parity)'); }catch{}
        }
      }
      if(!RANKED_ENABLED) return false;
      if(_rankedSleepRemote===true) return false;
      return true;
    }catch{ return RANKED_ENABLED !== false; }
  }
  // Local ranked intent (comp copy): MY last join/create pID + timestamp.
  // 10-min freshness like _compRoom; bridges transient block loss. The
  // server stats `ranked` block is the only confirmer (liveRanked above).
  let _rankedRoom=null; // {code, at}
  // Leave guard: MY leave-op (254 out) means "in no room" — the server can
  // keep serving the just-left block until it ages out, which would
  // resurrect the UI after disableRanked hid it. While set, liveRanked()
  // hides everything. Cleared on the next join/create with a room code.
  // Auth never touches it (mid-match re-auths must recover, not stick).
  let _rankedLeaveGuard=false;
  function sniffPid(hex){
    // Photon strings are length-prefixed, so the code may glue to a prefix
    // byte ("07"+"PBS05") — slide a 5-char window over the post-key bytes.
    // Trigger-happy by design: the by-code endpoint is authoritative and a
    // wrong guess just resolves to room null (cached 30s).
    try{
      const h=String(hex||'').toUpperCase();
      const key='704944'; // "pID"
      let i=h.indexOf(key);
      while(i>=0){
        const win=h.slice(i+key.length, i+key.length+96); // value follows the key
        for(let j=0; j+10<=win.length; j+=2){
          const g=win.slice(j, j+10);
          if(!/^[0-9A-F]{10}$/.test(g)) continue;
          let s='';
          for(let k=0;k<10;k+=2) s+=String.fromCharCode(parseInt(g.slice(k,k+2),16));
          if(/^[A-Z0-9]{5}$/.test(s)) return s;
        }
        i=h.indexOf(key, i+1);
      }
    }catch{}
    return null;
  }
  // stG value sniffer: Photon string key "stG" serializes as 73 0003 737447,
  // value follows as one type byte + payload (62=byte, 69=int32 BE, 6b=int16
  // BE). Returns the int value or null. Only a refresh trigger (the stats
  // poll stays the truth) so a misparse just costs one extra fetchStats.
  function sniffStg(hex){
    try{
      const h=String(hex||'').toUpperCase();
      const key='730003737447'; // 's' + len 3 + "stG"
      const i=h.indexOf(key);
      if(i<0) return null;
      const t=h.slice(i+key.length, i+key.length+2);
      const d=h.slice(i+key.length+2, i+key.length+12);
      if(t==='62' && d.length>=2) return parseInt(d.slice(0,2),16);
      if(t==='6B' && d.length>=4) return parseInt(d.slice(0,4),16);
      if(t==='69' && d.length>=8) return parseInt(d.slice(0,8),16);
    }catch{}
    return null;
  }
  // ---- MY OWN PHOTON ACTOR NUMBER -------------------------------------
  // Photon answers MY join/create with the actor number it assigned me (param
  // 254, the first param of that response) and echoes the same number on my own
  // Join / PropertiesChanged events (PlayerProperties."255" == my game name).
  // Tracking it makes an inbound Leave event PROOF: a Leave whose ActorNr is
  // mine IS my own leave — the same statement as my LeaveRoom op — instead of
  // matching the leaver's name against my username or assuming every 254-in
  // belongs to somebody else. The number is per-room: cleared on auth (the next
  // room hands out a fresh one) and on a proven self-leave. Never throws.
  let _myActor={nr:null, at:0, via:''};
  function _noteMyActor(nr, via){
    try{
      const n=Number(nr);
      if(!Number.isInteger(n) || n<1 || n>255) return false;
      const v=String(via||'');
      if(_myActor.nr!==n || _myActor.via!==v){
        _myActor.nr=n; _myActor.at=Date.now(); _myActor.via=v;
        try{ logInfo(`[ACTOR] my actorNr=${n} (${v||'?'})`); }catch{}
      }
      return true;
    }catch{ return false; }
  }
  // Drops the pin with the room (auth, proven self-leave). The next join/create
  // response pins the next number, so forgetting can only lose a stale actor —
  // never a live one — and it keeps a recycled number from looking like mine.
  function _clearMyActor(why){
    try{
      if(_myActor.nr==null) return;
      try{ logInfo(`[ACTOR] my actorNr forgotten (${why||'?'})`); }catch{}
      _myActor.nr=null; _myActor.at=0; _myActor.via='';
    }catch{}
  }
  // True only when the number is the one Photon pinned to ME — the proof the
  // Leave handler keys on. Unknown on either side is never a match.
  function isMyActor(nr){
    try{ return nr!=null && _myActor.nr!=null && Number(nr)===_myActor.nr; }catch{ return false; }
  }
  // Minimal GpBinary (LE, minimal-encoded counts) param walker: captures are raw
  // bytes, and ActorNr rides inside the param list (param 254 of a Leave event,
  // of a join/create response, of a Join event). Mirrors companion/decryptor.js
  // and server/lib/gpbinary.py without pulling in the whole parser. Bounds-
  // checked, depth-capped; a value it cannot follow ends the walk and keeps the
  // params read so far (ActorNr is the FIRST param of every join/create
  // response, so a partial read still carries it). Null when nothing could be
  // read. Never throws.
  function _gpParams(bytes, type){
    let out=null;
    try{
      if(!bytes || bytes.length<5) return null;
      if(type!==3 && type!==4) return null;   // 3 = OperationResponse, 4 = EventData
      let p=3;
      const u8=()=>{ if(p>=bytes.length) throw 0; return bytes[p++]; };
      const cnt=()=>{ const v=u8(); return v>=0x80 ? (v | (u8()<<8)) : v; };
      const skip=n=>{ if(n<0 || p+n>bytes.length) throw 0; p+=n; };
      const str=()=>{ const n=cnt(); if(p+n>bytes.length) throw 0; let s=''; for(let i=0;i<n;i++) s+=String.fromCharCode(bytes[p+i]); p+=n; return s; };
      const val=(t,d)=>{
        if(d>8) throw 0;
        if(t===2) return u8()!==0;
        if(t===3) return u8();
        if(t===4){ const v=u8()|(u8()<<8); return v>0x7fff ? v-0x10000 : v; }
        if(t===5){ skip(4); return null; }
        if(t===6){ skip(8); return null; }
        if(t===7) return str();
        if(t===8 || t===42) return null;
        if(t===9 || t===10){ let r=0,s=0,b=0; do{ b=u8(); r|=(b&0x7f)<<s; s+=7; }while((b&0x80) && s<35); return r; }
        if(t===11) return u8();
        if(t===12) return -u8();
        if(t===13) return u8()|(u8()<<8);
        if(t===14) return -(u8()|(u8()<<8));
        if(t===15) return u8();
        if(t===16) return -u8();
        if(t===17) return u8()|(u8()<<8);
        if(t===18) return -(u8()|(u8()<<8));
        if(t>=27 && t<=34) return t===28;   // true / false / type-zero
        if(t===20){                        // Dictionary
          const kt=u8(), vt=u8(), n=cnt();
          if(n>0xffff) throw 0;
          const o={};
          for(let i=0;i<n;i++){
            const k=val((kt===0||kt===42)?u8():kt, d+1);
            o[String(k)]=val((vt===0||vt===42)?u8():vt, d+1);
          }
          return o;
        }
        if(t===21){                        // Hashtable
          const n=cnt();
          if(n>0xffff) throw 0;
          const o={};
          for(let i=0;i<n;i++){ const k=val(u8(), d+1); o[String(k)]=val(u8(), d+1); }
          return o;
        }
        if(t===23){                        // ObjectArray
          const n=cnt();
          if(n>0xffff) throw 0;
          const a=[];
          for(let i=0;i<n;i++) a.push(val(u8(), d+1));
          return a;
        }
        if(t & 0x40){                      // typed array
          const elem=t & 0x3f;
          if(elem===3){ const n=cnt(); skip(n); return null; }   // byte[]
          let n, eff=elem;
          if(t===0x40){ n=cnt(); eff=u8(); } else n=cnt();
          if(n>0xffff) throw 0;
          const a=[];
          for(let i=0;i<n;i++) a.push(val(eff, d+1));
          return a;
        }
        throw 0;                           // Custom (19) etc: payload length unknown
      };
      // The code byte (b2) is already read for us by the record: an
      // OperationResponse continues with returnCode(2)+debug, an EventData goes
      // straight to the param count.
      if(type===3){ u8(); u8(); const dbg=u8(); if(dbg===7){ const n=cnt(); skip(n); } }
      const n=cnt();
      if(n>0xffff) throw 0;
      out={};
      for(let i=0;i<n;i++){
        if(p>=bytes.length) break;
        const k=u8(), t=u8();
        out[k]=val(t, 0);
      }
      return out;
    }catch{ return out; }
  }
  // ActorNr + player name from one captured message ({actorNr, name}), or null
  // when the message carries no param list at all. Never throws.
  function sniffMyActor(bytes, type){
    try{
      const params=_gpParams(bytes, type);
      if(!params) return null;
      const nr=params[254];
      if(!Number.isInteger(nr) || nr<1 || nr>255) return {actorNr:null, name:null};
      // PlayerProperties (249) on a Join event / Properties (251) on a 253 hold
      // the name under key "255"; on a join RESPONSE 249 is keyed by actor, so no
      // name shows up there — none is needed, the response is already mine.
      let name=null;
      for(const k of [249,251]){
        const bag=params[k];
        if(bag && typeof bag==='object' && typeof bag['255']==='string' && bag['255']){ name=bag['255']; break; }
      }
      return {actorNr:nr, name};
    }catch{ return null; }
  }
  // My own game username, normalized the way the server normalizes names (color
  // tags and the "dc:" prefix stripped). Empty mine can never match, so no pin
  // is ever learned from a name we do not have. Never throws.
  function _isMyGameName(nm){
    try{
      const mine=_normName(GMGet('bnc_game_username','')||'');
      const his=_normName(nm);
      return !!(mine && his && mine===his);
    }catch{ return false; }
  }
  // Track MY join/create carrying a pID (local intent, comp copy). The
  // server stats `ranked` block is the only confirmer — this just records
  // which code I meant, clears the leave guard, and kicks a fast stats
  // pull + transition burst so the lobby card lands fast. Never throws.
  function rankedTrack(pid){
    try{
      pid=String(pid||'').trim().toUpperCase();
      if(!pid || !/^[A-Z0-9]{5}$/.test(pid)) return;
      try{ _rankedLeaveGuard=false; }catch{}
      try{
        if(!_rankedRoom || _rankedRoom.code!==pid) _rankedRoom={code:pid, at:Date.now()};
        else _rankedRoom.at=Date.now();
      }catch{ try{ _rankedRoom={code:pid, at:Date.now()}; }catch{} }
      try{ fetchRankedRoom(pid); }catch{}
      try{ logInfo(`[RANKED] track ${pid}`); }catch{}
      try{ fetchStats(false); }catch{}
      try{ startTransitionBurst('ranked-track'); }catch{}
      try{ renderInfoPanel(); }catch{}
      try{ updateRankedOverlay(); updateRankedLobby(); updateRankedMatchUi(); }catch{}
    }catch{}
  }
  // Direct room resolve (comp parity: fetchCompRoom). Asks the server which
  // ranked room a code belongs to and remembers the room id for 10 minutes.
  // Local intent only: liveRanked() still needs the stats-block + session
  // agreement before anything paints. A null verdict clears a stale binding
  // so a later session can't agree with a dead join. Never throws.
  function fetchRankedRoom(code){
    try{
      if(_publicRoom) return; // public matchmaking: never resolve
      code=String(code||'').trim().toUpperCase();
      if(!code || !/^[A-Z0-9]{5}$/.test(code)) return;
      if(_rankedRoom && _rankedRoom.code===code && _rankedRoom.rid && Date.now()-_rankedRoom.at<600000) return; // fresh
      rankedApiGet(`/api/ranked/room/by-code?code=${encodeURIComponent(code)}`, (j)=>{
        try{
          const r=j&&j.room;
          if(r && r.id){
            _rankedRoom={code, rid:String(r.id), at:Date.now()};
            try{ logInfo(`[RANKED] room ${code} -> ${r.id}`); }catch{}
          } else if(_rankedRoom && _rankedRoom.code===code){
            _rankedRoom=null;
          }
          try{ updateRankedOverlay(); updateRankedLobby(); updateRankedMatchUi(); }catch{}
        }catch{}
      });
    }catch{}
  }
  // Ranked intent reset (comp parity: resetCompRoomLocal). Clears the local
  // binding only (no server call); the next pID join re-tracks fresh.
  // Never touches statsCache.ranked (server truth). Never throws.
  function resetRankedRoomLocal(){
    try{ _rankedRoom=null; }catch{}
    try{ updateRankedOverlay(); updateRankedLobby(); updateRankedMatchUi(); }catch{}
    try{ renderInfoPanel(); }catch{}
  }
  // Ranked mode OFF: set the leave guard + clear local intent, hide every
  // ranked surface, close the panel. Silent except the log (leaves happen
  // often). A leave hides locally and never re-shows: the next join/create
  // with a code is the only path that re-tracks. Never touches
  // statsCache.ranked (server truth) or _capSnap. Never throws.
  function disableRanked(reason){
    try{ _rankedLeaveGuard=true; }catch{}
    try{ _rankedRoom=null; }catch{}
    try{ _lastMatchEvidenceAt=0; }catch{}  // release any breather hold now
    try{ _optPending=0; _optBase=null; _optRid=null; _optVer=null; _optAt=0; }catch{}  // drop optimistic layer
    try{ const el=document.getElementById('bnc-ranked-start'); if(el) el.remove(); }catch{}
    try{ _rankedStartTimer=null; }catch{}
    try{ if(typeof rankedPanel!=='undefined' && rankedPanel && rankedPanel.style.display!=='none') toggleRankedPanel(false); }catch{}
    try{ renderInfoPanel(); }catch{}
    try{ updateRankedOverlay(); updateRankedLobby(); updateRankedMatchUi(); }catch{}
    try{ logInfo(`[RANKED] leave (${reason||"?"})`); }catch{}
  }
  function getBindKb(b){
    try{
      const raw=GMGet(b.key, null);
      if(raw && typeof raw==='object' && raw.key) return raw;
      if(typeof raw==='string' && raw){
        try{ const j=JSON.parse(raw); if(j && j.key) return j; }catch{}
        const parts=String(raw).split('+');
        const kb={ctrl:false,shift:false,alt:false,meta:false,key:b.def.key};
        for(const p of parts){ const l=p.trim().toLowerCase(); if(l==='ctrl'||l==='control') kb.ctrl=true; else if(l==='cmd'||l==='meta'||l==='command') kb.meta=true; else if(l==='shift') kb.shift=true; else if(l==='alt') kb.alt=true; else if(p.trim()) kb.key=p.trim().toUpperCase(); }
        return kb;
      }
    }catch{}
    return Object.assign({}, b.def);
  }
  function matchBindKb(e, kb){
    return bindMatches(e, kb);
  }
  // Layout-proof matching: Alt+Shift switches Windows layouts and Ctrl+Alt
  // composes AltGr chars, so e.key arrives as 'z'/'å'/etc. instead of 'W'.
  // e.code is the physical key (KeyW) — layout- and modifier-independent.
  // Stored binds without a code (saved before this fix) fall back to e.key.
  function bindMatches(e, kb){
    try{
      if(kb && kb.code && e.code && String(e.code).toUpperCase()===String(kb.code).toUpperCase())
        return modKey(e)===!!(kb.ctrl||kb.meta) && !!e.shiftKey===!!kb.shift && !!e.altKey===!!kb.alt;
    }catch{}
    const k=(e.key||'').toUpperCase();
    return k===String(kb && kb.key || '').toUpperCase() && modKey(e)===!!(kb.ctrl||kb.meta) && !!e.shiftKey===!!kb.shift && !!e.altKey===!!kb.alt;
  }
  function matchBind(e){
    for(const b of BNC_BINDS){
      // drawer + ranked + leaderboard + host panels have their own dispatcher
      // below (Esc/panel handling) — matching them here too would fire twice:
      // open here, instant close there on the SAME keypress (stopPropagation
      // does not stop same-node listeners; only stopImmediatePropagation
      // would, and registration order makes this one run first).
      if(b.id==='drawer' || b.id==='ranked' || b.id==='leaderboard' || b.id==='host11' || b.id==='host12') continue;
      // Alt+Shift+L is the leaderboard default: when the press matches the
      // leaderboard bind, 'Load file' (same factory default) yields to it.
      try{ if(b.id==='load' && typeof isLbKeybind==='function' && isLbKeybind(e)) continue; }catch{}
      if(b.dev && !isDev()) continue;
      if(matchBindKb(e, getBindKb(b))) return b;
    }
    return null;
  }
  // Typing guard: hotkeys must never fire from a text field (game chat, our
  // search/chat boxes, token prompt). The login modal has its own check;
  // this covers everything else, including our own panels.
  function isTypingKey(){
    try{
      const ae=document.activeElement;
      if(!ae) return false;
      if(ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'||ae.isContentEditable) return true;
      if(ae.closest && ae.closest('#bnc-lb-card,#bnc-drawer,#bnc-ranked,#bnc-keys,#bnc-host11,#bnc-host12')) return true;
      return false;
    }catch{ return false; }
  }
  // Watchdog: a stranded capture flag used to kill ALL hotkeys at once.
  function releaseStuckCapture(){
    try{
      if(_keybindCapturing && Date.now()-_keybindCaptureAt>10000){
        _keybindCapturing=false;
        try{ logErr('[KEYS] capture stuck >10s, released'); }catch{}
      }
    }catch{}
  }
  document.addEventListener('keydown', (e)=>{
    if(_keybindCapturing) return;
    try{ releaseStuckCapture(); }catch{}
    if (isModalOrInputFocused() || isTypingKey()) return;
    // composing in our box: hotkeys stay silent (the forwarder owns the keys)
    try{ if((e.key||'')!=='Escape' && untrapHoldActive()) return; }catch{}
    // Fixed host key (not in BNC_BINDS): F7 = backed out to lobby with no
    // code. Clears the local comp-room + ranked bindings only (no server
    // call) so re-setting/rejoining the same code re-resolves fresh via
    // fetchCompRoom / rankedTrack instead of sitting on stale state.
    if((e.key==='F7'||e.code==='F7') && !e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey){
      e.preventDefault(); e.stopPropagation();
      try{ resetCompRoomLocal(); }catch(err){ try{ logErr(`[KEYS] F7 comp reset failed ${err && err.message}`); }catch{} }
      try{ resetRankedRoomLocal(); }catch(err){ try{ logErr(`[KEYS] F7 ranked reset failed ${err && err.message}`); }catch{} }
      return;
    }
    const b=matchBind(e);
    if(b){ e.preventDefault(); e.stopPropagation(); try{ b.run(); }catch(err){ try{ logErr(`[KEYS] bind ${b.id} failed ${err && err.message}`); }catch{} } }
  }, true);

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.id = 'bnc-file-input';
  fileInput.accept = '.json';
  fileInput.style.display = 'none';
  fileInput.addEventListener('change', (e)=>handleFileLoad(e.target.files[0]));
  document.documentElement.appendChild(fileInput);

  const collectBadge = document.createElement('div');
  collectBadge.id = 'bnc-collect-badge';
  Object.assign(collectBadge.style, {
    position: 'fixed', top: '8px', left: '8px', zIndex: '2147483646',
    background: '#7c3aed', color: '#fff', padding: '4px 10px', borderRadius: '6px',
    fontSize: '12px', fontFamily: 'monospace', fontWeight: '600',
    display: 'none', boxShadow: '0 2px 8px rgba(0,0,0,0.3)'
  });
  document.documentElement.appendChild(collectBadge);

  function updateCollectBadge(){
    if(collectMode){
      collectBadge.textContent = `🔴 COLLECT ${collectedPackets.length}`;
      collectBadge.style.display = 'block';
    }else{
      collectBadge.style.display = 'none';
    }
  }

  // --- LOG FEED (bottom, passthrough + pause + decrypted hex) - dev only, hidden by default in v2 ---
  const DEV_MODE = !!GMGet('bnc_dev_mode', false);
  let feedEnabled = DEV_MODE && GMGet('bnc_feed_enabled', GMGet('bn_feed_enabled', false));
  let feedPaused = GMGet('bnc_feed_paused', GMGet('bn_feed_paused', false));
  const feedWrap = document.createElement('div');
  feedWrap.id = 'bnc-log-wrap';
  Object.assign(feedWrap.style, {
    position: 'fixed', bottom: '0', left: '0', right: '0', zIndex: '2147483645',
    display: (DEV_MODE && feedEnabled) ? 'block' : 'none', pointerEvents: 'none'
  });
  const feedBar = document.createElement('div');
  Object.assign(feedBar.style, {
    display: 'flex', gap: '6px', padding: '2px 6px', background: 'rgba(0,0,0,0.92)', borderTop: '1px solid rgba(255,255,255,0.12)',
    fontFamily: 'monospace', fontSize: '10px', pointerEvents: 'auto'
  });
  feedBar.innerHTML = `<span style="opacity:0.5">BNC feed - zone/stG/alv full</span><button id="bnc-feed-pause" style="background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:4px;padding:0 6px;cursor:pointer">${feedPaused?'▶ Resume':'⏸ Pause'}</button><button id="bnc-feed-expand" style="background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:4px;padding:0 6px;cursor:pointer">⤢ Expand</button><span id="bnc-feed-hint" style="opacity:0.5">toggle · pause · hover to scroll · drag top edge to resize</span>`;
  const feedEl = document.createElement('div');
  feedEl.id = 'bnc-log-feed';
  Object.assign(feedEl.style, {
    maxHeight: '38vh', height: '38vh', background: 'rgba(0,0,0,0.88)', color: '#e5e7eb',
    fontFamily: 'monospace', fontSize: '11px', lineHeight: '14px',
    padding: '6px 8px', overflowY: 'auto', overflowX: 'hidden', pointerEvents: 'auto',
    borderTop: '1px solid rgba(255,255,255,0.08)', backdropFilter: 'blur(2px)', resize: 'vertical'
  });
  feedEl.tabIndex = 0;
  feedEl.innerHTML = '<div style="opacity:0.5">waiting… decrypted hex will show here</div>';
  feedWrap.appendChild(feedBar);
  feedWrap.appendChild(feedEl);
  (document.body || document.documentElement).appendChild(feedWrap);
  feedWrap.addEventListener('mouseenter', ()=>{ feedWrap.style.pointerEvents = 'auto'; });
  feedWrap.addEventListener('mouseleave', ()=>{ if(!feedEl.matches(':hover') && !feedBar.matches(':hover')) feedWrap.style.pointerEvents = 'none'; });
  feedEl.addEventListener('mouseenter', ()=>{ feedWrap.style.pointerEvents = 'auto'; });
  feedEl.addEventListener('mouseleave', ()=>{ feedWrap.style.pointerEvents = 'none'; });
  setTimeout(()=>{ feedWrap.style.pointerEvents = 'none'; }, 500);

  let feedLines = [];
  function feedLog(evt, fields){
    if(!feedEnabled) return;
    if(feedPaused) return;
    const ts = new Date().toLocaleTimeString('en-GB', {hour12:false}) + '.' + String(Date.now()%1000).padStart(3,'0');
    const color = {join:'#10b981', leave:'#f59e0b', killfeed:'#ef4444', zone:'#a855f7', stg:'#f97316', alv:'#3b82f6', collect:'#7c3aed', capture:'#10b981', decrypted:'#38bdf8', hex:'#fbbf24', info:'#9ca3af', sent:'#10b981', err:'#ef4444', filtered:'#6b7280'}[evt] || '#9ca3af';
    const fieldStr = Object.entries(fields).map(([k,v])=> {
      // zone: full dump (up to 12k) - wwzw spams multiple zone ticks with huge floats per phase, need full view
      const cap = (evt==='zone' || evt==='stg' || (evt==='decrypted' && fields.packet==='zone')) ? 12000 : 4000;
      let s;
      if(typeof v==='object' && v!==null) s = JSON.stringify(v);
      else s = String(v);
      if(s.length>cap) s = s.slice(0,cap)+' …(+'+(s.length-cap)+' chars)';
      // escape html
      s = s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return `${k}=${s}`;
    }).join(' ');
    const line = `<div style="white-space:pre-wrap;word-break:break-all"><span style="opacity:0.45">${ts}</span> <span style="color:${color};font-weight:700">${evt}</span> <span style="opacity:0.9">${fieldStr}</span></div>`;
    feedLines.push(line);
    if(feedLines.length>800) feedLines.shift();
    const wasAtBottom = feedEl.scrollTop + feedEl.clientHeight >= feedEl.scrollHeight - 20;
    feedEl.innerHTML = feedLines.join('');
    if(wasAtBottom) feedEl.scrollTop = feedEl.scrollHeight;
  }
  function feedInfo(msg){ feedLog('info', {msg}); }
  function toggleFeed(){
    feedEnabled = !feedEnabled;
    GMSet('bnc_feed_enabled', feedEnabled);
    syncDevUI();
    showToast(feedEnabled ? 'Feed ON' : 'Feed OFF', false);
  }
  function togglePauseFeed(){
    feedPaused = !feedPaused;
    GMSet('bnc_feed_paused', feedPaused);
    const btn=document.getElementById('bnc-feed-pause');
    if(btn) btn.textContent = feedPaused ? '▶ Resume' : '⏸ Pause';
    feedLog('info', {msg: feedPaused ? 'paused (scrollable)' : 'resumed'});
    showToast(feedPaused ? 'Feed paused' : 'Feed resumed', false);
  }
  // Dev gating: feed UI + dev keybinds exist only when the DB row says so.
  // Called on every stats render (stats arrive async — hidden until then).
  function syncDevUI(){
    const dev=isDev();
    try{
      const dt=document.getElementById('bnc-dev-row');
      if(dt) dt.style.display=dev?'':'none';
    }catch{}
    try{
      if(feedWrap) feedWrap.style.display=(dev && DEV_MODE && feedEnabled)?'block':'none';
    }catch{}
    try{
      const h=document.getElementById('bnc-feed-hint');
      if(h){
        const fb=BNC_BINDS.find(x=>x.id==='feed'), pb=BNC_BINDS.find(x=>x.id==='pause');
        h.textContent=`${formatKeybind(getBindKb(fb))} toggle · ${formatKeybind(getBindKb(pb))} pause · hover to scroll · drag top edge to resize`;
      }
    }catch{}
    try{ if(typeof renderKeybindRows==='function') renderKeybindRows(); }catch{}
  }
  setTimeout(()=>{
    const btn=document.getElementById('bnc-feed-pause');
    if(btn) btn.onclick=()=>{
      feedPaused = !feedPaused;
      GMSet('bnc_feed_paused', feedPaused);
      btn.textContent = feedPaused ? '▶ Resume' : '⏸ Pause';
      feedLog('info', {msg: feedPaused ? 'paused' : 'resumed'});
    };
    const exp=document.getElementById('bnc-feed-expand');
    if(exp) exp.onclick=()=>{
      const isBig = feedEl.style.maxHeight==='70vh';
      feedEl.style.maxHeight = isBig ? '38vh' : '70vh';
      feedEl.style.height = isBig ? '38vh' : '70vh';
      exp.textContent = isBig ? '⤢ Expand' : '⤡ Collapse';
      feedLog('info', {msg: isBig ? 'feed collapsed 38vh' : 'feed expanded 70vh'});
    };
  }, 800);

  // --- PLAYER DRAWER (left overlay, skin, discord, game_id, game_mode, stats polled /api/stats 15s) ---
  const GM_NAMES_DRAWER = {zw_lg:'Late Game Zonewars', zw:'Zone Wars', wwzw:'Duo Wild West', ww:'Solo Wild West', WW:'Solo Wild West', bf:'Boxfights', BF:'Boxfights', bf2v2:'Boxfights 2v2', BF2V2:'Boxfights 2v2', va:'V-Arena', VA:'V-Arena', tdm_bvr:'Red Vs Blue', RVB:'Red Vs Blue', r_1v1:'Realistic 1v1', R_1V1:'Realistic 1v1', df:'Free Mode', DF:'Free Mode', lobby:'Lobby'};
  function fmtGmDrawer(raw, duo){ if(!raw) return '-'; const k=String(raw).trim(); let label = GM_NAMES_DRAWER[k] || GM_NAMES_DRAWER[k.toLowerCase()] || k.replace(/[_-]+/g,' ').replace(/\b\w/g,s=>s.toUpperCase()); if(duo && (k.toLowerCase()==='df' || k.toLowerCase()==='zw_lg') && !/^(solo|duo|\(duo\))/i.test(label)) label = '(Duo) ' + label; return label; }
  function fmtPlaytimeDrawer(s){ if(s==null) return '-'; s=Math.max(0,Math.floor(Number(s)||0)); const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=s%60; if(h) return `${h}h ${m}m`; if(m) return `${m}m ${sec}s`; return `${sec}s`; }
  function fmtLastSeen(iso){
    try{
      if(!iso) return '—';
      const ms=new Date(String(iso)).getTime()-Date.now();
      const age=Math.max(0, Math.round(-ms/1000));
      if(age<300) return 'Online now';
      if(age<3600) return `${Math.floor(age/60)}m ago`;
      if(age<86400) return `${Math.floor(age/3600)}h ago`;
      return `${Math.floor(age/86400)}d ago`;
    }catch{ return '—'; }
  }
  function skinUrlDrawer(id){ if(!id || id==='-' ) return null; return `https://buildnow-api.netlify.app/skins/${encodeURIComponent(String(id).trim())}.png`; }
  // team number -> diamond color (1 blue, 2 red, 3 green, 4 orange, 5 purple, 6 yellow, 7 cyan, 8 pink)
  function teamColorDrawer(pt){
    const n=parseInt(String(pt==null?'':pt).trim(), 10);
    const map={1:'#3b82f6',2:'#ef4444',3:'#22c55e',4:'#f97316',5:'#a855f7',6:'#eab308',7:'#06b6d4',8:'#ec4899'};
    return map[n]||null;
  }
  // keybind for drawer - customizable via button in drawer (default F2, shared with the shortcuts menu)
  function getDrawerKeybind(){
    try{
      const raw=GMGet('bnc_drawer_keybind', null);
      if(raw && typeof raw==='object' && raw.key) return raw;
      if(typeof raw==='string' && raw){
        try{ const j=JSON.parse(raw); if(j && j.key) return j; }catch{}
        // legacy string like "Ctrl+Shift+I" (or "Cmd+Shift+I" on Mac)
        const parts=String(raw).split('+');
        const kb={ctrl:false,shift:false,alt:false,meta:false,key:'F2'};
        for(const p of parts){ const l=p.trim().toLowerCase(); if(l==='ctrl'||l==='control') kb.ctrl=true; else if(l==='cmd'||l==='meta'||l==='command') kb.meta=true; else if(l==='shift') kb.shift=true; else if(l==='alt') kb.alt=true; else if(p.trim()) kb.key=p.trim().toUpperCase(); }
        return kb;
      }
    }catch{}
    return {ctrl:false, shift:false, alt:false, meta:false, key:'F2'};
  }
  function formatKeybind(kb){ const a=[]; if(kb.meta) a.push('Cmd'); if(kb.ctrl) a.push('Ctrl'); if(kb.shift) a.push('Shift'); if(kb.alt) a.push('Alt'); a.push(String(kb.key||'').toUpperCase()); return a.join('+'); }
  function isDrawerKeybind(e){
    return bindMatches(e, getDrawerKeybind());
  }
  let _keybindCapturing=false;
  let _keybindCaptureAt=0; // watchdog timestamp: a stranded capture used to kill every hotkey at once
  let apiOnline=false;

  let infoPanel=null, infoUpdateTimer=null, statsCache=null, statsPollTimer=null, drawerBackdrop=null;
  let coinsCache=null, _lastCoinsAt=0;
  // Drawer always starts closed on page load (first open) — never auto-open from storage.
  let drawerOpen = false;
  try { GMSet('bnc_info_open', false); } catch {}
  // Ranked/host shelved: drop stale ranked session hint + force individualist.
  try {
    const _sg = GMGet('bnc_session_gm', null);
    if(_sg && String(_sg).toLowerCase().indexOf('ranked') === 0) GMDel('bnc_session_gm');
    // bnc_host_mode is host-owned: set true in Violentmonkey storage to unlock F9/F11/F12 (never auto-reset here).
  } catch {}
  let drawerTab = GMGet('bnc_drawer_tab','stats'); // 'stats' | 'players'
  if(drawerTab!=='stats'&&drawerTab!=='players') drawerTab='stats';
  let playersCache=null, playersPollTimer=null, playersLoading=false, _playersSeq=0, _statsSeq=0;
  let lobbyAudio=null;
  let bgLobbyPollTimer=null;
  let _audioPlaying=false;
  let _audioPlayPending=false; // play() in flight (buffering) — blocks duplicate play() calls
  let _authCount=0; // outgoing Authenticate packets seen (first = login, never touches audio)
  let _lastPauseReason=null; // reason string of the last _audioPause() call (for STOPPED tracing)
  function ensureLobbyAudio(){
    if(lobbyAudio) return lobbyAudio; try{ if(typeof _inCompRoom==='function'&&_inCompRoom()){ try{ _audioPause('comp-room'); }catch{} return null; } }catch{} // comp rooms: no audio tag at all (decode + autoplay cost frames)
    try{
      logInfo('[AUDIO] ensureLobbyAudio create hidden tag');
      lobbyAudio=document.createElement('audio');
      lobbyAudio.id='bnc-lobby-audio';
      lobbyAudio.loop=true;
      lobbyAudio.autoplay=true;
      lobbyAudio.setAttribute('autoplay','');
      lobbyAudio.setAttribute('playsinline','');
      lobbyAudio.preload='auto';
      lobbyAudio.crossOrigin='anonymous';
      // hidden audio tag that auto plays — use off-screen instead of display:none (some browsers block display:none audio)
      lobbyAudio.style.position='fixed';
      lobbyAudio.style.left='-9999px';
      lobbyAudio.style.width='1px';
      lobbyAudio.style.height='1px';
      lobbyAudio.style.opacity='0';
      lobbyAudio.style.pointerEvents='none';
      lobbyAudio.volume=Math.max(0,Math.min(1, Number(GMGet('bnc_lobby_volume', 0.5))));
      // detailed audio event logs + track _audioPlaying state
      try{
        ['loadstart','canplay','canplaythrough','playing','pause','ended','error','stalled','suspend','volumechange','emptied'].forEach(ev=>{
          lobbyAudio.addEventListener(ev, ()=>{
            const st = `src=${String(lobbyAudio.src||'')} vol=${lobbyAudio.volume.toFixed(2)} paused=${lobbyAudio.paused} loop=${lobbyAudio.loop} err=${lobbyAudio.error? lobbyAudio.error.code:''} readyState=${lobbyAudio.readyState} networkState=${lobbyAudio.networkState}`;
            if(ev==='error') logErr(`[AUDIO] event ${ev} ${st} ${lobbyAudio.error? lobbyAudio.error.message:''}`);
            else if(ev==='playing') logOk(`[AUDIO] event ${ev} ${st}`);
            else logInfo(`[AUDIO] event ${ev} ${st}`);
            try{ feedLog(ev==='error'?'err': ev==='playing'?'ok':'info', {audio:ev, src:String(lobbyAudio.src||''), vol:lobbyAudio.volume, paused:lobbyAudio.paused}); }catch{}
            // Track _audioPlaying state: only flip to false on pause/ended/error/emptied events
            if(ev==='pause' || ev==='ended' || ev==='error' || ev==='emptied'){
              if(_audioPlaying) _audioPlaying=false; // only flip if currently true
              _audioPlayPending=false; // play attempt settled
            }
            if(ev==='playing'){
              _audioPlaying=true; // set true when playing starts (idempotent)
              _audioPlayPending=false; // play attempt settled
            }
            // STOPPED tracing: log what stopped playback and the last known reason
            if(ev==='pause' || ev==='ended' || ev==='error'){
              logInfo(`[AUDIO] STOPPED event=${ev} reason=${_lastPauseReason||'external/unknown'} vol=${lobbyAudio.volume.toFixed(2)} src=${String(lobbyAudio.src||'').slice(-50)} inMatch=${_optimisticState.inMatch} readyState=${lobbyAudio.readyState}${lobbyAudio.error? ' errCode='+lobbyAudio.error.code : ''}`);
              try{ feedLog('info', {audio:'stopped', event:ev, reason:_lastPauseReason||'external/unknown', vol:lobbyAudio.volume, inMatch:_optimisticState.inMatch}); }catch{}
              _lastPauseReason=null; // consume (next stop needs a fresh reason)
            }
          });
        });
      }catch{}
      (document.body||document.documentElement).appendChild(lobbyAudio);
      logInfo(`[AUDIO] appended hidden tag vol=${lobbyAudio.volume} loop=${lobbyAudio.loop} autoplay=${lobbyAudio.autoplay}`);
      // try resume on user gesture (drawer open) — only if not already playing and not in match
      try{ const resume=()=>{ logInfo('[AUDIO] click resume try'); try{ const rs = statsCache && statsCache.session; const rsLobby = effInMatch() !== true; if(lobbyAudio && !_audioPlaying && !_audioPlayPending && rsLobby && lobbyAudio.paused && lobbyAudio.src) { _audioPlayPending=true; lobbyAudio.play().then(()=> logOk('[AUDIO] resume playing')).catch(e=> { _audioPlayPending=false; logErr(`[AUDIO] resume blocked ${e.message||e}`); }); } }catch(e){ logErr(`[AUDIO] resume err ${e.message}`);} }; document.addEventListener('click', resume, {once:true}); }catch{}
    }catch(e){ logErr(`[AUDIO] ensure failed ${e.message}`); }
    return lobbyAudio;
  }
  // Single choke point for stopping lobby audio — records WHY for STOPPED tracing
  function _audioPause(reason){
    try{
      const a = lobbyAudio;
      if(!a){ logInfo(`[AUDIO] STOP request reason=${reason} (no audio tag)`); return; }
      logInfo(`[AUDIO] STOP request reason=${reason} paused=${a.paused} playing=${_audioPlaying} pending=${_audioPlayPending} inMatch=${_optimisticState.inMatch} readyState=${a.readyState} vol=${a.volume.toFixed(2)}`);
      _lastPauseReason = reason;
      if(!a.paused) a.pause();
    }catch(e){ logErr(`[AUDIO] pause err ${e.message}`); }
  }
  function updateLobbyMusic(){ try{ if(typeof _inCompRoom==='function'&&_inCompRoom()){ try{ _audioPause('comp-room'); }catch{} return; } }catch{} // comp rooms: silent rooms, never music
    try{
      const sc=statsCache;
      const sess=sc && sc.session;
      // Use the freshest signal there is (packets → capture shadow → server):
      // "lobby" means nothing claims match, so a stale server 'match' cannot
      // keep the music paused in the lobby.
      const isLobby = effInMatch() !== true;
      const rawUrl = sc && sc.lobby_music_url; const url = rawUrl ? (rawUrl.startsWith('http') ? rawUrl : 'https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/lobby-music/'+rawUrl.replace(/^music\//,'')).replace(/\?$/,'') : null;
      const audio=ensureLobbyAudio();
      if(!audio){ logErr('[AUDIO] update no audio'); return; }
      // volume from GM
      const vol = Math.max(0,Math.min(1, Number(GMGet('bnc_lobby_volume', 0.5))));
      audio.volume = vol;
      logInfo(`[AUDIO] update isLobby=${isLobby} optimistic=${_optimisticState.inMatch} hasUrl=${!!url} url=${String(url||'')} src=${String(audio.src||'')} vol=${vol.toFixed(2)} paused=${audio.paused} readyState=${audio.readyState} networkState=${audio.networkState} state=${sess? sess.state:'no-sess'}`);
      try{ feedLog('info', {audio:'update', isLobby, inMatch:_optimisticState.inMatch, curScn:_optimisticState.curScn, hasUrl:!!url, vol, paused:audio.paused, state:sess? sess.state:null}); }catch{}
      // GUARD: pause ONLY on optimistic in-match. A MainLobby curScn (or anything
      // else while lobby) must never pause the music.
      if(_optimisticState.inMatch){
        _audioPause('in-match');
        return;
      }
      // Lobby (optimistic): never pause here — without a URL there is nothing to start.
      if(!url){
        logInfo('[AUDIO] no url, nothing to play (no pause)');
        return;
      }
      // Server still thinks match (it trails live packets ~2-5s): hold, don't play yet.
      if(!isLobby){
        logInfo(`[AUDIO] hold (server still match, optimistic lobby) state=${sess? sess.state:'no-sess'}`);
        return;
      }
      // Guard FIRST: skip when audio is actually producing sound (ground truth = paused flag).
      // The _audioPlaying var can desync (e.g. src swapped without a pause event), so resync it here.
      if(!audio.paused && audio.readyState >= 2){
        _audioPlaying = true; // resync (playing event may have been missed)
        logInfo('[AUDIO] actually playing, skip everything (no restart)');
        return;
      }
      // Guard: play() already in flight (buffering) — a second play() restarts the track
      if(_audioPlayPending){
        logInfo('[AUDIO] play already pending, skip duplicate play()');
        return;
      }
      _audioPlaying = false; // resync (stale true with no sound -> allow play below)
      // only reload when src changed or browser dropped source (only reached when NOT sounding)
      const needsReload = audio.src !== url || audio.networkState === 3 || (audio.error && audio.error.code);
      if(needsReload){
        logInfo(`[AUDIO] src reload src_changed=${audio.src!==url} networkState=${audio.networkState} errCode=${audio.error? audio.error.code:'none'} url=${String(url)}`);
        try{ audio.src=url; audio.load(); logInfo('[AUDIO] load called'); }catch(e){ logErr(`[AUDIO] load err ${e.message}`); }
      } else {
        logInfo('[AUDIO] src cached, skip load');
      }
      // try play (may need user gesture - drawer open is gesture)
      logInfo(`[AUDIO] play() try vol=${vol} loop=${audio.loop} autoplay=${audio.autoplay} paused=${audio.paused} readyState=${audio.readyState}`);
      _audioPlayPending = true; // mark play in flight before calling
      const p = audio.play();
      if(p && p.then) p.then(()=> logOk('[AUDIO] play() ok playing')).catch(e=> { _audioPlayPending=false; logErr(`[AUDIO] play() blocked ${e.message||e} — need user click on page/drawer`); });
      else if(p && p.catch) p.catch(e=> { _audioPlayPending=false; logErr(`[AUDIO] play() blocked ${e.message||e}`); });
      else { _audioPlayPending=false; logInfo('[AUDIO] play() no promise'); }
    }catch{}
  }
  function prefetchLobbyMusic(){ try{ if(typeof _inCompRoom==='function'&&_inCompRoom()) return; }catch{} // comp rooms: no prefetch either
    try{
      const sc=statsCache;
      const rawUrl=sc && sc.lobby_music_url;
      if(!rawUrl) return;
      const url=rawUrl.startsWith('http') ? rawUrl : 'https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/lobby-music/'+rawUrl.replace(/^music\//,'');
      if(!url) return;
      // warm browser cache with fetch
      fetch(url, {method:'HEAD'}).catch(()=>{});
      // only preload if silent and not in match (avoid restart; ground truth = paused)
      const audio=ensureLobbyAudio();
      if(!audio || _optimisticState.inMatch || !audio.paused) return;
      if(audio.src!==url){
        logInfo(`[AUDIO] prefetch src=${url.slice(-40)}`);
        try{ audio.src=url; audio.load(); }catch{}
      }
    }catch{}
  }
  function startBgLobbyPoll(){ if(bgLobbyPollTimer) return; bgLobbyPollTimer=setInterval(()=>{ if(sessionId) fetchStats(false); }, (typeof BNC_LITE!=="undefined"&&BNC_LITE)?30000:15000); }
  function stopBgLobbyPoll(){ if(bgLobbyPollTimer){ clearInterval(bgLobbyPollTimer); bgLobbyPollTimer=null; } }

  function createInfoPanel(){
    // cleanup legacy handle (removed per request)
    try{ const _oldH=document.getElementById('bnc-drawer-handle'); if(_oldH) _oldH.remove(); }catch{}
    if(document.getElementById('bnc-drawer')) return;
    // backdrop - blocks game clicks when drawer open, non-passthrough
    drawerBackdrop=document.createElement('div');
    drawerBackdrop.id='bnc-drawer-backdrop';
    Object.assign(drawerBackdrop.style,{position:'fixed', inset:'0', zIndex:'2147483644', background:'rgba(0,0,0,0.42)', backdropFilter:'blur(2px)', display:'none', pointerEvents:'auto'});
    drawerBackdrop.addEventListener('click', ()=> toggleInfoPanel(false));
    drawerBackdrop.addEventListener('wheel', e=> e.stopPropagation(), {passive:false});
    drawerBackdrop.addEventListener('mousedown', e=> e.stopPropagation());
    drawerBackdrop.addEventListener('mousemove', e=> e.stopPropagation());
    (document.body||document.documentElement).appendChild(drawerBackdrop);

    // drawer - left overlay (centered content, no cards-in-cards)
    infoPanel=document.createElement('div');
    infoPanel.id='bnc-drawer';
    Object.assign(infoPanel.style,{position:'fixed', left:'0', top:'0', bottom:'0', zIndex:'2147483645', width:'380px', maxWidth:'88vw', background:'#0f0f0f', color:'#e5e7eb', borderRight:'1px solid rgba(255,255,255,0.10)', display:'flex', flexDirection:'column', transform:'translateX(-100%)', transition:'transform 220ms cubic-bezier(0.22,1,0.36,1)', pointerEvents:'auto', fontFamily:'Inter, system-ui, sans-serif', boxShadow:'8px 0 32px rgba(0,0,0,0.45)', overflow:'hidden'});
    infoPanel.innerHTML=`
      <div style="display:flex;align-items:center;justify-content:flex-end;padding:10px 10px 0 10px;flex-shrink:0">
        <button id="bnc-drawer-close" style="width:28px;height:28px;border:1px solid rgba(255,255,255,0.12);background:#1a1a1a;color:#9ca3af;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px">✕</button>
      </div>
      <div id="bnc-drawer-body" style="flex:1;overflow-y:auto;overflow-x:hidden;padding:8px 20px 16px 20px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;scrollbar-width:thin">
        <div style="opacity:0.7;font-size:12px">Loading…</div>
      </div>
      <div id="bnc-drawer-footer" style="padding:10px 14px;border-top:1px solid rgba(255,255,255,0.06);display:flex;justify-content:space-between;align-items:center;gap:8px;flex-shrink:0;font-size:11px;color:#6b7280">
        <span id="bnc-drawer-version">v${BNC_VERSION}</span>
        <span id="bnc-drawer-ranked" style="display:none;cursor:pointer;color:#9ca3af;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:45%" title="Click to copy — use it to report abuse"></span>
        <span id="bnc-drawer-api" style="display:flex;align-items:center;gap:6px"><span id="bnc-drawer-dot" style="width:8px;height:8px;border-radius:50%;background:#6b7280;display:inline-block"></span><span id="bnc-drawer-api-text">Offline</span></span>
      </div>`;
    // non-passthrough: stop game from seeing wheel/keys inside drawer
    infoPanel.addEventListener('wheel', e=> e.stopPropagation(), {passive:false});
    infoPanel.addEventListener('mousedown', e=> e.stopPropagation());
    infoPanel.addEventListener('click', e=> e.stopPropagation());
    infoPanel.addEventListener('keydown', e=> e.stopPropagation());
    (document.body||document.documentElement).appendChild(infoPanel);
    infoPanel.querySelector('#bnc-drawer-close').onclick=(e)=>{ e.stopPropagation(); toggleInfoPanel(false); };
    // Esc to close
    document.addEventListener('keydown', (e)=>{ if(e.key==='Escape' && drawerOpen) toggleInfoPanel(false); }, true);
    // Apply initial open state without animation flash
    if(drawerOpen) openDrawerImmediate();
  }
  function openDrawerImmediate(){
    if(!infoPanel||!drawerBackdrop) return;
    infoPanel.style.transition='none';
    infoPanel.style.transform='translateX(0)';
    drawerBackdrop.style.display='block';
    // force reflow then restore transition
    void infoPanel.offsetWidth;
    infoPanel.style.transition='transform 220ms cubic-bezier(0.22,1,0.36,1)';
    drawerOpen=true;
  }
  function setDrawerOpen(open){
    if(!infoPanel || !drawerBackdrop) createInfoPanel();
    drawerOpen=!!open;
    GMSet('bnc_info_open', drawerOpen);
    if(drawerOpen){
      infoPanel.style.transform='translateX(0)';
      drawerBackdrop.style.display='block';
      // hidden audio tag that auto plays - trigger within user gesture (fixes browser autoplay block)
      try{
        const sc=statsCache;
        const sess=sc && sc.session;
        const isLobby = effInMatch() !== true;
        if(isLobby && sc && sc.lobby_music_url){
          const aud=ensureLobbyAudio();
          // Drawer must NEVER restart lobby music: only act when audio is silent, no play
          // in flight, and we're in lobby (optimistic AND server — server trails, so both).
          // Ground truth = aud.paused.
          const drawerIsLobby = effInMatch() !== true;
          if(aud && aud.paused && !_audioPlayPending && drawerIsLobby){
            const rawUrl=sc.lobby_music_url||''; const fullUrl=(rawUrl.startsWith('http')? rawUrl: 'https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/lobby-music/'+rawUrl.replace(/^music\//,'')).replace(/\?$/,'');
            if(aud.src !== fullUrl || aud.networkState===3 || (aud.error && aud.error.code)){ try{ aud.src=fullUrl; aud.load(); logInfo(`[AUDIO] drawer open src set ${fullUrl}`); }catch{} }
            aud.volume=Math.max(0,Math.min(1, Number(GMGet('bnc_lobby_volume',0.5))));
            _audioPlaying = false; // resync before play
            _audioPlayPending = true; // mark play in flight
            const p=aud.play(); if(p&&p.catch) p.catch(()=>{ _audioPlayPending=false; });
          } else if(aud){
            logInfo(`[AUDIO] drawer open skip play inMatch=${_optimisticState.inMatch} paused=${aud.paused} pending=${_audioPlayPending}`);
          }
        } else {
          ensureLobbyAudio();
        }
      }catch{}
      fetchStats(true);
      startStatsPoll();
      startBgLobbyPoll();
      if(drawerTab==='players'){ fetchPlayers(true); startPlayersPoll(); }
      else { try{ const _sess=statsCache&&statsCache.session; const _rooms=_sess&&_sess.rooms; const _cid=_sess&&_sess.current_room_id; const _cr=_cid&&_rooms?_rooms[_cid]:null; const _pio=getPartyInfo(_sess); if((_pio.isParty&&_pio.code)||(_cr&&String(_cr.room_id||'')!=='lobby')){ fetchPlayers(true); startPlayersPoll(); } }catch{} }
    } else {
      infoPanel.style.transform='translateX(-100%)';
      drawerBackdrop.style.display='none';
      stopStatsPoll();
      stopPlayersPoll();
      // keep bg poll for lobby music if we have a session and music, else stop
      if(sessionId) startBgLobbyPoll(); else stopBgLobbyPoll();
    }
  }
  function toggleInfoPanel(force){
    if(!infoPanel) createInfoPanel();
    if(typeof force==='boolean') setDrawerOpen(force);
    else setDrawerOpen(!drawerOpen);
  }
  function switchDrawerTab(tab){
    drawerTab = tab==='players' ? 'players' : 'stats';
    GMSet('bnc_drawer_tab', drawerTab);
    renderInfoPanel();
    if(drawerTab==='players'){ fetchPlayers(true); startPlayersPoll(); } else { stopPlayersPoll(); }
  }
  function fetchPlayers(forceRender){
    if(!sessionId){ if(forceRender) renderInfoPanel(); return; }
    if(playersLoading) return;
    playersLoading=true;
    const mySeq=++_playersSeq;
    const base=resolveApiBase();
    const url=base + '/api/players?session_id=' + encodeURIComponent(sessionId);
    const GM = typeof GM_xmlhttpRequest!=='undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
    const onDone=(ok, txt)=>{
      playersLoading=false;
      if(mySeq!==_playersSeq) return; // superseded: a newer poll already went out
      if(ok){
        try{
          const j=JSON.parse(txt);
          playersCache=j;
          if(drawerTab==='players') renderInfoPanel();
          else { try{ const _pi=getPartyInfo(statsCache&&statsCache.session); if(_pi.isParty) renderInfoPanel(); }catch{} }
        }catch{ if(forceRender && drawerTab==='players') renderInfoPanel(); }
      } else if(forceRender && drawerTab==='players') renderInfoPanel();
    };
    if(GM){
      try{
        GM({method:'GET', url, timeout:8000, onload:r=> onDone(r.status>=200&&r.status<300, r.responseText), onerror:()=> onDone(false,''), ontimeout:()=> onDone(false,'')});
        return;
      }catch{}
    }
    fetch(url,{method:'GET'}).then(r=>r.text().then(t=> onDone(r.ok, t)).catch(()=> onDone(false,''))).catch(()=> onDone(false,''));
  }
  function startPlayersPoll(){ try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE) return; }catch{} stopPlayersPoll(); fetchPlayers(false); playersPollTimer=setInterval(()=> fetchPlayers(false), 15000); }
  function stopPlayersPoll(){ if(playersPollTimer){ clearInterval(playersPollTimer); playersPollTimer=null; } }
  // Prompt roster refresh after membership changes (queue/match leave or
  // join). Delayed 3s so the leave/join capture batch reaches the server
  // first (server prunes on Leave, /api/players is correct after that);
  // debounced to one refresh per 8s for mass-leave bursts.
  let _playersRefreshT=null, _lastPlayersRefresh=0;
  function schedulePlayersRefresh(reason){ try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE) return; }catch{}
    try{
      const now=Date.now();
      if(now-_lastPlayersRefresh<8000) return;
      _lastPlayersRefresh=now;
      if(_playersRefreshT){ try{ clearTimeout(_playersRefreshT); }catch{} _playersRefreshT=null; }
      try{ logInfo(`[PLAYERS] refresh scheduled (${reason})`); }catch{}
      _playersRefreshT=setTimeout(()=>{ _playersRefreshT=null; try{ fetchPlayers(false); }catch{} }, 3000);
    }catch{}
  }
  // centered drawer - skin top, username, discord - game_id, state - gm, separator, stats, footer, keybind button + tabs
  function renderInfoPanel(){
    if(!infoPanel) createInfoPanel();
    try{ maybeStreakToast(); }catch{}
    try{ if(statsCache) _lastStatsAt=Date.now(); }catch{}
    const body=document.getElementById('bnc-drawer-body');
    if(!body) return;
    const dname = discordUsername || GMGet('bnc_discord_username','-') || '-';
    const uname = GMGet('bnc_game_username','-') || dname || '-';
    const gid = GMGet('bnc_game_id','-') || '-';
    const skin = GMGet('bnc_skin','-') || '-';
    const gmRaw = GMGet('bnc_session_gm','-') || '-';
    const skinUrl = skinUrlDrawer(skin);
    const sc = statsCache;
    const kills = sc? sc.kills : null;
    const deaths = sc? sc.deaths : null;
    const kd = sc && sc.kd!=null ? sc.kd : null;
    const wins = sc? sc.wins : null;
    const coinsBal = (coinsCache && coinsCache.balance!=null) ? coinsCache.balance : null;
    const tpr = sc && sc.tourney_pr!=null ? sc.tourney_pr : null;
    const tprDelta = sc && sc.pr_delta_pct!=null ? sc.pr_delta_pct : null;
    const tprTxt = tpr==null ? null : (`${tpr}` + (tprDelta==null ? '' : (tprDelta>0 ? ` (+${tprDelta}%)` : (tprDelta<0 ? ` (${tprDelta}%)` : ' (=)'))));
    const rankPct = sc && sc.rank && sc.rank.total>=2 ? sc.rank.pct : null;
    const pGlobal = sc? sc.playtime_global : null;
    const sess = sc && sc.session ? sc.session : null;
    const _curRoom = sess && sess.rooms ? sess.rooms[sess.current_room_id] : null;
    const _isDuoRoom = !!(_curRoom && _curRoom.gv && String(_curRoom.gv).toLowerCase() !== 'solo');
    const gmLive = sess && sess.game_mode ? fmtGmDrawer(sess.game_mode, _isDuoRoom) : fmtGmDrawer(gmRaw, _isDuoRoom);
    // state: Lobby vs In Match - spec says show "In Lobby" when no game mode.
    // Same authority as everywhere else (packets first), so the drawer can never
    // disagree with the Discord card or the music.
    let stateLabel='In Lobby';
    try{ if(effInMatch()===true) stateLabel='In Match'; }catch{}
    // per current gm playtime
    let perGmCurrent=null;
    if(sc && sc.playtime_per_gm){
      const curRaw = (sess && sess.game_mode) ? String(sess.game_mode) : String(gmRaw);
      const keys=[curRaw, curRaw.toLowerCase(), curRaw.toUpperCase()];
      for(const k of keys){ if(sc.playtime_per_gm[k]!=null){ perGmCurrent=sc.playtime_per_gm[k]; break; } }
      if(perGmCurrent==null){
        for(const [kk,v] of Object.entries(sc.playtime_per_gm)){ if(kk.toLowerCase()===curRaw.toLowerCase()){ perGmCurrent=v; break; } }
      }
    }
    const skinImg = skinUrl
      ? `<img src="${skinUrl}" alt="${String(skin).slice(0,24)}" style="width:112px;height:112px;object-fit:contain;background:radial-gradient(140px 90px at 50% 20%, rgba(255,255,255,0.06), transparent 70%), #111;border:1px solid rgba(255,255,255,0.08);border-radius:16px;image-rendering:pixelated;flex-shrink:0" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" /><div style="display:none;width:112px;height:112px;align-items:center;justify-content:center;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:16px;font-size:32px;flex-shrink:0">▣</div>`
      : `<div style="width:112px;height:112px;display:flex;align-items:center;justify-content:center;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:16px;font-size:32px;flex-shrink:0">▣</div>`;
    const kbLabel = formatKeybind(getDrawerKeybind());
    const row = (label,val)=> `<div style="display:flex;justify-content:space-between;align-items:center;width:100%;max-width:240px;font-size:13px"><span style="color:#9ca3af">${label}</span><span style="font-weight:700;color:#fff">${val==null? '-' : val}</span></div>`;
    const hasGm = gmLive && gmLive !== '-' && gmLive !== '-' && gmLive.toLowerCase() !== 'unknown' && gmLive.toLowerCase() !== 'lobby';
    // party: "In Party - <state> - GM" when the player is in a party
    const _partyInfo = getPartyInfo(sess);
    const isInParty = !!(_partyInfo.isParty && _partyInfo.code);
    let stateGmLine = hasGm && stateLabel.toLowerCase() !== gmLive.toLowerCase() ? `${stateLabel} - ${gmLive}` : stateLabel;
    if(isInParty){
      stateGmLine = hasGm ? `In Party - ${stateLabel} - ${gmLive}` : `In Party - ${stateLabel}`;
    }
    // No game mode: bare status only ("In Lobby", never "In Lobby - …").
    if(!hasGm){
      stateGmLine = isInParty ? `In Party - ${stateLabel}` : stateLabel;
    }
    try{ stateGmLine = String(stateGmLine).replace(/\s+-\s*$/, ''); }catch{}
    // ranked overrides party: "In Match - Ranked Match - GM"
    // liveRanked() also requires fresh captures — a ghost block from a dead
    // session (kick/reload) never renders as ranked.
    const _ranked = liveRanked();
    if(_ranked){
      stateGmLine = hasGm ? `${stateLabel} - Ranked Match - ${gmLive}` : `${stateLabel} - Ranked Match`;
    }
    // Room tag (host-set via /scrim /tourney): most specific status wins over
    // party text, but never over a live ranked block. Tourney shows its name,
    // scrims show the mode (same convention as the friends presence line).
    // Stale stats must not freeze a tag on screen: hide it when the last
    // successful poll is older than 2 minutes (fail-open when unknown).
    // Public rooms never show comp lines, even with a trailing server tag.
    try{
      const _sessTag = sess && sess.tag;
      let _tagFresh = true;
      try{ if(_lastStatsAt) _tagFresh = (Date.now()-_lastStatsAt)<=120000; }catch{}
      let _pub=false; try{ _pub=!!_publicRoom; }catch{}
      if(!_pub && _tagFresh && _sessTag && (_sessTag.kind==='tourney' || _sessTag.kind==='scrim') && stateLabel==='In Match' && !_ranked){
        const _tn = String(_sessTag.name||'').replace(/</g,'&lt;');
        stateGmLine = _sessTag.kind==='tourney'
          ? `In Tourney - ${_tn||'Tournament'}`
          : `In Scrims - ${hasGm ? gmLive : 'Custom'}`;
      } else if(!_pub && !_ranked && stateLabel==='In Match' && _compRoom){
        // local fallback: join-detected comp room whose tag hasn't landed
        // in stats yet (host started but tag row lagging). Same line shape.
        try{
          const _cc=(sess&&sess.short_code)||null;
          if(_cc && String(_cc).toUpperCase()===String(_compRoom.code||'').toUpperCase()){
            const _cn=String((_compRoom&&_compRoom.name)||'Tourney').replace(/</g,'&lt;');
            stateGmLine = `In Tourney - ${_cn}`;
          }
        }catch{}
      } else if(!_pub && !_ranked && stateLabel==='In Match'){
        // stats-poll fallback: comp_tourney from the server even without
        // a room tag or local _compRoom (tag row missing, pID not sniffed).
        try{
          const _ct=statsCache&&statsCache.comp_tourney;
          if(_ct && _ct.name){
            stateGmLine = `In Tourney - ${String(_ct.name).replace(/</g,'&lt;')}`;
          }
        }catch{}
      }
    }catch{}
    const smallRow = (label,val)=> `<div style="display:flex;justify-content:space-between;align-items:center;width:100%;max-width:240px;font-size:11px"><span style="color:#9ca3af">${label}</span><span style="font-weight:600;color:#fff">${val==null? '-' : val}</span></div>`;
    const perGmRow = hasGm && perGmCurrent!=null ? smallRow('Playtime ('+gmLive+')', fmtPlaytimeDrawer(perGmCurrent)) : '';
    // Empty account (no game username and no stats yet): placeholder instead of blank rows
    const _gmUname = GMGet('bnc_game_username', null);
    const noStats = (kills==null) && (!_gmUname || _gmUname==='-');
    const emptyStatsHtml = `<div style="display:flex;flex-direction:column;gap:8px;align-items:center;width:100%;max-width:240px;padding:18px 14px;background:#171717;border:1px dashed rgba(255,255,255,0.16);border-radius:12px;text-align:center"><div style="font-size:26px">📊</div><div style="font-size:14px;font-weight:800;color:#fff">No stats yet</div><div style="font-size:12px;color:#9ca3af;line-height:1.5">Play 1-2 matches to get your account ready.<br>Kills, deaths, wins and skins will show up here.</div></div>`;
    // ranked arena rows (below K/D): ranked-session ONLY. Casual lobbies and
    // matches never show points/tier rows — liveRanked() (_ranked above) is
    // the gate; values come from the session-independent arena block (your
    // current ladder points), NOT the ranked block's tourney points (0 on a
    // fresh tourney). Never throws.
    const fmtPts = n => n==null ? '-' : Number(n).toLocaleString('en-US');
    const _arena = (sc && sc.arena) || null;
    const _arenaGated = (_ranked && _arena && _arena.points!=null) ? _arena : null;
    // progress-to-next lives on the session-independent arena block now
    // (the ranked block carries points/tier only).
    const _arenaNext = (sc && sc.arena) || null;
    let arenaRowsHtml = '';
    if(_arenaGated){
      const _msColor = _arena.milestone_color || '#fff';
      const _msIcon = _arena.milestone_icon_url
        ? `<img src="${String(_arena.milestone_icon_url).replace(/"/g,'')}" alt="" style="width:20px;height:20px;object-fit:contain;vertical-align:-3px;margin-right:4px" onerror="this.style.display='none'" />`
        : '';
      arenaRowsHtml = `<div style="display:flex;justify-content:space-between;align-items:center;width:100%;max-width:240px;font-size:13px"><span style="color:#9ca3af">Arena Points</span><span style="font-weight:700;color:${_msColor}">${_msIcon}${fmtPts(_arena.points)} (${String(_arena.milestone||'').replace(/</g,'&lt;')})</span></div>`;
      if(_arenaNext && _arenaNext.next_milestone){
        arenaRowsHtml += `<div style="font-size:12px;color:#9ca3af;text-align:center;margin-top:2px">${fmtPts(_arenaNext.points_to_next)} points needed to reach ${String(_arenaNext.next_milestone).replace(/</g,'&lt;')} (${_arenaNext.next_points_pct||0}%)</div>`;
      } else {
        arenaRowsHtml += `<div style="font-size:12px;color:#9ca3af;text-align:center;margin-top:2px">Max rank reached</div>`;
      }
    }
    // v2.19.0 streak shelf: session-independent, always visible when arena present.
    try{
      const _st = (_arenaNext && _arenaNext.streaks) || (sc && sc.arena && sc.arena.streaks) || null;
      if(_st && (_st.daily!=null || _st.win!=null)){
        const _esc = s => String(s==null?'':s).replace(/</g,'&lt;');
        arenaRowsHtml += `<div style="display:flex;justify-content:space-between;width:100%;max-width:240px;font-size:12px;margin-top:4px"><span style="color:#9ca3af">🔥 Win streak</span><span style="color:#fff;font-weight:700">x${_esc(_st.win||0)}${_st.best_win?` (best ${_esc(_st.best_win)})`:''}</span></div>`;
        arenaRowsHtml += `<div style="display:flex;justify-content:space-between;width:100%;max-width:240px;font-size:12px"><span style="color:#9ca3af">📅 Daily streak</span><span style="color:#fff;font-weight:700">x${_esc(_st.daily||0)}</span></div>`;
        if(_st.best_match_kills!=null) arenaRowsHtml += `<div style="display:flex;justify-content:space-between;width:100%;max-width:240px;font-size:12px"><span style="color:#9ca3af">💀 Best match</span><span style="color:#fff;font-weight:700">${_esc(_st.best_match_kills)} kills</span></div>`;
      }
    }catch{}
    // 🏆 comp rows are "my points in THIS tourney" — only while holding the
    // tourney room. sc.comp is lifetime open-tourney data (server sends it
    // whenever a tourney is open) and would otherwise linger in the drawer
    // long after leaving. Gate mirrors updateTourneyHud: server comp_tourney
    // block, local _compRoom code match, or room tag.
    const _inTourneyRoom = (()=>{ try{
      const _code=(sess&&sess.short_code)||null;
      if(!_code) return false;
      if(statsCache&&statsCache.comp_tourney) return true;
      try{ if(_compRoom&&String(_compRoom.code||'').toUpperCase()===String(_code).toUpperCase()) return true; }catch{}
      try{ const _tg=sess&&sess.tag; if(_tg&&(_tg.kind==='tourney'||_tg.kind==='scrim')) return true; }catch{}
      return false;
    }catch{ return false; } })();
    const statsRowsHtml = noStats ? emptyStatsHtml : `
          ${row('Kills', kills)}
          ${row('Deaths', deaths)}
          ${row('K/D', kd)}
          ${arenaRowsHtml}
          ${row('Play points', sc && sc.play_points!=null ? fmtPts(sc.play_points) : null)}
          ${(_inTourneyRoom && sc && sc.comp && sc.comp.length ? sc.comp.map(c=>row('🏆 '+String(c.tourney_name!=null?c.tourney_name:('Event #'+c.tourney_id)).replace(/</g,'&lt;'), fmtPts(c.points)+` (${c.wins||0}W)`)).join('') : '')}
          ${row('Wins', wins)}
          ${row('💰 Nectar Coins', coinsBal!=null ? fmtCoinsCompact(coinsBal) : null)}
          ${row('Tourney PR', tprTxt)}
          ${row('Playtime', pGlobal!=null? fmtPlaytimeDrawer(pGlobal):null)}
          ${perGmRow}
          ${rankPct!=null? `<div style="font-size:12px;color:#9ca3af;text-align:center;margin-top:2px">Better than <span style="color:#fff;font-weight:800">${rankPct}%</span> of players</div>`:''}`;
    // Players tab visibility follows room lifecycle (Create/Join -> show, Leave -> hide).
    // /api/stats serves a FLAT session (room_id/state, no rooms map): a
    // non-lobby room_id is a real room, lobby or match. Self-leave prunes
    // the room server-side -> room_id falls back to lobby -> hidden.
    // Plus the instant local flag: server truth lags joins by seconds, so a
    // just-sent create/join op (or scene flip) shows the tab on the packet.
    const _sessRoomId = (sess && sess.room_id) || null;
    const _pcRoomId = (playersCache && playersCache.room_id) || null;
    let hasRealRoom = (!!_sessRoomId && String(_sessRoomId)!=='lobby')
      || (!!_pcRoomId && String(_pcRoomId)!=='lobby');
    try{ if(!hasRealRoom && localRoomActive()) hasRealRoom=true; }catch{}
    const isLobbyTab = !hasRealRoom;
    // No player list without a real room: clear stale roster when back to lobby pseudo-room
    // (fixes not refreshing). Party status still shows on the detail line above.
    if(isLobbyTab){
      // clean up stale player list when back in lobby (fixes not refreshing)
      if(playersCache){ playersCache=null; }
      if(drawerTab==='players'){
        drawerTab='stats';
        try{ GMSet('bnc_drawer_tab', drawerTab); }catch{}
        stopPlayersPoll();
      }
    }
    // tab bar - Players whenever a real room exists (lobby or match), never for pure lobby pseudo-room
    const tabBtn=(id,label,active)=>`<button id="${id}" style="flex:1;max-width:120px;padding:6px 0;border-radius:999px;border:1px solid ${active?'#fff':'rgba(255,255,255,0.12)'};background:${active?'#fff':'#171717'};color:${active?'#000':'#9ca3af'};font-size:12px;font-weight:700;cursor:pointer">${label}</button>`;
    const tabBar = isLobbyTab
      ? `<div style="display:flex;gap:6px;justify-content:center;width:100%;margin-bottom:4px">${tabBtn('bnc-tab-stats','Stats',drawerTab==='stats')}</div>`
      : `<div style="display:flex;gap:6px;justify-content:center;width:100%;margin-bottom:4px">${tabBtn('bnc-tab-stats','Stats',drawerTab==='stats')}${tabBtn('bnc-tab-players','Players',drawerTab==='players')}</div>`;
    const statsHtml = `
      <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;width:100%;padding:10px 0 6px 0">
        ${skinImg}
        <div style="font-size:19px;font-weight:800;color:#fff;text-align:center;word-break:break-word;max-width:240px">${String(uname).replace(/</g,'&lt;')}</div>
        <div style="font-size:12px;color:#9ca3af;text-align:center;word-break:break-all;max-width:240px">${String(dname).replace(/</g,'&lt;')} - ${String(gid).replace(/</g,'&lt;')}</div>
        <div style="font-size:13px;color:#e5e7eb;text-align:center;margin-top:2px">${stateGmLine}</div>
        <div style="width:48px;height:1px;background:rgba(255,255,255,0.12);margin:10px 0"></div>
        <div style="display:flex;flex-direction:column;gap:7px;align-items:center;width:100%">
          ${statsRowsHtml}
        </div>
        <button id="bnc-keybind-set" style="margin-top:14px;padding:8px 14px;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:#171717;color:#e5e7eb;font-size:12px;font-weight:600;cursor:pointer">Set open keybind: ${kbLabel}</button>
        <div id="bnc-keybind-hint" style="font-size:11px;color:#6b7280;min-height:14px"></div>
          <div style="margin-top:10px;width:100%;max-width:240px;display:flex;flex-direction:column;gap:4px;align-items:center">
           <div style="font-size:11px;color:#9ca3af">Lobby Music Volume</div>
           <div style="display:flex;gap:8px;align-items:center;width:100%">
             <span style="font-size:11px;color:#6b7280">🔈</span>
             <input type="range" id="bnc-volume-slider" min="0" max="100" value="${Math.round(Number(GMGet('bnc_lobby_volume',0.5))*100)}" style="flex:1;accent-color:#fff">
             <span id="bnc-volume-label" style="font-size:11px;color:#e5e7eb;min-width:32px;text-align:right">${Math.round(Number(GMGet('bnc_lobby_volume',0.5))*100)}%</span>
              <span style="font-size:11px;color:#6b7280">🔊</span>
            </div>
          </div>
        </div>
      `;
    // players tab content
    let playersHtml='';
    if(drawerTab==='players'){
      const pc = playersCache;
      if(!sessionId){
        playersHtml = `<div style="padding:20px 0;color:#9ca3af;font-size:12px">No session - join a match</div>`;
      } else if(!pc){
        playersHtml = `<div style="padding:20px 0;color:#9ca3af;font-size:12px">Loading players…</div>`;
      } else if(!pc.players || pc.players.length===0){
        playersHtml = `<div style="padding:20px 0;color:#9ca3af;font-size:12px">No players in game</div>`;
      } else {
        const count = pc.count || pc.players.length;
        const lateCount = pc.players.filter(p=>p && p.late_join).length;
        const header = `<div style="font-size:13px;font-weight:700;color:#fff;margin-bottom:8px;text-align:center">Players in Game - ${count}${pc.game_mode? ' · '+fmtGmDrawer(pc.game_mode):''}${lateCount? ` · <span style="color:#f59e0b">${lateCount} late join${lateCount>1?'s':''}</span>`:''}</div>`;
        const rows = pc.players.map(pl=>{
          const skinId = (pl.skin!=null && String(pl.skin).trim()!=='' && String(pl.skin).toLowerCase()!=='nan' && String(pl.skin).toLowerCase()!=='null') ? pl.skin : 'cct_0_default';
          const skinP = skinUrlDrawer(skinId);
          const inDb = !!pl.in_db;
          const img = `<div style="position:relative;width:36px;height:36px;flex-shrink:0"><img src="${skinP}" style="width:36px;height:36px;object-fit:contain;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:8px;flex-shrink:0" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div style="display:none;width:36px;height:36px;align-items:center;justify-content:center;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:8px;font-size:14px;flex-shrink:0">▣</div><span style="position:absolute;bottom:-2px;right:-2px;width:10px;height:10px;border-radius:50%;background:${inDb?'#10b981':'#4b5563'};border:2px solid #0f0f0f;display:inline-block" title="${inDb?'In DB':'Not in DB'}"></span></div>`;
          const esc=(v)=> String(v).replace(/</g,'&lt;');
          const toNaN=(v)=> (v==null || String(v).trim()==='' || String(v).toLowerCase()==='null' || String(v).toLowerCase()==='undefined') ? 'NaN' : esc(v);
          const titleName = pl.username!=null ? esc(pl.username) : 'NaN';
          const lateBadge = pl.late_join ? ` <span style="font-size:9px;font-weight:800;color:#000;background:#f59e0b;border-radius:4px;padding:1px 5px;vertical-align:middle;white-space:nowrap">LATE JOIN</span>` : '';
          const _ptColor = teamColorDrawer(pl.pt);
          const _ptNum = parseInt(String(pl.pt==null?'':pl.pt).trim(), 10);
          const teamDia = _ptColor ? `<span title="Team ${Number.isFinite(_ptNum)?_ptNum:esc(pl.pt)}" style="display:inline-block;width:9px;height:9px;background:${_ptColor};transform:rotate(45deg);margin-right:7px;flex-shrink:0;vertical-align:baseline"></span>` : '';
          const nameLine = `<div style="font-size:13px;font-weight:700;color:#fff;word-break:break-word">${teamDia}${titleName}${lateBadge}</div>`;
          const gidFull = pl.game_id!=null ? String(pl.game_id) : 'NaN';
          const discordVal = pl.discord_username!=null ? String(pl.discord_username) : 'NaN';
          const detailLine = `Discord: ${toNaN(discordVal)} · Game ID: ${toNaN(gidFull)}`;
          const detailDiv = `<div style="font-size:11px;color:#9ca3af;word-break:break-word;white-space:normal;line-height:1.35">${detailLine}</div>`;
          // Add-friend: in-DB players only, never self. Wired after render.
          const _meDid = (typeof myDiscordId==='function' ? myDiscordId() : null);
          const _canAdd = !!(inDb && pl.discord_id && !pl.is_self && _meDid && String(pl.discord_id)!==String(_meDid));
          const addBtn = _canAdd ? `<button data-addfr="${esc(pl.discord_id)}" title="Send friend request" style="background:#fff;color:#000;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11px;font-weight:700;flex-shrink:0;white-space:nowrap">Add</button>` : '';
          return `<div style="display:flex;gap:10px;align-items:center;width:100%;max-width:300px;padding:8px 10px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px"><div style="flex-shrink:0">${img}</div><div style="flex:1;min-width:0;text-align:left;display:flex;flex-direction:column;gap:2px">${nameLine}${detailDiv}</div>${addBtn}</div>`;
        }).join('');
        playersHtml = header + `<div style="display:flex;flex-direction:column;gap:6px;align-items:center;width:100%;max-height:46vh;overflow-y:auto;padding-right:4px">${rows}</div>`;
      }
    }
    body.innerHTML = tabBar + (drawerTab==='stats' ? statsHtml : `<div style="display:flex;flex-direction:column;align-items:center;width:100%;padding:8px 0">${playersHtml}</div>`);
    // players-tab add-friend buttons (in-DB only; server dedups/409s repeats).
    // States upgrade after render: already friends -> Remove, pending -> Sent.
    try{
      body.querySelectorAll('button[data-addfr]').forEach(b=>{
        b.onclick=(e)=>{
          try{ if(e) e.stopPropagation(); }catch{}
          const me=(typeof myDiscordId==='function' ? myDiscordId() : null);
          sendFriendRequest(me, String(b.dataset.addfr||''), b, 'Add');
        };
      });
      try{ upgradeFriendButtons(body); }catch{}
    }catch{}
    // tab handlers
    const tsBtn=document.getElementById('bnc-tab-stats');
    const tpBtn=document.getElementById('bnc-tab-players');
    if(tsBtn) tsBtn.onclick=()=> switchDrawerTab('stats');
    if(tpBtn) tpBtn.onclick=()=> switchDrawerTab('players');
    // footer api dot (central truth: stats + capture health)
    try{ updateApiDot(); }catch{}
    // footer ranked match id (click to copy for abuse reports)
    const rkFoot=document.getElementById('bnc-drawer-ranked');
    if(rkFoot){
      if(_ranked && _ranked.match_id){
        rkFoot.style.display='inline';
        rkFoot.textContent = _ranked.match_id + ' · report abuse';
        rkFoot.onclick=(e)=>{
          try{ if(e) e.stopPropagation(); }catch{}
          const mid=String(_ranked.match_id);
          const done=()=> showToast('Match ID copied — paste it in your abuse report', false);
          try{
            if(typeof GM_setClipboard==='function'){ GM_setClipboard(mid); done(); }
            else if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(mid).then(done, done); }
            else done();
          }catch{ try{ done(); }catch{} }
        };
      } else {
        rkFoot.style.display='none';
        rkFoot.onclick=null;
      }
    }
    // ranked overlay follows every stats render (drawer open or not)
    try{ updateRankedOverlay(); }catch{}
    // tourney HUD follows every stats render too (passthrough, drawer-independent)
    try{ updateTourneyHud(); }catch{}
    // ranked-tourney HUD follows every stats render (never when comp/room claim it)
    try{ updateRtHud(); }catch{}
    // ranked lobby card follows every stats render
    try{ updateRankedLobby(); }catch{}
    // ranked match UI (lobby/match switch, fee toast, host panel)
    try{ updateRankedMatchUi(); }catch{}
    // ranked points toast follows every stats render
    try{ updatePtsToast(); }catch{}
    // ladder-movement mini toast follows every stats render (match AND
    // breather — seals land while the lobby card is hidden)
    try{ updateLadderMoveToast(); }catch{}
    // rank up/down toast follows every stats render
    try{ updateRankToast(); }catch{}
    // ranked start overlay (first match only)
    try{ updateRankedStart(); }catch{}
    // dev gating follows every stats render (stats arrive async)
    try{ syncDevUI(); }catch{}
    // keybind button handler (only in stats tab)
    const kbBtn=document.getElementById('bnc-keybind-set');
    const kbHint=document.getElementById('bnc-keybind-hint');
    if(kbBtn){
      kbBtn.onclick=()=>{
        if(_keybindCapturing) return;
        _keybindCapturing=true;
        try{ _keybindCaptureAt=Date.now(); }catch{}
        kbBtn.textContent='Press new keybind…';
        if(kbHint) kbHint.textContent='Press any combo (e.g. Ctrl+Shift+K, or Cmd+Shift+K on Mac). Esc to cancel.';
        const handler=(e)=>{
          if(e.key==='Escape'){ _keybindCapturing=false; kbBtn.textContent='Set open keybind: '+formatKeybind(getDrawerKeybind()); if(kbHint) kbHint.textContent='Cancelled.'; document.removeEventListener('keydown', handler, true); return; }
          if(['Control','Shift','Alt','Meta'].includes(e.key)) return;
          e.preventDefault(); e.stopPropagation();
          const nb={ctrl:!!e.ctrlKey, meta:!!e.metaKey, shift:!!e.shiftKey, alt:!!e.altKey, key:String(e.key).toUpperCase(), code:String(e.code||'').toUpperCase()||null};
          if(!nb.key) return;
          const dwarn=bindWarning(nb, 'drawer');
          if(dwarn){ if(kbHint) kbHint.textContent='⚠ '+dwarn; return; }
          try{ GMSet('bnc_drawer_keybind', JSON.stringify(nb)); }catch{ GMSet('bnc_drawer_keybind', nb); }
          _keybindCapturing=false;
          kbBtn.textContent='Set open keybind: '+formatKeybind(nb);
          if(kbHint) kbHint.textContent='Saved.';
          showToast('Keybind set to '+formatKeybind(nb), false);
          document.removeEventListener('keydown', handler, true);
        };
        document.addEventListener('keydown', handler, true);
        setTimeout(()=>{ if(_keybindCapturing){ _keybindCapturing=false; kbBtn.textContent='Set open keybind: '+formatKeybind(getDrawerKeybind()); if(kbHint) kbHint.textContent='Timed out.'; document.removeEventListener('keydown', handler, true); } }, 8000);
      };
    }
    const volSlider=document.getElementById('bnc-volume-slider');    const volLabel=document.getElementById('bnc-volume-label');
    if(volSlider){
      const syncVol=()=>{
        const v=Math.max(0,Math.min(100, Number(volSlider.value)));
        GMSet('bnc_lobby_volume', v/100);
        logInfo(`[AUDIO] volume slider ${v}%`);
        try{ feedLog('info', {audio:'volume', vol:v/100}); }catch{}
        if(volLabel) volLabel.textContent=v+'%';
        const aud=ensureLobbyAudio();
        if(aud){ aud.volume=v/100; logInfo(`[AUDIO] volume set to ${aud.volume.toFixed(2)}`); }
      };
      volSlider.oninput=syncVol;
      volSlider.onchange=syncVol;
    }
    try{ updateLobbyMusic(); }catch(e){ logErr(`[AUDIO] update after render err ${e.message}`); }
    try{ prefetchLobbyMusic(); }catch{}
  }
  function fetchStats(forceRender){
    if(!sessionId && !discordId) { if(forceRender) renderInfoPanel(); return; }
    // Sequence guard: overlapping polls (15s timer, bg poll, transition
    // bursts) must never paint stale data over fresh — a slow older response
    // arriving last used to flip kills +2 back to +1. Only the newest
    // response for each transport applies. Never throws.
    const mySeq=++_statsSeq;
    const fresh=(s)=> s===_statsSeq; // GM and fetch race each other too
    const base=resolveApiBase();
    const qs = sessionId ? `session_id=${encodeURIComponent(sessionId)}` : `discord_id=${encodeURIComponent(discordId)}`;
    const url = base + '/api/stats?' + qs;
    const GM = typeof GM_xmlhttpRequest!=='undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
    // Round-trip clock for the Nerds latency rows: stamped per poll, read when
    // that poll settles (success or not).
    const _t0=(typeof performance!=='undefined'&&performance.now)?performance.now():Date.now();
    const doFetch = (useGM)=>{
      if(useGM && GM){
        try{
          GM({method:'GET', url, headers:{'Content-Type':'application/json'}, timeout:8000,
            onload: r=>{ if(!fresh(mySeq)) return; if(r.status>=200&&r.status<300){ try{ const j=JSON.parse(r.responseText); statsCache=j; logInfo(`[AUDIO] stats fetched lobby_music=${String(j.lobby_music_url||'').slice(-30)} state=${j.session? j.session.state:'none'}`); try{ feedLog('info', {audio:'stats', hasMusic:!!j.lobby_music_url, state:j.session? j.session.state:null}); }catch{} apiOnline=true; _apiPing(_t0,true); if(j.skin) GMSet('bnc_skin', j.skin); if(j.game_username) GMSet('bnc_game_username', j.game_username); if(j.game_id) GMSet('bnc_game_id', j.game_id); if(j.discord_username) { discordUsername=j.discord_username; GMSet('bnc_discord_username', discordUsername); } try{ _reconcileServerState(j.session); }catch{} renderInfoPanel(); try{ _hbFails=0; startHeartbeat(); }catch{} try{ const _pi=getPartyInfo(j.session); const _inMatch=effInMatch()===true; if((_pi.isParty&&_pi.code)||_inMatch) fetchPlayers(false); }catch{} }catch(e){ logErr(`[AUDIO] stats parse err ${e.message}`); apiOnline=false; if(forceRender) renderInfoPanel(); } } else { apiOnline=false; if(forceRender) renderInfoPanel(); } try{ updateApiDot(); }catch{} },
            onerror: ()=>{ if(!fresh(mySeq)) return; apiOnline=false; _apiPing(_t0,false); if(forceRender) renderInfoPanel(); try{ updateApiDot(); }catch{} },
            ontimeout: ()=>{ if(!fresh(mySeq)) return; apiOnline=false; _apiPing(_t0,false); if(forceRender) renderInfoPanel(); try{ updateApiDot(); }catch{} }});
          return;
        }catch{}
      }
      fetch(url, {method:'GET'}).then(r=>r.text().then(t=>{ if(!fresh(mySeq)) return; if(r.ok){ try{ const j=JSON.parse(t); statsCache=j; logInfo(`[AUDIO] stats fetched (fetch) lobby_music=${String(j.lobby_music_url||'').slice(-30)} state=${j.session? j.session.state:'none'}`); try{ feedLog('info', {audio:'stats-fetch', hasMusic:!!j.lobby_music_url, state:j.session? j.session.state:null}); }catch{} try{ _reconcileServerState(j.session); }catch{} apiOnline=true; _apiPing(_t0,true); try{ _hbFails=0; startHeartbeat(); }catch{} if(j.skin) GMSet('bnc_skin', j.skin); if(j.game_username) GMSet('bnc_game_username', j.game_username); if(j.game_id) GMSet('bnc_game_id', j.game_id); if(j.discord_username) { discordUsername=j.discord_username; GMSet('bnc_discord_username', discordUsername); } renderInfoPanel(); try{ const _pi2=getPartyInfo(j.session); const _inMatch2=effInMatch()===true; if((_pi2.isParty&&_pi2.code)||_inMatch2) fetchPlayers(false); }catch{} }catch(e){ logErr(`[AUDIO] stats parse err (fetch) ${e.message}`); apiOnline=false; if(forceRender) renderInfoPanel(); } } else { apiOnline=false; if(forceRender) renderInfoPanel(); } try{ updateApiDot(); }catch{} }).catch(()=>{ if(!fresh(mySeq)) return; apiOnline=false; if(forceRender) renderInfoPanel(); try{ updateApiDot(); }catch{} })).catch(()=>{ if(!fresh(mySeq)) return; _apiPing(_t0,false); apiOnline=false; if(forceRender) renderInfoPanel(); try{ updateApiDot(); }catch{} });
    };
    doFetch(true);
    try{ fetchCoins(); }catch{}
  }
  // --- NECTAR COINS drawer line: balance from /api/coins/balance, throttled
  // to 60s (balance moves on wagers/daily, not per kill). Unknown renders '-'
  // like every other row. Never throws.
  // GM_xmlhttpRequest first: the game page is a foreign origin, so plain
  // fetch() is CORS-blocked and the balance silently stayed unknown ("-").
  function fetchCoins(){
    try{
      const now=Date.now();
      if(now-(_lastCoinsAt||0)<60000) return;
      let did=null; try{ did=(typeof myDiscordId==='function'?myDiscordId():null)||discordId||null; }catch{}
      if(!did) return;
      _lastCoinsAt=now;
      let base=''; try{ base=resolveApiBase(); }catch{}
      if(!base) return;
      const url=base+'/api/coins/balance?discord_id='+encodeURIComponent(did);
      const apply=(txt,ok)=>{
        try{
          if(!ok) return;
          const j=JSON.parse(txt);
          if(j && j.balance!=null && !j.error){
            coinsCache={balance:Number(j.balance)||0, earned:Number(j.total_earned)||0, wagered:Number(j.total_wagered)||0, ts:Date.now()};
            try{ renderInfoPanel(); }catch{}
          }
        }catch{}
      };
      const GM = typeof GM_xmlhttpRequest!=='undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
      if(GM){
        try{
          GM({method:'GET', url, headers:{'Content-Type':'application/json'}, timeout:8000,
              onload:r=>apply(r.responseText, r.status>=200 && r.status<300),
              onerror:()=>{}, ontimeout:()=>{}});
          return;
        }catch{}
      }
      fetch(url,{method:'GET'}).then(r=>r.text().then(t=>apply(t, r.ok))).catch(()=>{});
    }catch{}
  }
  // Compact coin formatting: 1.2K / 3.4M / 5.6B / 7.8T. Whole numbers stay
  // exact below 1000 (a balance of 500 never renders as "500.0").
  function fmtCoinsCompact(n){
    try{
      const v=Number(n);
      if(!isFinite(v)) return '-';
      const neg=v<0, a=Math.abs(v);
      const units=[[1e12,'T'],[1e9,'B'],[1e6,'M'],[1e3,'K']];
      for(const [d,s] of units){
        if(a>=d){
          const t=a/d;
          const txt=t>=100?Math.round(t).toString():t.toFixed(1).replace(/\.0$/,'');
          return (neg?'-':'')+txt+s;
        }
      }
      return (neg?'-':'')+Math.round(a).toLocaleString('en-US');
    }catch{ return '-'; }
  }
  function startStatsPoll(){
    stopStatsPoll();
    fetchStats(false);
    // Comp-style cadence: 30s in-match (optimistic state) / 15s in lobby.
    const interval = (_optimisticState.inMatch ? 30000 : 15000);
    statsPollTimer=setInterval(()=> fetchStats(false), interval);
  }
  function stopStatsPoll(){ if(statsPollTimer){ clearInterval(statsPollTimer); statsPollTimer=null; } }
  // auto-create drawer shell immediately so handle is visible + hidden audio tag that auto plays
  setTimeout(()=>{ ensureLobbyAudio(); createInfoPanel(); renderInfoPanel(); if(drawerOpen){ startStatsPoll(); if(drawerTab==='players') startPlayersPoll(); } if(sessionId){ startBgLobbyPoll(); try{ startHeartbeat(); }catch{} } try{ logInfo(`[RANKED] companion v${BNC_VERSION} boot`); }catch{} }, 700);
  // refresh when session established after login
  const _origStoreSession = storeSession;
  // wrap storeSession to trigger render
  // monkey-patch via interval check on sessionId change
  let _lastSid = sessionId;
  setInterval(()=>{ if(sessionId!==_lastSid){ _lastSid=sessionId; renderInfoPanel(); if(drawerOpen){ fetchStats(false); if(drawerTab==='players') fetchPlayers(false); } else if(sessionId){ startBgLobbyPoll(); try{ startHeartbeat(); }catch{} fetchStats(false); } } }, 1000);

  // --- RANKED OVERLAY (Phase 5): bottom-left bare counter + tier badge ---
  // Game frame only, pointer-transparent, driven by the stats poll. Like the
  // comp HUD, the point number stays a bare primary counter; Ranked adds its
  // tier as a small second-line badge rather than replacing that counter.
  let rankedOverlay=null, _lastRankedPts=null, _rankedFlashT=null, rankedActive=false, _lastOvSig='?', _rankedPendingSeal=null;
  function ensureRankedOverlay(){
    if(!IS_GAME_FRAME) return null;
    if(rankedOverlay) return rankedOverlay;
    if(document.getElementById('bnc-ranked-overlay')){ rankedOverlay=document.getElementById('bnc-ranked-overlay'); return rankedOverlay; }
    const el=document.createElement('div');
    el.id='bnc-ranked-overlay';
    Object.assign(el.style,{position:'fixed', left:'30%', bottom:'20px', zIndex:'2147483640', display:'none', flexDirection:'column', alignItems:'flex-start', gap:'2px', background:'rgba(0,0,0,0.75)', border:'1px solid rgba(255,255,255,0.12)', borderRadius:'10px', padding:'8px 14px', fontFamily:'Inter, system-ui, sans-serif', fontSize:'14px', fontWeight:'700', color:'#fff', pointerEvents:'none', transition:'box-shadow 200ms, border-color 200ms', whiteSpace:'nowrap'});
    (document.body||document.documentElement).appendChild(el);
    rankedOverlay=el;
    return el;
  }
  // Field-test debug chip (game frame only): curScn, match/lobby guess, live
  // match_version, pending-seal state, points. Opt-in via the DBG button in
  // the ranked panel (persisted in localStorage); zero cost when off.
  function rankedDebugEnabled(){ try{ return localStorage.getItem('bnc_ranked_dbg')==='1'; }catch{ return false; } }
  function ensureRankedDebugChip(){
    if(!IS_GAME_FRAME) return null;
    let el=document.getElementById('bnc-ranked-dbg');
    if(el) return el;
    el=document.createElement('div');
    el.id='bnc-ranked-dbg';
    Object.assign(el.style,{position:'fixed', top:'10px', left:'10px', zIndex:'2147483645', display:'none', flexDirection:'column', gap:'1px', background:'rgba(0,0,0,0.72)', border:'1px solid rgba(147,197,253,0.35)', borderRadius:'8px', padding:'5px 9px', fontFamily:'ui-monospace, Menlo, Consolas, monospace', fontSize:'10px', lineHeight:'1.5', fontWeight:'600', color:'#bfdbfe', pointerEvents:'none', whiteSpace:'pre'});
    (document.body||document.documentElement).appendChild(el);
    return el;
  }
  function updateRankedDebugChip(){
    try{
      if(!rankedDebugEnabled()){ const el0=document.getElementById('bnc-ranked-dbg'); if(el0) el0.style.display='none'; return; }
      const el=ensureRankedDebugChip(); if(!el) return;
      const rk=liveRanked();
      if(!rk){ el.textContent='rk off (no ranked session)'; el.style.display='flex'; return; }
      const im=effInMatch();
      const scn=(typeof curScn!=='undefined' && curScn)?String(curScn):'-';
      const v=Number(rk.match_version)||0;
      const pend=!!_rankedPendingSeal;
      const line1=`scn=${scn} ${im===true?'[MATCH]':(im===false?'[LOBBY]':'[..?]')} rk=on`;
      const line2=`v=${v} seal=${pend?'PENDING':'ok'} pts=${rk.points==null?'-':rk.points}`;
      // Stuck-seal visibility: ~age of the newest banked event (a live
      // match that stops aging while seal=PENDING is a stuck seal) and the
      // open match's unmapped-name count (evidence waiting for a link).
      let age='?';
      const ages=rk.last_event_age_s;
      if(ages!=null && Number(ages)>=0){
        const s=Number(ages);
        age='~'+(s<90? s+'s' : s<3600? Math.round(s/60)+'m' : Math.round(s/3600)+'h');
      }
      const unm=(rk.unmapped_names==null)?'?':Number(rk.unmapped_names);
      const line3=`age=${age} unmapped=${unm}${unm>0?' \u26a0':''}`;
      el.textContent=line1+'\n'+line2+'\n'+line3;
      el.style.display='flex';
    }catch{}
  }
  function updateRankedOverlay(){
    const sc=statsCache;
    const rk=liveRanked();
    const sess=sc && sc.session ? sc.session : null;
    // Optimistic-first: flip in ms off local/capture signals; the server
    // session only confirms. srvMatch tracks the server alone for the
    // "sync" chip (shown while truth is still in flight).
    const _eim=effInMatch();
    const inMatch=_eim===true;
    rankedActive=!!(rk && inMatch);
    // Pending seal: between the server's match_version bump and the
    // ranked_results row landing, the shown total is provisional - in
    // match (overlay) AND out (a dead player back in lobby sees a total
    // that still excludes the running match). Server publishes
    // pending_seal; local derivation is the fallback for older servers.
    // Keyed rid:version so a room change can never leak stale pending.
    try{
      if(rk && rk.rid){
        const v=Number(rk.match_version)||0;
        const lr=rk.last_result;
        const key=String(rk.rid)+':'+v;
        const pend=(rk.pending_seal===true||rk.pending_seal===false)
          ? rk.pending_seal
          : (v>0 && !(lr && Number(lr.match_version)===v));
        if(v>0 && pend){ _rankedPendingSeal=key; }
        else{ _rankedPendingSeal=null; }
      }else{ _rankedPendingSeal=null; }
    }catch{}
    const el=ensureRankedOverlay();
    if(!el) return;
    const shown=(rk?rk.points:'-');
    const sig=(rk?rk.rid:'-')+'|'+(inMatch?'M':'L')+'|'+shown+'|'+(_rankedPendingSeal?'P':'C');
    if(sig!==_lastOvSig){
      _lastOvSig=sig;
      try{ logInfo(`[RANKED] match-overlay -> ${(rk&&inMatch)?'SHOW':'HIDE'} rid=${rk?rk.rid:'-'} inMatch=${inMatch} pts=${shown}`); }catch{}
    }
    if(!rk || !inMatch){
      el.style.display='none';
      return;
    }
    const pts0=(rk.points==null ? 0 : rk.points);
    const color=rk.milestone_color || '#fff';
    // Optimistic layer: server_total + priced kills until the seal moves
    // the total (then the layer resets — never stacked onto the seal).
    let _optShow=0;
    try{
      if(typeof OPTIMISTIC_KILLS!=='undefined' && OPTIMISTIC_KILLS){
        _optShow=_optLive();
        if(!_optShow){
          const _orid=String(rk.rid||''), _osp=Number(rk.points)||0, _over=Number(rk.match_version)||0;
          if(_osp!==_optBase || _orid!==_optRid || _over!==_optVer){ _optReset({rid:_orid, pts:_osp, ver:_over}); }
        }
      }
    }catch{}
    const pts=pts0+_optShow;
    // v1: plain ladder total, no pending marker (the seal-watch refreshes
    // the number within ~2s of the seal — no honesty footnote needed).
    // Rank icon rides before the number and re-renders from the same block
    // every poll, so it flips the instant the milestone does.
    const iconHtml=(rk.milestone_icon_url
      ? `<img src="${String(rk.milestone_icon_url).replace(/"/g,'')}" alt="" style="width:30px;height:30px;object-fit:contain" onerror="this.style.display='none'" />`
      : '');
    el.innerHTML=`<div style="display:flex;align-items:center;gap:8px;font-size:23px;font-weight:800;color:#fff">${iconHtml}<span>${Number(pts).toLocaleString('en-US')}</span></div>`;
    el.style.display='flex';
    if(_lastRankedPts!=null && pts!==_lastRankedPts){
      el.style.borderColor=color;
      el.style.boxShadow=`0 0 18px ${color}`;
      try{ clearTimeout(_rankedFlashT); }catch{}
      _rankedFlashT=setTimeout(()=>{ try{ el.style.borderColor='rgba(255,255,255,0.12)'; el.style.boxShadow='none'; }catch{} }, 700);
    }
    _lastRankedPts=pts;
  }

  // Mini ladder toast: every ladder movement gets one small toast that
  // matches the counter exactly (seals, streaks, corrections alike) —
  // gains green, losses red, exactly-once per total. Runs every render
  // ON PURPOSE outside the lobby card: seals land during the breather
  // while the hold keeps the card hidden, and a toast gated on visibility
  // would never fire (the exact v1 bug this replaces).
  function updateLadderMoveToast(){
    let rk=null, sc=null;
    try{ rk=(typeof liveRanked==='function') ? liveRanked() : null; }catch{ return; }
    try{ sc=statsCache; }catch{}
    if(!rk) return;
    if(typeof MINI_TOASTS!=='undefined' && !MINI_TOASTS) return;
    if(!IS_GAME_FRAME) return;
    let ptsRaw=null;
    try{
      const _ar=sc && sc.arena ? sc.arena : null;
      ptsRaw=(_ar && _ar.points!=null) ? Number(_ar.points) : Number(rk.points||0);
    }catch{ return; }
    if(!Number.isFinite(ptsRaw)) return;
    try{
      const _rid=String(rk.rid||'');
      const _had=Object.prototype.hasOwnProperty.call(_lastLobbyServerPts, _rid);
      const _prev=_had ? _lastLobbyServerPts[_rid] : null;
      _lastLobbyServerPts[_rid]=ptsRaw;
      if(_had && _prev!=null && ptsRaw!==_prev){
        const _d=ptsRaw-_prev;
        queuePtsToast(_d>=0?'POINTS EARNED':'POINTS LOST', _d,
          {key:`seal:${_rid}:${ptsRaw}`, noCoalesce:true, dwell:2200});
      }
    }catch{}
  }
  // When curScn leaves MainLobby, hide lobby card and show match UI.
  // First match of the room also raises the start overlay (once per rid).
  // --- RANK UP / DOWN TOASTS: like victory/defeat, for milestone moves ---
  // Fires when the ranked block's milestone changes within one rid: green
  // RANK UP naming the new tier, red RANK DOWN naming it. Exactly-once per
  // rid+milestone. The counter icons below re-render from the same block,
  // so icon and toast can never disagree.
  let _lastRank={};
  function _rankIdx(name){
    try{
      const n=String(name||'');
      for(let i=0;i<RANKED_TIERS.length;i++){ if(RANKED_TIERS[i].name===n) return i; }
    }catch{}
    return -1;
  }
  function updateRankToast(){
    let rk=null;
    try{ rk=(typeof liveRanked==='function') ? liveRanked() : null; }catch{ return; }
    if(!rk) return;
    if(!IS_GAME_FRAME) return;
    const rid=String(rk.rid||'');
    const ms=String(rk.milestone||'Unranked');
    if(!rid) return;
    const had=Object.prototype.hasOwnProperty.call(_lastRank, rid);
    const prev=_had ? _lastRank[rid] : null;
    _lastRank[rid]=ms;
    if(!had || prev===ms) return;
    const up=_rankIdx(ms)>_rankIdx(prev);
    try{
      queuePtsToast(up?'RANK UP':'RANK DOWN', 0,
        {key:`rank:${rid}:${ms}`, noCoalesce:true, dwell:3000, tone:up?'up':'down', sub:ms});
      logInfo(`[RANKED] rank ${up?'up':'down'} ${prev} -> ${ms}`);
    }catch{}
  }
  // When curScn leaves MainLobby, hide lobby card and show match UI.
  // First match of the room also raises the start overlay (once per rid).
  // --- RANKED MATCH UI: lobby->match transition on curScn ---
  // When curScn leaves MainLobby, hide lobby card and show match UI.
  // First match of the room also raises the start overlay (once per rid).
  let _lastFeeMver=0, _matchHostPanelT=null;
  function updateRankedMatchUi(){
    if(!IS_GAME_FRAME) return;
    const sc=statsCache;
    const rk=liveRanked();
    const sess=sc && sc.session ? sc.session : null;
    const _eim=effInMatch();
    const inMatch=_eim===true;
    // Lobby card: show only when NOT in match.
    try{
      const lobbyEl=document.getElementById('bnc-ranked-lobby');
      if(lobbyEl) lobbyEl.style.display=(rk && !inMatch && sess) ? 'flex' : 'none';
    }catch{}
    // Match overlay (points): show only when in match
    try{ updateRankedOverlay(); }catch{}
    try{ updateRankedDebugChip(); }catch{}
    // Start overlay: first sealed version edge while in match.
    const _srvMatch=!!(sess && String(sess.state||'').toLowerCase()==='match');
    if(rk && inMatch && _srvMatch){
      const mver=Number(rk.match_version)||0;
      if(mver>0 && mver!==_lastFeeMver){
        _lastFeeMver=mver;
        try{ updateRankedStart(); }catch{}
      }
    }
    if(!rk || !inMatch){ _lastFeeMver=0; _curScnFlipAt=0; }
  }
  // ranked tick: drawer poll only runs when open — keep ranked fresh when closed.
  // 5s while a code is tracked (seals/toasts/overlay surface in seconds).
  // Comp parity: no presence ping, no extra endpoint — stats poll is truth.
  setInterval(()=>{ try{
    try{
      let _rkTracked=false;
      try{ _rkTracked=!!liveRanked() || (!!_rankedRoom && Date.now()-(_rankedRoom.at||0)<600000); }catch{}
      if(!drawerOpen && (_rkTracked || rankedActive)) fetchStats(false);
    }catch{ try{ if(!drawerOpen && rankedActive) fetchStats(false); }catch{} }
    // capture watchdog: logged in but nothing reaching the server. Stats can
    // stay green for hours while captures are dead (the exact shape that hid
    // the JKVUD session) — say so loudly instead of failing silent.
    try{
      if(sessionId && _capFails>=3 && Date.now()-_lastCapWarn>60000){
        _lastCapWarn=Date.now();
        try{ logErr(`[CAPTURE] ${_capFails} consecutive failures - captures are NOT reaching the server (ranked UI + seals will stall)`); }catch{}
      } else if(sessionId && _capSeen>0 && captureAgeMs()>120000 && !wsFresh() && Date.now()-_lastCapWarn>60000){
        _lastCapWarn=Date.now();
        try{ logErr(`[CAPTURE] stale ${Math.round(captureAgeMs()/1000)}s with ${_capSeen} packets seen - hook or session stalled (ranked UI + seals will stall)`); }catch{}
      }
    }catch{}
  }catch{} }, 5000);

  // --- RANKED LOBBY CARD: lobby-only tall card, center-left ---
  // Order: season line / tier icon + big points / progress bar to next /
  // room line (code, players, sealed) / host line. Ranked-session ONLY and
  // lobby-state only — but it SURVIVES match end: after the last seal the
  // server stops serving the block, so an ended endpoint verdict renders the
  // cached snapshot (marked Final) until the player leaves the room.
  // Pointer-transparent, game frame only.
  let rankedLobbyCard=null, _lastLobbySig='?';
  function ensureRankedLobby(){
    if(!IS_GAME_FRAME) return null;
    if(rankedLobbyCard) return rankedLobbyCard;
    if(document.getElementById('bnc-ranked-lobby')){ rankedLobbyCard=document.getElementById('bnc-ranked-lobby'); return rankedLobbyCard; }
    const el=document.createElement('div');
    el.id='bnc-ranked-lobby';
    Object.assign(el.style,{position:'fixed', left:'24px', top:'50%', transform:'translateY(-50%)', zIndex:'2147483640', display:'none', flexDirection:'column', alignItems:'center', gap:'8px', width:'250px', background:'rgba(0,0,0,0.78)', border:'1px solid rgba(255,255,255,0.12)', borderRadius:'14px', padding:'18px 14px', fontFamily:'Inter, system-ui, sans-serif', textAlign:'center', pointerEvents:'none'});
    (document.body||document.documentElement).appendChild(el);
    rankedLobbyCard=el;
    return el;
  }
  function updateRankedLobby(){
    const sc=statsCache;
    const rk=liveRanked();
    const sess=sc && sc.session ? sc.session : null;
    const _eim=effInMatch();
    const inMatch=_eim===true;
    const el=ensureRankedLobby();
    const pend=!!(typeof _rankedPendingSeal!=='undefined' && _rankedPendingSeal);
    if(!el) return;
    // Ranked-session ONLY: arena data alone (casual lobby) never shows the
    // card. A leave hides locally and never re-shows (liveRanked owns that).
    const show=!!(rk && !inMatch && sess);
    const sig=(rk?`${rk.rid}:${rk.points}/${rk.milestone}`:'-')+'|'+(sess?String(sess.state||'?'):'none')+'|'+(show?'SHOW':'HIDE')+'|'+(pend?'P':'C');
    let _ladderPts='-';
    try{ const _a=sc&&sc.arena?sc.arena:null; if(_a&&_a.points!=null) _ladderPts=String(Number(_a.points)); }catch{}
    if(sig!==_lastLobbySig){
      _lastLobbySig=sig;
      try{ logInfo(`[RANKED] lobby-card -> ${show?'SHOW':'HIDE'} ranked=${rk?rk.rid+' '+rk.points+'pts/'+rk.milestone:'none'} ladder=${_ladderPts} session=${sess?sess.state:'none'}`); }catch{}
    }
    if(!show){ el.style.display='none'; return; }
    const color=rk.milestone_color || '#fff';
    const icon=rk.milestone_icon_url
      ? `<img src="${String(rk.milestone_icon_url).replace(/"/g,'')}" alt="" style="width:80px;height:80px;object-fit:contain" onerror="this.style.display='none'" />`
      : '';
    // Big number = LADDER points (your current points), not tourney points:
    // in a ranked session players care about their own total, and a fresh
    // tourney would otherwise read "0". Falls back to the block's points.
    const _ar=sc && sc.arena ? sc.arena : null;
    const ptsRaw=(_ar && _ar.points!=null) ? Number(_ar.points) : Number(rk.points||0);
    // Room line replaced: code/player-count out, next-milestone progress in.
    let roomLine='';
    try{
      if(_ar && _ar.next_milestone){
        roomLine=`${Number(_ar.points_to_next||0).toLocaleString('en-US')} pts to ${String(_ar.next_milestone).replace(/</g,'&lt;')}`;
      } else if(_ar){
        roomLine='Max rank reached';
      }
    }catch{ roomLine=''; }
    // v1: plain ladder total, no pending marker.
    const pts=ptsRaw.toLocaleString('en-US');
    const ms=String(rk.milestone||'Unranked').replace(/</g,'&lt;');
    const season=(sc && sc.arena && sc.arena.season!=null) ? `Nectar Ranked Season ${sc.arena.season}` : 'Nectar Ranked';
    // Room exists but was never started — seals can't fire. Same card plus
    // an unmissable amber prompt for the host.
    const notStarted=String(rk.status||'').toLowerCase()==='lobby';
    const sleepingNote=rankedEnabled()?'':'<div style="font-size:11px;font-weight:700;color:#f87171;margin-top:2px">RANKED SLEEPING - matches will not seal</div>';
    const startLine=notStarted ? `<div style="font-size:12px;font-weight:800;color:#f59e0b;margin-top:4px">NOT STARTED — host: /ranked start</div>` : '';
    el.innerHTML=`<div style="font-size:13px;font-weight:800;color:#fff">${season}</div>${icon}<div style="font-size:22px;font-weight:800;color:${color}">${pts} (${ms})</div><div style="font-size:11px;color:#9ca3af">${roomLine}</div>${startLine}${sleepingNote}`;
    el.style.display='flex';
  }

  // --- RANKED POINTS TOASTS: component queue on seals ---
  // Fires on a match-version bump with a matching last_result. When the
  // server ships the seal breakdown, each nonzero component queues its own
  // toast (ELIMINATIONS / VICTORY / PLACEMENT / DEFEAT) — losses included,
  // placement never swallowed into a single total. A capped match appends
  // a MATCH TOTAL summary with the enforced total. Without a breakdown
  // (older servers) falls back to the single total toast with the same
  // priority as Comp: wins up -> VICTORY, kills up -> ELIMINATION,
  // otherwise PLACEMENT. A result write can trail its version bump, so keep
  // the edge pending until that matching row arrives.
  // --- RANKED POINTS TOAST QUEUE: lossless FIFO, exactly-once ---
  // Seal toasts carry deterministic per-component keys (rid:v:pts:comp)
  // fired at most once per session and never coalesced: two seals with
  // identical numbers stay two seals. Legacy single toasts keep the old
  // identical title+delta 60s coalescing behind opts.noCoalesce=false.
  let _prevRankSnap=null, _pendingRankToast=null;
  // v1 ship switch: seal toasts (rooms + sessions) are OFF — counters are
  // the source of truth tonight. Flip to true to restore component toasts.
  // Baselines below keep tracking so re-enabling never back-fires old seals.
  const RANKED_SEAL_TOASTS=false;
  // Mini toasts stay ON: one small toast per priced kill (matches the
  // optimistic bump 1:1) and one per ladder movement (matches the counter
  // exactly, seals + streaks + corrections alike). These can never
  // disagree with the counter — they ARE its movements.
  const MINI_TOASTS=true;
  let _lastLobbyServerPts={};
  const _ptsToastQueue=[];
  const _ptsToastShownKeys={};
  let _ptsToastBusy=false;
  function queuePtsToast(title, delta, opts){
    try{
      opts=opts||{};
      const t=String(title), d=Number(delta)||0, now=Date.now();
      if(opts.key && _ptsToastShownKeys[opts.key]){
        try{ logInfo(`[RANKED] points-toast skip duplicate key=${opts.key} "${t}"`); }catch{}
        return;
      }
      for(const q of _ptsToastQueue){
        if(!opts.noCoalesce && q.title===t && q.delta===d && now-q.at<60000){
          q.count=(q.count||1)+1; q.at=now;
          try{ logInfo(`[RANKED] points-toast coalesced "${t}" ${d>=0?'+':''}${d} x${q.count}`); }catch{}
          return;
        }
      }
      const item={title:t, delta:d, at:now, count:1, dwell:Number(opts.dwell)||3500, end:!!opts.pinnedLast, key:opts.key||null, tone:opts.tone||null, sub:opts.sub||null};
      if(item.end) _ptsToastQueue.push(item);   // session summary always last
      else{
        let i=_ptsToastQueue.length;
        while(i>0 && _ptsToastQueue[i-1].end) i--;
        _ptsToastQueue.splice(i,0,item);
      }
      while(_ptsToastQueue.length>12){           // overflow: drop oldest non-summary
        // 12 deep: two full component seals (5 each) plus headroom, so a
        // back-to-back seal never eats the previous seal's components
        let di=-1;
        for(let k=0;k<_ptsToastQueue.length;k++){ if(!_ptsToastQueue[k].end){ di=k; break; } }
        if(di<0) break;
        const dropped=_ptsToastQueue.splice(di,1)[0];
        try{ logInfo(`[RANKED] points-toast queue overflow dropped "${dropped.title}"`); }catch{}
      }
      try{ logInfo(`[RANKED] points-toast queued "${t}" ${d>=0?'+':''}${d} depth=${_ptsToastQueue.length}`); }catch{}
      _pumpPtsToasts();
    }catch{}
  }
  function _pumpPtsToasts(){
    if(_ptsToastBusy || !_ptsToastQueue.length) return;
    if(!IS_GAME_FRAME){ _ptsToastQueue.length=0; return; }
    _ptsToastBusy=true;
    const item=_ptsToastQueue.shift();
    const title=item.count>1 ? `${item.title} x${item.count}` : item.title;
    const delta=item.delta;
    if(item.key){ try{ _ptsToastShownKeys[item.key]=Date.now(); }catch{} }
    // tone overrides the gain color (rank toasts carry delta 0 but still
    // need green/red); sub replaces the "+N Points" line (rank name).
    const gain=item.tone ? item.tone==='up' : delta>=0;
    const el=document.createElement('div');
    Object.assign(el.style,{position:'fixed', left:'0', top:'50%', transform:'translate(-120%,-50%)', transition:'transform 300ms cubic-bezier(0.22,1,0.36,1)', zIndex:'2147483641', background:'#0f0f0f', border:'1px solid rgba(255,255,255,0.12)', borderLeft:'3px solid '+(gain?'#10b981':'#ef4444'), borderRadius:'0 12px 12px 0', padding:'12px 18px 12px 14px', fontFamily:'Inter, system-ui, sans-serif', pointerEvents:'none', boxShadow:'8px 0 32px rgba(0,0,0,0.45)'});
    const subLine=item.sub!=null
      ? `<div style="font-size:22px;font-weight:800;color:${gain?'#10b981':'#ef4444'}">${String(item.sub).replace(/</g,'&lt;')}</div>`
      : `<div style="font-size:22px;font-weight:800;color:${gain?'#10b981':'#ef4444'}">${gain?'+':''}${Number(delta).toLocaleString('en-US')} Points</div>`;
    el.innerHTML=`<div style="font-size:15px;font-weight:800;color:#fff">${title.replace(/</g,'&lt;')}</div>${subLine}`;
    try{ (document.body||document.documentElement).appendChild(el); }catch{ _ptsToastBusy=false; return; }
    requestAnimationFrame(()=>{ requestAnimationFrame(()=>{ try{ el.style.transform='translate(0,-50%)'; }catch{} }); });
    setTimeout(()=>{
      try{ el.style.transform='translate(-120%,-50%)'; }catch{}
      setTimeout(()=>{ try{ el.remove(); }catch{} _ptsToastBusy=false; _pumpPtsToasts(); }, 350);
    }, Math.max(1500, item.dwell||3500));
  }
  function _rankedToastNumber(v){
    const n=Number(v);
    return Number.isFinite(n) ? n : null;
  }
  function updatePtsToast(){
    const rk=liveRanked();
    // Ranked totals are optional during the compatibility rollout. Prefer
    // them when present (true increase comparison), otherwise last_result is
    // the event-level fallback: won => a win happened; kills > 0 => kills
    // happened. This keeps old stats responses fully compatible.
    const snap=rk
      ? {active:true, rid:String(rk.rid||''), v:Number(rk.match_version)||0,
        pts:Number(rk.points)||0, wins:_rankedToastNumber(rk.wins),
        kills:_rankedToastNumber(rk.kills)}
      : {active:false, rid:'', v:0, pts:0, wins:null, kills:null};
    const prev=_prevRankSnap;
    _prevRankSnap=snap;
    if(!prev) return;  // first sighting: baseline only
    // Tourney sessions (RT-*) are owned by the RT toasts below: the same
    // seal would otherwise queue twice (seal components here + the
    // standings-delta toast there). Keep tracking the baseline so a later
    // room session re-baselines silently.
    if(snap.active && /^RT-/i.test(snap.rid||'')){ _pendingRankToast=null; return; }
    if(!RANKED_SEAL_TOASTS){ _pendingRankToast=null; return; }  // v1: counters only
    if(!IS_GAME_FRAME){ _pendingRankToast=null; return; }  // track baseline everywhere, toast in game only
    if(!snap.active || snap.rid!==prev.rid){
      _pendingRankToast=null;
      return;  // left room / new room: re-baseline silently
    }
    if(snap.v>prev.v){
      _pendingRankToast={rid:snap.rid, v:snap.v, prev};
    }
    const pending=_pendingRankToast;
    if(!pending || pending.rid!==snap.rid || pending.v!==snap.v) return;
    const lr=rk && rk.last_result;
    // The poll can see the room version before the results row. Retain the
    // old snapshot and retry on the next stats/capture render instead of
    // silently losing the version edge.
    if(!(lr && Number(lr.match_version)===snap.v)) return;
    const gained=Number(lr.gained);
    _pendingRankToast=null;
    // Include the post-seal total so a later score correction is a distinct,
    // deterministic event while duplicate polls remain deduped by the queue.
    const sealKey=`${snap.rid}:${snap.v}:${snap.pts}`;
    // Component queue: one toast per nonzero seal component, in scoring
    // order, each exactly-once by its own key. Losses queue too (red),
    // placement is never merged away, and a capped match closes with its
    // enforced total. Zero components queue nothing (a 0-0 seal is silent).
    const bd=lr.breakdown;
    if(bd && typeof bd==='object'){
      const comps=[['ELIMINATIONS', Number(bd.kills)], ['VICTORY', Number(bd.win)],
                   ['PLACEMENT', Number(bd.placement)], ['DEFEAT', Number(bd.loss)]];
      let shown=0;
      for(const [title, val] of comps){
        if(!Number.isFinite(val) || val===0) continue;
        queuePtsToast(title, val, {key:`${sealKey}:${title}`, noCoalesce:true, dwell:2400});
        shown++;
      }
      if(bd.capped===true && Number.isFinite(gained)){
        queuePtsToast('MATCH TOTAL (capped)', gained, {key:`${sealKey}:TOTAL`, noCoalesce:true, dwell:2800});
        shown++;
      }
      try{ logInfo(`[RANKED] points-toast components v=${snap.v} shown=${shown} gained=${gained}`); }catch{}
      return;
    }
    if(!Number.isFinite(gained) || gained===0) return;
    const winsUp=(snap.wins!=null && pending.prev.wins!=null)
      ? snap.wins>pending.prev.wins
      : lr.won===true;
    const killsUp=(snap.kills!=null && pending.prev.kills!=null)
      ? snap.kills>pending.prev.kills
      : Number(lr.kills)>0;
    const label=winsUp ? 'VICTORY' : (killsUp ? 'ELIMINATION' : 'PLACEMENT');
    queuePtsToast(label, gained, {key:sealKey});
  }

  // --- RANKED START OVERLAY: first match of the room only ---
  // Full-screen card: rules reminder (dynamic economy wording).
  // Click or 5s auto-dismiss. Late joiners (match_version > 1) never see it.
  const _rankedStartShown={};
  let _rankedStartTimer=null;
  function _dismissRankedStart(){
    try{ clearTimeout(_rankedStartTimer); }catch{}
    _rankedStartTimer=null;
    try{ const el=document.getElementById('bnc-ranked-start'); if(el) el.remove(); }catch{}
  }
  function updateRankedStart(){
    return;  // v1: "This is a Ranked Match" panel removed — no pre-match popup.
  }

  function getRankedKeybind(){
    try{
      const raw=GMGet('bnc_ranked_keybind', null);
      if(raw && typeof raw==='object' && raw.key) return raw;
      if(typeof raw==='string' && raw){
        try{ const j=JSON.parse(raw); if(j && j.key) return j; }catch{}
        const parts=String(raw).split('+');
        const kb={ctrl:false,shift:false,alt:false,meta:false,key:'F9'};
        for(const p of parts){ const l=p.trim().toLowerCase(); if(l==='ctrl'||l==='control') kb.ctrl=true; else if(l==='cmd'||l==='meta'||l==='command') kb.meta=true; else if(l==='shift') kb.shift=true; else if(l==='alt') kb.alt=true; else if(p.trim()) kb.key=p.trim().toUpperCase(); }
        return kb;
      }
    }catch{}
    return {ctrl:false, shift:false, alt:false, meta:false, key:'F9'};
  }
  function isRankedKeybind(e){
    return bindMatches(e, getRankedKeybind());
  }
  function getHost11Keybind(){
    try{
      const raw=GMGet('bnc_host11_keybind', null);
      if(raw && typeof raw==='object' && raw.key) return raw;
    }catch{}
    return {ctrl:false, shift:false, alt:false, meta:false, key:'F11'};
  }
  function isHost11Keybind(e){ return bindMatches(e, getHost11Keybind()); }
  function getHost12Keybind(){
    try{
      const raw=GMGet('bnc_host12_keybind', null);
      if(raw && typeof raw==='object' && raw.key) return raw;
    }catch{}
    return {ctrl:false, shift:false, alt:false, meta:false, key:'F12'};
  }
  function isHost12Keybind(e){ return bindMatches(e, getHost12Keybind()); }

  // --- RANKED PANEL (Phase 8): F9 — milestones map + ranked leaderboard ---
  const RANKED_ASSETS='https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/assets';
  const RANKED_TIERS=[
    {name:'Unranked', min:0, fee:0, color:'#6b7280'},
    {name:'Bronze', min:500, fee:25, color:'#CD7F32'},
    {name:'Silver', min:1200, fee:60, color:'#C0C0C0'},
    {name:'Gold', min:2500, fee:125, color:'#FFD700'},
    {name:'Platinum', min:4000, fee:200, color:'#E5E7EB'},
    {name:'Diamond', min:6000, fee:300, color:'#B9F2FF'},
    {name:'Emerald', min:9000, fee:450, color:'#10b981'},
    {name:'Champion', min:13000, fee:650, color:'#f59e0b'},
    {name:'Master', min:18000, fee:900, color:'#9333EA'},
    {name:'Grandmaster', min:25000, fee:1250, color:'#ef4444'},
  ];
  // Bucket filenames: platinum is stored as platinium.png (typo upstream).
  const RANKED_ICON_SLUG=t=>t==='Platinum'?'platinium':t.toLowerCase();
  RANKED_TIERS.forEach(t=>{ t.icon=`${RANKED_ASSETS}/${RANKED_ICON_SLUG(t.name)}.png`; });
  // Panel icons render at one uniform size.
  const RANKED_ICON_PX=()=>56;
  let rankedPanel=null, rankedTab='milestones', rankedLbCache=null, _rankedFadeT=null;
  function ensureRankedStyle(){
    if(document.getElementById('bnc-ranked-style')) return;
    try{
      const st=document.createElement('style');
      st.id='bnc-ranked-style';
      st.textContent='@keyframes bncPulse{0%,100%{opacity:1}50%{opacity:0.3}}';
      (document.head||document.documentElement).appendChild(st);
    }catch{}
  }
  function createRankedPanel(){
    if(document.getElementById('bnc-ranked')){ rankedPanel=document.getElementById('bnc-ranked'); return; }
    ensureRankedStyle();
    const ov=document.createElement('div');
    ov.id='bnc-ranked';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483646', background:'rgba(0,0,0,0.64)', display:'none', opacity:'0', transition:'opacity 180ms ease-out', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif'});
    ov.innerHTML=`<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:14px;width:700px;max-width:94vw;height:500px;max-height:90vh;display:flex;flex-direction:column;overflow:hidden"><div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.08)"><div><b style="font-size:16px;color:#fff">Nectar Ranked</b><div id="bnc-ranked-season" style="font-size:11px;color:#9ca3af;margin-top:2px">Season —</div></div><button id="bnc-ranked-dbg-btn" style="background:#1f2937;color:#93c5fd;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11px;margin-right:6px" title="Toggle the field-test debug chip (curScn / match version / pending seal)">DBG</button><button id="bnc-ranked-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px">Close</button></div><div style="display:flex;gap:8px;padding:12px 18px 0 18px"><button id="bnc-ranked-tab-ms" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Milestones</button><button id="bnc-ranked-tab-lb" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Leaderboard</button></div><div id="bnc-ranked-body" style="flex:1;overflow:auto;padding:16px 18px"></div></div>`;
    (document.body||document.documentElement).appendChild(ov);
    ov.addEventListener('click', (e)=>{ if(e.target===ov) toggleRankedPanel(false); });
    ov.querySelector('#bnc-ranked-close').onclick=()=> toggleRankedPanel(false);
    ov.querySelector('#bnc-ranked-dbg-btn').onclick=()=>{ try{ const on=localStorage.getItem('bnc_ranked_dbg')==='1'; try{ localStorage.setItem('bnc_ranked_dbg', on?'0':'1'); }catch{} try{ logInfo(`[RANKED] debug chip ${on?'OFF':'ON'}`); }catch{} updateRankedDebugChip(); }catch{} };
    ov.querySelector('#bnc-ranked-tab-ms').onclick=()=>{ rankedTab='milestones'; renderRankedPanel(); };
    ov.querySelector('#bnc-ranked-tab-lb').onclick=()=>{ rankedTab='leaderboard'; renderRankedPanel(); fetchRankedLb(); };
    rankedPanel=ov;
  }
  // True while the player is mid-match in an active ranked room: the ranked
  // panel stays locked (leaderboard/milestones would be a live aid).
  function rankedLocked(){
    try{
      const rk=liveRanked();
      if(!rk) return false;
      return effInMatch()===true;
    }catch{ return false; }
  }
  // ---- RANKED CODE RECHECK MODAL (F9 fallback) ---------------------------
  // Missed the join sniff (late inject, host started the ranked/tourney room
  // before we tracked it, pID never seen): F9 would just say "only available
  // during a ranked session" and leave the player stuck. Instead F9 opens
  // this modal asking for the room code, then re-runs the exact join-time
  // path (comp resolve + ranked-tourney resolve + rankedTrack) and a fast
  // stats pull so the session is picked up and tracking continues. Never
  // throws.
  function hideRankedCodeModal(){
    try{ const w=document.getElementById('bnc-ranked-code'); if(w && w._bncCleanup) w._bncCleanup(); if(w) w.remove(); }catch{}
  }
  function rankedCodeStatus(msg, tone){
    try{
      const el=document.getElementById('bnc-ranked-code-status');
      if(!el) return;
      el.textContent=msg||'';
      el.style.color = tone==='bad' ? '#fca5a5' : (tone==='ok' ? '#6ee7b7' : '#9ca3af');
    }catch{}
  }
  function currentSessionCode(){
    try{
      const sess=statsCache && statsCache.session;
      const c=sess && sess.short_code ? String(sess.short_code).trim().toUpperCase() : '';
      if(/^[A-Z0-9]{5}$/.test(c)) return c;
    }catch{}
    try{
      if(typeof _rankedRoom!=='undefined' && _rankedRoom && _rankedRoom.code && /^[A-Z0-9]{5}$/.test(String(_rankedRoom.code).toUpperCase())) return String(_rankedRoom.code).toUpperCase();
    }catch{}
    return '';
  }
  // The join-time path, replayed with a hand-entered code: comp resolve +
  // ranked-tourney resolve + rankedTrack (which itself resolves the ranked
  // room, clears the leave guard, kicks a stats pull + transition burst).
  // Mode (comp tourney / ranked tourney / ranked) is decided server-side by
  // whichever resolve hits — the client just tracks all three and lets the
  // live blocks + HUD priority sort it out. Never throws.
  function recheckRankedCode(code){
    try{
      code=String(code||'').trim().toUpperCase();
      if(!/^[A-Z0-9]{5}$/.test(code)) return false;
      try{ _publicRoom=false; }catch{}
      try{ _rankedLeaveGuard=false; }catch{}
      try{ fetchCompRoom(code); }catch{}
      try{ fetchRtRoom(code); }catch{}
      try{ rankedTrack(code); }catch{}
      try{ fetchStats(false); }catch{}
      try{ startTransitionBurst('ranked-code-recheck'); }catch{}
      try{ updateTourneyHud(); }catch{}
      try{ updateRtHud(); }catch{}
      try{ logInfo(`[RANKED] code recheck ${code}`); }catch{}
      return true;
    }catch{ return false; }
  }
  function showRankedCodeModal(prefill){
    if(document.getElementById('bnc-ranked-code')) return;
    const wrap=document.createElement('div');
    wrap.id='bnc-ranked-code';
    Object.assign(wrap.style,{position:'fixed',inset:'0',zIndex:'2147483647',background:'rgba(0,0,0,0.82)',display:'flex',alignItems:'center',justifyContent:'center',padding:'24px',fontFamily:'Inter, system-ui, sans-serif'});
    const card=document.createElement('div');
    Object.assign(card.style,{background:'#0a0a0a',border:'1px solid rgba(255,255,255,0.16)',borderRadius:'14px',padding:'26px',width:'440px',boxSizing:'border-box',boxShadow:'0 8px 32px rgba(0,0,0,0.55)'});
    card.innerHTML='<div style="font-size:30px;margin-bottom:6px">🏆</div>'
      + '<div style="font-size:18px;font-weight:800;color:#fff;margin-bottom:6px">Join ranked / tourney tracking</div>'
      + '<div style="font-size:13px;color:#9ca3af;line-height:1.6;margin-bottom:14px">No ranked/tourney session tracked yet — the host already started one, or the join was missed. Enter the 5-letter room code to recheck and keep tracking.</div>'
      + '<input id="bnc-ranked-code-input" type="text" placeholder="Room code (ABC12)" autocomplete="off" spellcheck="false" maxlength="5" style="width:100%;height:40px;padding:0 12px;border:1px solid #374151;border-radius:8px;background:#111;color:#fff;font-size:15px;font-family:monospace;letter-spacing:1.5px;box-sizing:border-box;outline:none;text-transform:uppercase">'
      + '<div style="display:flex;gap:8px;margin-top:12px">'
      +   '<button id="bnc-ranked-code-go" style="flex:1;height:40px;border:0;border-radius:8px;background:#fff;color:#000;font-weight:700;cursor:pointer">Recheck &amp; track</button>'
      +   '<button id="bnc-ranked-code-later" style="flex:1;height:40px;border:1px solid #374151;border-radius:8px;background:#1f2937;color:#e5e7eb;font-weight:600;cursor:pointer">Not now</button>'
      + '</div>'
      + '<div id="bnc-ranked-code-status" style="margin-top:10px;font-size:12px;color:#9ca3af;min-height:16px"></div>';
    wrap.appendChild(card);
    (document.body||document.documentElement).appendChild(wrap);
    wrap.addEventListener('click', e=> e.stopPropagation());
    const input=card.querySelector('#bnc-ranked-code-input');
    // Same capture-phase guard as the session modal: the game installs
    // window-level capture listeners first, so kill the event for the game
    // without preventDefault (the input needs every key).
    const guard=(e)=>{ try{ if(input && (e.target===input || card.contains(e.target) || card.contains(document.activeElement))) { e.stopPropagation(); if(e.stopImmediatePropagation) e.stopImmediatePropagation(); } }catch{} };
    const events=['keydown','keyup','keypress','input','paste','cut','compositionstart','compositionupdate','compositionend'];
    const targets=[window, document, document.documentElement];
    try{ if(typeof unsafeWindow!=='undefined' && unsafeWindow!==window) targets.push(unsafeWindow); }catch{}
    events.forEach(ev=> targets.forEach(t=>{ try{ t.addEventListener(ev, guard, true); }catch{} }));
    wrap._bncCleanup=()=>{ events.forEach(ev=> targets.forEach(t=>{ try{ t.removeEventListener(ev, guard, true); }catch{} })); };
    let busy=false;
    const submit=()=>{
      if(busy) return;
      const code=String(input && input.value || '').trim().toUpperCase();
      if(!/^[A-Z0-9]{5}$/.test(code)){ rankedCodeStatus('Enter the 5-letter room code (ask the host).', 'bad'); return; }
      busy=true;
      const go=card.querySelector('#bnc-ranked-code-go');
      if(go) go.textContent='Rechecking…';
      rankedCodeStatus('Rechecking ranked / tourney session…');
      recheckRankedCode(code);
      // Give the by-code resolves + stats poll a moment, then report what
      // the session actually is. A hit opens the panel (lobby) or just
      // tracks quietly (mid-match lock keeps the panel shut).
      setTimeout(()=>{
        try{
          busy=false;
          if(go) go.textContent='Recheck & track';
          let hit=null;
          try{ hit=liveRanked(); }catch{}
          let comp=false, rt=false;
          try{ comp=typeof _inCompRoom==='function' ? !!_inCompRoom(code) : !!(statsCache&&statsCache.comp_tourney); }catch{}
          try{ rt=!!(statsCache&&statsCache.ranked_tourney); }catch{}
          if(hit || comp || rt){
            const what=comp?'tourney':(hit?'ranked':'ranked tourney');
            rankedCodeStatus(`Tracked as ${what} — keep playing.`, 'ok');
            try{ showToast(`Tracking ${what} session ${code}`, false); }catch{}
            setTimeout(()=>{ try{ hideRankedCodeModal(); }catch{} try{ toggleRankedPanel(true); }catch{} }, 600);
          } else {
            rankedCodeStatus('Still no ranked/tourney session for that code — check the code or ask the host to re-set it.', 'bad');
          }
        }catch{}
      }, 2500);
    };
    const go=card.querySelector('#bnc-ranked-code-go');
    if(go) go.onclick=submit;
    const later=card.querySelector('#bnc-ranked-code-later');
    if(later) later.onclick=()=>{ hideRankedCodeModal(); };
    if(input){
      try{ input.value=String(prefill||currentSessionCode()||'').toUpperCase(); }catch{}
      input.addEventListener('keydown', (e)=>{ if(e.key==='Enter'){ e.preventDefault(); submit(); } });
    }
    setTimeout(()=>{ try{ if(input){ input.focus(); input.select(); } }catch{} }, 80);
  }
  function toggleRankedPanel(force){
    if(typeof rankedEnabled==='function' && !rankedEnabled()){ showToast('Ranked is sleeping - streaks are live', false); try{ logInfo('[RANKED] panel sleeping'); }catch{} return; }
    createRankedPanel();
    const isHidden=rankedPanel.style.display==='none' || !rankedPanel.style.display;
    const show=typeof force==='boolean' ? force : isHidden;
    // Panel is ranked-session only: blocked outside one, and locked mid-match
    // inside one (leaderboard/milestones would be a live aid). The blocked
    // path opens the code-recheck modal instead of dead-ending: the host may
    // have started a ranked/tourney room we never sniffed (late inject,
    // missed pID) — entering the code re-runs the join path and tracking
    // continues.
    try{ if(show && GMGet('bnc_host_mode', false)!==true){ showToast('Host mode off — enable bnc_host_mode in Violentmonkey to unlock', true); return; } }catch{} if(show && !liveRanked()){
      showToast('No ranked session tracked - enter the room code', true);
      try{ logInfo('[RANKED] panel blocked - not in ranked session (code modal)'); }catch{}
      try{ showRankedCodeModal(currentSessionCode()); }catch{}
      return;
    }
    if(show && rankedLocked()){ showToast('Ranked panel is locked during a ranked match', true); return; }
    try{ clearTimeout(_rankedFadeT); }catch{}
    if(show){
      rankedPanel.style.display='flex';
      requestAnimationFrame(()=>{ requestAnimationFrame(()=>{ try{ rankedPanel.style.opacity='1'; }catch{} }); });
      renderRankedPanel();
      fetchRankedLb(true);
    } else {
      try{ rankedPanel.style.opacity='0'; }catch{}
      _rankedFadeT=setTimeout(()=>{ try{ rankedPanel.style.display='none'; }catch{} }, 180);
    }
  }
  // "12d 4h" style countdown to the season reset. Null when unknown/passed.
  function _resetIn(iso){
    try{
      if(!iso) return null;
      const ms=new Date(iso).getTime()-Date.now();
      if(!(ms>0)) return null;
      const m=Math.floor(ms/60000), d=Math.floor(m/1440), h=Math.floor((m%1440)/60);
      if(d>0) return `${d}d ${h}h`;
      if(h>0) return `${h}h ${m%60}m`;
      return `${m}m`;
    }catch{ return null; }
  }
  function updateRankedSeason(){
    if(!rankedPanel) return;
    const el=rankedPanel.querySelector('#bnc-ranked-season');
    if(!el) return;
    const a=currentArena();
    const c=rankedLbCache;
    const season=a && a.season!=null ? a.season : (c && c.season!=null ? c.season : null);
    const reset=a && a.reset_at ? a.reset_at : (c ? c.reset_at : null);
    if(season!=null){
      const left=_resetIn(reset);
      el.textContent=`Season ${season}`+(left?` · Resets in ${left}`:'');
      el.title=reset?`Resets at ${reset}`:'';
    } else {
      el.textContent='Season —';
      el.title='';
    }
  }
  function currentTierName(){
    try{
      const a=statsCache && statsCache.arena ? statsCache.arena : null;
      if(a && a.milestone) return String(a.milestone);
      const rk=statsCache && statsCache.ranked ? statsCache.ranked : null;
      if(rk && rk.milestone) return String(rk.milestone);
      const pts=a && a.points!=null ? Number(a.points) : (rk && rk.points!=null ? Number(rk.points) : 0);
      let name='Unranked';
      for(const t of RANKED_TIERS){ if(pts>=t.min) name=t.name; }
      return name;
    }catch{ return 'Unranked'; }
  }
  function currentArena(){
    try{
      if(statsCache && statsCache.arena) return statsCache.arena;
      if(statsCache && statsCache.ranked) return statsCache.ranked;
    }catch{}
    return null;
  }
  function renderRankedPanel(){
    if(!rankedPanel) return;
    const body=rankedPanel.querySelector('#bnc-ranked-body');
    const msBtn=rankedPanel.querySelector('#bnc-ranked-tab-ms');
    const lbBtn=rankedPanel.querySelector('#bnc-ranked-tab-lb');
    const tabStyle=(active)=>`flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer;border:1px solid ${active?'#fff':'rgba(255,255,255,0.12)'};background:${active?'#fff':'#171717'};color:${active?'#000':'#9ca3af'}`;
    if(msBtn) msBtn.style.cssText=tabStyle(rankedTab==='milestones');
    if(lbBtn) lbBtn.style.cssText=tabStyle(rankedTab==='leaderboard');
    if(!body) return;
    updateRankedSeason();
    if(rankedTab==='milestones'){
      const here=currentTierName();
      const ar=currentArena();
      const pts=ar && ar.points!=null ? Number(ar.points) : 0;
      const tier=RANKED_TIERS.find(t=>t.name===here) || RANKED_TIERS[0];
      const pct=ar && ar.next_points_pct!=null ? Math.max(0, Math.min(100, Number(ar.next_points_pct))) : 0;
      const toNext=ar && ar.next_milestone
        ? `${Number(ar.points_to_next||0).toLocaleString('en-US')} points to ${String(ar.next_milestone).replace(/</g,'&lt;')}`
        : 'Max rank reached';
      const summaryPx=RANKED_ICON_PX(tier.name);
      const summary=`<div style="display:flex;align-items:center;gap:12px;background:#171717;border:1px solid ${tier.color};border-radius:10px;padding:12px 14px;margin-bottom:16px"><img src="${tier.icon}" alt="" style="width:${summaryPx}px;height:${summaryPx}px;object-fit:contain;flex-shrink:0" onerror="this.style.display='none'" /><div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700;color:#fff">${tier.name} · ${Number(pts).toLocaleString('en-US')} pts</div><div style="height:6px;background:rgba(255,255,255,0.10);border-radius:999px;margin-top:8px;overflow:hidden"><div style="height:100%;width:${pct}%;background:${tier.color};border-radius:999px"></div></div><div style="font-size:11px;color:rgba(255,255,255,0.56);margin-top:6px">${toNext} (${pct}%)</div></div></div>`;
      const cards=RANKED_TIERS.map(t=>{
        const isHere=t.name===here;
        const px=RANKED_ICON_PX(t.name);
        return `<div style="background:#171717;border:1px solid ${isHere?t.color:'rgba(255,255,255,0.10)'};border-radius:10px;padding:14px 8px 12px 8px;text-align:center"><div style="height:72px;display:flex;align-items:center;justify-content:center"><img src="${t.icon}" alt="${t.name}" style="width:${px}px;height:${px}px;object-fit:contain" onerror="this.style.display='none'" /></div><div style="font-size:12px;font-weight:700;color:${t.color};margin-top:10px">${t.name}</div><div style="font-size:11px;color:rgba(255,255,255,0.56);margin-top:2px">${Number(t.min).toLocaleString('en-US')}+</div><div style="font-size:11px;color:rgba(255,255,255,0.36)">Fee ${t.fee}</div>${isHere?`<div style="font-size:11px;font-weight:700;color:#fff;margin-top:8px">● You are here</div>`:''}</div>`;
      }).join('');
      body.innerHTML=`${summary}<div style="display:grid;grid-template-columns:repeat(5,1fr);grid-auto-rows:1fr;gap:12px">${cards}</div><div style="font-size:11px;color:rgba(255,255,255,0.36);text-align:center;margin-top:12px">Entry fees burn on every ranked start — higher tier, steeper entry.</div>`;
    } else {
      renderRankedLb(rankedLbCache ? rankedLbCache.rows : null);
    }
  }
  function renderRankedLb(rows){
    if(!rankedPanel) return;
    const body=rankedPanel.querySelector('#bnc-ranked-body');
    if(!body) return;
    if(!rows){
      body.innerHTML=`<div style="text-align:center;color:#9ca3af;font-size:13px;padding:40px 0">Loading leaderboard…</div>`;
      return;
    }
    if(!rows.length){
      body.innerHTML=`<div style="text-align:center;color:#9ca3af;font-size:13px;padding:40px 0">No ranked players yet — be the first.</div>`;
      return;
    }
    let meDid=null;
    try{ meDid=String(discordId||''); }catch{}
    const lines=rows.map(r=>{
      const did=String(r.discord_id||'');
      const isMe=meDid && did===meDid;
      const nm=String(r.game_username||r.discord_username||did).replace(/</g,'&lt;');
      const icon=r.milestone_icon_url?`<img src="${String(r.milestone_icon_url).replace(/"/g,'')}" alt="" style="width:18px;height:18px;object-fit:contain;vertical-align:-3px;margin-right:6px" onerror="this.style.display='none'" />`:'';
      return `<div style="display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:8px;background:${isMe?'rgba(255,255,255,0.08)':'transparent'};border:1px solid ${isMe?'rgba(255,255,255,0.25)':'transparent'}"><span style="width:28px;font-size:12px;font-weight:800;color:#9ca3af">#${r.rank}</span><span style="flex:1;font-size:13px;font-weight:${isMe?'800':'600'};color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${icon}${nm}</span><span style="font-size:13px;font-weight:800;color:${r.milestone_color||'#fff'}">${Number(r.points||0).toLocaleString('en-US')}</span><span style="width:90px;text-align:right;font-size:11px;color:#9ca3af">${String(r.milestone||'').replace(/</g,'&lt;')}</span></div>`;
    }).join('');
    body.innerHTML=`<div style="display:flex;flex-direction:column;gap:2px">${lines}</div>`;
  }
  // Privileged GET for the F9 leaderboard (explicit user action — the only
  // client-initiated ranked read left). GM_xhr first, bare fetch fallback.
  function rankedApiGet(path, cb){
    let base=''; try{ base=resolveApiBase(); }catch{}
    if(!base){ try{cb(null);}catch{} return; }
    const url=base+path;
    const done=(ok,txt)=>{
      let j=null;
      try{ j=(ok && txt) ? JSON.parse(txt) : null; }catch{ j=null; }
      try{ cb(j); }catch{}
    };
    try{
      const GM=typeof GM_xmlhttpRequest!=='undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
      if(GM){ GM({method:'GET', url, timeout:10000, onload:r=>done(r.status>=200&&r.status<300, r.responseText), onerror:()=>done(false,''), ontimeout:()=>done(false,'')}); return; }
    }catch{}
    fetch(url,{method:'GET'}).then(r=>r.text().then(t=>done(r.ok,t)).catch(()=>done(false,''))).catch(()=>done(false,''));
  }
  function fetchRankedLb(quiet){
    const now=Date.now();
    if(rankedLbCache && now-rankedLbCache.at<60000){
      if(rankedTab==='leaderboard' && !quiet) renderRankedLb(rankedLbCache.rows);
      updateRankedSeason();
      return;
    }
    if(rankedTab==='leaderboard' && !quiet) renderRankedLb(null);
    rankedApiGet('/api/leaderboard?ranked=true&limit=20', (j)=>{
      if(j) rankedLbCache={at:Date.now(), rows:j.results||[], season:j.season!=null?j.season:null, reset_at:j.reset_at||null};
      else rankedLbCache={at:Date.now(), rows:[], season:null, reset_at:null};
      updateRankedSeason();
      if(rankedTab==='leaderboard') renderRankedPanel();
    });
  }

  // --- HOST PANELS: F11 roster + F12 controls (tourney room only) ---
  // Same card pattern as the ranked/LB panels. Host-gating is contextual:
  // panels open inside a tracked tourney room (_inCompRoom); the comp
  // endpoints carry no auth (same as the dashboard), so this gate only
  // keeps non-hosts from firing host actions by accident.
  let host11Panel=null, host12Panel=null, _hostFadeT=null;
  let _hostTourneyCache=null, _hostPrCache=null;
  function hostTourneyId(){
    try{ if(_compRoom && _compRoom.id!=null) return Number(_compRoom.id); }catch{}
    try{ const t=(statsCache||{}).comp_tourney; if(t && t.tourney_id!=null) return Number(t.tourney_id); }catch{}
    return null;
  }
  function hostPanelsAllowed(){
    try{ if(typeof _inCompRoom==='function' && _inCompRoom()) return true; }catch{}
    try{ if(typeof hostRankedCtx==='function' && hostRankedCtx()) return true; }catch{}
    try{ if(hostTourneyId()!=null) return true; }catch{}
    return false;
  }
  // Ranked host context (comp-copy detection): liveRanked() is the gate —
  // local _rankedRoom intent + server stats `ranked` block confirmer, leave
  // guard hides it the moment the host leaves. A code scored as a comp
  // tourney stays comp (server skips ranked seals there).
  function hostRankedRoom(){
    try{
      const r=typeof liveRanked==='function' ? liveRanked() : null;
      if(!r || !r.rid) return null;
      return {rid:String(r.rid), code:String(r.short_code||'').toUpperCase(),
              is_host:!!r.is_host, status:r.status||null};
    }catch{ return null; }
  }
  function hostRankedCtx(){
    try{
      const r=hostRankedRoom();
      if(!r) return null;
      if(typeof _inCompRoom==='function' && _inCompRoom()) return null;
      return r;
    }catch{ return null; }
  }
  function hostRankedAllowed(){
    try{ const r=hostRankedCtx(); return !!(r && r.is_host); }catch{ return false; }
  }
  function hostApiPost(path, body, cb){
    let base=''; try{ base=resolveApiBase(); }catch{}
    if(!base){ try{cb(null);}catch{} return; }
    const url=base+path;
    const done=(ok,txt)=>{ let j=null; try{ j=(ok&&txt)?JSON.parse(txt):null; }catch{ j=null; } try{cb(j);}catch{} };
    const payload=JSON.stringify(body||{});
    try{
      const GM=typeof GM_xmlhttpRequest!=='undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
      if(GM){ GM({method:'POST', url, data:payload, headers:{'Content-Type':'application/json'}, timeout:10000, onload:r=>done(r.status>=200&&r.status<300, r.responseText), onerror:()=>done(false,''), ontimeout:()=>done(false,'')}); return; }
    }catch{}
    fetch(url,{method:'POST', headers:{'Content-Type':'application/json'}, body:payload}).then(r=>r.text().then(t=>done(r.ok,t)).catch(()=>done(false,''))).catch(()=>done(false,''));
  }
  function hostOpenMatch(){
    try{
      const ms=((_hostTourneyCache||{}).matches)||[];
      const live=ms.filter(m=>m && (m.status==='open'||m.status==='manual'));
      live.sort((a,b)=>(b.version||0)-(a.version||0));
      return live[0]||null;
    }catch{ return null; }
  }
  function createHost11Panel(){
    if(document.getElementById('bnc-host11')){ host11Panel=document.getElementById('bnc-host11'); return; }
    const ov=document.createElement('div');
    ov.id='bnc-host11';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483646', background:'rgba(0,0,0,0.64)', display:'none', opacity:'0', transition:'opacity 180ms ease-out', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif'});
    ov.innerHTML=`<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:14px;width:640px;max-width:94vw;height:480px;max-height:90vh;display:flex;flex-direction:column;overflow:hidden"><div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.08)"><div><b id="bnc-host11-title" style="font-size:16px;color:#fff">Host roster</b><div id="bnc-host11-sub" style="font-size:11px;color:#9ca3af;margin-top:2px">—</div></div><div><button id="bnc-host11-refresh" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;margin-right:6px">Refresh</button><button id="bnc-host11-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px">Close</button></div></div><div id="bnc-host11-body" style="flex:1;overflow:auto;padding:16px 18px;font-size:13px;color:#e5e7eb"></div></div>`;
    (document.body||document.documentElement).appendChild(ov);
    ov.addEventListener('click', (e)=>{ if(e.target===ov) toggleHost11Panel(false); });
    ov.querySelector('#bnc-host11-close').onclick=()=> toggleHost11Panel(false);
    ov.querySelector('#bnc-host11-refresh').onclick=()=> fetchHostPanel();
    host11Panel=ov;
  }
  // Contextual refresh: ranked room (no comp context) -> ranked variant,
  // otherwise the existing comp tourney path.
  function hostCompCtx(){
    try{ if(typeof _inCompRoom==='function' && _inCompRoom()) return true; }catch{}
    try{ if(typeof hostTourneyId==='function' && hostTourneyId()!=null) return true; }catch{}
    return false;
  }
  function fetchHostPanel(){
    try{
      if(!hostCompCtx() && typeof hostRankedCtx==='function' && hostRankedCtx()){ fetchHostRanked(); return; }
    }catch{}
    fetchHostTourney();
  }
  function fetchHostTourney(){
    const tid=hostTourneyId();
    const body=document.getElementById('bnc-host11-body');
    if(tid==null){ if(body) body.innerHTML='<span style="color:#9ca3af">No tourney context — join the tourney room first.</span>'; return; }
    if(body) body.innerHTML='<span style="color:#9ca3af">Loading…</span>';
    rankedApiGet('/api/comp/tourney?id='+encodeURIComponent(tid), (j)=>{
      _hostTourneyCache=j||null;
      rankedApiGet('/api/leaderboard?sort=pr&limit=100', (lb)=>{
        try{
          const rows=((lb||{}).results)||[]; _hostPrCache={};
          rows.forEach(r=>{ if(r&&r.discord_id!=null&&r.tourney_pr!=null) _hostPrCache[String(r.discord_id)]=r.tourney_pr; });
        }catch{}
        renderHost11Panel();
      });
    });
  }
  function renderHost11Panel(){
    const body=document.getElementById('bnc-host11-body'); if(!body) return;
    try{ const _t=document.getElementById('bnc-host11-title'); if(_t) _t.textContent='Host roster'; }catch{}
    const t=(_hostTourneyCache||{}).tourney||{};
    const sub=document.getElementById('bnc-host11-sub');
    if(sub) sub.textContent=(t.name||'Tourney')+' · '+(t.status||'');
    const st=(_hostTourneyCache||{}).standings||[];
    if(!st.length){ body.innerHTML='<span style="color:#9ca3af">No sealed matches yet.</span>'; return; }
    const prm=_hostPrCache||{};
    let h='<table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="color:#9ca3af;text-align:left"><th>#</th><th>Player</th><th>Pts</th><th>K</th><th>W</th><th>Avg</th><th>PR</th></tr>';
    st.forEach((r,i)=>{
      const nm=(r&&r.name)||'?'; const did=String((r&&r.discord_id)||'');
      const pr=prm[did];
      h+=`<tr style="border-top:1px solid rgba(255,255,255,0.06)"><td>${i+1}</td><td><b style="color:#fff">${String(nm).replace(/</g,'&lt;')}</b></td><td>${r.points||0}</td><td>${r.kills||0}</td><td>${r.wins||0}</td><td>${r.avg_placement!=null?r.avg_placement:'—'}</td><td>${pr!=null?pr:'—'}</td></tr>`;
    });
    body.innerHTML=h+'</table>';
  }
  function toggleHost11Panel(force){
    createHost11Panel();
    const isHidden=host11Panel.style.display==='none'||!host11Panel.style.display;
    const show=typeof force==='boolean'?force:isHidden;
    try{ if(show && GMGet('bnc_host_mode', false)!==true){ showToast('Host mode off — enable bnc_host_mode in Violentmonkey to unlock', true); return; } }catch{} if(show && !hostPanelsAllowed()){ showToast('Host roster needs a tourney room', true); return; }
    try{ clearTimeout(_hostFadeT); }catch{}
    if(show){ host11Panel.style.display='flex'; requestAnimationFrame(()=>{ requestAnimationFrame(()=>{ try{ host11Panel.style.opacity='1'; }catch{} }); }); fetchHostPanel(); }
    else { try{ host11Panel.style.opacity='0'; }catch{} _hostFadeT=setTimeout(()=>{ try{ host11Panel.style.display='none'; }catch{} }, 180); }
  }
  // --- RANKED ROSTER VARIANT (F11 in a ranked room, no comp context) ---
  // Gold-themed host roster: live match board from /api/ranked/match joined
  // with season points from the ranked leaderboard. Same card pattern as the
  // comp roster; refresh re-pulls both. Never throws.
  let _hostRankedCache=null, _hostRankedLb=null;
  const RANKED_GOLD_BAR='background:linear-gradient(135deg, rgba(255,215,0,0.22), rgba(245,158,11,0.14));border:1px solid rgba(255,215,0,0.35);border-radius:8px;padding:8px 12px;margin-bottom:10px';
  function fetchHostRanked(){
    const ctx=(typeof hostRankedCtx==='function') ? hostRankedCtx() : null;
    const body=document.getElementById('bnc-host11-body');
    const title=document.getElementById('bnc-host11-title');
    if(title) title.innerHTML='Ranked roster';
    if(!ctx){ if(body) body.innerHTML='<span style="color:#9ca3af">No ranked context — join the ranked room first.</span>'; return; }
    if(body) body.innerHTML='<span style="color:#9ca3af">Loading…</span>';
    const rk=((statsCache||{}).ranked)||{};
    const mver=rk.match_version;
    const sub=document.getElementById('bnc-host11-sub');
    if(sub) sub.textContent=`${ctx.code||'Ranked'} · ${ctx.status||''} · ${ctx.rid}`;
    const pullLb=(match)=>{
      _hostRankedCache={ctx, match:match||null};
      rankedApiGet('/api/leaderboard?ranked=true&limit=100', (lb)=>{
        try{
          const rows=((lb||{}).results)||[]; _hostRankedLb={};
          rows.forEach(r=>{ if(r&&r.discord_id!=null&&r.points!=null) _hostRankedLb[String(r.discord_id)]=r.points; });
        }catch{ _hostRankedLb={}; }
        renderHost11Ranked();
      });
    };
    if(mver==null){ pullLb(null); return; }
    rankedApiGet('/api/ranked/match?room_id='+encodeURIComponent(ctx.rid)+'&match_version='+encodeURIComponent(mver), (j)=>{
      pullLb(j);
    });
  }
  function renderHost11Ranked(){
    const body=document.getElementById('bnc-host11-body'); if(!body) return;
    const title=document.getElementById('bnc-host11-title');
    if(title) title.innerHTML='<span style="background:linear-gradient(135deg,#FFD700,#f59e0b);-webkit-background-clip:text;background-clip:text;color:transparent">Ranked roster</span>';
    const cache=_hostRankedCache||{};
    const ctx=cache.ctx||{};
    const m=cache.match||{};
    const board=m.board||[];
    const lb=_hostRankedLb||{};
    let h=`<div style="${RANKED_GOLD_BAR}"><b style="color:#FFD700">${String(ctx.code||'Ranked').replace(/</g,'&lt;')}</b>`
      +`<span style="color:#e5e7eb"> · ${m.sealed?'sealed':'live'}${m.winner_discord_id?' · winner crowned':''} · ${board.length} playing</span></div>`;
    if(!board.length){ body.innerHTML=h+'<span style="color:#9ca3af">No banked events yet this match.</span>'; return; }
    h+='<table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="color:#9ca3af;text-align:left"><th>#</th><th>Player</th><th>K</th><th>Plc</th><th>Match</th><th>Season</th></tr>';
    board.forEach((r,i)=>{
      const nm=String((r&&r.name)||'?').replace(/</g,'&lt;');
      const did=String((r&&r.discord_id)||'');
      const crown=(r&&r.won)?' 👑':'';
      const season=lb[did];
      h+=`<tr style="border-top:1px solid rgba(255,255,255,0.06)"><td>${i+1}</td><td><b style="color:#fff">${nm}</b>${crown}</td><td>${(r&&r.kills)||0}</td><td>${(r&&r.placement)!=null?r.placement:'—'}</td><td>${(r&&r.points)||0}</td><td>${season!=null?season:'—'}</td></tr>`;
    });
    body.innerHTML=h+'</table>';
  }
  function createHost12Panel(){
    if(document.getElementById('bnc-host12')){ host12Panel=document.getElementById('bnc-host12'); return; }
    const ov=document.createElement('div');
    ov.id='bnc-host12';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483646', background:'rgba(0,0,0,0.64)', display:'none', opacity:'0', transition:'opacity 180ms ease-out', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif'});
    ov.innerHTML=`<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:14px;width:560px;max-width:94vw;max-height:90vh;display:flex;flex-direction:column;overflow:hidden"><div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.08)"><div><b id="bnc-host12-title" style="font-size:16px;color:#fff">Host controls</b><div id="bnc-host12-sub" style="font-size:11px;color:#9ca3af;margin-top:2px">—</div></div><button id="bnc-host12-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px">Close</button></div><div id="bnc-host12-body" style="padding:16px 18px;font-size:13px;color:#e5e7eb;overflow:auto"></div><div id="bnc-host12-status" style="padding:8px 18px;font-size:12px;color:#9ca3af;border-top:1px solid rgba(255,255,255,0.08)"></div></div>`;
    (document.body||document.documentElement).appendChild(ov);
    ov.addEventListener('click', (e)=>{ if(e.target===ov) toggleHost12Panel(false); });
    ov.querySelector('#bnc-host12-close').onclick=()=> toggleHost12Panel(false);
    host12Panel=ov;
  }
  function host12Say(t, bad){ try{ const el=document.getElementById('bnc-host12-status'); if(el){ el.textContent=t; el.style.color=bad?'#f87171':'#9ca3af'; } }catch{} }
  function renderHost12Panel(){
    const body=document.getElementById('bnc-host12-body'); if(!body) return;
    try{ const _t=document.getElementById('bnc-host12-title'); if(_t) _t.textContent='Host controls'; }catch{}
    const t=(_hostTourneyCache||{}).tourney||{};
    const sub=document.getElementById('bnc-host12-sub');
    const m=hostOpenMatch();
    if(sub) sub.textContent=(t.name||'Tourney')+(m?(' · match v'+m.version+' ('+m.status+')'):' · no open match');
    const inp=(id,ph)=>`<input id="${id}" placeholder="${ph}" style="background:#1f2937;color:#fff;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:6px 8px;font-size:12px;width:170px">`;
    const btn=(id,label)=>`<button id="${id}" style="background:#7c3aed;color:#fff;border:0;border-radius:6px;padding:6px 10px;cursor:pointer;font-size:12px;margin-left:6px">${label}</button>`;
    body.innerHTML=
      `<div style="margin-bottom:10px">${inp('h12-winner','winner name or id')}${btn('h12-winGo','Set winner')}</div>`+
      `<div style="margin-bottom:10px">${btn('h12-sealGo','Seal match')}${btn('h12-openGo','Open blank match')}</div>`+
      `<div style="margin-bottom:10px">${inp('h12-rm','player name or id')}${btn('h12-rmGo','Remove')}${btn('h12-addGo','Add back')}</div>`+
      `<div style="color:#9ca3af;font-size:12px">Same endpoints as the dashboard. Re-pull F11 to see results.</div>`;
    const q=(id)=>document.getElementById(id);
    const needMatch=()=>{ const mm=hostOpenMatch(); if(!mm){ host12Say('No open match in this tourney.', true); return null; } return mm; };
    const resolveDid=(raw)=>{
      try{
        raw=String(raw||'').trim(); if(!raw) return null;
        const st=((_hostTourneyCache||{}).standings)||[];
        for(const r of st){ if(String(r.discord_id)===raw) return String(r.discord_id); }
        const low=raw.toLowerCase();
        for(const r of st){ if(String(r.name||'').toLowerCase()===low) return String(r.discord_id); }
      }catch{} return null;
    };
    q('h12-winGo').onclick=()=>{ const mm=needMatch(); if(!mm) return; const raw=q('h12-winner').value; const did=raw?resolveDid(raw):null; if(raw&&!did){ host12Say('Unknown player — use the F11 name or id.', true); return; } hostApiPost('/api/comp/match/winner',{match_id:mm.id,winner_discord_id:did},(j)=>{ host12Say(j&&!j.error?('Winner set'+(j.placements?' (+P1)':'')):('Failed: '+((j&&j.error)||'no response')), !(j&&!j.error)); fetchHostTourney(); }); };
    q('h12-sealGo').onclick=()=>{ const mm=needMatch(); if(!mm) return; hostApiPost('/api/comp/match/seal',{match_id:mm.id},(j)=>{ host12Say(j&&!j.error?'Sealed.':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error)); fetchHostTourney(); }); };
    q('h12-openGo').onclick=()=>{ const tid=hostTourneyId(); if(tid==null){ host12Say('No tourney context.', true); return; } hostApiPost('/api/comp/match/open',{tourney_id:tid},(j)=>{ host12Say(j&&!j.error?('Opened v'+j.version+'.'):'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error)); fetchHostTourney(); }); };
    q('h12-rmGo').onclick=()=>{ const mm=needMatch(); if(!mm) return; const did=resolveDid(q('h12-rm').value); if(!did){ host12Say('Unknown player.', true); return; } hostApiPost('/api/comp/match/remove',{match_id:mm.id,discord_id:did},(j)=>{ host12Say(j&&!j.error?'Removed (ignored in placements).':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error)); fetchHostTourney(); }); };
    q('h12-addGo').onclick=()=>{ const mm=needMatch(); if(!mm) return; const did=resolveDid(q('h12-rm').value); if(!did){ host12Say('Unknown player.', true); return; } hostApiPost('/api/comp/match/add-back',{match_id:mm.id,discord_id:did},(j)=>{ host12Say(j&&!j.error?'Added back.':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error)); fetchHostTourney(); }); };
  }
  function toggleHost12Panel(force){
    createHost12Panel();
    const isHidden=host12Panel.style.display==='none'||!host12Panel.style.display;
    const show=typeof force==='boolean'?force:isHidden;
    try{ if(show && GMGet('bnc_host_mode', false)!==true){ showToast('Host mode off — enable bnc_host_mode in Violentmonkey to unlock', true); return; } }catch{} if(show && !hostPanelsAllowed()){ showToast('Host controls need a tourney room', true); return; }
    try{ clearTimeout(_hostFadeT); }catch{}
    if(show){
      host12Panel.style.display='flex';
      requestAnimationFrame(()=>{ requestAnimationFrame(()=>{ try{ host12Panel.style.opacity='1'; }catch{} }); });
      try{
        if(!hostCompCtx() && typeof hostRankedCtx==='function' && hostRankedCtx()){ renderHost12Ranked(); return; }
      }catch{}
      const tid=hostTourneyId(); if(tid!=null){ rankedApiGet('/api/comp/tourney?id='+encodeURIComponent(tid),(j)=>{ _hostTourneyCache=j||null; renderHost12Panel(); }); } else renderHost12Panel();
    }
    else { try{ host12Panel.style.opacity='0'; }catch{} _hostFadeT=setTimeout(()=>{ try{ host12Panel.style.display='none'; }catch{} }, 180); }
  }
  // --- RANKED CONTROLS VARIANT (F12 in a ranked room, no comp context) ---
  // Gold-themed host controls: backout to lobby + end room (host-only API;
  // start/restart/fees stay on the /ranked bot commands, which own
  // eligibility, fees and threads). Sealing is automatic. Never throws.
  function renderHost12Ranked(){
    const body=document.getElementById('bnc-host12-body'); if(!body) return;
    const ctx=(typeof hostRankedCtx==='function') ? hostRankedCtx() : null;
    try{ const _t=document.getElementById('bnc-host12-title'); if(_t) _t.innerHTML='<span style="background:linear-gradient(135deg,#FFD700,#f59e0b);-webkit-background-clip:text;background-clip:text;color:transparent">Ranked controls</span>'; }catch{}
    const sub=document.getElementById('bnc-host12-sub');
    if(sub) sub.textContent=ctx?`${ctx.code||'Ranked'} · ${ctx.status||''} · ${ctx.rid}`:'—';
    const btn=(id,label,gold)=>`<button id="${id}" style="background:${gold?'linear-gradient(135deg,#FFD700,#f59e0b)':'#1f2937'};color:${gold?'#000':'#e5e7eb'};border:0;border-radius:6px;padding:6px 12px;cursor:pointer;font-size:12px;font-weight:${gold?'800':'400'};margin:0 6px 6px 0">${label}</button>`;
    body.innerHTML=
      `<div style="${RANKED_GOLD_BAR}"><span style="color:#e5e7eb">Seals are automatic at match end. Use these for room lifecycle.</span></div>`+
      `<div style="margin-bottom:6px">${btn('h12-rBackout','Backout to lobby',true)}${btn('h12-rEnd','End room',false)}</div>`+
      `<div style="margin-bottom:6px">${inp('h12-rWinner','winner name or id (blank clears)')}${btn('h12-rWinGo','Set winner')}${btn('h12-rRecalc','Recalc')}</div>`+
      `<div style="color:#9ca3af;font-size:12px">Start, restart and fees stay on the <b>/ranked</b> Discord commands. Sealed matches stand through a backout. Recalc recomputes the latest sealed match from banked rows.</div>`;
    host12Say('');
    const q=(id)=>document.getElementById(id);
    const did=()=>{ try{ return myDiscordId(); }catch{ return null; } };
    const needCtx=()=>{ const c=(typeof hostRankedCtx==='function')?hostRankedCtx():null; if(!c){ host12Say('No ranked context.', true); return null; } return c; };
    q('h12-rBackout').onclick=()=>{
      const c=needCtx(); if(!c) return;
      const d=did(); if(!d){ host12Say('Link your Discord account first.', true); return; }
      host12Say('Backing out…');
      hostApiPost('/api/ranked/room/backout',{room_id:c.rid,discord_id:d},(j)=>{
        host12Say(j&&!j.error?'Backed out — room is lobby. Restart via /ranked start.':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error));
      });
    };
    q('h12-rEnd').onclick=()=>{
      const c=needCtx(); if(!c) return;
      const d=did(); if(!d){ host12Say('Link your Discord account first.', true); return; }
      host12Say('Ending…');
      hostApiPost('/api/ranked/room/end',{room_id:c.rid,discord_id:d},(j)=>{
        host12Say(j&&!j.error?'Ended.':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error));
      });
    };
    const lastSealedMver=()=>{ try{ const lr=((statsCache||{}).ranked||{}).last_result; return (lr&&lr.match_version!=null)?Number(lr.match_version):null; }catch{ return null; } };
    q('h12-rWinGo').onclick=()=>{
      const c=needCtx(); if(!c) return;
      const d=did(); if(!d){ host12Say('Link your Discord account first.', true); return; }
      const mv=lastSealedMver(); if(mv==null){ host12Say('No sealed match yet.', true); return; }
      const raw=(q('h12-rWinner').value||'').trim()||null;
      const send=(wdid)=>{
        host12Say('Setting winner…');
        hostApiPost('/api/ranked/match/winner',{room_id:c.rid,match_version:mv,discord_id:d,winner_discord_id:wdid},(j)=>{
          host12Say(j&&!j.error?'Winner set — ledger recalculated.':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error));
        });
      };
      if(!raw){ send(null); return; }
      if(/^\d+$/.test(raw)){ send(raw); return; }
      rankedApiGet('/api/ranked/match?room_id='+encodeURIComponent(c.rid)+'&match_version='+encodeURIComponent(mv),(j)=>{
        try{
          const b=(j&&j.board)||[]; const low=raw.toLowerCase();
          const hit=b.find(r=>String(r.discord_id)===raw)||b.find(r=>String(r.name||'').toLowerCase()===low);
          if(!hit){ host12Say('Unknown player — use the F11 name or id.', true); return; }
          send(String(hit.discord_id));
        }catch{ host12Say('Resolve failed.', true); }
      });
    };
    q('h12-rRecalc').onclick=()=>{
      const c=needCtx(); if(!c) return;
      const d=did(); if(!d){ host12Say('Link your Discord account first.', true); return; }
      const mv=lastSealedMver(); if(mv==null){ host12Say('No sealed match yet.', true); return; }
      host12Say('Recalculating…');
      hostApiPost('/api/ranked/match/recalc',{room_id:c.rid,match_version:mv,discord_id:d},(j)=>{
        if(j&&!j.error){
          const df=j.diffs||{}; const kl=Object.keys(df);
          if(!kl.length){ host12Say('Recalc done - no changes.'); return; }
          const parts=[];
          for(const key of kl){ parts.push(key+':'+(df[key]>0?'+':'')+df[key]); }
          host12Say('Recalc done - ledger moved ('+parts.join(', ')+').');
        } else host12Say('Failed: '+((j&&j.error)||'no response'), true);
      });
    };
  }
  // --- LEADERBOARD PANEL: Alt+Shift+L — all players, any time ---
  // Standalone overlay (same card pattern as the ranked panel): sortable
  // columns (points/kills/wins/K-D/playtime), paginated, Esc/backdrop closes.
  // (insights/friends/events/ranked tabs removed; fetchers below kept as dead code)
  function getLbKeybind(){
    try{
      const raw=GMGet('bnc_lb_keybind', null);
      if(raw && typeof raw==='object' && raw.key) return raw;
      if(typeof raw==='string' && raw){
        try{ const j=JSON.parse(raw); if(j && j.key) return j; }catch{}
        const parts=String(raw).split('+');
        const kb={ctrl:false,shift:false,alt:false,meta:false,key:'F4'};
        for(const p of parts){ const l=p.trim().toLowerCase(); if(l==='ctrl'||l==='control') kb.ctrl=true; else if(l==='cmd'||l==='meta'||l==='command') kb.meta=true; else if(l==='shift') kb.shift=true; else if(l==='alt') kb.alt=true; else if(p.trim()) kb.key=p.trim().toUpperCase(); }
        return kb;
      }
    }catch{}
    return {ctrl:false, shift:false, alt:false, meta:false, key:'F4'};
  }
  function isLbKeybind(e){
    return bindMatches(e, getLbKeybind());
  }
  // Text inputs inside game-page overlays: the game swallows keys otherwise
  // (character insertion dies even though clicks work). Capture-phase trap
  // on the input itself + forced focus + no-autocomplete. The focus call is
  // deferred a tick: shooter games re-focus their canvas inside the same
  // click, so focusing synchronously loses the race every time. Never throws.
  // Applied to every panel text field (friend search, chat). Does NOT affect
  // the global keybind dispatcher (that one ignores inputs already).
  function untrapInput(el){
    if(!el || el.dataset.bncUntrapped) return;
    try{ el.dataset.bncUntrapped='1'; }catch{}
    try{ el.setAttribute('autocomplete','off'); }catch{}
    try{ el.setAttribute('autocapitalize','off'); }catch{}
    try{ el.setAttribute('spellcheck','false'); }catch{}
    const trap=(e)=>{
      try{
        // one-line remote diagnosis: if the game preventDefaults at
        // window/document capture, defaultPrevented is already true here and
        // NO element-level trick can restore typing (hence the keyboard below).
        if(e && e.type==='keydown' && !el.dataset.bncVkDiag){
          try{ el.dataset.bncVkDiag='1'; }catch{}
          try{ logInfo(`[INPUT] first key id=${el.id||'?'} key=${e.key} defaultPrevented=${!!e.defaultPrevented}`); }catch{}
        }
      }catch{}
      try{ e.stopPropagation(); }catch{} try{ if(e.stopImmediatePropagation) e.stopImmediatePropagation(); }catch{}
    };
    ['keydown','keyup','keypress'].forEach(ev=>{
      try{ el.addEventListener(ev, trap, true); }catch{}
    });
    const grab=(e)=>{
      try{ e.stopPropagation(); }catch{}
      try{ el.focus(); }catch{}
      try{ setTimeout(()=>{ try{ if(document.activeElement!==el) el.focus(); }catch{} }, 0); }catch{}
      try{ untrapHold(el); }catch{}
    };
    try{
      el.addEventListener('mousedown', grab, true);
      el.addEventListener('click', grab, true);
    }catch{}
  }
  // Focus hold: shooter pages steal focus back to their canvas after every
  // click (no cursor ever blinks). After an explicit engage, re-assert focus
  // on a 250ms tick while the panel context is still valid. Releases on:
  // legit in-panel navigation, Escape, tab switch, panel close, 60s timeout.
  // One console line per engage for remote diagnosis. Never throws.
  let _untrapHoldEl=null, _untrapHoldUntil=0, _untrapHoldTimer=null, _untrapHoldLogged=false;
  let _untrapFwdOn=false, _untrapFwdLogged=false;
  function untrapHoldActive(){
    try{
      return !!(_untrapHoldEl && document.contains(_untrapHoldEl) && Date.now()<=_untrapHoldUntil
        && lbTab==='friends' && lbPanel && lbPanel.style.display!=='none');
    }catch{ return false; }
  }
  // Key forwarder: while the user is composing in our box, steal printable
  // keys at document capture, block the game (preventDefault + stop), and
  // inject them into the input as values. Focus races become irrelevant:
  // the characters land even when the canvas holds focus. Single-char keys
  // + Backspace only; shortcuts, Tab, Escape and IME pass through untouched.
  // Never throws. Registered once.
  function untrapForward(e){
    try{
      if(!untrapHoldActive()) return;
      const el=_untrapHoldEl;
      if(e.ctrlKey || e.metaKey || e.altKey) return;
      if(e.isComposing) return;
      const k=e.key;
      const put=(fn)=>{
        try{ e.preventDefault(); }catch{}
        try{ e.stopPropagation(); }catch{}
        try{ if(e.stopImmediatePropagation) e.stopImmediatePropagation(); }catch{}
        try{ fn(); }catch{}
        try{
          if(!_untrapFwdLogged){
            _untrapFwdLogged=true;
            try{ logInfo(`[INPUT] forward inject id=${el.id||'?'}`); }catch{}
          }
        }catch{}
      };
      if(k==='Enter'){
        put(()=>{ try{ if(typeof el._vkbdAction==='function') el._vkbdAction(); }catch{} });
        return;
      }
      if(k==='Backspace'){
        put(()=>{
          const v=el.value||'';
          let s=(document.activeElement===el && el.selectionStart!=null)?el.selectionStart:v.length;
          let t=(document.activeElement===el && el.selectionEnd!=null)?el.selectionEnd:v.length;
          s=Math.max(0,Math.min(s,v.length)); t=Math.max(0,Math.min(t,v.length));
          if(s===t) s=Math.max(0,s-1);
          el.value=v.slice(0,s)+v.slice(t);
          try{ el.focus(); }catch{}
          try{ el.setSelectionRange(s,s); }catch{}
          el.dispatchEvent(new Event('input',{bubbles:true}));
        });
        return;
      }
      if(k && k.length===1){
        put(()=>{
          const v=el.value||'';
          let s=(document.activeElement===el && el.selectionStart!=null)?el.selectionStart:v.length;
          let t=(document.activeElement===el && el.selectionEnd!=null)?el.selectionEnd:v.length;
          s=Math.max(0,Math.min(s,v.length)); t=Math.max(0,Math.min(t,v.length));
          el.value=v.slice(0,s)+k+v.slice(t);
          try{ el.focus(); }catch{}
          try{ el.setSelectionRange(s+1,s+1); }catch{}
          el.dispatchEvent(new Event('input',{bubbles:true}));
        });
        return;
      }
    }catch{}
  }
  function untrapHold(el){
    try{
      _untrapHoldEl=el; _untrapHoldUntil=Date.now()+60000; _untrapHoldLogged=false;
      try{ logInfo(`[INPUT] hold engage id=${el.id||'?'} active=${document.activeElement===el}`); }catch{}
      if(!_untrapFwdOn){
        _untrapFwdOn=true;
        try{ document.addEventListener('keydown', untrapForward, true); }catch{}
      }
      if(_untrapHoldTimer) return;
      _untrapHoldTimer=setInterval(()=>{
        try{
          const el2=_untrapHoldEl;
          if(!el2 || Date.now()>_untrapHoldUntil){ _untrapHoldEl=null; return; }
          // context still valid? friends tab open with this input in DOM
          if(!document.contains(el2)){ _untrapHoldEl=null; return; }
          if(lbTab!=='friends' || !lbPanel || lbPanel.style.display==='none'){ _untrapHoldEl=null; return; }
          if(document.activeElement===el2) return;
          // legit navigation inside the panel releases (don't trap the user)
          const ae=document.activeElement;
          if(ae && ae!==document.body && lbPanel.contains(ae)){ _untrapHoldEl=null; return; }
          try{ el2.focus(); }catch{}
          if(!_untrapHoldLogged){
            _untrapHoldLogged=true;
            try{ logInfo(`[INPUT] hold re-assert id=${el2.id||'?'}`); }catch{}
          }
        }catch{}
      }, 250);
    }catch{}
  }
  function untrapRelease(){
    try{ _untrapHoldEl=null; _untrapHoldUntil=0; }catch{}
  }
  // On-screen keyboard for panel text fields (search, chat): clicks always
  // reach the panel, so typing via taps works even when the game eats every
  // physical keystroke (defaultPrevented at window capture — see diagnosis
  // above, which no element-level handler can undo). One ⌨ toggle per input,
  // grid sits below its row, Enter triggers the input's own action. Never throws.
  function attachVkbd(input, onEnter){
    try{
      if(!input || input.dataset.bncVkbd) return;
      input.dataset.bncVkbd='1';
      const toggle=document.createElement('button');
      toggle.textContent='⌨';
      toggle.title='On-screen keyboard';
      toggle.style.cssText='background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:8px;padding:7px 10px;cursor:pointer;font-size:13px;flex-shrink:0';
      const grid=document.createElement('div');
      grid.style.cssText='display:none;flex-direction:column;gap:4px;width:100%;padding:6px 0 2px';
      const rows=['qwertyuiop','asdfghjkl','zxcvbnm','1234567890'];
      const press=(ch)=>{
        try{ input.focus(); }catch{}
        try{
          const s=input.selectionStart==null?input.value.length:input.selectionStart;
          const e2=input.selectionEnd==null?input.value.length:input.selectionEnd;
          const v=input.value||'';
          if(ch==='\b'){ input.value=v.slice(0,Math.max(0,s-1))+v.slice(e2); }
          else { input.value=v.slice(0,s)+ch+v.slice(e2); }
          const pos=ch==='\b'?Math.max(0,s-1):s+ch.length;
          try{ input.setSelectionRange(pos,pos); }catch{}
          input.dispatchEvent(new Event('input',{bubbles:true}));
        }catch{}
      };
      rows.forEach((rk)=>{
        const r=document.createElement('div');
        r.style.cssText='display:flex;gap:3px;justify-content:center';
        [...rk].forEach((ch)=>{
          const b=document.createElement('button');
          b.textContent=ch;
          b.style.cssText='flex:1;max-width:30px;background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:6px 0;cursor:pointer;font-size:12px';
          b.onclick=(ev)=>{ try{ if(ev) ev.stopPropagation(); }catch{} press(ch); };
          r.appendChild(b);
        });
        grid.appendChild(r);
      });
      const last=document.createElement('div');
      last.style.cssText='display:flex;gap:3px;justify-content:center';
      [['space',' ',4],['⌫','\b',2],['Enter','\n',2],['Hide','',2]].forEach(([label,ch,flex])=>{
        const b=document.createElement('button');
        b.textContent=label;
        b.style.cssText=`flex:${flex};background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:6px 0;cursor:pointer;font-size:12px`;
        b.onclick=(ev)=>{
          try{ if(ev) ev.stopPropagation(); }catch{}
          if(label==='Hide'){ grid.style.display='none'; return; }
          if(label==='Enter'){ try{ onEnter&&onEnter(); }catch{} return; }
          press(ch);
        };
        last.appendChild(b);
      });
      grid.appendChild(last);
      toggle.onclick=(ev)=>{
        try{ if(ev) ev.stopPropagation(); }catch{}
        grid.style.display=(grid.style.display==='none'||!grid.style.display)?'flex':'none';
        try{ input.focus(); }catch{}
      };
      const row=input.parentElement;
      if(row){ row.appendChild(toggle); row.parentElement.insertBefore(grid, row.nextSibling); }
      ['mousedown','click'].forEach((ev)=>{
        try{
          grid.addEventListener(ev, (e)=>{ try{ e.stopPropagation(); }catch{} }, true);
          toggle.addEventListener(ev, (e)=>{ try{ e.stopPropagation(); }catch{} }, true);
        }catch{}
      });
    }catch{}
  }
  // --- TOURNEY HUD: passthrough point counter, visible only while holding
  // a pID-tagged tourney room. Data rides the stats poll (comp_tourney);
  // the overlay itself never captures clicks/keys (pointer-events none),
  // so the game stays fully playable underneath it.
  let tourneyHud=null;
  // Local comp-room detection (join-time, faster than the stats poll): the
  // room pID is checked against open comp tourneys once per join, like the
  // ranked tracker. Cleared on auth/leave with the room flag.
  let _compRoom=null, _compHudData=null, _publicRoom=false;
  // F7 host-backout reset: drop the local comp-room binding (backed out to
  // lobby holding no code). No server call — the next pID join re-resolves
  // the same code fresh via fetchCompRoom. Never throws.
  function resetCompRoomLocal(){
    try{ _compRoom=null; }catch{}
    try{ _compHudData=null; }catch{}
    try{ _publicRoom=false; }catch{}
    try{ updateTourneyHud(); }catch{}
    try{ showToast('Comp room cleared (F7) — re-set or rejoin the code', false); }catch{}
    try{ logInfo('[COMP] room binding cleared via F7 (host backout, no code)'); }catch{}
  }
  function fetchCompRoom(code){
    try{
      if(_publicRoom) return; // public matchmaking: never resolve
      code=String(code||'').trim().toUpperCase();
      if(!code || !/^[A-Z0-9]{5}$/.test(code)) return;
      if(_compRoom && _compRoom.code===code && Date.now()-_compRoom.at<600000) return; // fresh
      rankedApiGet(`/api/comp/tourney/by-code?code=${encodeURIComponent(code)}`, (j)=>{
        try{
          const t=j&&j.tourney;
          if(t && t.id!=null){
            _compRoom={code, id:t.id, name:t.name||'Tourney', at:Date.now()};
            try{ logInfo(`[COMP] room ${code} -> tourney #${t.id}`); }catch{}
            fetchCompHud();
          } else if(_compRoom && _compRoom.code===code){
            _compRoom=null; _compHudData=null;
          }
          updateTourneyHud();
        }catch{}
      });
    }catch{}
  }
  function fetchCompHud(){
    try{
      const cr=_compRoom;
      if(!cr) return;
      let did=null;
      try{ did=myDiscordId(); }catch{}
      rankedApiGet(`/api/comp/tourney?id=${encodeURIComponent(cr.id)}`, (j)=>{
        try{
          const b=(j&&j.standings)||[];
          const me=b.findIndex(r=>did && String(r.discord_id)===String(did));
          const row=me>=0?b[me]:null;
          _compHudData=row?{name:cr.name, points:row.points||0, kills:row.kills||0, wins:row.wins||0,
            rank:me+1, players:b.length}:{name:cr.name, points:0, kills:0, wins:0, rank:null, players:b.length};
          updateTourneyHud();
        }catch{}
      });
    }catch{}
  }
  function ensureTourneyHud(){
    if(!IS_GAME_FRAME) return null;
    if(tourneyHud) return tourneyHud;
    if(document.getElementById('bnc-tourney-hud')){ tourneyHud=document.getElementById('bnc-tourney-hud'); return tourneyHud; }
    const el=document.createElement('div');
    el.id='bnc-tourney-hud';
    Object.assign(el.style,{position:'fixed', left:'40%', bottom:'14px', zIndex:'2147483639', display:'none', flexDirection:'column', alignItems:'flex-start', gap:'2px', background:'rgba(0,0,0,0.72)', border:'1px solid rgba(255,255,255,0.12)', borderRadius:'10px', padding:'8px 14px', fontFamily:'Inter, system-ui, sans-serif', pointerEvents:'none', textAlign:'left', whiteSpace:'nowrap'});
    (document.body||document.documentElement).appendChild(el);
    tourneyHud=el;
    return el;
  }
  function updateTourneyHud(){
    const el=ensureTourneyHud();
    if(!el) return;
    // hidden in public rooms (pid-less matchmaking, never a tourney).
    // Otherwise show whenever we have tourney data: either from the local
    // pID join (_compRoom) OR from the server stats poll (comp_tourney).
    if(_publicRoom){ el.style.display='none'; return; }
    let d=null;
    try{ d=(statsCache&&statsCache.comp_tourney)||null; }catch{}
    // hide the moment we're no longer holding that room: fresh stats with
    // no block, or a different/no code, both mean left.
    let sameRoom=false;
    try{
      const cur=(statsCache&&statsCache.session&&statsCache.session.short_code)||null;
      sameRoom=!!(d&&cur);
    }catch{}
    if((!d || !sameRoom) && _compRoom){
      // local fallback: join-detected comp room, stats tag not there yet
      // (or tag missing) — show what we know until stats confirm or deny.
      try{
        const cur2=(statsCache&&statsCache.session&&statsCache.session.short_code)||null;
        const fresh=Date.now()-_compRoom.at<600000;
        if(fresh && cur2 && String(cur2).toUpperCase()===String(_compRoom.code).toUpperCase()){
          d=_compHudData||{name:_compRoom.name, points:0, kills:0, wins:0, rank:null, players:0};
          sameRoom=true;
        } else if(!fresh || (cur2 && String(cur2).toUpperCase()!==String(_compRoom.code).toUpperCase())){
          _compRoom=null; _compHudData=null;
        }
      }catch{}
    }
    if(!d || !sameRoom){
      el.style.display='none';
      return;
    }
    const escS=(v)=> String(v==null?'—':v).replace(/</g,'&lt;');
    const pts=d.points!=null?Number(d.points).toLocaleString('en-US'):'—';
    // bare counter: just the number. No name, no rank, no K/W, no "pts".
    el.innerHTML=`<div style="font-size:23px;font-weight:800;color:#fff">${escS(pts)}</div>`;
    el.style.display='flex';
    try{ updateCompToast(d); }catch{}
  }
  // --- COMP TOASTS: ELIMINATION / VICTORY / PLACEMENT +X Points (left-slide,
  // same queue as ranked). Fires on tourney-point increases within one
  // tourney: wins up -> VICTORY, kills up -> ELIMINATION, else PLACEMENT.
  let _prevCompSnap=null;
  function updateCompToast(d){
    const snap=(d && d.tourney_id!=null)
      ? {tid:String(d.tourney_id), pts:Number(d.points)||0, kills:Number(d.kills)||0, wins:Number(d.wins)||0}
      : {tid:'', pts:0, kills:0, wins:0};
    const prev=_prevCompSnap;
    _prevCompSnap=snap;
    if(!prev) return;  // first sighting: baseline only
    if(!IS_GAME_FRAME) return;  // track baseline everywhere, toast in game only
    if(!snap.tid || snap.tid!==prev.tid) return;  // left room / new tourney: re-baseline silently
    const delta=snap.pts-prev.pts;
    if(!(delta>0)) return;
    const label=(snap.wins>prev.wins) ? 'VICTORY' : ((snap.kills>prev.kills) ? 'ELIMINATION' : 'PLACEMENT');
    queuePtsToast(label, delta, {key:`comp:${snap.tid}:${snap.pts}`});
  }
  // --- RANKED-TOURNEY HUD (comp parity, gold identity) ---
  // Same contract as the comp trio: join-time by-code resolve with 10-min
  // memory (_rtRoom) + server stats `ranked_tourney` block confirmer.
  // Priority: comp room first, ranked room second, ranked tourney last —
  // this HUD shows only when neither of the first two claims the session.
  // Counter is comp-bare (the number, nothing else) with a gold accent;
  // toasts reuse the shared queue with their own baseline. Never throws.
  let _rtRoom=null, _rtHudData=null, _prevRtSnap=null, rtHud=null;
  function resetRtRoomLocal(){
    try{ _rtRoom=null; }catch{}
    try{ _rtHudData=null; }catch{}
    try{ _prevRtSnap=null; }catch{}
    try{ updateRtHud(); }catch{}
  }
  function fetchRtRoom(code){
    try{
      if(_publicRoom) return; // public matchmaking: never resolve
      code=String(code||'').trim().toUpperCase();
      if(!code || !/^[A-Z0-9]{5}$/.test(code)) return;
      if(_rtRoom && _rtRoom.code===code && Date.now()-_rtRoom.at<600000) return; // fresh
      rankedApiGet(`/api/ranked/tourney/by-code?code=${encodeURIComponent(code)}`, (j)=>{
        try{
          const t=j&&j.tourney;
          if(t && t.id!=null){
            _rtRoom={code, id:t.id, name:t.name||'Tourney', at:Date.now()};
            try{ logInfo(`[RT] room ${code} -> tourney #${t.id}`); }catch{}
            fetchRtHud();
          } else if(_rtRoom && _rtRoom.code===code){
            _rtRoom=null; _rtHudData=null;
          }
          updateRtHud();
        }catch{}
      });
    }catch{}
  }
  function fetchRtHud(){
    try{
      const cr=_rtRoom;
      if(!cr) return;
      let did=null;
      try{ did=myDiscordId(); }catch{}
      rankedApiGet(`/api/ranked/tourney?id=${encodeURIComponent(cr.id)}`, (j)=>{
        try{
          const b=(j&&j.standings)||[];
          const me=b.findIndex(r=>did && String(r.discord_id)===String(did));
          const row=me>=0?b[me]:null;
          _rtHudData=row?{tourney_id:cr.id, name:cr.name, points:row.points||0, kills:row.kills||0, wins:row.wins||0,
            rank:me+1, players:b.length}:{tourney_id:cr.id, name:cr.name, points:0, kills:0, wins:0, rank:null, players:b.length};
          updateRtHud();
        }catch{}
      });
    }catch{}
  }
  function ensureRtHud(){
    if(!IS_GAME_FRAME) return null;
    if(rtHud) return rtHud;
    if(document.getElementById('bnc-rt-hud')){ rtHud=document.getElementById('bnc-rt-hud'); return rtHud; }
    const el=document.createElement('div');
    el.id='bnc-rt-hud';
    Object.assign(el.style,{position:'fixed', left:'40%', bottom:'64px', zIndex:'2147483638', display:'none', flexDirection:'column', alignItems:'flex-start', gap:'2px', background:'rgba(0,0,0,0.72)', border:'1px solid rgba(255,215,0,0.35)', borderRadius:'10px', padding:'8px 14px', fontFamily:'Inter, system-ui, sans-serif', pointerEvents:'none', textAlign:'left', whiteSpace:'nowrap'});
    (document.body||document.documentElement).appendChild(el);
    rtHud=el;
    return el;
  }
  // Least-specific surfaces hide first: comp room, then ranked room, and
  // only then may the ranked-tourney counter show. Mirrors updateTourneyHud
  // otherwise: stats block first, fresh local memory as fallback.
  function updateRtHud(){
    const el=ensureRtHud();
    if(!el) return;
    if(_publicRoom){ el.style.display='none'; return; }
    let d=null;
    try{ d=(statsCache&&statsCache.ranked_tourney)||null; }catch{}
    let sameRoom=false;
    try{
      const cur=(statsCache&&statsCache.session&&statsCache.session.short_code)||null;
      sameRoom=!!(d&&cur);
    }catch{}
    if((!d || !sameRoom) && _rtRoom){
      try{
        const cur2=(statsCache&&statsCache.session&&statsCache.session.short_code)||null;
        const fresh=Date.now()-_rtRoom.at<600000;
        if(fresh && cur2 && String(cur2).toUpperCase()===String(_rtRoom.code).toUpperCase()){
          d=_rtHudData||{tourney_id:_rtRoom.id, name:_rtRoom.name, points:0, kills:0, wins:0, rank:null, players:0};
          sameRoom=true;
        } else if(!fresh || (cur2 && String(cur2).toUpperCase()!==String(_rtRoom.code).toUpperCase())){
          _rtRoom=null; _rtHudData=null;
        }
      }catch{}
    }
    if(!d || !sameRoom){
      el.style.display='none';
      return;
    }
    // Toast edges fire even when the chip itself is hidden: on tourney
    // sessions the synthesized ranked block makes liveRanked() claim the
    // room (ranked lobby card/overlay own the display), but the seal deltas
    // here are still the only toast source. Hide first would mute
    // VICTORY/ELIMINATION/PLACEMENT toasts entirely.
    try{ updateRtToast(d); }catch{}
    // Session end: sealed == series_len (and len > 0) raises the end
    // overlay once per tourney — players otherwise never learn the series
    // is over until the host stops it.
    try{ updateRankedSessionEnd(d); }catch{}
    try{ if(typeof _inCompRoom==='function' && _inCompRoom()){ el.style.display='none'; return; } }catch{}
    // Ranked-session economy: no session-standings counter anywhere — the
    // ranked overlay and lobby card show the ladder total, toasts/embeds
    // report what each match paid. Toast edges above still fire.
    el.style.display='none';
    return;
  }
  // --- RANKED SESSION END: series complete overlay (once per tourney) ---
  let _sessionEndShown={}, _sessionEndTimer=null;
  function _dismissSessionEnd(){
    try{ clearTimeout(_sessionEndTimer); }catch{}
    _sessionEndTimer=null;
    try{ const el=document.getElementById('bnc-ranked-end'); if(el) el.remove(); }catch{}
  }
  function updateRankedSessionEnd(d){
    if(!IS_GAME_FRAME) return;
    const tid=d && d.tourney_id!=null ? String(d.tourney_id) : '';
    if(!tid || _sessionEndShown[tid]) return;
    const series=Number((d && d.series_len)||0), sealed=Number((d && d.sealed)||0);
    if(!(series>0 && sealed>=series)) return;
    _sessionEndShown[tid]=true;
    if(document.getElementById('bnc-ranked-end')) return;
    let total=null;
    try{
      const sc=statsCache||{}, ar=sc.arena||null;
      total=(ar && ar.points!=null) ? Number(ar.points) : null;
    }catch{}
    try{ logInfo(`[RANKED] session-end SHOW tid=${tid} sealed=${sealed}/${series}`); }catch{}
    const ov=document.createElement('div');
    ov.id='bnc-ranked-end';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483643', background:'rgba(0,0,0,0.72)', display:'flex', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif', cursor:'pointer'});
    ov.innerHTML=`<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:16px;padding:28px;width:420px;max-width:92vw;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,0.5)"><div style="font-size:20px;font-weight:800;color:#fff">Ranked Session ended</div><div style="font-size:13px;color:#e5e7eb;line-height:1.6;margin-top:10px">Series complete — ${sealed}/${series} matches sealed${total!=null?`<br>Your total: <b>${total.toLocaleString('en-US')}</b> pts`:''}</div><div style="font-size:11px;color:#6b7280;margin-top:14px">host: /ranked stop to close · click anywhere to close</div></div>`;
    ov.addEventListener('click', ()=> _dismissSessionEnd());
    try{ (document.body||document.documentElement).appendChild(ov); }catch{ return; }
    _sessionEndTimer=setTimeout(()=> _dismissSessionEnd(), 8000);
  }
  // --- RT TOASTS: same rule as comp (wins -> VICTORY, kills ->
  // ELIMINATION, else PLACEMENT), own baseline per tourney. Shared queue.
  // Seals with a stored breakdown (frozen dynamic or room-seal bundle)
  // queue one toast per nonzero component instead of the single delta —
  // same shape as ranked rooms, exactly-once per version+component.
  let _rtSealDone={};
  function updateRtToast(d){
    const snap=(d && d.tourney_id!=null)
      ? {tid:String(d.tourney_id), pts:Number(d.points)||0, kills:Number(d.kills)||0, wins:Number(d.wins)||0}
      : {tid:'', pts:0, kills:0, wins:0};
    const prev=_prevRtSnap;
    _prevRtSnap=snap;
    if(!prev) return;  // first sighting: baseline only
    if(!IS_GAME_FRAME) return;  // track baseline everywhere, toast in game only
    if(!snap.tid || snap.tid!==prev.tid) return;  // left room / new tourney: re-baseline silently
    if(!RANKED_SEAL_TOASTS) return;  // v1: counters only
    // A fresh stored breakdown owns its seal (components, no delta toast).
    // An already-toasted breakdown falls through: a newer seal without one
    // still gets the legacy single toast.
    let _rtOwned=false;
    try{
      const lr=d && d.last_result, bd=lr && lr.breakdown;
      if(lr && bd && typeof bd==='object' && Number(lr.match_version)>0){
        const v=Number(lr.match_version);
        if(_rtSealDone[snap.tid]!==v){
          _rtSealDone[snap.tid]=v;
          const comps=[['ELIMINATIONS', Number(bd.kills)], ['VICTORY', Number(bd.win)],
                       ['PLACEMENT', Number(bd.placement)], ['DEFEAT', Number(bd.loss)]];
          for(const [title, val] of comps){
            if(!Number.isFinite(val) || val===0) continue;
            queuePtsToast(title, val, {key:`rt:${snap.tid}:${v}:${lr.gained}:${title}`, noCoalesce:true, dwell:2400});
          }
          if(bd.capped===true && Number.isFinite(Number(lr.gained))){
            queuePtsToast('MATCH TOTAL (capped)', Number(lr.gained), {key:`rt:${snap.tid}:${v}:${lr.gained}:TOTAL`, noCoalesce:true, dwell:2800});
          }
          try{ logInfo(`[RANKED] rt-toast components v=${v} gained=${lr.gained}`); }catch{}
          _rtOwned=true;
        }
      }
    }catch{}
    if(_rtOwned) return;  // baseline already tracked above, no delta toast
    const delta=snap.pts-prev.pts;
    if(!Number.isFinite(delta) || delta===0) return;
    if(delta<0){
      queuePtsToast('DEFEAT', delta, {key:`rt:${snap.tid}:${snap.pts}`});
      return;
    }
    const label=(snap.wins>prev.wins) ? 'VICTORY' : ((snap.kills>prev.kills) ? 'ELIMINATION' : 'PLACEMENT');
    queuePtsToast(label, delta, {key:`rt:${snap.tid}:${snap.pts}`});
  }
  // Party-invite gate: pID required, plus tourney rooms and mid-match both
  // block invites (locked by comp rules). Single choke point for every
  // Invite button in drawer + panel.
  function canPartyInvite(){
    try{
      const sess=statsCache&&statsCache.session;
      const code=sess&&sess.short_code;
      if(!code) return {ok:false, code:null, why:'Join a private room first (your pID shows here)'};
      const tag=sess&&sess.tag;
      if(tag && String(tag.kind||'')==='tourney') return {ok:false, code:null, why:'Invites off in tourney rooms'};
      // Same match authority as everything else, so a stale server 'match'
      // cannot keep invites disabled after the player is back in the lobby.
      if(effInMatch()===true) return {ok:false, code:null, why:'Invites off mid-match'};
      return {ok:true, code:String(code).toUpperCase(), why:''};
    }catch{ return {ok:false, code:null, why:'Unavailable'}; }
  }
  // Plain JSON POST (no session tokens — friends/party API is discord_id-keyed).
  function apiPostJson(path, body, cb){
    let base=''; try{ base=resolveApiBase(); }catch{}
    if(!base){ try{cb(null);}catch{} return; }
    const url=base+path;
    const done=(ok,txt)=>{
      let j=null;
      try{ j=(ok && txt) ? JSON.parse(txt) : null; }catch{ j=null; }
      try{ cb(j); }catch{}
    };
    try{
      const GM=typeof GM_xmlhttpRequest!=='undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
      if(GM){ GM({method:'POST', url, data:JSON.stringify(body||{}), headers:{'Content-Type':'application/json'}, timeout:10000, onload:r=>done(r.status>=200&&r.status<300, r.responseText), onerror:()=>done(false,''), ontimeout:()=>done(false,'')}); return; }
    }catch{}
    fetch(url,{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})}).then(r=>r.text().then(t=>done(r.ok,t)).catch(()=>done(false,''))).catch(()=>done(false,''));
  }
  function myDiscordId(){
    try{ return (statsCache&&statsCache.discord_id)||GMGet('bnc_discord_id',GMGet('bn_discord_id',null))||null; }catch{ return null; }
  }
  // Live private-room code I hold right now (invite gating). Mirrors the
  // stats short_code: current room's pID, never the lobby pseudo-room.
  function myLiveCode(){
    try{
      const sess=statsCache&&statsCache.session;
      if(!sess||!sess.rooms) return null;
      const cur=sess.rooms[sess.current_room_id]||null;
      if(!cur||String(cur.room_id||'')==='lobby') return null;
      const c=String(cur.short_code||'').trim().toUpperCase();
      return (c.length===5)?c:null;
    }catch{ return null; }
  }
  function copyText(t, okMsg){
    const done=()=>{ try{ showToast(okMsg||'Copied', false); }catch{} };
    try{
      if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(String(t)).then(done, done); return; }
    }catch{}
    try{
      const ta=document.createElement('textarea');
      ta.value=String(t); (document.body||document.documentElement).appendChild(ta);
      ta.select(); document.execCommand('copy'); ta.remove(); done();
    }catch{ try{ done(); }catch{} }
  }
  let lbPanel=null, lbSort='points', lbOrder='desc', lbOffset=0, lbRows=[], lbCount=0, lbLoading=false, _lbFadeT=null;
  let lbTab='board', lbIns=null, lbInsLoading=false; // 'board' | 'graphics' | 'presence' (insights/friends/events/ranked removed)
  let lbEvents=null, lbEventsLoading=false, lbEventFilter='all', lbEventDetail=null, lbEventLoading=false;
  let lbProfile=null, lbProfileIns=null, lbProfileLoading=false; // open player profile (within panel)
  let lbFriends=null, lbFriendsLoading=false, lbInvites=null, lbSearch=[], lbSearchLoading=false;
  let lbUnread=null, lbChatWith=null, lbChat=[], lbChatLoading=false, lbChatTimer=null, _chatRefocus=false;
  const LB_LIMIT=20;
  const LB_SORTS=[['points','Points'],['kills','Kills'],['wins','Wins'],['kd','K/D'],['playtime_global','Playtime'],['comp','Comp'],['pr','PR']];
  function createLbPanel(){
    if(document.getElementById('bnc-lb')){ lbPanel=document.getElementById('bnc-lb'); return; }
    const ov=document.createElement('div');
    ov.id='bnc-lb';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483646', background:'rgba(0,0,0,0.64)', display:'none', opacity:'0', transition:'opacity 180ms ease-out', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif'});
    ov.innerHTML=`<div id="bnc-lb-card" style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:14px;width:700px;max-width:94vw;height:500px;max-height:90vh;display:flex;flex-direction:column;overflow:hidden"><div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.08)"><div><b style="font-size:16px;color:#fff">Leaderboard</b><div id="bnc-lb-sub" style="font-size:11px;color:#9ca3af;margin-top:2px">All players</div></div><button id="bnc-lb-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px">Close</button></div><div id="bnc-lb-tabs" style="display:flex;flex-wrap:wrap;gap:8px;padding:12px 18px 0 18px"><button id="bnc-lb-tab-board" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Leaderboard</button><button id="bnc-lb-tab-gfx" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Graphics</button><button id="bnc-lb-tab-rpc" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Presence</button></div><div id="bnc-lb-sorts" style="display:flex;gap:8px;padding:12px 18px 0 18px;flex-wrap:wrap"></div><div id="bnc-lb-body" style="flex:1;overflow:auto;padding:16px 18px"></div><div id="bnc-lb-foot" style="display:flex;justify-content:space-between;align-items:center;padding:10px 18px;border-top:1px solid rgba(255,255,255,0.08)"><button id="bnc-lb-prev" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:5px 14px;cursor:pointer;font-size:12px">‹ Prev</button><span id="bnc-lb-page" style="font-size:11px;color:#9ca3af"></span><button id="bnc-lb-next" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:5px 14px;cursor:pointer;font-size:12px">Next ›</button></div></div>`;
    (document.body||document.documentElement).appendChild(ov);
    // visible focus ring on panel text inputs (proves focus landed)
    try{
      if(!document.getElementById('bnc-lb-input-style')){
        const st=document.createElement('style');
        st.id='bnc-lb-input-style';
        st.textContent='#bnc-fr-q:focus,#bnc-chat-in:focus{outline:2px solid #fff !important;border-color:#fff !important;}'
          + '#bnc-lb-tabs>button{flex:1 1 0;min-width:70px}';
        (document.head||document.documentElement).appendChild(st);
      }
    }catch{}
    // non-passthrough: game must not see clicks/wheel/keys inside the panel
    const card=ov.querySelector('#bnc-lb-card');
    ['mousedown','click','keydown'].forEach(ev=> card.addEventListener(ev, e=> e.stopPropagation()));
    card.addEventListener('wheel', e=> e.stopPropagation(), {passive:false});
    ov.addEventListener('click', (e)=>{ if(e.target===ov) toggleLbPanel(false); });
    ov.querySelector('#bnc-lb-close').onclick=()=> toggleLbPanel(false);
    ov.querySelector('#bnc-lb-tab-board').onclick=()=>{ lbTab='board'; lbProfile=null; try{ stopLbChat(); }catch{} try{ untrapRelease(); }catch{} renderLbPanel(); };
    // removed tabs: insights / friends / events / ranked
    ov.querySelector('#bnc-lb-tab-gfx').onclick=()=>{ lbTab='graphics'; try{ stopLbChat(); }catch{} try{ untrapRelease(); }catch{} renderLbPanel(); };
    ov.querySelector('#bnc-lb-tab-rpc').onclick=()=>{ lbTab='presence'; try{ stopLbChat(); }catch{} try{ untrapRelease(); }catch{} renderLbPanel(); try{ if(rpcCfg().enabled) rpcProbe(()=>{ renderLbRpc(); startRpcView(); }); else { renderLbRpc(); startRpcView(); } }catch{} };
    ov.querySelector('#bnc-lb-prev').onclick=()=>{ if(lbOffset>0){ lbOffset=Math.max(0, lbOffset-LB_LIMIT); fetchLb(); } };
    ov.querySelector('#bnc-lb-next').onclick=()=>{ lbOffset+=LB_LIMIT; fetchLb(); };
    lbPanel=ov;
  }
  function toggleLbPanel(force){
    createLbPanel();
    const isHidden=lbPanel.style.display==='none' || !lbPanel.style.display;
    const show=typeof force==='boolean' ? force : isHidden;
    try{ clearTimeout(_lbFadeT); }catch{}
    if(show){
      lbPanel.style.display='flex';
      requestAnimationFrame(()=>{ requestAnimationFrame(()=>{ try{ lbPanel.style.opacity='1'; }catch{} }); });
      if(lbTab==='presence'){ renderLbRpc(); startRpcView(); } else { lbTab='board'; fetchLb(); }
    } else {
      try{ lbPanel.style.opacity='0'; }catch{}
      try{ stopLbChat(); }catch{}
      try{ stopRpcView(); }catch{}
      try{ untrapRelease(); }catch{}
      _lbFadeT=setTimeout(()=>{ try{ lbPanel.style.display='none'; }catch{} }, 180);
    }
  }
  function fetchLb(){
    if(!lbPanel) return;
    renderLbPanel();
    lbLoading=true; renderLbBody();
    rankedApiGet(`/api/leaderboard?sort=${encodeURIComponent(lbSort)}&order=${encodeURIComponent(lbOrder)}&limit=${LB_LIMIT}&offset=${lbOffset}`, (j)=>{
      lbLoading=false;
      if(j && j.results){ lbRows=j.results; lbCount=j.count||j.results.length; }
      else { lbRows=[]; lbCount=0; }
      renderLbPanel();
    });
  }
  function renderLbPanel(){
    if(!lbPanel) return;
    const tabBtn=(id,active)=>{
      const el=document.getElementById(id);
      if(!el) return;
      el.style.border='1px solid '+(active?'#fff':'rgba(255,255,255,0.12)');
      el.style.background=active?'#fff':'#171717';
      el.style.color=active?'#000':'#9ca3af';
    };
    tabBtn('bnc-lb-tab-board', lbTab==='board');
    // removed tabs: insights / friends / events / ranked
    tabBtn('bnc-lb-tab-gfx', lbTab==='graphics');
    tabBtn('bnc-lb-tab-rpc', lbTab==='presence');
    if(lbTab!=='presence'){ try{ stopRpcView(); }catch{} }
    // profile view replaces the board content (sorts + pagination hidden)
    if(lbTab==='board' && lbProfile){ renderLbProfile(); return; }
    const sorts=document.getElementById('bnc-lb-sorts');
    if(sorts) sorts.style.display = lbTab==='board' ? 'flex' : 'none';
    const foot=document.getElementById('bnc-lb-foot');
    if(foot) foot.style.display = lbTab==='board' ? 'flex' : 'none';
    if(lbTab==='insights'||lbTab==='friends'||lbTab==='events'||lbTab==='ranked'){ lbTab='board'; }
    if(lbTab==='graphics'){ renderLbGfx(); return; }
    if(lbTab==='presence'){ renderLbRpc(); startRpcView(); return; }
    if(sorts){
      sorts.innerHTML=LB_SORTS.map(([v,label])=>{
        const active = lbSort===v;
        return `<button data-lb-sort="${v}" style="flex:1;min-width:90px;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer;border:1px solid ${active?'#fff':'rgba(255,255,255,0.12)'};background:${active?'#fff':'#171717'};color:${active?'#000':'#9ca3af'}">${label}${active?(lbOrder==='desc'?' ↓':' ↑'):''}</button>`;
      }).join('');
      sorts.querySelectorAll('button[data-lb-sort]').forEach(btn=>{
        btn.onclick=()=>{
          const v=btn.dataset.lbSort;
          if(lbSort===v){ lbOrder = lbOrder==='desc' ? 'asc' : 'desc'; }
          else { lbSort=v; lbOrder='desc'; }
          lbOffset=0; fetchLb();
        };
      });
    }
    renderLbBody();
    const page=document.getElementById('bnc-lb-page');
    if(page) page.textContent = lbRows.length ? `Showing ${lbOffset+1}–${lbOffset+lbRows.length}` : 'No players yet';
    const sub=document.getElementById('bnc-lb-sub');
    if(sub) sub.textContent = `All players · sorted by ${(LB_SORTS.find(s=>s[0]===lbSort)||[,'?'])[1]}`;
  }
  function fetchLbIns(){
    if(!lbPanel || lbTab!=='insights') return;
    let did=null;
    try{ did=(statsCache&&statsCache.discord_id)||GMGet('bnc_discord_id',null)||null; }catch{}
    if(!did){ lbIns=null; lbInsLoading=false; renderLbIns(); return; }
    lbInsLoading=true; renderLbIns();
    rankedApiGet(`/api/insights?discord_id=${encodeURIComponent(did)}`, (j)=>{
      lbInsLoading=false;
      lbIns=(j && !j.error) ? j : null;
      if(lbTab==='insights') renderLbIns();
    });
  }
  function renderLbIns(){
    const body=document.getElementById('bnc-lb-body');
    if(!body) return;
    const sub=document.getElementById('bnc-lb-sub');
    const esc=(v)=> String(v==null?'—':v).replace(/</g,'&lt;');
    const sess=(m)=>{
      if(!m) return '—';
      const res=m.win?'W':(m.defeat?'L':'–');
      return `${m.kills||0}K/${m.deaths||0}D ${res} (${esc(fmtGmDrawer(m.gm))})`;
    };
    if(sub) sub.textContent='Your gameplay insights';
    if(lbInsLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading insights…</div>`; return; }
    if(!lbIns){
      let did=null;
      try{ did=(statsCache&&statsCache.discord_id)||GMGet('bnc_discord_id',null)||null; }catch{}
      body.innerHTML=!did
        ? `<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Link your account first — open the player info drawer to link.</div>`
        : `<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">No insights yet — play a few matches first.</div>`;
      return;
    }
    const card=(label,val)=>`<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 12px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:12px"><span style="color:#9ca3af">${label}</span><span style="color:#fff;font-weight:700;text-align:right">${val}</span></div>`;
    const fear=lbIns.biggest_fear, farm=lbIns.biggest_free_kill;
    const mateI=lbIns.fav_teammate;
    const mateLine=mateI&&mateI.player?`${esc(mateI.player)} (${mateI.shared||0} shared)`:'Not yet found';
    const form=(lbIns.recent_form||[]).slice(0,5).map((m,i)=>`<div style="font-size:12px;color:#e5e7eb;padding:5px 12px">${i+1}. ${esc(sess(m))}</div>`).join('') || `<div style="font-size:12px;color:#9ca3af;padding:5px 12px">No recent matches</div>`;
    const mastery=(lbIns.mode_mastery||[]).slice(0,4).map(m=>`<div style="font-size:12px;color:#e5e7eb;padding:5px 12px;display:flex;justify-content:space-between"><span>${esc(fmtGmDrawer(m.gm))} · ${m.matches} matches</span><span>${esc(m.kd)} K/D · ${Math.round((m.win_rate||0)*100)}% W</span></div>`).join('');
    const styleTip=(typeof PLAYSTYLE_TIPS!=='undefined' && PLAYSTYLE_TIPS[lbIns.playstyle]) || 'Playstyle from kills/match, K/D and win rate.';
    body.innerHTML=`<div style="display:flex;flex-direction:column;gap:6px">`
      + `<div style="font-size:13px;font-weight:800;color:#fff;text-align:center;padding:4px 0">Playstyle: <span title="${esc(styleTip)}" style="text-decoration:underline dotted;cursor:help">${esc(lbIns.playstyle)}</span> <span style="color:#6b7280;font-weight:400">· ${lbIns.matches_analyzed||0} matches</span></div>`
      + card('🔥 Best session', esc(sess(lbIns.best_session)))
      + card('🧊 Worst session', esc(sess(lbIns.worst_session)))
      + card('😱 Biggest fear', fear&&fear.player?`${esc(fear.player)} (${fear.deaths} deaths)`:'—')
      + card('🎯 Biggest free kill', farm&&farm.player?`${esc(farm.player)} (${farm.kills} kills)`:'—')
      + card('🎮 Fav mode', esc(fmtGmDrawer(lbIns.fav_game_mode)))
      + card('🤝 Best teammate', mateLine)
      + card('🧤 Clutch wins (≤1 kill)', esc(lbIns.clutch_factor))
      + `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">RECENT FORM</div>` + form
      + (mastery?`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">MODE MASTERY</div>`+mastery:'')
      + `</div>`;
  }
  // --- FRIENDS TAB: request / accept / decline / invite-to-party / join ---
  // Invite is gated on holding a live private pID (myLiveCode); join copies
  // the code — the game needs manual entry, no auto-join is possible.
  function fetchLbFriends(){
    if(!lbPanel || lbTab!=='friends') return;
    stopLbChat();
    const did=myDiscordId();
    if(!did){ lbFriends=null; lbInvites=null; lbUnread=null; lbFriendsLoading=false; renderLbFriends(); return; }
    lbFriendsLoading=true; renderLbFriends();
    rankedApiGet(`/api/friends/list?discord_id=${encodeURIComponent(did)}`, (j)=>{
      lbFriends=(j && !j.error) ? j : null;
      try{ _lbFriendsAt=Date.now(); }catch{}
      rankedApiGet(`/api/party/invites?discord_id=${encodeURIComponent(did)}`, (k)=>{
        lbInvites=(k && !k.error) ? k : null;
        rankedApiGet(`/api/friends/unread?discord_id=${encodeURIComponent(did)}`, (u)=>{
          lbFriendsLoading=false;
          lbUnread=(u && !u.error) ? u : null;
          if(lbTab==='friends') renderLbFriends();
        });
      });
    });
  }
  function stopLbChat(){
    try{ if(lbChatTimer){ clearInterval(lbChatTimer); lbChatTimer=null; } }catch{}
    lbChatWith=null;
  }
  function fetchLbChat(){
    if(!lbPanel || lbTab!=='friends' || !lbChatWith) return;
    const did=myDiscordId();
    if(!did) return;
    lbChatLoading=true; renderLbFriends();
    rankedApiGet(`/api/friends/messages?discord_id=${encodeURIComponent(did)}&with=${encodeURIComponent(lbChatWith)}&limit=50`, (j)=>{
      lbChatLoading=false;
      lbChat=(j && !j.error && j.messages) ? j.messages : [];
      if(lbTab==='friends' && lbChatWith) renderLbFriends();
      // reading marks them read server-side — refresh the unread badges
      try{
        rankedApiGet(`/api/friends/unread?discord_id=${encodeURIComponent(did)}`, (u)=>{
          lbUnread=(u && !u.error) ? u : lbUnread;
          if(lbTab==='friends' && lbChatWith) renderLbFriends();
        });
      }catch{}
    });
  }
  function startLbChatPoll(){
    try{ if(lbChatTimer) clearInterval(lbChatTimer); }catch{}
    if(typeof BNC_LITE!=="undefined"&&BNC_LITE){ try{ console.log("[BNC-LITE] chat poll disabled"); }catch{} } else lbChatTimer=setInterval(()=>{ try{ if(lbTab==='friends' && lbChatWith && lbPanel && lbPanel.style.display!=='none') fetchLbChat(); }catch{} }, 5000);
  }
  function lbFriendAction(path, body, okMsg){
    apiPostJson(path, body, (j)=>{
      if(j && (j.ok || !j.error)){ try{ showToast(okMsg||'Done', false); }catch{} }
      else { try{ showToast('Failed: '+(((j||{}).error)||'server error'), true); }catch{} }
      try{ _lbFriendsAt=0; }catch{} // friendship changed: refresh cache below
      fetchLbFriends();
    });
  }
  let _lbFriendsAt=0;
  // Cached friends list for Add/Remove/Sent button states (60s). Never throws.
  function ensureLbFriendsCache(cb){
    const done=(ok)=>{ try{ cb(ok); }catch{} };
    try{
      const did=myDiscordId();
      if(!did){ done(false); return; }
      if(lbFriends && Date.now()-_lbFriendsAt<60000){ done(true); return; }
      rankedApiGet(`/api/friends/list?discord_id=${encodeURIComponent(did)}`, (j)=>{
        if(j && !j.error){ lbFriends=j; _lbFriendsAt=Date.now(); done(true); }
        else done(false);
      });
    }catch{ done(false); }
  }
  // Rewrites Add buttons by real relationship: already friends -> Remove,
  // request pending -> Sent (disabled), else stays Add. Runs on players-tab
  // rows, search results and the profile panel alike. Never throws.
  function upgradeFriendButtons(root){
    try{
      const btns=(root||document).querySelectorAll
        ? (root||document).querySelectorAll('button[data-addfr]') : [];
      if(!btns.length) return;
      ensureLbFriendsCache((ok)=>{
        if(!ok || !lbFriends) return;
        const isF=new Set(((lbFriends.friends)||[]).map(f=>String(f.discord_id)));
        const isO=new Set(((lbFriends.outgoing)||[]).map(f=>String(f.discord_id)));
        btns.forEach(b=>{
          try{
            if(!document.contains(b)) return;
            const did=String(b.dataset.addfr||'');
            if(isF.has(did)){ b.disabled=false; b.textContent='Remove'; b.dataset.friendOp='remove'; }
            else if(isO.has(did)){ b.disabled=true; b.textContent='Sent'; delete b.dataset.friendOp; }
          }catch{}
        });
      });
    }catch{}
  }
  function sendFriendRequest(did, target, btn, addLabel){
    if(!did || !target || !btn || btn.disabled) return;
    if(btn.dataset.friendOp==='remove'){
      btn.disabled=true;
      apiPostJson('/api/friends/remove', {discord_id:did, friend_discord_id:target}, (j)=>{
        btn.disabled=false;
        if(j && (j.ok || !j.error)){
          btn.textContent=addLabel||'Add'; delete btn.dataset.friendOp;
          try{ _lbFriendsAt=0; }catch{}
          try{ showToast('Friend removed', false); }catch{}
        } else { try{ showToast('Failed: '+(((j||{}).error)||'server error'), true); }catch{} }
      });
      return;
    }
    btn.disabled=true; btn.textContent='…';
    apiPostJson('/api/friends/request', {requester_discord_id:did, addressee_discord_id:target}, (j)=>{
      if(j && (j.ok || (j.status&&j.status!=='pending'))){ btn.textContent='Sent'; try{ showToast('Friend request sent', false); }catch{} }
      else if(j && j.error && /already/i.test(j.error)){ btn.textContent='Sent'; }
      else { btn.disabled=false; btn.textContent=addLabel||'Add'; try{ showToast('Failed: '+(((j||{}).error)||'server error'), true); }catch{} }
      try{ _lbFriendsAt=0; }catch{}
    });
  }
  function renderLbFriends(){
    const body=document.getElementById('bnc-lb-body');
    if(!body) return;
    const sub=document.getElementById('bnc-lb-sub');
    const esc=(v)=> String(v==null?'—':v).replace(/</g,'&lt;');
    if(sub) sub.textContent='Friends & party invites';
    const did=myDiscordId();
    if(!did){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Link your account first — open the player info drawer to link.</div>`; return; }
    if(lbFriendsLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading friends…</div>`; return; }
    if(!lbFriends && !lbInvites){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Couldn't load friends — server unreachable?</div>`; return; }
    // chat view replaces the lists while open
    if(lbChatWith){ renderLbChatView(body, esc); wireLbChat(body, did, esc); return; }
    const code=myLiveCode();
    const unreadBy=(lbUnread&&lbUnread.by_friend)||{};
    const presDot=(detail)=>({offline:'#4b5563',lobby:'#eab308',match:'#22c55e',scrim:'#a855f7',tourney:'#f59e0b'}[detail]||'#4b5563');
    const presText=(f)=>{
      const p=f.presence||{detail:'offline'};
      if(p.detail==='tourney') return `In Tourney - ${(p.tag&&p.tag.name)||'?'}`;
      if(p.detail==='scrim') return (p.tag&&p.tag.name) ? `In Scrims - ${p.tag.name}` : `In Scrims - ${fmtGmDrawer(p.gm)}`;
      if(p.detail==='match') return `In Match - ${fmtGmDrawer(p.gm)}`;
      if(p.detail==='lobby') return 'In Lobby';
      return 'Offline';
    };
    const btn=(id,label,enabled,title)=>`<button data-fr="${id}" ${enabled?'':'disabled'} title="${esc(title||label)}" style="background:${enabled?'#fff':'#1f2937'};color:${enabled?'#000':'#6b7280'};border:0;border-radius:6px;padding:4px 10px;cursor:${enabled?'pointer':'not-allowed'};font-size:11px;font-weight:700;white-space:nowrap">${label}</button>`;
    const unreadBadge=(did2)=>{
      const n=Number(unreadBy[String(did2)]||0);
      return n>0?`<span style="background:#ef4444;color:#fff;border-radius:999px;font-size:10px;font-weight:800;padding:1px 7px;margin-left:6px">${n}</span>`:'';
    };
    // row() takes pre-built SAFE html for the title (dot/badge markup) —
    // escape dynamic text BEFORE composing (esc() here would print tags).
    const row=(titleHtml,sub2,controls)=>`<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 12px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:12px"><div style="min-width:0"><div style="color:#fff;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${titleHtml}</div>${sub2?`<div style="color:#6b7280;font-size:11px">${esc(sub2)}</div>`:''}</div><div style="display:flex;gap:6px;flex-shrink:0;align-items:center">${controls}</div></div>`;
    const nm=(e)=> e.discord_username || e.game_username || String(e.discord_id||'').slice(0,6);
    let html=`<div style="display:flex;flex-direction:column;gap:6px">`;
    // incoming party invites first (join = copy code)
    const incoming=(lbInvites&&lbInvites.incoming)||[];
    if(incoming.length){
      html+=`<div style="font-size:11px;font-weight:800;color:#f59e0b;letter-spacing:.06em;padding:4px 12px 0">PARTY INVITES — TAP JOIN, THEN ENTER THE CODE IN-GAME</div>`;
      incoming.forEach((inv,i)=>{
        const from=inv.from_discord_username || inv.from_game_username || String(inv.from_discord_id||'').slice(0,6);
        html+=row(`📨 ${esc(from)} → room ${esc(inv.code||'?')}`, null, `<button data-join="${i}" style="background:#f59e0b;color:#000;border:0;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:11px;font-weight:800;white-space:nowrap">Join</button>`);
      });
    }
    // friends + presence + invite/chat buttons
    const friends=(lbFriends&&lbFriends.friends)||[];
    const unreadTotal=Number((lbUnread&&lbUnread.total)||0);
    if(sub) sub.textContent='Friends & party invites'+(unreadTotal>0?` · ${unreadTotal} unread`:'');
    html+=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">FRIENDS (${friends.length})${code?` · YOUR CODE: ${esc(code)}`:' · HOLD A PRIVATE ROOM TO INVITE'}</div>`;
    html+= friends.length ? friends.map(f=>{
      const p=f.presence||{detail:'offline'};
      const dot=`<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${presDot(p.detail)};margin-right:6px" title="${esc(presText(f))}"></span>`;
      const sub2=[presText(f), (f.game_username&&f.discord_username)?f.game_username:null].filter(Boolean).join(' · ');
      const gate=canPartyInvite();
      return row(dot+esc(nm(f))+unreadBadge(f.discord_id), sub2,
        `<button data-chat="${esc(f.discord_id)}" title="Chat" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11px;font-weight:700;white-space:nowrap">Chat</button>`
        +btn('inv:'+f.discord_id, 'Invite', gate.ok, gate.ok?`Invite to room ${gate.code}`:gate.why));
    }).join('')
      : `<div style="font-size:12px;color:#9ca3af;padding:5px 12px">No friends yet — search below to add some.</div>`;
    // incoming requests
    const reqs=(lbFriends&&lbFriends.incoming)||[];
    if(reqs.length){
      html+=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">INCOMING REQUESTS (${reqs.length})</div>`;
      reqs.forEach(f=>{ html+=row(esc(nm(f)), null, btn('acc:'+(f.requester_discord_id||f.discord_id),'Accept',true)+btn('dec:'+(f.requester_discord_id||f.discord_id),'Decline',true)); });
    }
    // outgoing requests (sent by me, awaiting answer) with cancel
    const outg=(lbFriends&&lbFriends.outgoing)||[];
    if(outg.length){
      html+=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">OUTGOING REQUESTS (${outg.length})</div>`;
      outg.forEach(f=>{ html+=row(esc(nm(f)), 'Waiting for answer', btn('can:'+f.discord_id,'Cancel',true,'Withdraw this request')); });
    }
    // nearby: people in the current room, no typing needed (Chat/Add inline).
    // Built from the already-fetched players cache; states upgrade below.
    try{
      const _near=((playersCache&&playersCache.players)||[]).filter(p=>
        p && p.in_db && p.discord_id && !p.is_self && String(p.discord_id)!==String(did));
      if(_near.length){
        html+=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">NEARBY — IN YOUR ROOM (${_near.length})</div>`;
        _near.slice(0,12).forEach(p=>{
          const label=p.discord_username || p.username || String(p.discord_id).slice(0,6);
          html+=row(esc(label), p.game_username&&p.discord_username?p.game_username:null,
            `<button data-chat="${esc(String(p.discord_id))}" title="Chat" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11px;font-weight:700;white-space:nowrap">Chat</button>`
            +`<button data-addfr="${esc(String(p.discord_id))}" data-add-label="Add" style="background:#fff;color:#000;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11px;font-weight:700;white-space:nowrap">Add</button>`);
        });
      }
    }catch{}
    // add by search
    html+=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">ADD FRIEND</div>
      <div style="display:flex;gap:6px;padding:0 12px"><input id="bnc-fr-q" placeholder="Discord or game name…" value="" style="flex:1;background:#1f2937;border:1px solid rgba(255,255,255,0.12);border-radius:8px;padding:7px 10px;color:#e5e7eb;font-size:12px;outline:none;min-width:0"><button id="bnc-fr-go" style="background:#fff;color:#000;border:0;border-radius:8px;padding:7px 14px;cursor:pointer;font-size:12px;font-weight:800">Search</button></div>
      <div id="bnc-fr-results" style="display:flex;flex-direction:column;gap:6px"></div>`;
    html+=`</div>`;
    body.innerHTML=html;
    // wire search
    const q=document.getElementById('bnc-fr-q'), go=document.getElementById('bnc-fr-go');
    const doSearch=()=>{
      const v=(q&&q.value||'').trim();
      if(v.length<2){ try{ showToast('Type at least 2 characters', true); }catch{} return; }
      const box=document.getElementById('bnc-fr-results');
      if(box) box.innerHTML=`<div style="font-size:12px;color:#9ca3af;padding:4px 12px">Searching…</div>`;
      rankedApiGet(`/api/search?q=${encodeURIComponent(v)}&limit=10`, (j)=>{
        lbSearch=(j&&j.results)||[];
        const bx=document.getElementById('bnc-fr-results');
        if(!bx) return;
        bx.innerHTML=lbSearch.length ? lbSearch.map((u,i)=>{
          const label=u.discord_username || u.game_username || String(u.discord_id||'').slice(0,6);
          const sub2=u.game_username&&u.discord_username?u.game_username:(u.game_id||null);
          const mine=String(u.discord_id)===String(did);
          return row(esc(label), sub2, mine?`<span style="font-size:11px;color:#6b7280">you</span>`:`<button data-addfr="${esc(String(u.discord_id||''))}" data-add-label="Request" style="background:#fff;color:#000;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11px;font-weight:700;white-space:nowrap">Request</button>`);
        }).join('') : `<div style="font-size:12px;color:#9ca3af;padding:4px 12px">No players found.</div>`;
        bx.querySelectorAll('button[data-addfr]').forEach(b=>{
          b.onclick=()=>{
            sendFriendRequest(did, String(b.dataset.addfr||''), b, b.getAttribute('data-add-label')||'Add');
          };
        });
        try{ upgradeFriendButtons(bx); }catch{}
      });
    };
    if(go) go.onclick=doSearch;
    if(q){
      // capture (not bubble): runs before the generic trap below, so Enter
      // still searches while every other key dies before the game sees it.
      q.addEventListener('keydown', (e)=>{ if(e.key==='Enter'){ e.preventDefault(); e.stopPropagation(); doSearch(); } }, true);
      try{ q._vkbdAction=doSearch; }catch{}
      try{ untrapInput(q); }catch{}
      try{ attachVkbd(q, doSearch); }catch{}
    }
    // wire row buttons
    body.querySelectorAll('button[data-fr]').forEach(b=>{
      b.onclick=()=>{
        const [act, target]=String(b.dataset.fr).split(/:(.+)/);
        if(act==='inv'){
          const g2=canPartyInvite();
          if(!g2.ok){ try{ showToast(g2.why, true); }catch{} return; }
          lbFriendAction('/api/party/invite', {from_discord_id:did, to_discord_id:target, code:g2.code}, 'Invite sent');
        }
        else if(act==='acc'){ lbFriendAction('/api/friends/accept', {discord_id:did, requester_discord_id:target}, 'Friend added'); }
        else if(act==='dec'){ lbFriendAction('/api/friends/decline', {discord_id:did, requester_discord_id:target}, 'Declined'); }
        else if(act==='can'){ lbFriendAction('/api/friends/remove', {discord_id:did, friend_discord_id:target}, 'Request withdrawn'); }
      };
    });
    body.querySelectorAll('button[data-join]').forEach(b=>{
      b.onclick=()=>{
        const inv=incoming[Number(b.dataset.join)];
        if(!inv||!inv.code) return;
        copyText(inv.code, `Code ${inv.code} copied — enter it in a private room`);
      };
    });
    body.querySelectorAll('button[data-chat]').forEach(b=>{
      b.onclick=()=>{
        lbChatWith=String(b.dataset.chat||'');
        lbChat=[]; fetchLbChat(); startLbChatPoll();
      };
    });
    body.querySelectorAll('button[data-addfr]').forEach(b=>{
      b.onclick=()=>{
        sendFriendRequest(did, String(b.dataset.addfr||''), b, b.getAttribute('data-add-label')||'Add');
      };
    });
    try{ upgradeFriendButtons(body); }catch{}
  }
  function renderLbChatView(body, esc){
    const sub=document.getElementById('bnc-lb-sub');
    const friends=(lbFriends&&lbFriends.friends)||[];
    const peer=friends.find(f=>String(f.discord_id)===String(lbChatWith))||{discord_id:lbChatWith};
    const name=peer.discord_username || peer.game_username || String(lbChatWith||'').slice(0,6);
    if(sub) sub.textContent='Chat — '+name;
    const did=myDiscordId();
    // Poll refresh while typing: the input is alive and focused — update ONLY
    // the message list so the draft and focus survive. Full rebuilds happen
    // on open/send, where losing focus is correct.
    const liveIn=document.getElementById('bnc-chat-in');
    if(liveIn && document.activeElement===liveIn){
      const box=document.getElementById('bnc-chat-msgs');
      if(box){
        box.innerHTML=(lbChat||[]).map(m=>{
          const mine=String(m.from_discord_id)===String(did);
          return `<div style="align-self:${mine?'flex-end':'flex-start'};max-width:80%;background:${mine?'#fff':'#1f2937'};color:${mine?'#000':'#e5e7eb'};border-radius:10px;padding:6px 10px;font-size:12px;word-break:break-word">${esc(m.body)}</div>`;
        }).join('') || `<div style="color:#9ca3af;font-size:12px;text-align:center">No messages yet — say hi.</div>`;
        box.scrollTop=box.scrollHeight;
      }
      return;
    }
    const msgs=(lbChat||[]).map(m=>{
      const mine=String(m.from_discord_id)===String(did);
      return `<div style="align-self:${mine?'flex-end':'flex-start'};max-width:80%;background:${mine?'#fff':'#1f2937'};color:${mine?'#000':'#e5e7eb'};border-radius:10px;padding:6px 10px;font-size:12px;word-break:break-word">${esc(m.body)}</div>`;
    }).join('');
    body.innerHTML=`<div style="display:flex;flex-direction:column;gap:6px;height:100%">`
      + `<div style="padding:0 12px"><button id="bnc-chat-back" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:4px 12px;cursor:pointer;font-size:11px;font-weight:700">‹ Back</button></div>`
      + `<div id="bnc-chat-msgs" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding:6px 12px;min-height:200px;max-height:46vh">${lbChatLoading?`<div style="color:#9ca3af;font-size:12px;text-align:center">Loading…</div>`:((msgs||`<div style="color:#9ca3af;font-size:12px;text-align:center">No messages yet — say hi.</div>`))}</div>`
      + `<div style="display:flex;gap:6px;padding:0 12px"><input id="bnc-chat-in" maxlength="500" placeholder="Message ${esc(name)}…" style="flex:1;background:#1f2937;border:1px solid rgba(255,255,255,0.12);border-radius:8px;padding:8px 10px;color:#e5e7eb;font-size:12px;outline:none;min-width:0"><button id="bnc-chat-send" style="background:#fff;color:#000;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:12px;font-weight:800">Send</button></div>`
      + `</div>`;
  }
  function wireLbChat(body, did, esc){
    const back=document.getElementById('bnc-chat-back');
    if(back) back.onclick=()=>{ stopLbChat(); fetchLbFriends(); };
    const inp=document.getElementById('bnc-chat-in'), send=document.getElementById('bnc-chat-send');
    const box=document.getElementById('bnc-chat-msgs');
    if(box) box.scrollTop=box.scrollHeight;
    const doSend=()=>{
      const v=(inp&&inp.value||'').trim();
      if(!v || !lbChatWith || !did) return;
      if(send) send.disabled=true;
      apiPostJson('/api/friends/message', {from_discord_id:did, to_discord_id:lbChatWith, body:v.slice(0,500)}, (j)=>{
        if(send) send.disabled=false;
        if(j && j.ok){ if(inp) inp.value=''; _chatRefocus=true; fetchLbChat(); }
        else { try{ showToast('Failed: '+(((j||{}).error)||'server error'), true); }catch{} }
      });
    };
    if(send) send.onclick=doSend;
    if(inp){
      inp.addEventListener('keydown', (e)=>{ if(e.key==='Enter'){ e.preventDefault(); e.stopPropagation(); doSend(); } }, true);
      try{ inp._vkbdAction=doSend; }catch{}
      try{ untrapInput(inp); }catch{}
      try{ attachVkbd(inp, doSend); }catch{}
      // refocus only right after OUR send (never on poll refresh — reading
      // history must not yank focus while the user watches).
      if(_chatRefocus){ _chatRefocus=false; try{ inp.focus(); }catch{} }
    }
  }
  // --- EVENTS TAB: scheduled / running / ended tourneys + detail ---
  // List from GET /api/comp/tourneys (client-side status chips), detail from
  // GET /api/comp/tourney?id= (info, players, per-match boards, standings).
  function fetchLbEvents(){
    if(!lbPanel || lbTab!=='events') return;
    lbEventsLoading=true; renderLbEvents();
    rankedApiGet('/api/comp/tourneys', (j)=>{
      lbEventsLoading=false;
      lbEvents=(j && !j.error && j.tourneys) ? j.tourneys : [];
      if(lbTab==='events' && !lbEventDetail) renderLbEvents();
    });
  }
  let lbRanked=null, lbRankedLoading=false;
  function fetchLbRanked(){
    if(!lbPanel || lbTab!=='ranked') return;
    lbRankedLoading=true; renderLbRanked();
    rankedApiGet('/api/leaderboard?ranked=true&limit=20', (j)=>{
      lbRankedLoading=false;
      lbRanked=(j && !j.error && j.results) ? j.results : [];
      if(lbTab==='ranked') renderLbRanked();
    });
  }
  function renderLbRanked(){
    const body=document.getElementById('bnc-lb-body');
    if(!body) return;
    const sub=document.getElementById('bnc-lb-sub');
    const esc=(v)=> String(v==null?'—':v).replace(/</g,'&lt;');
    if(sub) sub.textContent='Ranked ladder · top 20';
    if(lbRankedLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading ranked…</div>`; return; }
    const rows=lbRanked||[];
    if(!rows.length){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">No ranked points yet — play a ranked match first.</div>`; return; }
    const medals=['🥇','🥈','🥉'];
    body.innerHTML=rows.map((r,i)=>{
      const nm=r.game_username||r.discord_username||('…'+String(r.discord_id||'').slice(-4));
      const ms=r.milestone||'Unranked';
      const dot=r.milestone_color?`<span style="width:9px;height:9px;border-radius:50%;background:${esc(r.milestone_color)};flex-shrink:0"></span>`:'';
      const left=medals[i]||`${i+1}.`;
      return `<div style="display:flex;gap:10px;align-items:center;padding:9px 12px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:12px;margin-bottom:6px"><span style="width:26px;text-align:center;flex-shrink:0">${left}</span>${dot}<div style="flex:1;min-width:0"><div style="color:#fff;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(nm)}</div><div style="color:#6b7280;font-size:11px">${esc(ms)}</div></div><span style="color:#fff;font-weight:800">${Number(r.points||0).toLocaleString('en-US')} pts</span></div>`;
    }).join('');
  }
  function fetchLbEventDetail(id){    if(!lbPanel) return;
    lbEventLoading=true; renderLbEvents();
    rankedApiGet(`/api/comp/tourney?id=${encodeURIComponent(id)}`, (j)=>{
      lbEventLoading=false;
      lbEventDetail=(j && !j.error) ? j : null;
      if(lbTab==='events') renderLbEvents();
    });
  }
  function evStatusMeta(st){
    st=String(st||'').toLowerCase();
    if(st==='running') return {label:'Running', color:'#22c55e'};
    if(st==='ended') return {label:'Ended', color:'#4b5563'};
    return {label:'Scheduled', color:'#eab308'};
  }
  function renderLbEvents(){
    const body=document.getElementById('bnc-lb-body');
    if(!body) return;
    const sub=document.getElementById('bnc-lb-sub');
    const esc=(v)=> String(v==null?'—':v).replace(/</g,'&lt;');
    if(lbEventDetail || lbEventLoading){ renderLbEventDetail(body, esc, sub); return; }
    if(sub) sub.textContent='Tournament events';
    if(lbEventsLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading events…</div>`; return; }
    const list=lbEvents||[];
    const chips=[['all','All'],['scheduled','Scheduled'],['running','Running'],['ended','Ended']];
    const rows=list.filter(t=>lbEventFilter==='all' || String(t.status||'').toLowerCase()===lbEventFilter);
    const chipHtml=chips.map(([v,label])=>{
      const active=lbEventFilter===v;
      return `<button data-ev-filter="${v}" style="flex:1;min-width:70px;padding:6px 0;border-radius:999px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid ${active?'#fff':'rgba(255,255,255,0.12)'};background:${active?'#fff':'#171717'};color:${active?'#000':'#9ca3af'}">${label}</button>`;
    }).join('');
    let html=`<div style="display:flex;gap:6px;margin-bottom:10px">${chipHtml}</div>`;
    if(!rows.length){
      const hint=lbEventFilter==='running' ? 'No running events — check scheduled.' : lbEventFilter==='ended' ? 'No ended events yet.' : 'No events yet — hosts create them with /tourney-create.';
      html+=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">${hint}</div>`;
    } else {
      html+=rows.map(t=>{
        const m=evStatusMeta(t.status);
        const prog=(t.series_len||0)>0 ? `${t.sealed||0}/${t.series_len}` : `${t.sealed||0} played`;
        return `<div data-ev-open="${t.id}" title="Open event" style="display:flex;gap:10px;align-items:center;padding:9px 12px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:12px;cursor:pointer;margin-bottom:6px"><span style="width:9px;height:9px;border-radius:50%;background:${m.color};flex-shrink:0" title="${m.label}"></span><div style="flex:1;min-width:0"><div style="color:#fff;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">#${t.id} ${esc(t.name)}</div><div style="color:#6b7280;font-size:11px">${m.label} · ${prog} matches · ${t.players||0} players</div></div><span style="color:#6b7280;font-size:14px">›</span></div>`;
      }).join('');
    }
    body.innerHTML=html;
    body.querySelectorAll('button[data-ev-filter]').forEach(b=>{
      b.onclick=()=>{ lbEventFilter=b.dataset.evFilter||'all'; renderLbEvents(); };
    });
    body.querySelectorAll('[data-ev-open]').forEach(el=>{
      el.onclick=()=>{ lbEventDetail=null; fetchLbEventDetail(el.dataset.evOpen); };
    });
  }
  function renderLbEventDetail(body, esc, sub){
    if(lbEventLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading event…</div>`; return; }
    if(!lbEventDetail){
      body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Couldn't load this event.</div>
        <div style="text-align:center"><button id="bnc-ev-back" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:5px 14px;cursor:pointer;font-size:12px;font-weight:700">‹ Back</button></div>`;
      const bb=document.getElementById('bnc-ev-back');
      if(bb) bb.onclick=()=>{ lbEventDetail=null; renderLbEvents(); };
      return;
    }
    const t=lbEventDetail.tourney||{};
    const m=evStatusMeta(t.status);
    if(sub) sub.textContent=`Event #${t.id}`;
    const card=(label,val)=>`<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 12px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:12px"><span style="color:#9ca3af">${label}</span><span style="color:#fff;font-weight:700;text-align:right">${val}</span></div>`;
    // Scoring readout from the tourney points table (WIN owns P1: 1st dropped).
    const evOrd=(n)=>{ try{ n=Number(n); const s=['th','st','nd','rd']; const v=n%100; return (n+(s[(v-20)%10]||s[v]||s[0])).toUpperCase(); }catch{ return '—'; } };
    let scoringHtml='';
    try{
      const pt=(t&&t.points_table)||{};
      const pl=pt.placement||[10,7,5,3,1];
      const parts=[`WIN = ${pt.win!=null?pt.win:3} PTS`, `KILLS = ${pt.kill!=null?pt.kill:1} PTS`];
      pl.forEach((p,i)=>{ if(i===0) return; parts.push(`${evOrd(i+1)} = ${p} PTS`); });
      scoringHtml=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">SCORING</div>`
        + `<div style="font-size:12px;color:#e5e7eb;padding:5px 12px;line-height:1.7">${parts.map(esc).join(' · ')}</div>`;
    }catch{}
    const players=lbEventDetail.players||[];
    const standings=lbEventDetail.standings||[];
    const matches=lbEventDetail.matches||[];
    const boards=lbEventDetail.match_boards||[];
    const boardFor=(v)=>{ const b=boards.find(x=>x&&(x.version===v)); return (b&&b.rows)||[]; };
    let html=`<div style="display:flex;flex-direction:column;gap:6px">`
      + `<div style="display:flex;align-items:center;gap:10px"><button id="bnc-ev-back" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px;font-weight:700;flex-shrink:0">‹ Back</button><b style="font-size:15px;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0">#${t.id} ${esc(t.name)}</b><span style="font-size:10px;font-weight:800;color:${m.color}">${m.label.toUpperCase()}</span></div>`
      + card('Series', `${t.sealed||0}/${t.series_len||'∞'} matches`)
      + (t.code?card('Room code', `<span style="font-family:monospace">${esc(t.code)}</span>`):'')
      + scoringHtml
      + `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">LEADERBOARD (${standings.length})</div>`
      + (standings.length ? standings.slice(0,15).map((r,i)=>`<div style="font-size:12px;color:#e5e7eb;padding:5px 12px;display:flex;justify-content:space-between"><span>${i+1}. ${esc(r.name||'?')}</span><span><b>${r.points||0}</b> pts · ${r.kills||0}K · ${r.wins||0}W${r.avg_placement!=null?' · '+r.avg_placement+' avg':''}</span></div>`).join('') : `<div style="font-size:12px;color:#9ca3af;padding:5px 12px">No sealed matches yet.</div>`)
      + `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">MATCHES (${matches.length})</div>`
      + (matches.length ? matches.map(mm=>{
          const rows=boardFor(mm.version);
          return `<div style="background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:8px 12px"><div style="font-size:12px;color:#fff;font-weight:700">Match v${mm.version} <span style="color:#6b7280;font-weight:400">· winner ${esc(mm.winner||'—')}</span></div>`
            + (rows.length?rows.slice(0,5).map((r,i)=>`<div style="font-size:11px;color:#9ca3af;display:flex;justify-content:space-between;padding:2px 0"><span>${i+1}. ${esc(r.name||'?')}</span><span>${r.points||0} pts</span></div>`).join(''):'')
            + `</div>`;
        }).join('') : `<div style="font-size:12px;color:#9ca3af;padding:5px 12px">No matches sealed.</div>`)
      + `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">PLAYERS (${players.length})</div>`
      + `<div style="font-size:12px;color:#e5e7eb;padding:5px 12px;line-height:1.7">${players.length?players.slice(0,40).map(p=>esc(p.name||'?')).join(' · '):'No registrations yet.'}</div>`
      + `</div>`;
    body.innerHTML=html;
    const back=document.getElementById('bnc-ev-back');
    if(back) back.onclick=()=>{ lbEventDetail=null; renderLbEvents(); };
  }
  function renderLbBody(){
    const body=document.getElementById('bnc-lb-body');
    if(!body) return;
    const esc=(v)=> String(v==null?'—':v).replace(/</g,'&lt;');    if(lbLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading…</div>`; return; }
    if(!lbRows.length){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">No players yet — play a match first.</div>`; return; }
    const prMode=(typeof lbSort!=='undefined' && lbSort==='pr');
    const rows=lbRows.map(r=>{
      const name=r.game_username || r.discord_username || '—';
      const kd=(r.kd==null)?'—':r.kd;
      const pt=prMode ? (r.tourney_pr==null?'—':r.tourney_pr) : ((r.points==null)?'—':Number(r.points).toLocaleString('en-US'));
      const color=r.milestone_color || '#fff';
      return `<div style="display:grid;grid-template-columns:34px 1fr 70px 56px 56px 56px 76px;gap:6px;align-items:center;padding:7px 4px;border-bottom:1px solid rgba(255,255,255,0.05);font-size:12px"><span style="color:#6b7280">#${esc(r.rank)}</span><span data-prof="${esc(r.discord_id||'')}" title="Open profile" style="color:#fff;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;text-decoration:underline;text-decoration-color:rgba(255,255,255,0.25)">${esc(name)}</span><span style="color:${color};font-weight:700;text-align:right">${esc(pt)}</span><span style="color:#e5e7eb;text-align:right">${esc(r.kills)}</span><span style="color:#e5e7eb;text-align:right">${esc(r.wins)}</span><span style="color:#e5e7eb;text-align:right">${esc(kd)}</span><span style="color:#9ca3af;text-align:right">${esc(fmtPlaytimeDrawer(r.playtime_global))}</span></div>`;
    }).join('');
    body.innerHTML=`<div style="display:grid;grid-template-columns:34px 1fr 70px 56px 56px 56px 76px;gap:6px;padding:4px;font-size:10px;font-weight:800;color:#6b7280;letter-spacing:.06em"><span>RANK</span><span>PLAYER</span><span style="text-align:right">${prMode?'PR':'POINTS'}</span><span style="text-align:right">KILLS</span><span style="text-align:right">WINS</span><span style="text-align:right">K/D</span><span style="text-align:right">PLAYTIME</span></div>`+rows;
    try{
      body.querySelectorAll('[data-prof]').forEach(el=>{
        el.onclick=(e)=>{
          try{ if(e) e.stopPropagation(); }catch{}
          const did=String(el.dataset.prof||'');
          const row=lbRows.find(r=>String(r.discord_id)===did);
          if(!row) return;
          lbProfile=row; lbProfileIns=null; renderLbPanel(); fetchLbProfileIns();
        };
      });
    }catch{}
  }
  function fetchLbProfileIns(){
    if(!lbPanel || !lbProfile) return;
    const did=lbProfile.discord_id;
    if(!did) return;
    lbProfileLoading=true;
    rankedApiGet(`/api/insights?discord_id=${encodeURIComponent(did)}`, (j)=>{
      lbProfileLoading=false;
      lbProfileIns=(j && !j.error) ? j : null;
      if(lbProfile) renderLbPanel();
    });
  }
  // Profile view: header (back + username), left skin box full height,
  // right grid of data boxes. Back returns to the leaderboard page.
  function renderLbProfile(){
    const sorts=document.getElementById('bnc-lb-sorts');
    if(sorts) sorts.style.display='none';
    const foot=document.getElementById('bnc-lb-foot');
    if(foot) foot.style.display='none';
    const body=document.getElementById('bnc-lb-body');
    if(!body || !lbProfile) return;
    const sub=document.getElementById('bnc-lb-sub');
    if(sub) sub.textContent='Player profile';
    const esc=(v)=> String(v==null?'—':v).replace(/</g,'&lt;').replace(/"/g,'&quot;');
    const r=lbProfile;
    const name=r.game_username || r.discord_username || '—';
    const meDid=(typeof myDiscordId==='function' ? myDiscordId() : null);
    const canAddFriend=!!(r.discord_id && meDid && String(r.discord_id)!==String(meDid));
    const skinId=(r.skin!=null && String(r.skin).trim()!=='' && String(r.skin).toLowerCase()!=='nan') ? r.skin : 'cct_0_default';
    const skinUrl=skinUrlDrawer(skinId);
    const ins=lbProfileIns;
    const box=(label,val,tip)=>`<div title="${esc(tip||label)}" style="background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:10px 12px;min-width:0"><div style="font-size:10px;font-weight:800;color:#6b7280;letter-spacing:.06em">${esc(label)}</div><div style="font-size:15px;font-weight:800;color:#fff;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${val}</div></div>`;
    const sess=(m)=>{
      if(!m) return '—';
      const res=m.win?'W':(m.defeat?'L':'–');
      return `${m.kills||0}K/${m.deaths||0}D ${res} · ${esc(fmtGmDrawer(m.gm))}`;
    };
    const mate=ins&&ins.fav_teammate;
    const mateVal=!ins?'…':(mate&&mate.player?`${esc(mate.player)} <span style="color:#6b7280;font-weight:400">(${mate.shared} shared)</span>`:'Not yet found');
    const styleVal=!ins?'…':esc(ins.playstyle||'—');
    const styleTip=!ins?'':(PLAYSTYLE_TIPS[ins.playstyle]||'Playstyle from kills/match, K/D and win rate.');
    const boxes=[
      box('Points', r.points!=null?Number(r.points).toLocaleString('en-US'):'—'),
      box('Kills', esc(r.kills)),
      box('Deaths', esc(r.deaths)),
      box('K/D', esc(r.kd)),
      box('Wins', esc(r.wins)),
      box('Playtime', esc(fmtPlaytimeDrawer(r.playtime_global))),
      box('Last online', esc(fmtLastSeen(r.last_seen)), r.last_seen ? ('Last seen ' + r.last_seen) : 'No heartbeat yet'),
      box('Playstyle', styleVal, styleTip),
      box('Best session', !ins?'…':esc(sess(ins.best_session))),
      box('Fav mode', !ins?'…':esc(fmtGmDrawer(ins.fav_game_mode))),
      box('Best teammate', mateVal, 'Most shared team-mode matches. Needs duo/team history.'),
      box('Biggest fear', !ins?'…':((ins.biggest_fear&&ins.biggest_fear.player)?`${esc(ins.biggest_fear.player)} (${ins.biggest_fear.deaths})`:'—')),
    ].join('');
    body.innerHTML=`<div style="display:flex;flex-direction:column;gap:10px;height:100%">`
      + `<div style="display:flex;align-items:center;gap:10px"><button id="bnc-prof-back" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px;font-weight:700">‹ Back</button><b style="font-size:16px;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0">${esc(name)}</b>${r.rank!=null?`<span style="font-size:11px;color:#6b7280">#${esc(r.rank)}</span>`:''}${canAddFriend?`<button id="bnc-prof-add" style="background:#fff;color:#000;border:0;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px;font-weight:800;flex-shrink:0">Add friend</button>`:''}</div>`
      + `<div style="flex:1;display:flex;gap:10px;min-height:0;overflow:auto">`
      + `<div style="width:190px;flex-shrink:0;background:#0a0a0a;border:1px solid rgba(255,255,255,0.08);border-radius:12px;display:flex;align-items:center;justify-content:center;align-self:stretch;padding:12px">${skinUrl?`<img src="${skinUrl}" style="max-width:100%;max-height:100%;object-fit:contain;image-rendering:pixelated" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div style="display:none;font-size:40px">▣</div>`:`<div style="font-size:40px">▣</div>`}</div>`
      + `<div style="flex:1;display:grid;grid-template-columns:1fr 1fr;gap:8px;align-content:start;min-width:0">${lbProfileLoading?`<div style="grid-column:1/-1;color:#9ca3af;font-size:12px;text-align:center;padding:16px 0">Loading insights…</div>`:boxes}</div>`
      + `</div></div>`;
    const back=document.getElementById('bnc-prof-back');
    if(back) back.onclick=()=>{ lbProfile=null; lbProfileIns=null; renderLbPanel(); };
    const addBtn=document.getElementById('bnc-prof-add');
    if(addBtn){
      addBtn.dataset.addfr=String((lbProfile&&lbProfile.discord_id)||'');
      try{ upgradeFriendButtons(lbPanel); }catch{}
      addBtn.onclick=()=>{
        const target=String((lbProfile&&lbProfile.discord_id)||'');
        const me=(typeof myDiscordId==='function' ? myDiscordId() : null);
        sendFriendRequest(me, target, addBtn, 'Add friend');
      };
    }
  }
  // ---- GRAPHICS FILTERS (F4 > Graphics tab) ----
  // One layer: plain CSS filter primitives on the game render surface only
  // (never the panel or the page, so the UI stays untinted while the game is
  // filtered). A preset is just a named bundle of slider values, so applying
  // one writes the sliders - the panel can never disagree with the canvas.
  // Values live in GM storage and are re-applied on a slow poll, because Unity
  // can swap the canvas (resolution change, fullscreen) at any time.
  const GFX_KEYS=['tint','saturation','brightness','contrast','grayscale'];
  const GFX_DEF={ tint:0, saturation:100, brightness:100, contrast:100, grayscale:0 };
  const GFX_RANGES={ tint:[-180,180], saturation:[0,200], brightness:[50,150], contrast:[50,150], grayscale:[0,100] };
  // Each preset is only a bundle of the five sliders above, applied by writing
  // them: tint is hue-rotate (negative = warmer, positive = cooler), then
  // saturate, brightness, contrast and grayscale ride on top. Colour-vision
  // entries rotate the hue axis and lift saturation so the colours a player
  // confuses land further apart - they do not simulate the deficiency.
  const GFX_PRESETS={
    none:{ label:'None', values:{ tint:0, saturation:100, brightness:100, contrast:100, grayscale:0 } },
    custom:{ label:'Custom' },
    vivid:{ label:'Vivid', values:{ tint:0, saturation:142, brightness:104, contrast:112, grayscale:0 } },
    competitive:{ label:'Competitive', values:{ tint:0, saturation:118, brightness:104, contrast:124, grayscale:0 } },
    night:{ label:'Night', values:{ tint:26, saturation:70, brightness:134, contrast:106, grayscale:0 } },
    warm:{ label:'Warm', values:{ tint:-16, saturation:112, brightness:104, contrast:102, grayscale:0 } },
    cool:{ label:'Cool', values:{ tint:18, saturation:108, brightness:102, contrast:104, grayscale:0 } },
    mono:{ label:'Mono', values:{ tint:0, saturation:0, brightness:106, contrast:118, grayscale:0 } },
    protanopia:{ label:'Red-weak (protanopia)', values:{ tint:-30, saturation:132, brightness:106, contrast:110, grayscale:0 } },
    deuteranopia:{ label:'Green-weak (deuteranopia)', values:{ tint:32, saturation:132, brightness:106, contrast:110, grayscale:0 } },
    tritanopia:{ label:'Blue-weak (tritanopia)', values:{ tint:-52, saturation:128, brightness:104, contrast:108, grayscale:0 } },
  };
  function gfxNum(k, def){
    try{
      const r=GFX_RANGES[k]||[0,100];
      let v=Number(GMGet('bnc_gfx_'+k, def));
      if(!isFinite(v)) v=def;
      return Math.max(r[0], Math.min(r[1], Math.round(v)));
    }catch{ return def; }
  }
  function gfxCfg(){
    try{
      let preset=String(GMGet('bnc_gfx_preset','none')||'none');
      if(!GFX_PRESETS[preset]) preset='none';
      const out={ preset };
      GFX_KEYS.forEach(k=>{ out[k]=gfxNum(k, GFX_DEF[k]); });
      return out;
    }catch{ return Object.assign({ preset:'none' }, GFX_DEF); }
  }
  // CSS filter string for a config: first-applied primitive wins, so tint sees
  // the raw canvas and grayscale flattens whatever the rest produced.
  function gfxCss(cfg){
    const c=cfg||gfxCfg();
    const parts=[];
    if(c.tint!==0) parts.push('hue-rotate('+c.tint+'deg)');
    if(c.saturation!==100) parts.push('saturate('+c.saturation+'%)');
    if(c.brightness!==100) parts.push('brightness('+c.brightness+'%)');
    if(c.contrast!==100) parts.push('contrast('+c.contrast+'%)');
    if(c.grayscale>0) parts.push('grayscale('+c.grayscale+'%)');
    return parts.join(' ');
  }
  function gfxTargets(){
    const out=[];
    try{ document.querySelectorAll('canvas').forEach(c=>{ if(c) out.push(c); }); }catch{}
    return out;
  }
  let _gfxOwned=[];
  // css null = plain reset. Idempotent: safe to call on a timer (it only
  // touches style.filter when the string actually changed).
  function gfxApply(){
    try{
      const css=gfxCss();
      const targets=gfxTargets();
      for(const el of _gfxOwned){ if(!css || targets.indexOf(el)<0){ try{ if(el.style.filter) el.style.filter=''; }catch{} } }
      for(const el of targets){ try{ if(el.style.filter!==css) el.style.filter=css; }catch{} }
      _gfxOwned=targets;
      return css;
    }catch{ return ''; }
  }
  function gfxWrite(k, v){ try{ GMSet('bnc_gfx_'+k, v); }catch{} }
  // Manual slider change: the values are the truth, so the named preset steps
  // aside and the chips fall back to "Custom".
  function gfxPersist(k, v){
    try{
      if(k!=='preset'){
        const cur=gfxCfg().preset;
        const base=GFX_PRESETS[cur] && GFX_PRESETS[cur].values;
        if(base && Number(base[k])!==Number(v)) gfxWrite('preset','custom');
      }
    }catch{}
    gfxWrite(k, v);
    try{ gfxApply(); }catch{}
  }
  // Picking a preset writes every slider it bundles, then re-applies once.
  function gfxUsePreset(name){
    const p=GFX_PRESETS[name];
    if(!p || !p.values) return;  // 'custom' is a state, not something to apply
    GFX_KEYS.forEach(k=> gfxWrite(k, p.values[k]));
    gfxWrite('preset', name);
    try{ gfxApply(); }catch{}
  }
  function renderLbGfx(){
    const body=document.getElementById('bnc-lb-body');
    if(!body) return;
    const sub=document.getElementById('bnc-lb-sub');
    if(sub) sub.textContent='Game filters — pick a preset';
    const cfg=gfxCfg();
    const presets=Object.keys(GFX_PRESETS).map(k=>{
      const active=cfg.preset===k;
      return '<button data-gfx-preset="'+k+'" style="flex:1;min-width:120px;padding:7px 10px;border-radius:999px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid '+(active?'#fff':'rgba(255,255,255,0.12)')+';background:'+(active?'#fff':'#171717')+';color:'+(active?'#000':'#9ca3af')+'">'+GFX_PRESETS[k].label+'</button>';
    }).join('');
    const canvases=gfxTargets().length;
    const css=gfxCss(cfg);
    body.innerHTML='<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:0 0 6px">PRESETS</div>'
      + '<div style="display:flex;gap:8px;flex-wrap:wrap">'+presets+'</div>'
      + '<div style="margin-top:12px;font-size:10px;color:#6b7280;line-height:1.7">Applies to the game canvas only — menus and this panel stay untouched.<br>'
      + (canvases? 'Live on '+canvases+' canvas'+(canvases===1?'':'es')+' in this frame: <code style="color:#e5e7eb">'+(css||'no filter')+'</code>'
                 : 'No canvas in this frame — press F4 while the game frame has focus so the filter lands on the game surface.')
      + '</div>';
    body.querySelectorAll('[data-gfx-preset]').forEach(b=>{
      b.onclick=()=>{ if(b.dataset.gfxPreset==='custom') return; gfxUsePreset(b.dataset.gfxPreset); renderLbGfx(); };
    });
  }

  // ---- PASSIVE COUNTERS (former NERDS TAB) -----------------------------
  // The Nerds tab (F4) was removed; what remains is only the passive data
  // feeds with no UI: per-packet net counters, socket ping pairing, socket
  // notes and the captured GL context list. All cheap (integer adds), never
  // sampled on a loop, never sent to the API. Kept because the capture path
  // and the boot hook already write here — deleting the object would mean
  // touching those hot paths for zero runtime gain.
  let _nerds={ fps:{frames:0,t0:0,fps:0,ms:0,worstMs:0,hist:[]}, net:{inP:0,inB:0,outP:0,outB:0,rates:{inP:0,inB:0,outP:0,outB:0,avgPkt:0},totalIn:0,totalOut:0,lastAt:0,lastDir:''}, draw:{calls:0,tris:0,verts:0,rates:{calls:0,tris:0},lastCalls:0,lastTris:0}, ws:null, glCtxs:[], raf:null, timer:null, rafLast:0,
    ping:{ samples:[], last:0, avg:0, min:0, max:0, jitter:0, n:0, at:0, op:0, opName:'', pend:{}, api:{samples:[],last:0,avg:0,min:0,max:0,jitter:0,n:0,at:0,fails:0} } };

  function netLen(d){
    try{
      if(!d) return 0;
      if(typeof d.byteLength==='number') return d.byteLength;
      if(typeof d.size==='number') return d.size;
      if(typeof d.length==='number') return d.length;
    }catch{}
    return 0;
  }
  // Called for EVERY frame on the socket (essential or filtered), so it stays
  // a couple of integer adds - nothing here may allocate.
  function _netNote(dir, len){
    try{
      const n=Number(len)||0;
      if(dir==='in'){ _nerds.net.inP++; _nerds.net.inB+=n; _nerds.net.totalIn+=n; }
      else { _nerds.net.outP++; _nerds.net.outB+=n; _nerds.net.totalOut+=n; }
      _nerds.net.lastAt=Date.now(); _nerds.net.lastDir=dir;
    }catch{}
  }
  // --- PING ---------------------------------------------------------
  // Photon answers an OperationRequest (type 2) with an OperationResponse
  // (type 3) carrying the SAME op code, so a two-byte header peek on both
  // directions times a real round trip on the socket the game is playing on.
  // This runs for EVERY frame (essential or filtered), so it allocates nothing
  // until a pair completes.
  //
  // What the recorded captures say about the sample rate (37 sessions): the
  // client's only frequent traffic is RaiseEvent 253 (~10/s), which the server
  // never answers, and op 249 appears ZERO times in all of them. The ops that do
  // come back are Authenticate 230, SetProperties 252, JoinRandomRoom 225,
  // JoinRoom 226 and CreateRoom 227 - 398 responses across the 37 sessions, a
  // handful per minute. A socket ping is therefore correct but sparse by
  // nature; the panel shows how old the sample is instead of pretending the
  // number is live.
  // 3s cut-off: a response that lands later than this is a stall, not a ping,
  // and pairing it would print a 3-second "ping" that never happened.
  const PING_TTL_MS=3000;   // unanswered request / implausible round trip cut-off
  const PING_OPS={249:'Ping 249',230:'Authenticate 230',231:'AuthenticateOnce 231',225:'JoinRandomRoom 225',226:'JoinRoom 226',227:'CreateRoom 227',224:'CancelJoinRandom 224',229:'JoinLobby 229',228:'LeaveLobby 228',252:'SetProperties 252',251:'GetProperties 251',220:'GetRegions 220'};
  function _pingHeader(d){
    try{
      if(!d) return null;
      let v=null;
      if(typeof ArrayBuffer!=='undefined'){
        if(d instanceof ArrayBuffer) v=new Uint8Array(d);
        else if(typeof DataView!=='undefined'&&d instanceof DataView) v=new Uint8Array(d.buffer, d.byteOffset, d.byteLength);
        else if(ArrayBuffer.isView(d)) v=d;
      }
      if(!v||v.length<3) return null;
      return { type:v[1], code:v[2] };
    }catch{} return null;
  }
  function _pingPush(bucket, rtt){
    try{
      if(!bucket) return;
      const r=Math.round(Number(rtt));
      if(!(r>=0&&r<60000)) return;
      // Jitter = EWMA of |this - previous| (mouse-friendly, stable to read).
      if(bucket.last){ const d=Math.abs(r-bucket.last); bucket.jitter=Math.round(bucket.jitter?((bucket.jitter*7+d)/8):d); }
      bucket.last=r; bucket.at=Date.now(); bucket.n=(bucket.n||0)+1;
      bucket.samples.push(r); if(bucket.samples.length>30) bucket.samples.shift();
      let mn=bucket.samples[0],mx=bucket.samples[0];
      for(const s of bucket.samples){ if(s<mn) mn=s; if(s>mx) mx=s; }
      // avg is an EWMA (a quarter of the new sample): a route change shows up
      // within a few samples instead of waiting out a 30-sample mean, which
      // read as a number that was right but stuck. min/max stay windowed.
      const prev=Number(bucket.avg)||0;
      bucket.avg=prev? Math.round((prev*3+r)/4) : r;
      bucket.min=mn; bucket.max=mx;
    }catch{}
  }
  function _pingNote(dir, data){
    try{
      const h=_pingHeader(data); if(!h) return;
      const p=_nerds.ping;
      const now=(typeof performance!=='undefined'&&performance.now)?performance.now():Date.now();
      if(dir==='out'){
        if(h.type!==2) return;
        const q=p.pend[h.code]||(p.pend[h.code]=[]);
        q.push(now);
        while(q.length&&(now-q[0])>PING_TTL_MS) q.shift();   // never answered -> stale
        if(q.length>6) q.shift();
        return;
      }
      if(h.type!==3) return;
      const q=p.pend[h.code];
      if(!q||!q.length) return;
      // Pair with the oldest *live* request: a request nothing ever answered is
      // dropped here too, otherwise a later response would inherit its age and
      // print a 4-second "ping".
      while(q.length&&(now-q[0])>PING_TTL_MS) q.shift();
      if(!q.length) return;
      const rtt=now-q.shift();
      if(!(rtt>=0&&rtt<=PING_TTL_MS)) return;
      p.op=h.code; p.opName=PING_OPS[h.code]||('op '+h.code);
      _pingPush(p, rtt);
    }catch{}
  }
  // The stats poll reaches the API over plain HTTPS, so its own round trip is a
  // second, independent sample - and the only one on a quiet socket.
  function _apiPing(t0, ok){
    try{
      const a=_nerds.ping.api;
      if(!ok){ a.fails=(a.fails||0)+1; return; }
      if(!t0) return;
      const now=(typeof performance!=='undefined'&&performance.now)?performance.now():Date.now();
      _pingPush(a, now-t0);
    }catch{}
  }
  // Remember the socket we are riding: Photon hosts carry the cluster id
  // (gcams1128.exitgames.com:19090/game/), which is the closest thing to a
  // region the client is told about.
  function _noteWs(url, state){
    try{
      const u=String(url||''); if(!u) return;
      const hostPort=u.replace(/^wss?:\/\//,'').split('/')[0].split('?')[0];
      const parts=hostPort.split(':');
      const host=parts[0], port=parts[1]||'';
      const m=host.match(/^gcams(\d+)/i);
      _nerds.ws={ url:u.slice(0,120), host:host, port:port, cluster:m?m[1]:null, label:(typeof labelForUrl==='function'?labelForUrl(u):'WS'), state:state||'open', at:Date.now() };
    }catch{}
  }
  // --- FPS CHIP ---------------------------------------------------------
  // The one number that answers "why does it feel bad", in the top-right corner:
  // the live frame rate plus this session's worst and best second. It is
  // click-through by design (pointer-events:none) - the corner still belongs to
  // the game - and it steps aside for the full-screen panels (never for the
  // drawer, which is a left-hand panel the corner can coexist with).
  const FPS_CHIP_ID='bnc-fps-chip';
  // Panel ROOTS that hide themselves with display:none. Never a card *inside*
  // one (#bnc-lb-card keeps display:flex while the #bnc-lb overlay around it is
  // hidden) and never the drawer: the drawer is a LEFT panel that hides by
  // transform, so once its element exists its inline display never reads 'none'
  // - consulting it meant "a panel is always open" and the chip stayed
  // invisible for everyone who plays with the drawer open. The chip sits
  // top-right, where the drawer never reaches anyway.
  const FPS_CHIP_PANELS=['bnc-lb','bnc-ranked','bnc-keys','bnc-host11','bnc-host12'];
  let _fpsChip={ el:null, raf:null, frames:0, t0:0, secFrames:0, secT0:0, fps:0, min:null, max:null, warm:1, hiddenBy:'', lastTs:0, worst:0, winWorst:0 };
  // Id of the panel covering the chip's corner, or '' when the corner is free.
  // Returning the id (instead of a boolean) is what makes "I don't see the
  // chip" answerable from the console: __BNC__.fpsChip.hiddenBy
  function _fpsChipBlocker(){
    try{
      for(const id of FPS_CHIP_PANELS){
        const el=document.getElementById(id);
        if(el && el.style && el.style.display && el.style.display!=='none') return id;
      }
    }catch{}
    return '';
  }
  function _fpsChipPaint(){
    const c=_fpsChip, el=c.el;
    if(!el) return;
    try{
      c.hiddenBy=_fpsChipBlocker();
      if(c.hiddenBy){ el.style.display='none'; return; }
      el.style.display='block';
      if(!c.fps){ el.textContent='-- FPS'; return; }   // first window still open
      el.textContent=c.fps+' FPS \u2193 '+((c.min==null)?'--':c.min)+' MIN FPS \u2191 '+((c.max==null)?'--':c.max)+' MAX FPS'+(c.winWorst>0?' \u00B7 '+c.winWorst+'ms worst':'');
      el.style.color=(c.fps>=55?'#a7f3d0':(c.fps>=30?'#fde68a':'#fecaca'));
    }catch{}
  }
  function _fpsChipFrame(ts){ try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE) return; }catch{}
    _fpsChip.raf=null; try{ if(typeof _inCompRoom==='function'&&_inCompRoom()){ try{ if(_fpsChip.el) _fpsChip.el.style.display='none'; }catch{} return; } }catch{} // comp rooms: stop the rAF loop cold (no reschedule below)
    try{
      const c=_fpsChip;
      let hidden=false;
      try{ hidden=!!document.hidden; }catch{}
      if(hidden){
        // rAF pauses in a hidden tab anyway: reset both windows so the first
        // visible second is measured from a clean start instead of from a
        // multi-second gap (which would read as one frame per window).
        c.t0=0; c.frames=0; c.secT0=0; c.secFrames=0; c.warm=1; c.lastTs=0; c.worst=0; c.winWorst=0;
      } else if(!c.t0){
        c.t0=ts; c.frames=0; c.secT0=ts; c.secFrames=0; c.lastTs=ts; c.worst=0;
      } else {
        const dt=ts-c.t0, sdt=ts-c.secT0;
        if(dt>0){
          c.frames++;
          // Worst SINGLE frame in this window: the 250ms average above hides
          // individual hitches (fifteen 16ms frames + one 23ms frame still
          // rounds to 60). Unity's in-game counter is instantaneous-ish, so
          // it swings while we sit pinned — this field moves with it.
          // Same-thread only: GPU-bound dips without main-thread delay are
          // invisible here, and values above display refresh are unobservable.
          const _d=ts-(c.lastTs||ts);
          if(_d>0 && _d<1000) c.worst=Math.max(c.worst||0,_d);
          c.lastTs=ts;
        }
        if(sdt>0) c.secFrames++;
        // Headline: repaint four times a second. A full second between updates
        // is what read as "accurate but slow" - the frame rate changes faster
        // than the number did.
        if(dt>=250){
          const fps=Math.round(c.frames*1000/dt);
          c.frames=0; c.t0=ts;
          if(fps>0 && fps<=1000){ c.fps=fps; c.winWorst=Math.round((c.worst||0)*10)/10; _fpsChipPaint(); }
          c.worst=0;
        }
        // MIN/MAX keep their one-second meaning: the worst and best second
        // since load, so a dip is visible without every quarter-second window
        // rewriting the extremes.
        if(sdt>=1000){
          const sec=Math.round(c.secFrames*1000/sdt);
          c.secFrames=0; c.secT0=ts;
          // The first second after a load or a tab switch is a warm-up, not a
          // sample: it would poison MIN forever ("min 3 FPS" on every boot).
          if(c.warm>0) c.warm--;
          else if(sec>0 && sec<=1000){
            c.min=(c.min==null)?sec:Math.min(c.min,sec);
            c.max=(c.max==null)?sec:Math.max(c.max,sec);
            _fpsChipPaint();
          }
        }
      }
    }catch{}
    try{ _fpsChip.raf=requestAnimationFrame(_fpsChipFrame); }catch{}
  }
  function _fpsChipHome(){
    // Re-parent into the fullscreen element when there is one: a fixed chip on
    // the body is not rendered while a wrapper element is fullscreen. (A bare
    // <canvas> cannot overlay children, so fullscreen canvases still win.)
    try{
      const el=_fpsChip.el||document.getElementById(FPS_CHIP_ID);
      if(!el) return;
      const fsEl=document.fullscreenElement||document.webkitFullscreenElement||null;
      const host=(fsEl && fsEl.tagName && fsEl.tagName!=='CANVAS') ? fsEl : (document.body||document.documentElement);
      if(host && el.parentNode!==host) host.appendChild(el);
    }catch{}
  }
  function mountFpsChip(){ try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE) return; }catch{}
    try{
      if(document.getElementById(FPS_CHIP_ID)) return; try{ if(typeof _inCompRoom==='function'&&_inCompRoom()) return; }catch{} // comp rooms: never mount (no rAF, no 4Hz paint)
      const el=document.createElement('div');
      el.id=FPS_CHIP_ID;
      el.title='BuildNow Companion - frame rate (mouse click-through)';
      el.style.cssText=[
        'position:fixed','top:8px','right:8px','z-index:2147483645',
        'pointer-events:none','user-select:none',
        'font:800 11px/1.25 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace',
        'color:#a7f3d0','background:rgba(0,0,0,0.46)',
        'border:1px solid rgba(255,255,255,0.12)','border-radius:8px',
        'padding:4px 8px','text-shadow:0 1px 2px rgba(0,0,0,0.65)',
        'letter-spacing:.02em','white-space:nowrap'
      ].join(';');
      el.textContent='-- FPS';
      (document.body||document.documentElement).appendChild(el);
      _fpsChip.el=el;
      _fpsChipHome();
      _fpsChipPaint();
      if(_fpsChip.raf==null) _fpsChip.raf=requestAnimationFrame(_fpsChipFrame);
      // Mount + verdict in the console: that pair is what tells a "the script
      // never ran" report apart from a chip that is mounted but hidden.
      logInfo('[FPS] chip mounted (top-right, click-through)');
      setTimeout(()=>{ try{ _fpsChipPaint(); logInfo(_fpsChip.hiddenBy?('[FPS] chip hidden while #'+_fpsChip.hiddenBy+' is open'):'[FPS] chip visible'); }catch{} }, 600);
    }catch{}
  }
  try{ setTimeout(mountFpsChip, 1200); }catch{}
  try{ document.addEventListener('fullscreenchange', _fpsChipHome); }catch{}
  try{ document.addEventListener('webkitfullscreenchange', _fpsChipHome); }catch{}
  function _gameCanvas(){
    try{
      const all=[...document.querySelectorAll('canvas')].filter(c=>c && c.width>0 && c.height>0);
      if(!all.length) return null;
      all.sort((a,b)=>(b.width*b.height)-(a.width*a.height));
      return all[0];
    }catch{ return null; }
  }
  // The game's WebGL context is CAPTURED, never created. Asking a canvas for a
  // context type it does not have is harmless, but asking one that has no
  // context yet CREATES it - a second context on Unity's canvas breaks the
  // game's boot (PlayerLoop recursion, "Runtime is not defined"). So the canvas
  // getContext is wrapped as early as this script runs (see the capture hook at
  // the bottom of this file) and this side only ever reads what the game itself
  // asked for. If the script loaded after the game already had its context,
  // there is nothing to read and the tab says so instead of probing.
  function _rememberGl(gl){
    try{
      if(!gl || typeof gl.getParameter!=='function') return;
      if(_nerds.glCtxs.some(c=>c.gl===gl)) return;
      _nerds.glCtxs.push({ gl:gl, canvas:gl.canvas||null, at:Date.now() });
      if(_nerds.glCtxs.length>4) _nerds.glCtxs.shift();
    }catch{}
  }
  // ---- DISCORD RICH PRESENCE (F4 > Presence tab) ----
  // The userscript owns only what it alone knows (scene, ranked session,
  // tourney, private-party pID) and pushes plain facts to the local Python
  // bridge, which formats them and drives Discord. Bridge defaults to
  // 127.0.0.1:8765 and falls back to 8766-8769 when the decryptor already owns
  // 8765; rpcProbe() asks each candidate /health and keeps the one that
  // answers as the presence bridge, so both services run side by side.
  // On by default: the boot rpcStart() below probes the tray bridge and the
  // 15s push loop keeps the card live with the panel closed. The Presence
  // tab's toggle (bnc_rpc_enabled) still opts out per user.
  const RPC_DEF={ enabled:true, port:8765, auto:true };
  const RPC_PORTS=[8765,8766,8767,8768,8769];
  const RPC_SERVICE='nectar-rich-presence';
  // 15s: fast enough that a scene flip lands on the card while it still
  // matters, slow enough to be invisible. The bridge's stale window is 45s, so
  // one missed push can never blank the card.
  const RPC_PUSH_MS=15000;
  const RPC_VIEW_MS=15000;   // how often the panel re-reads what Discord is showing
  // The tray app is how this runs in practice: it hosts the bridge in-process
  // (and the decryptor next to it), so there is no second terminal to keep open.
  // Users are pointed at the tray, never at a Python command line: the packaged
  // build is a double-click, and the source path is only the fallback for
  // machines without it.
  const RPC_CMD='python companion/tray/tray_app.py';
  const RPC_TRAY_CMD = (()=>{
    try{ return /win/i.test(String(navigator.platform||navigator.userAgent||'')) ? 'dist\\BuildNowTray.exe' : RPC_CMD; }catch{ return RPC_CMD; }
  })();
  let _rpc={ port:null, up:false, discord:false, probing:false, lastPush:0, lastErr:'', timer:null, since:0, lastSig:'', lastKey:'',
             activity:null, activityMode:'', activityAt:0, view:null };
  function rpcCfg(){
    try{
      let port=Number(GMGet('bnc_rpc_port', RPC_DEF.port));
      if(!isFinite(port) || port<1 || port>65535) port=RPC_DEF.port;
      return { enabled:GMGet('bnc_rpc_enabled', RPC_DEF.enabled)===true, port:port, auto:GMGet('bnc_rpc_auto', RPC_DEF.auto)!==false };
    }catch{ return Object.assign({}, RPC_DEF); }
  }
  function rpcUrl(port, path){ return 'http://127.0.0.1:'+port+path; }
  function rpcHttp(port, path, opts, cb){
    const o=opts||{};
    const method=o.method||'GET';
    const body=o.body||null;
    const done=(ok, txt)=>{ let j=null; try{ j=txt?JSON.parse(txt):null; }catch{ j=null; } try{ cb(!!ok, j, txt||''); }catch{} };
    try{
      const GM=(typeof GM_xmlhttpRequest!=='undefined') ? GM_xmlhttpRequest : ((typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null);
      if(GM){
        GM({ method:method, url:rpcUrl(port,path), data:body?JSON.stringify(body):undefined, headers:body?{'Content-Type':'application/json'}:undefined, timeout:4000,
             onload:r=>done(r.status>=200&&r.status<300, r.responseText), onerror:()=>done(false,''), ontimeout:()=>done(false,'') });
        return;
      }
    }catch{}
    try{
      fetch(rpcUrl(port,path), { method:method, headers:body?{'Content-Type':'application/json'}:undefined, body:body?JSON.stringify(body):undefined })
        .then(r=>r.text().then(t=>done(r.ok,t)).catch(()=>done(false,'')))
        .catch(()=>done(false,''));
    }catch{ done(false,''); }
  }
  function rpcProbe(cb){
    const cfg=rpcCfg();
    const cands=cfg.auto ? RPC_PORTS : [cfg.port];
    let i=0;
    _rpc.probing=true;
    const step=()=>{
      if(i>=cands.length){ _rpc.probing=false; _rpc.up=false; _rpc.port=null; try{ cb&&cb(false); }catch{} return; }
      const port=cands[i++];
      rpcHttp(port, '/health', null, (ok, j)=>{ if(ok && j && String(j.service||'')===RPC_SERVICE){ _rpc.probing=false; _rpc.up=true; _rpc.port=port; _rpc.discord=!!j.discord; try{ cb&&cb(true); }catch{} } else step(); });
    };
    step();
  }
  // Facts only — the bridge owns wording, icons and the join button.
  function rpcFacts(){
    const sess=(statsCache&&statsCache.session)||null;
    const tag=(sess&&sess.tag)||null;
    let inMatch=false, gmKey=null, duo=false;
    try{ inMatch=!!_optimisticState.inMatch; }catch{}
    try{
      if(sess){
        if(typeof sess.game_mode==='string' && sess.game_mode) gmKey=sess.game_mode;
        duo=String(sess.game_variant||'').toLowerCase()==='duo';
      }
    }catch{}
    // One authority for "am I in a match": effInMatch() is freshest-first (the
    // scene/auth/leave packets, then the capture shadow, then the server).
    // Reading sess.state straight into inMatch is what pinned the card on
    // "In Match - <mode>" while the player was in the lobby.
    try{ const eff=effInMatch(); if(eff!==null) inMatch=!!eff; }catch{}
    let gmLabel=null;
    try{ gmLabel=gmKey?fmtGmDrawer(gmKey, duo):null; }catch{}
    let partyActive=false, pid=null;
    try{ partyActive=!!(sess&&sess.party&&sess.party.is_party); }catch{}
    if(partyActive){
      // The drawer's own rule, with the drawer's own sources: the room we are
      // standing in first (that code is what a friend can type), then the
      // session's party/short code - a party formed in the lobby has no room
      // code of its own, and without that fallback the card read "in a party"
      // with no way in at all. Same 5-char gate the game uses for a pID.
      try{ pid=myLiveCode(); }catch{}
      if(!pid){ try{ pid=getPartyInfo(sess).code; }catch{} }
      if(pid){ pid=String(pid).trim().toUpperCase(); if(!/^[A-Z0-9]{5}$/.test(pid)) pid=null; }
    }
    const rt=(statsCache&&statsCache.ranked_tourney)||null;
    const ct=(statsCache&&statsCache.comp_tourney)||null;
    const rk=(statsCache&&statsCache.ranked)||null;
    let kind=null, name=null, extra=null;
    if(tag && String(tag.kind||'').toLowerCase()==='tourney' && tag.name){ kind='tourney'; name=String(tag.name); }
    if(!kind && rt && rt.name){ kind='tourney'; name=String(rt.name); }
    if(!kind && ct && ct.name){ kind='tourney'; name=String(ct.name); }
    // Field size only. Rank and points never ride along: a Discord card is
    // visible to the whole friends list, so the ladder stays private.
    if(kind==='tourney'){
      const t=rt||ct||null;
      if(t && t.players!=null) extra=Number(t.players)+' in tourney';
    } else if(rk && (rk.active || rk.short_code)){
      kind='ranked'; name='Ranked Session';
    }
    // (X/Y) on the activity line: players in this match over the room's max.
    let cur=null, max=null;
    try{ if(sess){ if(sess.players_count!=null) cur=Number(sess.players_count)||0; if(sess.max_players!=null) max=Number(sess.max_players)||0; } }catch{}
    let scene=null;
    try{ scene=_optimisticState.curScn||null; }catch{}
    const status=inMatch?'match':(sess?'lobby':'menu');
    const key=status+'|'+(kind||'')+'|'+(name||'');
    if(key!==_rpc.lastKey){ _rpc.lastKey=key; _rpc.since=Date.now(); }
    if(!_rpc.since) _rpc.since=Date.now();
    return {
      version: BNC_VERSION,
      ts: Date.now(),
      since: _rpc.since,
      user: String(GMGet('bnc_game_username','')||((statsCache&&statsCache.game_username)||''))||null,
      status: status,
      scene: scene,
      party: { active: partyActive, private: !!(partyActive&&pid), pid: pid||null },
      mode: { key: gmKey, label: gmLabel },
      context: { kind: kind, name: name, extra: extra },
      players: { cur: cur, max: max },
    };
  }
  function rpcPush(force){
    try{
      if(!rpcCfg().enabled) return;
      if(!_rpc.port){
        rpcProbe((up)=>{ if(up) rpcPush(force); else { _rpc.lastErr='bridge not found on '+RPC_PORTS.join('/'); try{ if(lbTab==='presence') renderLbRpc(); }catch{} } });
        return;
      }
      const facts=rpcFacts();
      const sig=JSON.stringify(facts);
      if(!force && sig===_rpc.lastSig && (Date.now()-_rpc.lastPush)<RPC_PUSH_MS) return;
      _rpc.lastSig=sig;
      rpcHttp(_rpc.port, '/presence', { method:'POST', body:facts }, (ok, j)=>{
        if(ok && j){
          _rpc.up=true; _rpc.discord=!!j.discord; _rpc.lastErr=j.discord?'':String(j.error||''); _rpc.lastPush=Date.now();
          // The bridge echoes back exactly what it handed Discord: the status
          // dot reads `discord` from it, and the activity is kept for debugging.
          if(j.activity){ _rpc.activity=j.activity; _rpc.activityAt=Date.now(); }
          if(j.mode) _rpc.activityMode=String(j.mode);
        } else {
          _rpc.up=false; _rpc.port=null; _rpc.lastErr='bridge error (is rich_presence.py running?)';
        }
        try{ if(lbTab==='presence') renderLbRpc(); }catch{}
      });
    }catch{}
  }
  function rpcClear(){
    try{
      const port=_rpc.port||rpcCfg().port;
      rpcHttp(port, '/clear', { method:'POST', body:{} }, ()=>{ _rpc.lastSig=''; _rpc.activity=null; _rpc.activityMode='cleared'; _rpc.activityAt=Date.now(); try{ if(lbTab==='presence') renderLbRpc(); }catch{} });
    }catch{}
  }
  // Tab closing (game quit): fire a best-effort /clear so the profile drops
  // the card immediately instead of lingering until the bridge's stale window
  // (45s) clears it. sendBeacon/fetch-keepalive survive pagehide where the
  // GM request bridge may not; anything missed here is still covered by the
  // stale timer, so this can never break the steady state.
  function rpcClearBeacon(){
    try{
      if(!rpcCfg().enabled) return;
      const port=_rpc.port||rpcCfg().port;
      const url=rpcUrl(port, '/clear');
      const body=JSON.stringify({});
      try{
        if(typeof navigator!=='undefined' && typeof navigator.sendBeacon==='function'){
          const blob=new Blob([body], {type:'application/json'});
          if(navigator.sendBeacon(url, blob)) return;
        }
      }catch{}
      try{ fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body, keepalive:true}); }catch{}
    }catch{}
  }
  try{
    window.addEventListener('pagehide', ()=>{ try{ rpcClearBeacon(); }catch{} });
    document.addEventListener('visibilitychange', ()=>{ try{ if(document.visibilityState==='hidden') rpcClearBeacon(); }catch{} });
  }catch{}
  // Keeps the panel's status fresh while it is open: /health reports whether
  // the bridge reached Discord (the tray's answer, not our guess at it).
  function rpcFetchActivity(cb){
    try{
      const port=_rpc.port||rpcCfg().port;
      const cands=_rpc.port?[_rpc.port]:(rpcCfg().auto?RPC_PORTS:[rpcCfg().port]);
      let i=0;
      const step=()=>{
        if(i>=cands.length){ try{ cb&&cb(false); }catch{} return; }
        const p=cands[i++];
        rpcHttp(p, '/health', null, (ok, j)=>{
          if(ok && j && String(j.service||'')===RPC_SERVICE){
            _rpc.up=true; _rpc.port=p; _rpc.discord=!!j.discord; _rpc.probing=false;
            _rpc.activity=(j.activity||null); _rpc.activityAt=Date.now(); _rpc.activityMode=String(j.mode||'');
            try{ cb&&cb(true); }catch{}
          } else step();
        });
      };
      step();
    }catch{ try{ cb&&cb(false); }catch{} }
  }
  // The preview refreshes on its own 15s clock, only while the tab is open.
  function startRpcView(){
    if(_rpc.view) return;   // already ticking: several render paths call this
    const beat=()=>{
      try{
        if(lbTab!=='presence' || !lbPanel || lbPanel.style.display==='none'){ stopRpcView(); return; }
        rpcFetchActivity(()=>{ try{ renderLbRpc(); }catch{} });
      }catch{}
    };
    try{ _rpc.view=setInterval(beat, RPC_VIEW_MS); }catch{}
    beat();
  }
  function stopRpcView(){
    try{ if(_rpc.view){ clearInterval(_rpc.view); _rpc.view=null; } }catch{}
  }
  function rpcStop(){
    try{ if(_rpc.timer){ clearInterval(_rpc.timer); _rpc.timer=null; } }catch{}
    try{ if(_rpc.port) rpcHttp(_rpc.port, '/clear', { method:'POST', body:{} }, ()=>{}); }catch{}
    _rpc.up=false; _rpc.port=null; _rpc.lastSig='';
  }
  function rpcStart(){
    try{
      rpcStop();
      if(!rpcCfg().enabled){ try{ if(lbTab==='presence') renderLbRpc(); }catch{} return; }
      rpcProbe((up)=>{ if(up) rpcPush(true); else { _rpc.lastErr='bridge not found on '+RPC_PORTS.join('/'); try{ if(lbTab==='presence') renderLbRpc(); }catch{} } });
      _rpc.timer=setInterval(()=>{ try{ if(!_rpc.port) rpcProbe((up)=>{ if(up) rpcPush(true); }); else rpcPush(false); }catch{} }, RPC_PUSH_MS);
    }catch{}
  }
  function rpcSet(k, v){
    try{ GMSet('bnc_rpc_'+k, v); }catch{}
    if(k==='enabled' || k==='port' || k==='auto'){ _rpc.port=null; rpcStart(); }
    try{ if(lbTab==='presence') renderLbRpc(); }catch{}
  }
  function renderLbRpc(){
    const body=document.getElementById('bnc-lb-body');
    if(!body) return;
    const sub=document.getElementById('bnc-lb-sub');
    if(sub) sub.textContent='Show what you are playing on Discord';
    const cfg=rpcCfg();
    const on=!!cfg.enabled;
    const up=!!_rpc.up;
    const live=up && _rpc.discord;
    const dot=!on?'#6b7280':(live?'#10b981':(up?'#eab308':'#ef4444'));
    // User wording only: what they get, what is missing, what to do about it.
    const state=!on?'Off'
      :(live?'On — your friends can see what you are playing.'
          :(up?'The tray is running, but the Discord app is closed. Open Discord and it updates itself.'
               :(_rpc.probing?'Looking for the tray…':'The tray is not running yet — launch it below.')));
    const esc=(v)=> String(v==null?'':v).replace(/</g,'&lt;');
    const btn='background:#1f2937;color:#e5e7eb;border:0;border-radius:8px;padding:8px 14px;font-size:11px;font-weight:700;cursor:pointer';
    // No card preview here on purpose: the panel is the switch and the status,
    // nothing else. What the card says lives on Discord itself.
    body.innerHTML='<label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#e5e7eb;font-weight:700;cursor:pointer"><input type="checkbox" id="bnc-rpc-enabled"'+(on?' checked':'')+' style="accent-color:#fff"> Show what I\'m playing on Discord</label>'
      + '<div style="display:flex;align-items:center;gap:8px;margin:10px 0 2px;font-size:12px;color:'+dot+';font-weight:700"><span style="width:8px;height:8px;border-radius:50%;background:'+dot+';display:inline-block"></span>'+esc(state)+'</div>'
      + '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px">'
      + (live
          ? '<button id="bnc-rpc-tray" disabled style="'+btn+';opacity:.45;cursor:default">Tray is running</button>'
          : '<button id="bnc-rpc-tray" style="'+btn+';background:#fff;color:#000">Launch the tray</button>')
      + '<button id="bnc-rpc-push" style="'+btn+'">Refresh now</button>'
      + '<button id="bnc-rpc-clear" style="'+btn+'">Clear card</button>'
      + '</div>'
      + '<div style="margin-top:10px;font-size:11px;color:#6b7280;line-height:1.7">'
      + 'The tray is one small app that keeps this and the game connection running in the background: launch it once and leave it in the system tray. Discord must be open for the card to appear.'
      + '</div>'
      + '<div style="margin-top:14px;font-size:11px;color:#6b7280;line-height:1.7">Friends see your mode, the scene you are in and whether you are in a party — never your rank or points. Two buttons ride along: <b>Join Nectar</b>, and <b>Join my party</b> while your party has a code to share. A lobby with no mode picked yet reads <b>Idle</b> on the card.</div>'
      + '<label style="display:flex;align-items:center;gap:6px;margin-top:14px;font-size:11px;color:#9ca3af;cursor:pointer"><input type="checkbox" id="bnc-rpc-auto"'+(cfg.auto?' checked':'')+' style="accent-color:#fff"> Auto-detect the tray</label>';
    const en=document.getElementById('bnc-rpc-enabled');
    if(en) en.onchange=()=>{ rpcSet('enabled', en.checked); };
    const auto=document.getElementById('bnc-rpc-auto');
    if(auto) auto.onchange=()=>{ rpcSet('auto', auto.checked); };
    const tray=document.getElementById('bnc-rpc-tray');
    if(tray) tray.onclick=()=>{ try{ copyText(RPC_TRAY_CMD, 'Tray ready — launch it once and leave it running'); }catch{} };
    const push=document.getElementById('bnc-rpc-push');
    if(push) push.onclick=()=>{ if(!rpcCfg().enabled){ try{ showToast('Turn presence on first', false); }catch{} return; } _rpc.lastSig=''; _rpc.lastErr=''; _rpc.port=null; rpcStart(); };
    const clr=document.getElementById('bnc-rpc-clear');
    if(clr) clr.onclick=()=>rpcClear();
  }
  // Filters re-apply after the game swaps canvases; presence pushes on its own
  // timer so it keeps working with the panel closed.
  try{ if(typeof BNC_LITE!=="undefined"&&BNC_LITE){} else setInterval(()=>{ try{ gfxApply(); }catch{} }, 4000); }catch{}
  try{ setTimeout(()=>{ try{ gfxApply(); }catch{} try{ rpcStart(); }catch{} }, 1500); }catch{}

  const PLAYSTYLE_TIPS={
    Aggressor:'Drops into fights: 3+ kills per match with a strong K/D.',
    Anchor:'Holds ground: solid K/D and wins half or more of the matches.',
    Support:'Plays for the team: fewer kills but a steady win rate.',
    Wildcard:'Unpredictable mix — every lobby is different.',
  };

  // --- KEYBINDS / SETTINGS MENU (Ctrl+Shift+K) ---
  let keybindOverlay=null;
  function createKeybindOverlay(){
    if(document.getElementById('bnc-keys')) return;
    const ov=document.createElement('div');
    ov.id='bnc-keys';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483646', background:'rgba(0,0,0,0.54)', display:'none', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif'});
    ov.innerHTML = `<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:12px;padding:20px;min-width:360px;max-width:520px;color:#e5e7eb"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><b style="font-size:15px;color:#fff">BuildNow Companion - Shortcuts & Settings</b><button id="bnc-keys-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 8px;cursor:pointer">Close</button></div><div id="bnc-keys-list" style="display:flex;flex-direction:column;gap:6px;font-size:12px"></div><div style="display:flex;gap:8px;margin-top:12px"><button id="bnc-keys-open-drawer" style="flex:1;padding:7px 0;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:#171717;color:#e5e7eb;font-size:12px;font-weight:700;cursor:pointer">Open player info</button><button id="bnc-keys-open-ranked" style="flex:1;padding:7px 0;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:#171717;color:#e5e7eb;font-size:12px;font-weight:700;cursor:pointer">Open ranked panel</button><button id="bnc-keys-open-lb" style="flex:1;padding:7px 0;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:#171717;color:#e5e7eb;font-size:12px;font-weight:700;cursor:pointer">Open leaderboard</button></div><div style="margin-top:8px;font-size:11px;color:#9ca3af"><code>&#96;</code> - This menu (rebindable like any row; Ctrl+Shift+K still works)</div><div style="margin-top:10px;font-size:11px;opacity:0.7">Panels open with one key: <b>F2</b> player info · <b>F4</b> leaderboard · <b>F9</b> ranked. Mac: F-keys work as-is (on small MacBooks hold <b>Fn</b>). Combos with Ctrl/⌘ also work via Change.</div><div style="margin-top:10px"><button id="bnc-keys-reset" style="width:100%;padding:7px 0;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:#171717;color:#e5e7eb;font-size:12px;font-weight:700;cursor:pointer">Reset all shortcuts to defaults</button></div><div id="bnc-dev-row" style="margin-top:14px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.08);font-size:11px;opacity:0.7">Settings: <label style="cursor:pointer"><input type="checkbox" id="bnc-dev-toggle"> Dev mode (feed)</label></div></div>`;
    (document.body||document.documentElement).appendChild(ov);
    ov.addEventListener('click', (e)=>{ if(e.target===ov) ov.style.display='none'; });
    ov.querySelector('#bnc-keys-close').onclick=()=> ov.style.display='none';
    // mouse fallback openers: keybinds are the fast path, but a dead combo
    // (browser/OS eats it) must never lock the user out of the panels.
    try{
      const od=ov.querySelector('#bnc-keys-open-drawer');
      if(od) od.onclick=()=>{ try{ toggleInfoPanel(true); }catch{} };
      const or_=ov.querySelector('#bnc-keys-open-ranked');
      if(or_) or_.onclick=()=>{ try{ toggleRankedPanel(true); }catch{} };
      const ol=ov.querySelector('#bnc-keys-open-lb');
      if(ol) ol.onclick=()=>{ try{ toggleLbPanel(true); }catch{} };
      const rs=ov.querySelector('#bnc-keys-reset');
      if(rs) rs.onclick=()=>{
        try{
          BNC_BINDS.forEach(b=>{ try{ GMDel(b.key); }catch{} });
          renderKeybindRows();
          showToast('Shortcuts reset: F2 player info · F4 leaderboard · F9 ranked', false);
        }catch{}
      };
    }catch{}
    const devT = ov.querySelector('#bnc-dev-toggle');
    devT.checked = !!GMGet('bnc_dev_mode', false);
    devT.onchange=()=>{ GMSet('bnc_dev_mode', devT.checked); showToast(devT.checked? 'Dev mode ON':'Dev mode OFF', false); if(devT.checked){ feedEnabled=true; feedWrap.style.display='block'; } else feedWrap.style.display='none'; try{ syncDevUI(); }catch{} };
    try{ renderKeybindRows(); }catch{}
    try{ syncDevUI(); }catch{}
    keybindOverlay=ov;
  }
  // Render one editable row per bind (dev binds hidden unless isDev()).
  // Re-rendered on every open + every stats poll; skipped mid-capture.
  function renderKeybindRows(){
    if(typeof _keybindCapturing!=='undefined' && _keybindCapturing) return;
    const list=document.getElementById('bnc-keys-list');
    if(!list) return;
    const dev=isDev();
    const rows=BNC_BINDS.filter(b=>!b.dev||dev).map(b=>
      `<div style="display:flex;align-items:center;gap:8px"><span style="flex:1;color:#e5e7eb">${b.label.replace(/</g,'&lt;')}</span><code style="background:#1f2937;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:2px 8px;white-space:nowrap">${formatKeybind(getBindKb(b))}</code><button data-bind="${b.id}" style="background:#171717;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:3px 10px;cursor:pointer;font-size:11px;font-weight:600">Change</button></div>`
    ).join('');
    list.innerHTML=rows;
    list.querySelectorAll('button[data-bind]').forEach(btn=>{
      btn.onclick=()=>{
        const b=BNC_BINDS.find(x=>x.id===btn.dataset.bind);
        if(b) captureBind(b, btn);
      };
    });
  }
  // Capture flow shared by every row: any key or combo, Esc cancels, 8s timeout.
  function captureBind(b, btn){
    if(_keybindCapturing) return;
    _keybindCapturing=true;
    try{ _keybindCaptureAt=Date.now(); }catch{}
    const orig=btn.textContent;
    btn.textContent='Press…';
    const hint=document.createElement('span');
    hint.style.cssText='font-size:11px;color:#6b7280';
    hint.textContent='Press any key or combo. Esc to cancel.';
    btn.after(hint);
    const done=(msg)=>{
      _keybindCapturing=false;
      try{ hint.remove(); }catch{}
      try{ renderKeybindRows(); }catch{}
      if(msg) showToast(msg, false);
      document.removeEventListener('keydown', handler, true);
    };
    const handler=(e)=>{
      if(e.key==='Escape'){ btn.textContent=orig; done(); return; }
      if(['Control','Shift','Alt','Meta'].includes(e.key)) return;
      e.preventDefault(); e.stopPropagation();
      const nb={ctrl:!!e.ctrlKey, meta:!!e.metaKey, shift:!!e.shiftKey, alt:!!e.altKey, key:String(e.key).toUpperCase(), code:String(e.code||'').toUpperCase()||null};
      if(!nb.key) return;
      const warn=bindWarning(nb, b.id);
      if(warn){ hint.textContent='⚠ '+warn; return; }  // stay capturing
      try{ GMSet(b.key, JSON.stringify(nb)); }catch{ try{ GMSet(b.key, nb); }catch{} }
      done(`${b.label} set to ${formatKeybind(nb)}`);
    };
    document.addEventListener('keydown', handler, true);
    setTimeout(()=>{ if(_keybindCapturing){ btn.textContent=orig; done(); } }, 8000);
  }
  function toggleKeybinds(){ if(!keybindOverlay) createKeybindOverlay(); const isHidden=keybindOverlay.style.display==='none' || !keybindOverlay.style.display; keybindOverlay.style.display = isHidden ? 'flex' : 'none'; if(isHidden){ try{ renderKeybindRows(); }catch{} try{ syncDevUI(); }catch{} } }
  // Hotkeys: drawer (customizable) + Ctrl+Shift+K keybinds (guard modal focus)
  // + Backquote menu bind (guard typing). Stuck capture flag auto-releases.
  // Composing in our box silences hotkeys too (Escape still releases first).
  document.addEventListener('keydown', (e)=>{
    if(_keybindCapturing) return;
    try{ releaseStuckCapture(); }catch{}
    if((e.key||'')!=='Escape'){ try{ if(untrapHoldActive()) return; }catch{} }
    // Held-key auto-repeat must never re-fire toggles: the repeat would
    // instantly undo the press (F9 panel flashing open-shut). Nothing in
    // this dispatcher is repeat-meaningful — every action is a toggle/menu.
    if(e.repeat) return;
    if (isModalOrInputFocused() || isTypingKey()) return;
    if((e.key||'')==='Escape' && document.getElementById('bnc-ranked-code')){ e.preventDefault(); try{ hideRankedCodeModal(); }catch{} return; }
    if((e.key||'')==='Escape' && typeof lbPanel!=='undefined' && lbPanel && lbPanel.style.display!=='none' && lbPanel.style.display){ e.preventDefault(); try{ untrapRelease(); }catch{} toggleLbPanel(false); return; }
    if((e.key||'')==='Escape' && typeof rankedPanel!=='undefined' && rankedPanel && rankedPanel.style.display!=='none' && rankedPanel.style.display){ e.preventDefault(); toggleRankedPanel(false); return; }
    if((e.key||'')==='Escape' && typeof host11Panel!=='undefined' && host11Panel && host11Panel.style.display!=='none' && host11Panel.style.display){ e.preventDefault(); toggleHost11Panel(false); return; }
    if((e.key||'')==='Escape' && typeof host12Panel!=='undefined' && host12Panel && host12Panel.style.display!=='none' && host12Panel.style.display){ e.preventDefault(); toggleHost12Panel(false); return; }
    if(isDrawerKeybind(e)){ e.preventDefault(); e.stopPropagation(); toggleInfoPanel(); return; }
    if(isRankedKeybind(e)){ e.preventDefault(); e.stopPropagation(); toggleRankedPanel(); return; }
    if(isLbKeybind(e)){ e.preventDefault(); e.stopPropagation(); toggleLbPanel(); return; }
    if(isHost11Keybind(e)){ e.preventDefault(); e.stopPropagation(); toggleHost11Panel(); return; }
    if(isHost12Keybind(e)){ e.preventDefault(); e.stopPropagation(); toggleHost12Panel(); return; }
    if(modKey(e) && e.shiftKey && (e.key||'').toUpperCase()==='K'){ e.preventDefault(); e.stopPropagation(); toggleKeybinds(); }
  }, true);

  // Ranked UI removed — ranked is not ready. (Host mode toggle removed too;
  // individualist tracking only for now.)

  function isPhotonUrl(url){ if(!url) return false; const u=String(url); if(u.includes(APP_ID)) return true; for(const h of PHOTON_HOSTS) if(u.toLowerCase().includes(h)) return true; if(u.includes('wss://')&&u.includes('exitgames')) return true; return false; }
  function shortUrl(url){ return String(url).replace(/^wss?:\/\//,'').replace(/\?libversion.*$/,'').slice(0,80); }
  function labelForUrl(url){ const u=String(url); if(u.includes('ns.exitgames.com')) return 'NameServer'; if(u.includes('gcams')){ const m=u.match(/gcams(\d+)/); return m?`GCAMS${m[1]}`:'GCAMS'; } if(u.includes('exitgames')) return 'Photon'; return 'WS'; }
  function hexOf(bytes){ let s=''; for(let i=0;i<bytes.length;i++) s+=(bytes[i]<16?'0':'')+bytes[i].toString(16); return s; }
  function isEssentialBytes(bytes){
    if(!bytes||bytes.length<3) return false;
    const type=bytes[1]; const code=bytes[2];
    if(type===6||type===7) return false;
    // Operations: only Join/Create/JoinRandom/Authenticate (225-231)
    if(type===2||type===3) return ESSENTIAL_OPS.has(code);
    // Events: only Join(255), Leave(254), PropertiesChanged(253)
    if(type===4){
      if(DROP_EVS_NOISY.has(code)) return false;
      // PropertiesChanged (253): keep all - filter useful props later in enqueue
      if(code===253) return true;
      return ESSENTIAL_EVS.has(code);
    }
    return type===1||type===5;
  }

  let captureSessionId=null;
  try{ captureSessionId=(globalThis.crypto&&crypto.randomUUID)?crypto.randomUUID():null; }catch{}
  if(!captureSessionId) captureSessionId='sniff-'+Math.random().toString(16).slice(2)+'-'+Date.now().toString(36);
  let seq=0, queue=[], flushTimer=null, flushInFlight=false, _flushStartAt=0;
  let lastCaptureBlockedLog=0;
  // Capture health: stamped on every successful /api/capture. The drawer dot
  // is stats-based (green while stats poll), which hides a dead capture
  // session — these counters let the dot + logs tell them apart. Never raise.
  let lastCaptureOkAt=0, _capFails=0, _capSeen=0, _lastCapWarn=0, _capBackoffMs=0;
  function captureAgeMs(){ try{ return lastCaptureOkAt? Date.now()-lastCaptureOkAt : -1; }catch{ return -1; } }
  // Footer dot truth: Offline (no api) / Login (no session) / Online (capture
  // fresh <90s) / Idle (hook alive, no essential packets — normal lobby idle)
  // / Cap STALE (hook dead too, no WS for 90s+).
  function updateApiDot(){
    try{
      const dot=document.getElementById('bnc-drawer-dot');
      const apiTxt=document.getElementById('bnc-drawer-api-text');
      if(!dot && !apiTxt) return;
      let color='#6b7280', txt='Offline';
      if(!apiOnline){ color='#6b7280'; txt='Offline'; }
      else if(!sessionId){ color='#6b7280'; txt='Login'; }
      else{
        const age=captureAgeMs();
        let sage='';
        try{ if(_lastStatsAt) sage=` stats ${Math.round((Date.now()-_lastStatsAt)/1000)}s`; }catch{}
        if(age>=0 && age<90000){ color='#10b981'; txt='Online'+sage; }
        else if(wsFresh()){ color='#3b82f6'; txt='Idle'+sage; }
        else{ color='#f59e0b'; txt='Cap STALE'+sage; }
      }
      if(dot) dot.style.background=color;
      if(apiTxt) apiTxt.textContent=txt;
      // tooltip shows which backend is active (local vs production)
      try{
        const base=resolveApiBase();
        const wrap=document.getElementById('bnc-drawer-api');
        if(wrap) wrap.title='API: '+base;
      }catch{}
    }catch{}
  }
  // v2.19.0 streak toasts + update nudge (never spam, never misfire).
  // Each streak toasts only when THAT streak advanced since the last poll:
  // the old signature check fired the daily branch on any unrelated change
  // (e.g. a win that moved win 0->1 showed "daily" instead of nothing).
  let _lastStatsAt=0, _lastStreakWin=null, _lastStreakDaily=null, _updateNudged=false;
  function maybeStreakToast(){
    try{
      const st=statsCache && statsCache.arena && statsCache.arena.streaks;
      if(!st) return;
      const w=Number(st.win||0), d=Number(st.daily||0);
      if(_lastStreakWin===null){ _lastStreakWin=w; _lastStreakDaily=d; return; }
      if(w>=2 && w>_lastStreakWin) showToast(`🔥 Win streak x${w}`, false);
      else if(d>=2 && d>_lastStreakDaily) showToast(`📅 Daily streak x${d}`, false);
      _lastStreakWin=w; _lastStreakDaily=d;
    }catch{}
  }

  // ---- LOCAL ROOM FLAG (instant players-tab visibility) ----
  // Server truth (stats poll) lags joins by seconds; this flag flips the
  // moment OUR create/join ops hit the wire, so the Players tab appears on
  // the packet, not on the next poll. Cleared by auth/leave ops, by fresh
  // server truth newer than the signal, or by a 3-minute timeout. It can
  // only ever ADD visibility, never hide a server-confirmed room. Never throws.
  let _localRoom={active:false, at:0};
  function noteLocalRoom(active){
    try{
      if(active){
        _localRoom.active=true; _localRoom.at=Date.now();
        try{ schedulePlayersRefresh('local-join'); }catch{}
      } else { _localRoom.active=false; _localRoom.at=0; }
    }catch{}
  }
  function localRoomActive(){
    try{
      if(!_localRoom.active) return false;
      if(Date.now()-_localRoom.at>180000) return false;
      if(_lastStatsAt && _lastStatsAt>_localRoom.at){
        const s=statsCache&&statsCache.session;
        const rooms=s&&s.rooms; const cid=s&&s.current_room_id;
        const cr=cid&&rooms?rooms[cid]:null;
        if(!cr||String(cr.room_id||'')==='lobby') return false;
      }
      return true;
    }catch{ return false; }
  }

  // ---- PROVEN SELF-LEAVE ----
  // One path for "I am no longer in a room", shared by MY LeaveRoom op (254
  // out) and an inbound Leave event whose ActorNr is PROVEN to be mine
  // (isMyActor). Both are the game telling us directly that the room is gone,
  // so the room flags, the optimistic lobby stamp and the ranked UI all come
  // down here — and the tracked actor number is dropped with the room (the next
  // join/create response pins the next one). Never throws.
  function _selfLeftRoom(reason){
    const why=reason||'self-leave';
    try{ schedulePlayersRefresh(why); }catch{}
    // MY leave is a LOBBY trigger, exactly like an auth: the game goes back to
    // the lobby without ever re-sending a scene, so waiting for a curScn left
    // the client "in match" for the whole lobby (paused music, match UI up,
    // wrong Discord card). Stamp the scene as lobby here. via:'leave' ends any
    // endless-series hold: a proven self-leave is the series end.
    try{ _applyOptimisticCurScn('MainLobby', {via:'leave'}); }catch{}
    try{ noteLocalRoom(false); }catch{}
    try{ _publicRoom=false; _compRoom=null; _compHudData=null; updateTourneyHud(); resetRtRoomLocal(); }catch{}
    try{ disableRanked(why); }catch{}
    try{ _clearMyActor(why); }catch{}
  }

  function enqueue(dir, bytes, url){
    if(isLocked) return;
    if(!bytes||!bytes.length) return;
    try{ _capSeen++; lastWsSeenAt=Date.now(); }catch{}    if(!sessionId || !currentReqToken){
      if(Date.now()-lastCaptureBlockedLog>5000){ lastCaptureBlockedLog=Date.now(); logInfo('capture skipped - no session (login required)'); }
      return;
    }
    const isEssential = isEssentialBytes(bytes);
    const rawHex = hexOf(bytes);
    const type = bytes[1], code = bytes[2];
    const rec = { seq:++seq, t:Date.now(), dir, url: shortUrl(url), fullUrl: String(url).slice(0,256), label: labelForUrl(url), rawHex, _isEssential: isEssential, _type:type, _code:code };
    // MY actor number (see _myActor): Photon returns it on MY join/create
    // response and echoes it on my own Join/PropertiesChanged event. Learned
    // HERE, before the leave/ranked chain below, so a Leave event in the same
    // batch can already be proven to be mine. Never throws.
    try{
      if(dir==='in'){
        const _ma=sniffMyActor(bytes, type);
        if(_ma && _ma.actorNr!=null){
          if(type===3 && (code===225||code===226||code===227)) _noteMyActor(_ma.actorNr, 'join-resp');
          // Event fallback (capture started mid-room, or the response was never
          // seen): only a packet that NAMES me pins a number, and only while
          // nothing is pinned yet — a same-named stranger must never re-pin it.
          else if(_myActor.nr==null && _ma.name && _isMyGameName(_ma.name)) _noteMyActor(_ma.actorNr, code===255?'join-ev':'props-ev');
        }
      }
    }catch{}
    // ranked tracking (comp-copy): ONLY on MY join/create (226/227 out):
    // pID present -> track the code, pid-less (public matchmaking) ->
    // ranked mode OFF. Auth (230/231) never touches the leave guard
    // (mid-match re-auths must recover); MY leave op (254 out) always
    // hides ranked UI + sets the guard. Incoming 226/227
    // (server responses/echoes) never touch tracking.
    // Incoming Leave events (254 in) belong to somebody else UNLESS the event's
    // ActorNr is the number Photon pinned to ME (isMyActor): then it is my own
    // leave and runs the exact same teardown as my leave op — proof, not a guess
    // from the leaver's name. Either way the player list is refreshed, because
    // the server prunes on Leave.
    try{
      if((type===2||type===3)&&(code===230||code===231)){ schedulePlayersRefresh('auth'); try{ _clearMyActor('auth'); }catch{} try{ noteLocalRoom(false); }catch{} try{ _publicRoom=false; _compRoom=null; _compHudData=null; updateTourneyHud(); resetRtRoomLocal(); }catch{} }
      else if((type===2||type===3)&&code===254&&dir==='out') _selfLeftRoom('leave-op');
      else if(type===4&&code===254&&dir==='in'){
        // PROVEN self-leave: a Leave event carries its ActorNr, and when that
        // number is the one Photon gave MY join the event is about me. Anything
        // else is somebody else leaving: refresh the list, never tear me down.
        let _selfEv=false;
        try{ const _meEv=sniffMyActor(bytes, type); _selfEv=!!(_meEv && isMyActor(_meEv.actorNr)); }catch{}
        if(_selfEv) _selfLeftRoom('leave-ev');
        else schedulePlayersRefresh('leave-ev');
      }
      else if((type===2||type===3)&&(code===225||code===226||code===227)&&dir==='out'){
        try{ noteLocalRoom(true); }catch{}
        // comp-room check on the join pID (stable comp detection) + ranked
        // intent copy on the same pID. Pid-less joins are public
        // matchmaking: never resolve, drop any stale tourney/ranked state.
        try{
          const _cpid=sniffPid(rawHex);
          if(_cpid){ _publicRoom=false; fetchCompRoom(_cpid); try{ fetchRtRoom(_cpid); }catch{} try{ rankedTrack(_cpid); }catch{} }
          else { _publicRoom=true; _compRoom=null; _compHudData=null; updateTourneyHud(); resetRtRoomLocal(); try{ disableRanked('public-join'); }catch{} }
        }catch{}
        try{ schedulePlayersRefresh('join-room'); }catch{}
      }
      // stG match markers (match start/end) ride 253s and join/create
      // responses — hook for the instant win toast + stats pull (no-op untracked).
      // (Ranked stG detection removed; stG still drives flush priority below.)
    }catch{}
    if(collectMode){
      collectedPackets.push({...rec, _bytes: new Uint8Array(bytes)});
      if(collectedPackets.length > COLLECT_MAX) collectedPackets.shift();
      try{ updateCollectBadge(); }catch{}
      logInfo(`COLLECT ${isEssential?'essential':'filtered'} ${rec.label} seq=${rec.seq} len=${bytes.length} -> ${collectedPackets.length}`);
      const hasSkin = rawHex.includes('736b6964');
      const hasPfid = rawHex.includes('70666964');
      feedLog('collect', {type, code, dir, label:rec.label, essential:isEssential, hex:rawHex.slice(0,12)+'..', count:collectedPackets.length, skin:hasSkin?'yes':'-', gid:hasPfid?'yes':'-'});
      if(hasSkin) feedLog('skin', {type, code, dir, hasSkin:true});
    }
    if(!isEssential){
      feedLog('filtered', {type, code, dir, label:rec.label, hex:rawHex.slice(0,12)+'..'});
      return;
    }

    // PropertiesChanged (253) filtering: keep packets with keys the server needs
    // (scene, alive, match marker, skins, teams, zone, party). Everything else is noise.
    if(type===4 && code===253) {
      const hx = rawHex.toLowerCase();
      const KEEP_253 = [
        '63757253636e', // curScn (scene changes)
        '616c76',       // alv (alive/eliminated)
        '737447',       // stG (match start/end marker)
        '736b6964',     // skid (skin)
        '706964',       // pid (pickaxe)
        '70666964',     // pfid (game id)
        '42506964',     // BPid
        '5f7074',       // _pt (team)
        '7074',         // pt (team fallback)
        '5061727479',   // Party
        '704944',       // pID (private room code)
        '627a5f',       // bz_ (zone phase/time/pattern)
        '7265616479',   // ready
        '323535',       // "255" (player name key)
      ];
      if(!KEEP_253.some(k => hx.includes(k))) {
        feedLog('filtered', {type, code, dir, reason:'no useful prop', hex:rawHex.slice(0,12)+'..'});
        return;
      }
    }

    // Optimistic parsing: detect curScn from raw bytes immediately
    if(type===4 && code===253 && dir==='in') {
      try {
        const curScnMatch = rawHex.match(/63757253636e07([0-9a-f]{2})([0-9a-f]+)/);
        if(curScnMatch) {
          const len = parseInt(curScnMatch[1], 16) * 2;
          const valHex = curScnMatch[2].slice(0, len);
          let curScn = '';
          for(let i = 0; i < valHex.length; i += 2) {
            curScn += String.fromCharCode(parseInt(valHex.slice(i, i+2), 16));
          }
          if(curScn && curScn.length > 0) {
            _applyOptimisticCurScn(curScn);
            try{ updateRankedMatchUi(); }catch{}
          }
        }
      } catch(e) {}
    }

    // Optimistic: detect Authenticate (opCode 230 = 0xE6) outgoing → lobby.
    // First auth = login: set lobby state silently (never touch audio — it's
    // either already starting or will start via stats). Later auths = back to
    // lobby → normal path, which unpauses if we flipped out of a match.
    // via:'auth' ends any endless-series hold: authenticate is the series end.
    if(dir==='out' && type===2 && (code===230 || code===231)) {   // both auth ops send us to the lobby
      _authCount++;
      if(_authCount === 1) {
        _applyOptimisticCurScn('MainLobby', {silent:true, via:'auth'});
      } else {
        _applyOptimisticCurScn('MainLobby', {via:'auth'});
      }
    }

    // Optimistic: detect killfeed (type=4, code=101) → increment kills/deaths immediately
    if(type===4 && code===101 && dir==='in') {
      try {
        const kfHex = rawHex.toLowerCase();
        // GPB string extraction: find String type (07), parse length, extract UTF-8
        function extractGpbStrings(hex) {
          const strings = [];
          let i = 0;
          while(i < hex.length - 2) {
            if(hex.slice(i, i+2) === '07') { // String type (two hex chars)
              i += 2;
              // parse variable-length count
              let len = 0;
              let shift = 0;
              let b;
              do {
                if(i >= hex.length) break;
                b = parseInt(hex.slice(i, i+2), 16);
                i += 2;
                len |= (b & 0x7f) << shift;
                shift += 7;
              } while(b & 0x80);
              // extract string bytes
              if(len > 0 && i + len*2 <= hex.length) {
                let s = '';
                for(let j = 0; j < len; j++) {
                  const c = parseInt(hex.slice(i + j*2, i + j*2 + 2), 16);
                  if(c >= 32 && c < 127) s += String.fromCharCode(c);
                }
                strings.push(s);
                i += len * 2;
              }
            } else {
              i += 2;
            }
          }
          return strings;
        }
        const strings = extractGpbStrings(kfHex);
        // visibility: log every string in 101 packets so unknown verbs/formats show in feed
        try{ feedLog('killfeed', {strings: strings.slice(0,4)}); }catch{}
        // find killfeed string (contains "killed", "eliminated" or "knocked")
        const kfStr = strings.find(s => s.includes('killed') || s.includes('eliminated') || s.includes('knocked'));
        if(kfStr) {
          const isKill = kfStr.includes('killed') || kfStr.includes('eliminated');
          const sep = kfStr.includes('killed') ? 'killed' : (kfStr.includes('eliminated') ? 'eliminated' : 'knocked');
          const parts = kfStr.split(sep);
          if(parts.length >= 2) {
            // killer: text before separator, strip color tags
            const killer = parts[0].replace(/<[^>]*>/g, '').trim();
            // victim: text after separator, strip color tags
            const victim = parts[1].replace(/<[^>]*>/g, '').trim();
            if(killer && victim) {
              logSys(`[OPTIMISTIC] killfeed detected killer=${killer} victim=${victim}`);
              try{ console.log(`%c[BNC]%c KILL killer=${killer} victim=${victim} seq=${rec.seq}`, 'color:#ef4444;font-weight:900',''); }catch{}
              try{ _applyOptimisticKillfeed(killer, victim); }catch{}
              // True optimism: toast + delta NOW, no server roundtrip.
              // Kills only (knocks don't seal). Death is silent here —
              // the seal toast covers it.
              // (Ranked optimistic delta removed with detection.)
            }
          }
        } else {
          // No known verb: never stay silent — raw strings diagnose new formats.
          try{ console.log(`%c[BNC]%c KILL-RAW seq=${rec.seq} strings=${JSON.stringify(strings.slice(0,4))}`, 'color:#f59e0b;font-weight:900',''); }catch{}
        }
      } catch(e) {}
    }

    delete rec._isEssential; delete rec._type; delete rec._code;
    queue.push(rec);
    feedLog('capture', {type, code, dir, label:rec.label, seq:rec.seq, hex:rawHex.slice(0,12)+'..', queued:queue.length});
    // Priority lane: kills + match markers skip the batch timer — the
    // toast/seal path needs them now, not in 250ms. Everything else batches.
    try{
      const _isKill=(type===4 && code===101);
      const _isMarker=((type===4 && code===253) || ((type===2||type===3)&&(code===226||code===227))) && sniffStg(rawHex)!=null;
      if(_isKill || _isMarker){ flush(); return; }
    }catch{}
    scheduleFlush();
  }
  function ensureHex(rec){ if(rec.rawHex) return rec.rawHex; try{ rec.rawHex=hexOf(rec._bytes); delete rec._bytes; }catch{ rec.rawHex=''; } return rec.rawHex; }
  function scheduleFlush(){ if(flushTimer) return; if(queue.length>=BATCH_MAX) flush(); else flushTimer=setTimeout(flush, FLUSH_MS); }
  // Heartbeat: every 5s send an empty test-cap batch so the server session
  // stays warm (_cached_at slides, stats/presence stay fresh) through quiet
  // stretches (idle lobby, mobile players). Records carry NO rawHex, so the
  // server skips them before engine.feed AND the processor — zero effect on
  // kills/wins/versions/seals — and NO req_token, so no rotation is burned.
  // A 200 response stamps client liveness (dot Online, captureFresh true):
  // the server answered fine, so Cap STALE must not fire while heartbeats
  // land. Ghost safety: the response body is ignored (no noteCaptureSnap),
  // and server session freshness runs on engine last_packet_at (real
  // packets only), so a truly dead session still ages out server-side and
  // its ranked/comp blocks drop. Never throws.
  let _lastRealFlushAt=0, _hbTimer=null, _hbLoggedOk=false, _hbFails=0;
  function sendHeartbeat(){
    try{
      if(isLocked) return;
      if(!sessionId) return;
      if(lastWsSeenAt<=0) return; // game traffic never seen on this page
      const now=Date.now();
      if(now-_lastRealFlushAt<5000) return; // real traffic already fresh
      if(flushInFlight) return;
      apiPost('/api/capture', { sessionId: captureSessionId, records: [{hb:1, t:now}], client_version:(typeof BNC_VERSION!=='undefined'?BNC_VERSION:null), _quiet:true, _noLiveness:true, _noToken:true, _noWipe:true }, (ok)=>{
        try{
          if(ok){
            _hbFails=0;
            try{ lastCaptureOkAt=Date.now(); lastCaptureSentAt=Date.now(); _capFails=0; }catch{}
            if(!_hbLoggedOk){ _hbLoggedOk=true; try{ logInfo('[CAPTURE] heartbeat ok (session warm)'); }catch{} }
          }
          else{
            _hbLoggedOk=false; _hbFails++;
            // persistent failure (dead session, long outage): stop instead
            // of erroring every 5s forever. Restarts on next login (the
            // session watcher calls startHeartbeat on session change).
            if(_hbFails>=12){ try{ logErr('[CAPTURE] heartbeat failing 60s+ - stopping (re-login resumes it)'); }catch{} try{ if(_hbTimer){ clearInterval(_hbTimer); _hbTimer=null; } }catch{} }
          }
        }catch{}
      });
    }catch{}
  }
  function startHeartbeat(){ try{ if(_hbTimer) return; _hbTimer=setInterval(sendHeartbeat, (typeof BNC_LITE!=="undefined"&&BNC_LITE)?15000:5000); }catch{} }
  function flush(){
    if(isLocked) { flushTimer=null; queue=[]; return; }
    if(!queue.length){ flushTimer=null; return; }
    if(!sessionId || !currentReqToken){ flushTimer=null; queue=[]; try{ if(Date.now()-lastCaptureBlockedLog>5000){ lastCaptureBlockedLog=Date.now(); logInfo('capture flush dropped - no session (login required)'); } }catch{} return; }
    // hard guard: a POST that never calls back (hung socket, no timeout path)
    // would brick captures forever while stats stays green. 30s without a
    // callback = force-reset loudly; the batch is already spliced, so at most
    // one batch is lost instead of the whole session.
    if(flushInFlight){
      try{
        if(Date.now()-(_flushStartAt||0)>30000){
          flushInFlight=false;
          try{ logErr('[CAPTURE] flush stuck >30s - force reset (a POST never called back)'); }catch{}
        }
      }catch{}
      if(flushInFlight){ if(!flushTimer) flushTimer=setTimeout(flush, FLUSH_MS); return; }
    }
    flushTimer=null;
    const batch=queue.splice(0,BATCH_MAX);
    for(const r of batch) ensureHex(r);
    const payload={ sessionId: captureSessionId, records: batch, client_version:(typeof BNC_VERSION!=='undefined'?BNC_VERSION:null) };
    try{ _lastRealFlushAt=Date.now(); }catch{}
    flushInFlight=true;
    try{ _flushStartAt=Date.now(); }catch{}
    feedLog('flush', {count:batch.length, queued:queue.length});
    apiPost('/api/capture', payload, (ok, txt)=>{
      flushInFlight=false;
      if(ok){
        try{ lastCaptureOkAt=Date.now(); _capFails=0; _capBackoffMs=0; }catch{}
        try{
          const j=JSON.parse(txt);
          if(j.new_req_token) { storeSession({new_req_token:j.new_req_token, new_counter:j.new_counter}); logInfo(`capture token rotated ${String(j.new_req_token).slice(0,4)}…`); feedLog('ok', {rotated:j.new_req_token.slice(0,4)}); }
          if(j.packets && j.packets.length){
            for(const p of j.packets){
              const dec = p.decrypted_hex || p.raw || {};
              // full decrypted payload - no truncation
              const fields = {packet:p.packet || p.packet_type, seq:p.seq, dir:p.dir, human:p.human || ''};
              if(p.killer || p.victim) { fields.killer=p.killer; fields.victim=p.victim; }
              if(p.player_name) fields.player=p.player_name;
              // console: one line per kill + alv so captures are visible
              // without the dev feed (zone/stG already log unconditionally).
              try{
                const _pt=p.packet || p.packet_type;
                if(_pt==='kill' && (p.killer || p.victim)){
                  console.log(`%c[BNC]%c KILL killer=${p.killer||'?'} victim=${p.victim||'?'} seq=${p.seq}`, 'color:#ef4444;font-weight:900','');
                } else if(_pt==='alive' || _pt==='eliminated'){
                  console.log(`%c[BNC]%c ALV name=${p.player_name||'?'} alive=${_pt==='alive'} seq=${p.seq}`, 'color:#3b82f6;font-weight:900','');
                }
              }catch{}
              if(p.zone_time!=null) fields.zone_time=p.zone_time;
              if(p.zone_phase!=null) fields.zone_phase=p.zone_phase;
              if(p.short_code) fields.code=p.short_code;
              if(p.room_gm) fields.gm=p.room_gm;
              if(p.room_id) fields.room_id=p.room_id.slice(0,12);
              if(p.match_version!=null) fields.ver=p.match_version;
              // stG change - explicit log (full props, no truncation)
              try{
                const stg = dec && (dec.properties ? dec.properties.stG : dec.stG);
                const lastStg = p._lastStg !== undefined ? p._lastStg : null;
                if(stg!=null) fields.stG = stg;
                // engine already updates last_stg; we also emit dedicated stg line when present
                if(dec && dec.properties && 'stG' in dec.properties){
                  feedLog('stg', {seq:p.seq, stG: dec.properties.stG, gm: p.room_gm || p.game_mode || '', room: (p.room_id||'').slice(0,8), curScn: p.cur_scn || '', human: p.human || '', full: dec.properties});
                }
              }catch{}
              // full gpb - no slice
              if(dec && Object.keys(dec).length) fields.gpb = dec;
              // full hex - no truncation
              feedLog('decrypted', fields);
              feedLog('hex', {seq:p.seq, hex:p.rawHex||''});
              // dedicated ZONE log - full fields, non-truncated (wwzw spams 3-4 ticks per phase with huge floats)
              const isZonePkt = (p.packet==='zone' || p.packet_type==='zone' || p.zone_time!=null || (dec && dec.properties && ('bz_phase_start' in dec.properties || 'bz_zone' in dec.properties || 'bz_phase' in dec.properties)));
              if(isZonePkt){
                const zoneFields = {seq:p.seq, dir:p.dir, gm:p.room_gm || p.game_mode || '', zone_time:p.zone_time, zone_phase:p.zone_phase, code:p.short_code||'', room:(p.room_id||'').slice(0,8), ver:p.match_version, curScn:p.cur_scn||'', zone:p.zone||null, rawHex:(p.rawHex||'').slice(0,1200), gpb:dec};
                // feed: cap 12k for zone, so full gpb visible
                feedLog('zone', zoneFields);
                // console: always full for debugging wwzw multi-ticks
                try{
                  console.log(`%c[BNC][ZONE]%c seq=${p.seq} gm=${zoneFields.gm} time=${String(p.zone_time)} phase=${String(p.zone_phase)} ver=${String(p.match_version)}`, 'color:#a855f7;font-weight:900','', {packet:p, decrypted:dec, hex:p.rawHex});
                  logGroup(`ZONE seq=${p.seq} time=${p.zone_time} phase=${p.zone_phase}`, ()=>{ console.log('packet', p); console.log('decrypted', dec); console.log('hex', p.rawHex); });
                }catch{}
              }
            }

            // Apply server reconciliation from capture response (shared helper)
            // + instant ranked refresh: captures arrive far more often than
            // stats polls during play — drive ranked UI off them (throttled
            // to 2s; polls + transitions cover the rest).
            let _capFlip=false;
            try{ noteCaptureSnap(j); }catch{}
            // (Ranked optimistic delta removed with detection: server seals
            // + stats poll are the only points path.)
            try{ _capFlip=_reconcileServerState(j.session); }catch{}
            try{
              const _now=Date.now();
              if(_now-(_lastCapUiAt||0)>=2000){
                _lastCapUiAt=_now;
                updateRankedOverlay(); updateRankedLobby(); updateRankedMatchUi(); updatePtsToast();
              }
            }catch{}
            if(_capFlip){ try { renderInfoPanel(); }catch{} }
          }
        }catch(e){ feedLog('decrypted', {err:e.message}); }
        feedLog('sent', {count:batch.length, ok:true});
      } else {
        try{ _capFails++; }catch{}
        let _lockScope='default'; try{ const _j=JSON.parse(txt||'{}'); if(_j.lock_scope) _lockScope=_j.lock_scope; if(_j.is_admin_locked) _lockScope='admin'; }catch{}
        const lower = String(txt).toLowerCase();
        if(lower.includes('locked') || lower.includes('account locked')){
          isLocked = true; queue=[]; flushTimer=null;
          logErr(`account locked - blocking game ${String(txt).slice(0,120)} scope=${_lockScope}`);
          showLockedPanel(String(txt).toLowerCase().includes('admin')? 'Account admin-locked' : 'Account locked', _lockScope);
          return;
        }
        logErr(`capture failed - requeue ${batch.length} txt=${String(txt).slice(0,120)}`);
        feedLog('err', {count:batch.length, ok:false, txt:String(txt).slice(0,120)});
        if(String(txt).includes('Invalid req_token') || String(txt).includes('expired')){
          queue=[]; feedLog('err', {msg:'token error - re-link'});
          if(String(txt).toLowerCase().includes('session expired')){
            // 2-day session expiry: onSessionExpired drops the session (login
            // token kept), re-validates silently and shows the refresh modal -
            // including the /refresh-token instructions when the saved token is
            // no longer good.
            try{ onSessionExpired('capture'); }catch{}
          }
        } else {
          queue=batch.concat(queue).slice(0,BATCH_MAX*4);
        }
      }
      if(queue.length){
        // A failing endpoint used to be retried every 40ms FOREVER: the same
        // batch replayed ~25x/second, which buried the server log in old
        // packets while the player was idle. Back off exponentially (1s ->
        // 30s) and say so once, then keep the queue intact for when the
        // server comes back.
        try{
          if(_capFails){ _capBackoffMs = _capBackoffMs ? Math.min(30000, _capBackoffMs*2) : 1000; }
          else { _capBackoffMs = 0; }
        }catch{ _capBackoffMs=0; }
        flushTimer=setTimeout(flush, _capBackoffMs || 40);
      }
    });
  }

  const hookKey='__bnc_hooked__';
  const win=(typeof unsafeWindow!=='undefined'&&unsafeWindow!==window)?unsafeWindow:window;
  const windowsToHook=new Set([window, win].filter(Boolean));
  function toBytes(data){
    try{
      if(!data) return null;
      if(data instanceof ArrayBuffer) return new Uint8Array(data.slice(0));
      if(ArrayBuffer.isView(data)) return new Uint8Array(data.buffer.slice(data.byteOffset||0,(data.byteOffset||0)+data.byteLength));
      const tag=Object.prototype.toString.call(data);
      if(tag==='[object ArrayBuffer]') return new Uint8Array(data.slice(0));
      if(tag.includes('Array')&&data&&data.buffer){ try{ return new Uint8Array(data.buffer.slice(data.byteOffset||0,(data.byteOffset||0)+data.byteLength)); }catch{} }
    }catch{} return null;
  }
  function peekHeader(data){
    try{
      if(!data) return null;
      if(data instanceof ArrayBuffer){ const v=new Uint8Array(data); return v.length>=3?{type:v[1],code:v[2]}:null; }
      if(ArrayBuffer.isView(data)) return data.length>=3?{type:data[1],code:data[2]}:null;
    }catch{} return null;
  }
  function isEssentialHeader(h){
    if(!h) return false;
    if(h.type===6||h.type===7) return false;
    if(h.type===2||h.type===3) return ESSENTIAL_OPS.has(h.code);
    if(h.type===4){ if(DROP_EVS_NOISY.has(h.code)) return false; return ESSENTIAL_EVS.has(h.code); }
    return h.type===1||h.type===5;
  }
  function captureOut(bytes, url){ try{ if(bytes&&bytes.length) enqueue('out', bytes, url||'unknown'); }catch{} }
  function captureIn(bytes, url){ try{ if(bytes&&bytes.length) enqueue('in', bytes, url||'unknown'); }catch{} }

  for(const w of windowsToHook){
    try{
      const Proto=w.WebSocket&&w.WebSocket.prototype;
      if(!Proto||Proto[hookKey+'_proto']) continue;
      const origSend=Proto.send;
      Proto.send=function(data){
        try{
          const u=this.url||'';
          if(isPhotonUrl(u)){
            try{ _netNote('out', netLen(data)); }catch{}
            try{ _pingNote('out', data); }catch{}
            const h=peekHeader(data);
            if(h&&!isEssentialHeader(h)) return origSend.call(this,data);
            const b=toBytes(data); if(b) captureOut(b,u);
            else if(typeof Blob!=='undefined'&&data instanceof Blob) data.arrayBuffer().then(ab=>{ const bb=toBytes(ab); if(bb) captureOut(bb,u); }).catch(()=>{});
            if(h && !authToastShown && (h.type===2||h.type===3) && (h.code===230||h.code===231)){
              authToastShown=true;
              try{
                const n=discordUsername || GMGet('bnc_discord_username','') || '';
                if(n) showToast(`Welcome back @${n}!`, false);
                else showToast('Welcome back!', false);
              }catch{}
            }
          }
        }catch{}
        return origSend.call(this,data);
      };
      const origAdd=Proto.addEventListener;
      Proto.addEventListener=function(type, listener, opts){
        if(type==='message'&&typeof listener==='function'&&isPhotonUrl(this.url||'')){
          const self=this;
          const wrapped=function(ev){
            try{
              const d=ev.data;
              try{ _netNote('in', netLen(d)); }catch{}
              try{ _pingNote('in', d); }catch{}
              if(peekHeader(d)&&!isEssentialHeader(peekHeader(d))) return listener.call(this,ev);
              const b=toBytes(d); if(b) captureIn(b,self.url);
              else if(typeof Blob!=='undefined'&&d instanceof Blob) d.arrayBuffer().then(ab=>{ const bb=toBytes(ab); if(bb) captureIn(bb,self.url); }).catch(()=>{});
            }catch{}
            return listener.call(this,ev);
          };
          wrapped.__orig=listener;
          return origAdd.call(this,type,wrapped,opts);
        }
        return origAdd.call(this,type,listener,opts);
      };
      try{
        const desc=Object.getOwnPropertyDescriptor(Proto,'onmessage');
        if(desc&&desc.set&&desc.get){
          const nativeGet=desc.get, nativeSet=desc.set;
          Object.defineProperty(Proto,'onmessage',{
            configurable:true,
            get(){ const v=nativeGet.call(this); return v&&v.__orig?v.__orig:v; },
            set(fn){
              if(typeof fn==='function'&&isPhotonUrl(this.url||'')){
                const self=this;
                const wrapped=function(ev){
                  try{
                    const d=ev.data;
                    try{ _netNote('in', netLen(d)); }catch{}
                    try{ _pingNote('in', d); }catch{}
                    if(peekHeader(d)&&!isEssentialHeader(peekHeader(d))) return fn.call(this,ev);
                    const b=toBytes(d); if(b) captureIn(b,self.url);
                  }catch{}
                  return fn.call(this,ev);
                };
                wrapped.__orig=fn; return nativeSet.call(this,wrapped);
              }
              return nativeSet.call(this,fn);
            }
          });
        }
      }catch(e){ logErr(`onmessage patch failed: ${e.message}`); }
      Proto[hookKey+'_proto']=true; logSys(`prototype patches on ${w===window?'window':'unsafeWindow'} ✓`);
    }catch(e){ logErr(`proto patch failed: ${e.message}`); }
  }
  for(const w of windowsToHook){
    try{
      const OrigWS=w.WebSocket; if(!OrigWS||OrigWS[hookKey]) continue;
      function HookedWS(url, protocols){
        const isDecryptor=String(url).includes('127.0.0.1:8765')||String(url).includes('localhost:8765');
        if(isDecryptor) return protocols!==undefined?new OrigWS(url,protocols):new OrigWS(url);
        const ws=protocols!==undefined?new OrigWS(url,protocols):new OrigWS(url);
        if(!isPhotonUrl(url)) return ws;
        logOk(`HOOKED Photon WSS → ${url}`);
        try{ ws.binaryType='arraybuffer'; }catch{}
        try{ _noteWs(url,'connecting'); }catch{}
        ws.addEventListener('open',()=>{ logOk(`WSS OPEN ${labelForUrl(url)}`); try{ _noteWs(url,'open'); }catch{} });
        ws.addEventListener('close',(ev)=>{ logSys(`WSS CLOSE ${labelForUrl(url)} code=${ev.code}`); try{ _noteWs(url,'closed'); }catch{} });
        return ws;
      }
      HookedWS.prototype=OrigWS.prototype;
      HookedWS.CONNECTING=OrigWS.CONNECTING; HookedWS.OPEN=OrigWS.OPEN; HookedWS.CLOSING=OrigWS.CLOSING; HookedWS.CLOSED=OrigWS.CLOSED;
      HookedWS[hookKey]=true; w.WebSocket=HookedWS;
      logSys(`WebSocket hook installed on ${w===window?'window':'unsafeWindow'} ✓`);
    }catch(e){ logErr(`hook failed: ${e.message}`); }
  }
  // --- WebGL context capture (passive log) ----------------------------------
  // Installed as early as this script runs so the context Unity creates later
  // is ours to read. This wrapper never creates anything: it only hands back
  // exactly what the game asked for, and remembers it in _nerds.glCtxs. (Do
  // NOT replace this with a getContext() probe: on a canvas without a context
  // yet that CREATES one, and Unity then boots with a dead canvas.)
  for(const w of windowsToHook){
    try{
      const proto=w.HTMLCanvasElement&&w.HTMLCanvasElement.prototype;
      if(!proto||proto[hookKey+'_gl']) continue;
      const origGet=proto.getContext;
      proto.getContext=function(type, attrs){
        const gl=origGet.call(this, type, attrs);
        try{
          if(gl && String(type||'').toLowerCase().indexOf('webgl')>=0) _rememberGl(gl);
        }catch{}
        return gl;
      };
      proto[hookKey+'_gl']=true;
      logSys(`WebGL capture installed on ${w===window?'window':'unsafeWindow'} ✓`);
    }catch(e){ logErr(`gl capture failed: ${e.message}`); }
  }
  try{
    const api={
      get sessionId(){return captureSessionId;},
      get queueLen(){return queue.length;},
      flush:()=>flush(),
      clearToken:()=>{ cleanTokenStorage(); showToast('Token cleared',false); },
      get collect(){ return { enabled: collectMode, count: collectedPackets.length, packets: collectedPackets }; },
      get myActorNr(){ return _myActor.nr; },
      get myActorVia(){ return _myActor.via; },
      // Where the FPS chip stands. The one call that answers "I don't see the
      // chip" without guessing: mounted? display? hidden by which panel? rect?
      get fpsChip(){
        try{
          const el=_fpsChip.el||document.getElementById(FPS_CHIP_ID);
          const r=el?el.getBoundingClientRect():null;
          return { mounted:!!el, display:el?el.style.display:'', hiddenBy:_fpsChip.hiddenBy||_fpsChipBlocker(),
                   text:el?el.textContent:'', fps:_fpsChip.fps, min:_fpsChip.min, max:_fpsChip.max,
                   worstMs:_fpsChip.winWorst,
                   rect:r?{top:Math.round(r.top),right:Math.round(r.right),w:Math.round(r.width),h:Math.round(r.height)}:null,
                   viewport:{w:innerWidth,h:innerHeight}, fullscreen:!!(document.fullscreenElement||document.webkitFullscreenElement) };
        }catch(e){ return { err:String(e&&e.message||e) }; }
      },
      toggleCollect: toggleCollectMode,
      saveCollect: downloadCollected,
      loadCollect: ()=> document.getElementById('bnc-file-input')?.click(),
      replayCollect: replayCollected,
      clearCollect: clearCollected
    };
    window.__BUILDNOW_COMPANION__=api;
    window.__BNC__=api;
    // legacy alias
    window.__PHOTON_SNIFFER__=api;
    if(typeof unsafeWindow!=='undefined'&&unsafeWindow!==window){ unsafeWindow.__BUILDNOW_COMPANION__=api; unsafeWindow.__BNC__=api; unsafeWindow.__PHOTON_SNIFFER__=api; }
    logSys('ready - BuildNow Companion active (project-nectar-6n70.onrender.com) · COLLECT Ctrl+Shift+C/S/L/R');
  }catch{}
})();
