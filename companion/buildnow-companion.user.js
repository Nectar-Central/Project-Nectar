// ==UserScript==
// @name         Nectar Buildnow Companion
// @namespace    https://github.com/Nectar-Central
// @version      2.23.8
// @description  Nectar Buildnow Companion v2.23.8 â€” leaderboard panel, insights, friends & chat, play points, clips.
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
// @updateURL    https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/cdn/nectar-companion.latest.user.js
// @downloadURL  https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/cdn/nectar-companion.latest.user.js
// ==/UserScript==

(function () {
  'use strict';
  const BNC_VERSION = '2.23.8';
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
  // local backend runs â€” everything reads Offline there. Never throws.
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
  function silentRevalidate(why){
    try{
      if(!loginToken) return;
      if(sessionId) { try{ stopRevalidate(); }catch{} return; } // already restored
      apiPost('/api/validate-token', {token: loginToken, _quiet:true}, (ok, txt)=>{
        try{
          if(ok){
            const j=JSON.parse(txt);
            if(j.discord_username) { discordUsername=j.discord_username; GMSet('bnc_discord_username', discordUsername); }
            if(j.game_username) GMSet('bnc_game_username', j.game_username);
            if(j.game_id) GMSet('bnc_game_id', j.game_id);
            if(j.session_id){ storeSession(j); try{ logInfo(`[AUTH] session restored (${why})`); }catch{} try{ showToast('Session restored', false); }catch{} }
            return;
          }
          let reason=''; try{ reason=(JSON.parse(txt||'{}').error)||''; }catch{}
          const rl=String(reason).toLowerCase();
          if(rl.includes('locked')){
            try{ showLockedPanel(reason || 'Account locked', 'default'); }catch{}
            try{ stopRevalidate(); }catch{}
            return;
          }
          const dead=rl.includes('invalid')||rl.includes('expired')||rl.includes('not found')||rl.includes('no account')||rl.includes('incorrect');
          if(dead){
            // token truly dead: full wipe + prompt (the only path that asks to link)
            cleanTokenStorage(); hasWelcomed=false;
            try{ stopRevalidate(); }catch{}
            setTimeout(()=>{ showTokenPrompt(); showToast('Session expired - enter new token', true); }, 800);
          } else {
            try{ logInfo(`[AUTH] revalidate failed (${reason||'network'}) - retrying quietly`); }catch{}
            scheduleRevalidate();
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
  // and overwrites the shell elements every tick â€” idempotent, safe everywhere.
  const BNC_REBRAND_BANNER = 'https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/lobby-music/nectar_banner.png';
  const BNC_REBRAND_TITLE = 'Nectar - Buildnow Enhanced';
  function bncRebrandOn(){ try{ return GMGet('bnc_loading_rebrand', true) !== false; }catch{ return true; } }
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
  function bncRebrandStop(){
    try{ if(bncRebrandTimer){ clearInterval(bncRebrandTimer); } }catch{}
    bncRebrandTimer=null; bncRebrandLogged=false;
  }

  // ---- CrazyGames frames: brand the loading shell ----
  // The loading shell lives inside an intermediate iframe (games.crazygames.com),
  // NOT on the top page. Let the rebrand run in every frame â€” it's idempotent.
  // Only the full tracker is gated to the game frame.
  try{ bncRebrandBoot(); bncRebrandOnce(); }catch(e){}

  // ---- Only the full tracker runs inside the game frame ----
  if (!IS_GAME_FRAME) return;

  // session restore - login required, no guest capture
  if(sessionId && currentReqToken){
    logInfoEarly(`session restored ${String(sessionId).slice(0,8)}â€¦ counter=${reqCounter}`);
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
            if(j.new_req_token) { storeSession({new_req_token:j.new_req_token, new_counter:j.new_counter}); logInfo(`req_token rotated ${String(j.new_req_token).slice(0,4)}â€¦ counter ${j.new_counter}`); }
            else if(j.initial_token) storeSession(j);
            else if(j.new_counter!=null) { reqCounter=j.new_counter; GMSet('bnc_req_counter', reqCounter); }
            if(j.game_username){ GMSet('bnc_game_username', j.game_username); logInfo(`game_username updated â†’ ${j.game_username}`); }
            if(j.game_id){ GMSet('bnc_game_id', j.game_id); logInfo(`game_id updated â†’ ${j.game_id}`); }
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
              if(j.current) { currentReqToken=j.current; GMSet('bnc_req_token', currentReqToken); logInfo(`replay sync token â†’ ${String(currentReqToken).slice(0,4)}â€¦`); }
              else if(j.new_req_token) storeSession(j);
              if(!retried){
                logInfo(`replay detected for ${path} - retry`);
                apiQueue.unshift({path, body: {...origBody, _retried:true}, cb, retried:true, quiet, noLive, noTok, noWipe});
                apiBusy = false; setTimeout(processApiQueue, 30); return;
              }
            }
            if(j.error && String(j.error).toLowerCase().includes('invalid')){
              if(j.current){ currentReqToken=j.current; GMSet('bnc_req_token', currentReqToken); logInfo(`mismatch sync token â†’ ${String(currentReqToken).slice(0,4)}â€¦`); if(!retried){ logInfo(`mismatch retry ${path}`); apiQueue.unshift({path, body: {...origBody, _retried:true}, cb, retried:true, quiet, noLive, noTok, noWipe}); apiBusy=false; setTimeout(processApiQueue,30); return; } }
              // rejected with no resync token: historically wiped the session
              // on first sight â€” but that fires on transient DB read misses
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
          // match-start bursts legitimately take longer â€” give them 30s.
          // A 15s timeout here resends a batch the server is still working
          // on, doubling its load (the timed-out work still completes).
          const postTimeout = String(u).includes('/api/capture') ? 30000 : 15000;
          GM({ method:'POST', url:u, headers:{'Content-Type':'application/json'}, data: JSON.stringify(body), timeout:postTimeout,
            onload: r=> { const ok=r.status>=200&&r.status<300; const txt=r.responseText||''; const isZone=txt.includes('"zone"'); if(!quiet||!ok){ (ok?logOk:logErr)(`API ${ok?'OK':'ERR'} ${u} â†’ ${r.status} ${String(txt).slice(0, isZone?1200:600)}`); } if(isZone){ try{ const j=JSON.parse(txt); const zps=(j.packets||[]).filter(p=> (p.packet||p.packet_type)==='zone'); if(zps.length){ logGroup(`API zone batch ${zps.length} packets`, ()=>{ zps.forEach(zp=> console.log('zone packet', zp, 'decrypted', zp.decrypted_hex||zp.raw)); }); } }catch{} } wrapOk(ok, r.responseText); },
            onerror: (e)=>{ wrapFail(`API GM_xhr error â†’ ${u} ${e&&e.error||''}`); },
            ontimeout: ()=>{ wrapFail(`API timeout (${postTimeout/1000}s) â†’ ${u}`); }});
          return true;
        }catch(e){ wrapFail(`API GM throw ${e.message}`); return false; }
      }
      try{
        fetch(u,{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(r=>r.text().then(t=>{ const ok=r.ok; const isZone=t.includes('"zone"'); if(!quiet||!ok){ (ok?logOk:logErr)(`API fetch ${ok?'OK':'ERR'} ${u} â†’ ${r.status} ${t.slice(0, isZone?1200:600)}`); } if(isZone){ try{ const j=JSON.parse(t); const zps=(j.packets||[]).filter(p=> (p.packet||p.packet_type)==='zone'); if(zps.length){ logGroup(`API zone batch ${zps.length} packets`, ()=>{ zps.forEach(zp=> console.log('zone packet', zp, 'decrypted', zp.decrypted_hex||zp.raw)); }); } }catch{} } wrapOk(ok,t); }).catch(()=>wrapFail(`fetch catch ${u}`))).catch((e)=>{ wrapFail(`API fetch fail ${u} ${e.message}`); });
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
  // ---- Party join/leave toast: big Scandinavian card, slides in from the left ----
  // Dark-canvas ladder (near-black surface, white ink rungs, 12% borders),
  // Inter, sentence case, left-aligned. Joined vs left is carried by shape
  // (filled vs hollow dot) plus words â€” never by emoji or hue alone.
  let _partyToastStyle=false;
  function ensurePartyToastStyle(){
    if(_partyToastStyle || !document.documentElement) return;
    _partyToastStyle=true;
    const st=document.createElement('style');
    st.id='bnc-party-toast-style';
    st.textContent=`@keyframes bnc-slide-in-left{from{transform:translateX(-110%);opacity:0}to{transform:translateX(0);opacity:1}}@keyframes bnc-slide-out-left{from{transform:translateX(0);opacity:1}to{transform:translateX(-110%);opacity:0}}.bnc-party-toast{animation:bnc-slide-in-left 260ms cubic-bezier(0.22,1,0.36,1)}`
      + `.bnc-party-toast.bnc-hide{animation:bnc-slide-out-left 200ms ease-in forwards}`
      + `@media (prefers-reduced-motion: reduce){.bnc-party-toast,.bnc-party-toast.bnc-hide{animation:none}}`;
    document.documentElement.appendChild(st);
  }
  function showPartyToast(name, isJoin){
    try{
      ensurePartyToastStyle();
      let wrap=document.getElementById('bnc-party-toasts');
      if(!wrap){
        wrap=document.createElement('div');
        wrap.id='bnc-party-toasts';
        Object.assign(wrap.style,{position:'fixed',left:'24px',top:'50%',transform:'translateY(-50%)',zIndex:'2147483646',display:'flex',flexDirection:'column',gap:'12px',pointerEvents:'none',fontFamily:'Inter, system-ui, sans-serif'});
        (document.body||document.documentElement).appendChild(wrap);
      }
      const el=document.createElement('div');
      el.className='bnc-party-toast';
      Object.assign(el.style,{display:'flex',alignItems:'center',gap:'16px',background:'#0A0A0A',border:'1px solid rgb(255 255 255 / 12%)',borderRadius:'12px',padding:'20px 24px',maxWidth:'400px'});
      const dot=document.createElement('span');
      if(isJoin){ Object.assign(dot.style,{width:'12px',height:'12px',borderRadius:'50%',background:'#fff',flexShrink:'0'}); }
      else { Object.assign(dot.style,{width:'8px',height:'8px',borderRadius:'50%',border:'2px solid rgb(255 255 255 / 56%)',flexShrink:'0'}); }
      const txt=document.createElement('div');
      Object.assign(txt.style,{fontSize:'18px',lineHeight:'1.4',textAlign:'left',wordBreak:'break-word'});
      const who=document.createElement('span');
      who.textContent=String(name||'?').slice(0,28);
      Object.assign(who.style,{color:'#fff',fontWeight:'600'});
      const what=document.createElement('span');
      what.textContent=isJoin?' joined the party':' left the party';
      Object.assign(what.style,{color:'rgb(255 255 255 / 56%)',fontWeight:'400'});
      txt.appendChild(who); txt.appendChild(what);
      el.appendChild(dot); el.appendChild(txt);
      wrap.appendChild(el);
      while(wrap.children.length>3) wrap.firstChild.remove();
      setTimeout(()=>{ try{ el.classList.add('bnc-hide'); setTimeout(()=>{ try{ el.remove(); }catch{} },220); }catch{} },3500);
    }catch{}
  }
  // Toasts only in the party lobby (in party + in lobby). Mid-match movement
  // stays quiet; the baseline still advances so no burst fires later.
  function _partyToastAllowed(){
    try{
      if(typeof _optimisticState!=='undefined' && _optimisticState.inMatch) return false;
      const sess=statsCache&&statsCache.session;
      if(!sess) return false;
      const pi=getPartyInfo(sess);
      if(!pi.isParty||!pi.code) return false;
      if(sess.state && String(sess.state).toLowerCase()==='match') return false;
      return true;
    }catch{ return false; }
  }
  // diff player lists -> join/leave toasts. Silent on first snapshot or full reset.
  // Never toasts yourself: your own join populates the list too, and your
  // own leave/return would spam the card for no information.
  let _prevPartyNames=null;
  function detectPartyJoins(pc){
    try{
      const cur=((pc&&pc.players)||[]).map(p=>p&&p.username?String(p.username):'').filter(Boolean);
      const curSet=new Set(cur);
      if(_prevPartyNames===null){ _prevPartyNames=curSet; return; }
      const prev=_prevPartyNames;
      _prevPartyNames=curSet;
      if(!_partyToastAllowed()) return;
      let me='';
      try{ me=String(_normName(GMGet('bnc_game_username','')||((statsCache&&statsCache.game_username)||''))||'').toLowerCase(); }catch{}
      const isMe=(n)=>{ try{ return !!me && String(_normName(n)||'').toLowerCase()===me; }catch{ return false; } };
      for(const n of curSet){ if(!prev.has(n)){ if(isMe(n)) continue; showPartyToast(n, true); try{ logInfo(`[PARTY] join ${n}`); }catch{} } }
      for(const n of prev){ if(!curSet.has(n)){ if(isMe(n)) continue; showPartyToast(n, false); try{ logInfo(`[PARTY] leave ${n}`); }catch{} } }
    }catch{}
  }
  function resetPartyJoins(){ _prevPartyNames=null; }
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
    card.innerHTML = `<div style="font-size:44px;margin-bottom:8px">${isAdmin?'â›”':'ðŸ”’'}</div><div style="font-size:18px;font-weight:800;color:#fff;margin-bottom:6px">${title}</div><div style="font-size:13px;color:#fca5a5;margin-bottom:6px;word-break:break-word">${String(reason||'Token reuse or sharing detected - friend account use is blocked.').replace(/</g,'&lt;')}</div><div style="font-size:12px;color:#9ca3af;margin-bottom:16px">${help}</div><div style="display:flex;gap:8px;justify-content:center"><button id="bnc-locked-disable" style="flex:1;height:40px;border:0;border-radius:8px;background:#fff;color:#000;font-weight:700;cursor:pointer">Disable & Reload</button><button id="bnc-locked-retry" style="flex:1;height:40px;border:1px solid #374151;border-radius:8px;background:#1f2937;color:#e5e7eb;font-weight:600;cursor:pointer">Retry</button></div><div style="margin-top:10px;font-size:11px;color:#6b7280">BNC is disabled while locked - no capture or API calls.</div>`;
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
      submitBtn.textContent='Linkingâ€¦';
      submitBtn.style.opacity='0.6';
      msg.textContent='Checkingâ€¦';
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
  const ESSENTIAL_OPS=new Set([225,226,227,228,230,231]); // 228 = LeaveRoom/queue-cancel candidate (semantics unconfirmed â€” watch op#228 lines in server logs before wiring removal)
  const ESSENTIAL_EVS=new Set([101,253,254,255]);
  const DROP_EVS_NOISY=new Set([200,201,202,204,206]);
  const BATCH_MAX=40, FLUSH_MS=250;

  // ---- OPTIMISTIC STATE ----
  let _optimisticState = {
    curScn: null,        // current scene from last packet
    inMatch: false,      // derived: true if curScn != MainLobby
    lastUpdateAt: 0,     // timestamp of last optimistic update
  };
  let _curScnFlipAt=0;   // timestamp when curScn flipped to match (3s grace before ranked UI)

  function _applyOptimisticCurScn(curScn, opts) {
    if (!curScn || curScn === _optimisticState.curScn) return;
    const wasInMatch = _optimisticState.inMatch;
    _optimisticState.curScn = curScn;
    _optimisticState.inMatch = curScn !== 'MainLobby';
    _optimisticState.lastUpdateAt = Date.now();
    logSys(`[OPTIMISTIC] curScn=${curScn} inMatch=${_optimisticState.inMatch}`);
    // RANKED-tagged mirror: scene flips are the lobby<->match transition â€”
    // keep them visible under a [RANKED] console filter too. (This point is
    // only reached on actual change â€” unchanged scenes return early above.)
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
  // once optimistic is stale (>OPTIMISTIC_TRUST_MS â€” server trails live packets
  // by seconds), which heals stuck flags even when idle (no capture batches
  // flowing). Returns true on flip.
  function _serverInMatch(sess) {
    if(!sess) return null;
    try {
      // FSM state wins over scene: leaving via leave/auth clears state to lobby while
      // cur_scn stays stale on the last match scene (no MainLobby packet ever comes).
      // Trusting stale cur_scn here stuck audio paused forever in the lobby.
      if(sess.state) return String(sess.state).toLowerCase() === 'match';
      const rooms = sess.rooms;
      const curRoom = rooms ? rooms[sess.current_room_id] : null;
      const serverCurScn = (curRoom && curRoom.cur_scn) || sess.cur_scn || null;
      if(serverCurScn) return serverCurScn !== 'MainLobby';
    } catch(e) {}
    return null;
  }
  function _reconcileServerState(sess) {
    try {
      const serverInMatch = _serverInMatch(sess);
      if(serverInMatch === null || serverInMatch === _optimisticState.inMatch) return false;
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
      try { updateLobbyMusic(); } catch {}  // enter-match pauses, exit-to-lobby resumes
      return true;
    } catch(e) { return false; }
  }

  // Optimistic killfeed dedup: the broadcast + its inbound retransmit arrive
  // as separate sightings ~0.5s apart â€” without this every kill shows +2
  // then "flips back" on the next stats poll. Same pair inside 5s bumps once.
  let _optKfLast = { key: '', at: 0 };
  function _applyOptimisticKillfeed(killer, victim) {
    const myName = GMGet('bnc_game_username', '') || '';
    if (!myName) return;
    const myNorm = _normName(myName);
    const killerNorm = _normName(killer);
    const victimNorm = _normName(victim);
    if (!statsCache) return;
    try {
      const key = `${killerNorm || ''}>${victimNorm || ''}`;
      const now = Date.now();
      if (key && key === _optKfLast.key && now - _optKfLast.at < 5000) {
        logSys(`[OPTIMISTIC] killfeed dup ignored ${killer}â†’${victim}`);
        return;
      }
      _optKfLast = { key, at: now };
    } catch {}
    if (myNorm && killerNorm && myNorm === killerNorm) {
      statsCache.kills = (statsCache.kills || 0) + 1;
      logSys(`[OPTIMISTIC] KILL +1 â†’ ${statsCache.kills} victim=${victim}`);
      try { renderInfoPanel(); } catch {}
    } else if (myNorm && victimNorm && myNorm === victimNorm) {
      statsCache.deaths = (statsCache.deaths || 0) + 1;
      logSys(`[OPTIMISTIC] DEATH +1 â†’ ${statsCache.deaths} killer=${killer}`);
      try { renderInfoPanel(); } catch {}
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
    showToast(`Replaying ${collectedPackets.length} packetsâ€¦`, false);
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
  // Stored binds win over defaults â€” existing users keep what they set.
  // Defaults are single keys (one press, no combos): panels on F2/F4/F9,
  // this menu on Backquote â€” kid-simple on Windows AND Mac (60% boards have
  // Backquote but no F-keys), dodging the Alt+Shift layout-switch trap plus
  // every Ctrl+Shift browser reservation. Stored binds always win, so
  // changing defaults only affects fresh installs. F1/F3/F5 stay
  // off-limits (browser takes them â€” see bindWarning). F11/F12 are host
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
    {id:'clip', label:'Save clip', key:'bnc_clip_keybind', dev:false, def:{ctrl:false,shift:false,alt:false,meta:false,key:'F8'}, run:()=>{ try{ saveClip('manual'); }catch{} }},
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
    if(reserved[sig]) return `Browser takes ${formatKeybind(kb)} (${reserved[sig]}) â€” pick another.`;
    for(const b of BNC_BINDS){
      if(b.id===selfId) continue;
      const cur=getBindKb(b);
      const sameMod=!!(cur.ctrl||cur.meta)===!!(kb.ctrl||kb.meta) && !!cur.shift===!!kb.shift && !!cur.alt===!!kb.alt;
      if(!sameMod) continue;
      const ck=String(cur.code||'').toUpperCase(), nk=String(kb.code||'').toUpperCase();
      const sameKey=(ck && nk) ? ck===nk : String(cur.key||'').toUpperCase()===k;
      if(sameKey)
        return `${formatKeybind(kb)} is already ${b.label} â€” pick another.`;
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
  // sends almost no essential packets for minutes â€” WS alive + captures
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
  // capture response built from packets older than this is a stale echo â€”
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
    }catch{}
  }
  // Local-signal trust window: server trails live packets (~8s observed),
  // so optimistic/capture signals stay authoritative this long before the
  // server is allowed to override them. One constant, three users.
  const OPTIMISTIC_TRUST_MS=25000;
  // Effective match state, freshest-first: optimistic sniff (<10s) â†’
  // capture shadow (<15s) â†’ server session. Returns true/false/null
  // (null = no signal at all). Renderers use this instead of reading
  // statsCache.session directly so flips show in ms, not polls.
  function effInMatch(){
    try{
      const now=Date.now();
      if(_optimisticState.inMatch && now-(_optimisticState.lastUpdateAt||0)<OPTIMISTIC_TRUST_MS) return true;
      if(_capSnap && now-_capSnap.at<=CAP_SNAP_FRESH_MS && _capSnap.inMatch) return true;
      const sess=statsCache && statsCache.session;
      if(sess && sess.state) return String(sess.state).toLowerCase()==='match';
      if(_capSnap && now-_capSnap.at<=CAP_SNAP_FRESH_MS) return !!_capSnap.inMatch;
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
            return; // server confirmed â€” back to normal cadence
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
  // Ranked verdict (comp-copy): local _rankedRoom intent + server stats
  // `ranked` block confirmer (liveRanked). No endpoint polls, no roster
  // checks. Leave guard + dead captures hide locally and never re-show
  // from a stale block.
  // Comp-tourney rooms score manually: every ranked surface (toasts,
  // overlays, lobby card, seal queue) stays dark while holding one. Gates:
  // server comp_tourney block, local _compRoom code match, or tourney/scrim
  // room tag â€” the same trio the tourney HUD uses. Never throws.
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
    // byte ("07"+"PBS05") â€” slide a 5-char window over the post-key bytes.
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
  // composes AltGr chars, so e.key arrives as 'z'/'Ã¥'/etc. instead of 'W'.
  // e.code is the physical key (KeyW) â€” layout- and modifier-independent.
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
      // drawer + ranked + leaderboard panels have their own dispatcher below
      // (Esc/panel handling) â€” matching them here too would fire twice.
      if(b.id==='drawer' || b.id==='ranked' || b.id==='leaderboard') continue;
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
      collectBadge.textContent = `ðŸ”´ COLLECT ${collectedPackets.length}`;
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
  feedBar.innerHTML = `<span style="opacity:0.5">BNC feed - zone/stG/alv full</span><button id="bnc-feed-pause" style="background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:4px;padding:0 6px;cursor:pointer">${feedPaused?'â–¶ Resume':'â¸ Pause'}</button><button id="bnc-feed-expand" style="background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:4px;padding:0 6px;cursor:pointer">â¤¢ Expand</button><span id="bnc-feed-hint" style="opacity:0.5">toggle Â· pause Â· hover to scroll Â· drag top edge to resize</span>`;
  const feedEl = document.createElement('div');
  feedEl.id = 'bnc-log-feed';
  Object.assign(feedEl.style, {
    maxHeight: '38vh', height: '38vh', background: 'rgba(0,0,0,0.88)', color: '#e5e7eb',
    fontFamily: 'monospace', fontSize: '11px', lineHeight: '14px',
    padding: '6px 8px', overflowY: 'auto', overflowX: 'hidden', pointerEvents: 'auto',
    borderTop: '1px solid rgba(255,255,255,0.08)', backdropFilter: 'blur(2px)', resize: 'vertical'
  });
  feedEl.tabIndex = 0;
  feedEl.innerHTML = '<div style="opacity:0.5">waitingâ€¦ decrypted hex will show here</div>';
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
      if(s.length>cap) s = s.slice(0,cap)+' â€¦(+'+(s.length-cap)+' chars)';
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
    if(btn) btn.textContent = feedPaused ? 'â–¶ Resume' : 'â¸ Pause';
    feedLog('info', {msg: feedPaused ? 'paused (scrollable)' : 'resumed'});
    showToast(feedPaused ? 'Feed paused' : 'Feed resumed', false);
  }
  // Dev gating: feed UI + dev keybinds exist only when the DB row says so.
  // Called on every stats render (stats arrive async â€” hidden until then).
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
        h.textContent=`${formatKeybind(getBindKb(fb))} toggle Â· ${formatKeybind(getBindKb(pb))} pause Â· hover to scroll Â· drag top edge to resize`;
      }
    }catch{}
    try{ if(typeof renderKeybindRows==='function') renderKeybindRows(); }catch{}
  }
  setTimeout(()=>{
    const btn=document.getElementById('bnc-feed-pause');
    if(btn) btn.onclick=()=>{
      feedPaused = !feedPaused;
      GMSet('bnc_feed_paused', feedPaused);
      btn.textContent = feedPaused ? 'â–¶ Resume' : 'â¸ Pause';
      feedLog('info', {msg: feedPaused ? 'paused' : 'resumed'});
    };
    const exp=document.getElementById('bnc-feed-expand');
    if(exp) exp.onclick=()=>{
      const isBig = feedEl.style.maxHeight==='70vh';
      feedEl.style.maxHeight = isBig ? '38vh' : '70vh';
      feedEl.style.height = isBig ? '38vh' : '70vh';
      exp.textContent = isBig ? 'â¤¢ Expand' : 'â¤¡ Collapse';
      feedLog('info', {msg: isBig ? 'feed collapsed 38vh' : 'feed expanded 70vh'});
    };
  }, 800);

  // --- PLAYER DRAWER (left overlay, skin, discord, game_id, game_mode, stats polled /api/stats 15s) ---
  const GM_NAMES_DRAWER = {zw_lg:'Late Game Zonewars', zw:'Zone Wars', wwzw:'Duo Wild West', ww:'Solo Wild West', WW:'Solo Wild West', bf:'Boxfights', BF:'Boxfights', bf2v2:'Boxfights 2v2', BF2V2:'Boxfights 2v2', va:'V-Arena', VA:'V-Arena', tdm_bvr:'Red Vs Blue', RVB:'Red Vs Blue', r_1v1:'Realistic 1v1', R_1V1:'Realistic 1v1', df:'Free Mode', DF:'Free Mode', lobby:'Lobby'};
  function fmtGmDrawer(raw, duo){ if(!raw) return '-'; const k=String(raw).trim(); let label = GM_NAMES_DRAWER[k] || GM_NAMES_DRAWER[k.toLowerCase()] || k.replace(/[_-]+/g,' ').replace(/\b\w/g,s=>s.toUpperCase()); if(duo && (k.toLowerCase()==='df' || k.toLowerCase()==='zw_lg') && !/^(solo|duo|\(duo\))/i.test(label)) label = '(Duo) ' + label; return label; }
  function fmtPlaytimeDrawer(s){ if(s==null) return '-'; s=Math.max(0,Math.floor(Number(s)||0)); const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), sec=s%60; if(h) return `${h}h ${m}m`; if(m) return `${m}m ${sec}s`; return `${sec}s`; }
  function fmtLastSeen(iso){
    try{
      if(!iso) return 'â€”';
      const ms=new Date(String(iso)).getTime()-Date.now();
      const age=Math.max(0, Math.round(-ms/1000));
      if(age<300) return 'Online now';
      if(age<3600) return `${Math.floor(age/60)}m ago`;
      if(age<86400) return `${Math.floor(age/3600)}h ago`;
      return `${Math.floor(age/86400)}d ago`;
    }catch{ return 'â€”'; }
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
  // Drawer always starts closed on page load (first open) â€” never auto-open from storage.
  let drawerOpen = false;
  try { GMSet('bnc_info_open', false); } catch {}
  // Ranked/host shelved: drop stale ranked session hint + force individualist.
  try {
    const _sg = GMGet('bnc_session_gm', null);
    if(_sg && String(_sg).toLowerCase().indexOf('ranked') === 0) GMDel('bnc_session_gm');
    GMSet('bnc_host_mode', false);
  } catch {}
  let drawerTab = GMGet('bnc_drawer_tab','stats'); // 'stats' | 'players'
  if(drawerTab!=='stats'&&drawerTab!=='players') drawerTab='stats';
  let playersCache=null, playersPollTimer=null, playersLoading=false, _playersSeq=0, _statsSeq=0;
  let lobbyAudio=null;
  let bgLobbyPollTimer=null;
  let _audioPlaying=false;
  let _audioPlayPending=false; // play() in flight (buffering) â€” blocks duplicate play() calls
  let _authCount=0; // outgoing Authenticate packets seen (first = login, never touches audio)
  let _lastPauseReason=null; // reason string of the last _audioPause() call (for STOPPED tracing)
  function ensureLobbyAudio(){
    if(lobbyAudio) return lobbyAudio;
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
      // hidden audio tag that auto plays â€” use off-screen instead of display:none (some browsers block display:none audio)
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
      // try resume on user gesture (drawer open) â€” only if not already playing and not in match
      try{ const resume=()=>{ logInfo('[AUDIO] click resume try'); try{ const rs = statsCache && statsCache.session; const rsLobby = !_optimisticState.inMatch && (!rs || String(rs.state||'').toLowerCase()!=='match'); if(lobbyAudio && !_audioPlaying && !_audioPlayPending && rsLobby && lobbyAudio.paused && lobbyAudio.src) { _audioPlayPending=true; lobbyAudio.play().then(()=> logOk('[AUDIO] resume playing')).catch(e=> { _audioPlayPending=false; logErr(`[AUDIO] resume blocked ${e.message||e}`); }); } }catch(e){ logErr(`[AUDIO] resume err ${e.message}`);} }; document.addEventListener('click', resume, {once:true}); }catch{}
    }catch(e){ logErr(`[AUDIO] ensure failed ${e.message}`); }
    return lobbyAudio;
  }
  // Single choke point for stopping lobby audio â€” records WHY for STOPPED tracing
  function _audioPause(reason){
    try{
      const a = lobbyAudio;
      if(!a){ logInfo(`[AUDIO] STOP request reason=${reason} (no audio tag)`); return; }
      logInfo(`[AUDIO] STOP request reason=${reason} paused=${a.paused} playing=${_audioPlaying} pending=${_audioPlayPending} inMatch=${_optimisticState.inMatch} readyState=${a.readyState} vol=${a.volume.toFixed(2)}`);
      _lastPauseReason = reason;
      if(!a.paused) a.pause();
    }catch(e){ logErr(`[AUDIO] pause err ${e.message}`); }
  }
  function updateLobbyMusic(){
    try{
      const sc=statsCache;
      const sess=sc && sc.session;
      // Use optimistic state for immediate response, fall back to server state
      const isLobby = !_optimisticState.inMatch && (!sess || String(sess.state||'').toLowerCase()!=='match');
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
      // Lobby (optimistic): never pause here â€” without a URL there is nothing to start.
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
      // Guard: play() already in flight (buffering) â€” a second play() restarts the track
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
      if(p && p.then) p.then(()=> logOk('[AUDIO] play() ok playing')).catch(e=> { _audioPlayPending=false; logErr(`[AUDIO] play() blocked ${e.message||e} â€” need user click on page/drawer`); });
      else if(p && p.catch) p.catch(e=> { _audioPlayPending=false; logErr(`[AUDIO] play() blocked ${e.message||e}`); });
      else { _audioPlayPending=false; logInfo('[AUDIO] play() no promise'); }
    }catch{}
  }
  function prefetchLobbyMusic(){
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
  function startBgLobbyPoll(){ if(bgLobbyPollTimer) return; bgLobbyPollTimer=setInterval(()=>{ if(sessionId) fetchStats(false); }, 15000); }
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
        <button id="bnc-drawer-close" style="width:28px;height:28px;border:1px solid rgba(255,255,255,0.12);background:#1a1a1a;color:#9ca3af;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px">âœ•</button>
      </div>
      <div id="bnc-drawer-body" style="flex:1;overflow-y:auto;overflow-x:hidden;padding:8px 20px 16px 20px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;scrollbar-width:thin">
        <div style="opacity:0.7;font-size:12px">Loadingâ€¦</div>
      </div>
      <div id="bnc-drawer-footer" style="padding:10px 14px;border-top:1px solid rgba(255,255,255,0.06);display:flex;justify-content:space-between;align-items:center;gap:8px;flex-shrink:0;font-size:11px;color:#6b7280">
        <span id="bnc-drawer-version">v${BNC_VERSION}</span>
        <span id="bnc-drawer-ranked" style="display:none;cursor:pointer;color:#9ca3af;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:45%" title="Click to copy â€” use it to report abuse"></span>
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
        const isLobby = !sess || String(sess.state||'').toLowerCase()!=='match';
        if(isLobby && sc && sc.lobby_music_url){
          const aud=ensureLobbyAudio();
          // Drawer must NEVER restart lobby music: only act when audio is silent, no play
          // in flight, and we're in lobby (optimistic AND server â€” server trails, so both).
          // Ground truth = aud.paused.
          const drawerIsLobby = !_optimisticState.inMatch && (!sess || String(sess.state||'').toLowerCase()!=='match');
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
          try{ detectPartyJoins(j); }catch{}
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
  function startPlayersPoll(){ stopPlayersPoll(); fetchPlayers(false); playersPollTimer=setInterval(()=> fetchPlayers(false), 15000); }
  function stopPlayersPoll(){ if(playersPollTimer){ clearInterval(playersPollTimer); playersPollTimer=null; } }
  // Prompt roster refresh after membership changes (queue/match leave or
  // join). Delayed 3s so the leave/join capture batch reaches the server
  // first (server prunes on Leave, /api/players is correct after that);
  // debounced to one refresh per 8s for mass-leave bursts.
  let _playersRefreshT=null, _lastPlayersRefresh=0;
  function schedulePlayersRefresh(reason){
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
    const tpr = sc && sc.tourney_pr!=null ? sc.tourney_pr : null;
    const tprDelta = sc && sc.pr_delta_pct!=null ? sc.pr_delta_pct : null;
    const tprTxt = tpr==null ? null : (`${tpr}` + (tprDelta==null ? '' : (tprDelta>0 ? ` (+${tprDelta}%)` : (tprDelta<0 ? ` (${tprDelta}%)` : ' (=)'))));
    const rankPct = sc && sc.rank && sc.rank.total>=2 ? sc.rank.pct : null;
    const pGlobal = sc? sc.playtime_global : null;
    const sess = sc && sc.session ? sc.session : null;
    const _curRoom = sess && sess.rooms ? sess.rooms[sess.current_room_id] : null;
    const _isDuoRoom = !!(_curRoom && _curRoom.gv && String(_curRoom.gv).toLowerCase() !== 'solo');
    const gmLive = sess && sess.game_mode ? fmtGmDrawer(sess.game_mode, _isDuoRoom) : fmtGmDrawer(gmRaw, _isDuoRoom);
    // state: Lobby vs In Match - spec says show "In Lobby" when no game mode
    let stateLabel='In Lobby';
    if(sess && sess.state){ stateLabel = String(sess.state).toLowerCase()==='match' ? 'In Match' : 'In Lobby'; }
    else if(sess && sess.cur_scn){ stateLabel = String(sess.cur_scn).toLowerCase().includes('lobby') ? 'In Lobby' : 'In Match'; }
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
      ? `<img src="${skinUrl}" alt="${String(skin).slice(0,24)}" style="width:112px;height:112px;object-fit:contain;background:radial-gradient(140px 90px at 50% 20%, rgba(255,255,255,0.06), transparent 70%), #111;border:1px solid rgba(255,255,255,0.08);border-radius:16px;image-rendering:pixelated;flex-shrink:0" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" /><div style="display:none;width:112px;height:112px;align-items:center;justify-content:center;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:16px;font-size:32px;flex-shrink:0">â–£</div>`
      : `<div style="width:112px;height:112px;display:flex;align-items:center;justify-content:center;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:16px;font-size:32px;flex-shrink:0">â–£</div>`;
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
    // No game mode: bare status only ("In Lobby", never "In Lobby - â€¦").
    if(!hasGm){
      stateGmLine = isInParty ? `In Party - ${stateLabel}` : stateLabel;
    }
    try{ stateGmLine = String(stateGmLine).replace(/\s+-\s*$/, ''); }catch{}
    // ranked overrides party: "In Match - Ranked Match - GM"
    // liveRanked() also requires fresh captures â€” a ghost block from a dead
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
    const emptyStatsHtml = `<div style="display:flex;flex-direction:column;gap:8px;align-items:center;width:100%;max-width:240px;padding:18px 14px;background:#171717;border:1px dashed rgba(255,255,255,0.16);border-radius:12px;text-align:center"><div style="font-size:26px">ðŸ“Š</div><div style="font-size:14px;font-weight:800;color:#fff">No stats yet</div><div style="font-size:12px;color:#9ca3af;line-height:1.5">Play 1-2 matches to get your account ready.<br>Kills, deaths, wins and skins will show up here.</div></div>`;
    // ranked arena rows (below K/D): ranked-session ONLY. Casual lobbies and
    // matches never show points/tier rows â€” liveRanked() (_ranked above) is
    // the gate; the session-independent arena block is deliberately unused.
    const fmtPts = n => n==null ? '-' : Number(n).toLocaleString('en-US');
    const _arena = _ranked;
    // progress-to-next lives on the session-independent arena block now
    // (the ranked block carries points/tier only).
    const _arenaNext = (sc && sc.arena) || null;
    let arenaRowsHtml = '';
    if(_arena && _arena.points!=null){
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
        arenaRowsHtml += `<div style="display:flex;justify-content:space-between;width:100%;max-width:240px;font-size:12px;margin-top:4px"><span style="color:#9ca3af">ðŸ”¥ Win streak</span><span style="color:#fff;font-weight:700">x${_esc(_st.win||0)}${_st.best_win?` (best ${_esc(_st.best_win)})`:''}</span></div>`;
        arenaRowsHtml += `<div style="display:flex;justify-content:space-between;width:100%;max-width:240px;font-size:12px"><span style="color:#9ca3af">ðŸ“… Daily streak</span><span style="color:#fff;font-weight:700">x${_esc(_st.daily||0)}</span></div>`;
        if(_st.best_match_kills!=null) arenaRowsHtml += `<div style="display:flex;justify-content:space-between;width:100%;max-width:240px;font-size:12px"><span style="color:#9ca3af">ðŸ’€ Best match</span><span style="color:#fff;font-weight:700">${_esc(_st.best_match_kills)} kills</span></div>`;
      }
    }catch{}
    // ðŸ† comp rows are "my points in THIS tourney" â€” only while holding the
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
          ${(_inTourneyRoom && sc && sc.comp && sc.comp.length ? sc.comp.map(c=>row('ðŸ† '+String(c.tourney_name!=null?c.tourney_name:('Event #'+c.tourney_id)).replace(/</g,'&lt;'), fmtPts(c.points)+` (${c.wins||0}W)`)).join('') : '')}
          ${row('Wins', wins)}
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
      try{ resetPartyJoins(); }catch{}
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
             <span style="font-size:11px;color:#6b7280">ðŸ”ˆ</span>
             <input type="range" id="bnc-volume-slider" min="0" max="100" value="${Math.round(Number(GMGet('bnc_lobby_volume',0.5))*100)}" style="flex:1;accent-color:#fff">
             <span id="bnc-volume-label" style="font-size:11px;color:#e5e7eb;min-width:32px;text-align:right">${Math.round(Number(GMGet('bnc_lobby_volume',0.5))*100)}%</span>
              <span style="font-size:11px;color:#6b7280">ðŸ”Š</span>
            </div>
          </div>
          <label style="margin-top:10px;display:flex;align-items:center;gap:6px;font-size:11px;color:#9ca3af;cursor:pointer"><input type="checkbox" id="bnc-rebrand-enabled"${bncRebrandOn()?' checked':''} style="accent-color:#fff"> Nectar loading branding</label>
        </div>
      `;
    // players tab content
    let playersHtml='';
    if(drawerTab==='players'){
      const pc = playersCache;
      if(!sessionId){
        playersHtml = `<div style="padding:20px 0;color:#9ca3af;font-size:12px">No session - join a match</div>`;
      } else if(!pc){
        playersHtml = `<div style="padding:20px 0;color:#9ca3af;font-size:12px">Loading playersâ€¦</div>`;
      } else if(!pc.players || pc.players.length===0){
        playersHtml = `<div style="padding:20px 0;color:#9ca3af;font-size:12px">No players in game</div>`;
      } else {
        const count = pc.count || pc.players.length;
        const lateCount = pc.players.filter(p=>p && p.late_join).length;
        const header = `<div style="font-size:13px;font-weight:700;color:#fff;margin-bottom:8px;text-align:center">Players in Game - ${count}${pc.game_mode? ' Â· '+fmtGmDrawer(pc.game_mode):''}${lateCount? ` Â· <span style="color:#f59e0b">${lateCount} late join${lateCount>1?'s':''}</span>`:''}</div>`;
        const rows = pc.players.map(pl=>{
          const skinId = (pl.skin!=null && String(pl.skin).trim()!=='' && String(pl.skin).toLowerCase()!=='nan' && String(pl.skin).toLowerCase()!=='null') ? pl.skin : 'cct_0_default';
          const skinP = skinUrlDrawer(skinId);
          const inDb = !!pl.in_db;
          const img = `<div style="position:relative;width:36px;height:36px;flex-shrink:0"><img src="${skinP}" style="width:36px;height:36px;object-fit:contain;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:8px;flex-shrink:0" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div style="display:none;width:36px;height:36px;align-items:center;justify-content:center;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:8px;font-size:14px;flex-shrink:0">â–£</div><span style="position:absolute;bottom:-2px;right:-2px;width:10px;height:10px;border-radius:50%;background:${inDb?'#10b981':'#4b5563'};border:2px solid #0f0f0f;display:inline-block" title="${inDb?'In DB':'Not in DB'}"></span></div>`;
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
          const detailLine = `Discord: ${toNaN(discordVal)} Â· Game ID: ${toNaN(gidFull)}`;
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
        rkFoot.textContent = _ranked.match_id + ' Â· report abuse';
        rkFoot.onclick=(e)=>{
          try{ if(e) e.stopPropagation(); }catch{}
          const mid=String(_ranked.match_id);
          const done=()=> showToast('Match ID copied â€” paste it in your abuse report', false);
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
        kbBtn.textContent='Press new keybindâ€¦';
        if(kbHint) kbHint.textContent='Press any combo (e.g. Ctrl+Shift+K, or Cmd+Shift+K on Mac). Esc to cancel.';
        const handler=(e)=>{
          if(e.key==='Escape'){ _keybindCapturing=false; kbBtn.textContent='Set open keybind: '+formatKeybind(getDrawerKeybind()); if(kbHint) kbHint.textContent='Cancelled.'; document.removeEventListener('keydown', handler, true); return; }
          if(['Control','Shift','Alt','Meta'].includes(e.key)) return;
          e.preventDefault(); e.stopPropagation();
          const nb={ctrl:!!e.ctrlKey, meta:!!e.metaKey, shift:!!e.shiftKey, alt:!!e.altKey, key:String(e.key).toUpperCase(), code:String(e.code||'').toUpperCase()||null};
          if(!nb.key) return;
          const dwarn=bindWarning(nb, 'drawer');
          if(dwarn){ if(kbHint) kbHint.textContent='âš  '+dwarn; return; }
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
    // loading-branding toggle (lives in the stats tab)
    const bncRebrandEl=document.getElementById('bnc-rebrand-enabled');
    if(bncRebrandEl){
      bncRebrandEl.onchange=()=>{
        GMSet('bnc_loading_rebrand', !!bncRebrandEl.checked);
        logInfo(`[REBRAND] loading branding ${bncRebrandEl.checked?'on â€” applying now':'off'}`);
        showToast(bncRebrandEl.checked?'Nectar loading branding on':'Nectar loading branding off', false);
        if(bncRebrandEl.checked){ try{ bncRebrandBoot(); bncRebrandOnce(); }catch{} renderInfoPanel(); }
        else{ try{ bncRebrandStop(); }catch{} }
      };
    }
    try{ updateLobbyMusic(); }catch(e){ logErr(`[AUDIO] update after render err ${e.message}`); }
    try{ prefetchLobbyMusic(); }catch{}
  }
  function fetchStats(forceRender){
    if(!sessionId && !discordId) { if(forceRender) renderInfoPanel(); return; }
    // Sequence guard: overlapping polls (15s timer, bg poll, transition
    // bursts) must never paint stale data over fresh â€” a slow older response
    // arriving last used to flip kills +2 back to +1. Only the newest
    // response for each transport applies. Never throws.
    const mySeq=++_statsSeq;
    const fresh=(s)=> s===_statsSeq; // GM and fetch race each other too
    const base=resolveApiBase();
    const qs = sessionId ? `session_id=${encodeURIComponent(sessionId)}` : `discord_id=${encodeURIComponent(discordId)}`;
    const url = base + '/api/stats?' + qs;
    const GM = typeof GM_xmlhttpRequest!=='undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
    const doFetch = (useGM)=>{
      if(useGM && GM){
        try{
          GM({method:'GET', url, headers:{'Content-Type':'application/json'}, timeout:8000,
            onload: r=>{ if(!fresh(mySeq)) return; if(r.status>=200&&r.status<300){ try{ const j=JSON.parse(r.responseText); statsCache=j; logInfo(`[AUDIO] stats fetched lobby_music=${String(j.lobby_music_url||'').slice(-30)} state=${j.session? j.session.state:'none'}`); try{ feedLog('info', {audio:'stats', hasMusic:!!j.lobby_music_url, state:j.session? j.session.state:null}); }catch{} apiOnline=true; if(j.skin) GMSet('bnc_skin', j.skin); if(j.game_username) GMSet('bnc_game_username', j.game_username); if(j.game_id) GMSet('bnc_game_id', j.game_id); if(j.discord_username) { discordUsername=j.discord_username; GMSet('bnc_discord_username', discordUsername); } try{ _reconcileServerState(j.session); }catch{} renderInfoPanel(); try{ _hbFails=0; startHeartbeat(); }catch{} try{ const _pi=getPartyInfo(j.session); const _inMatch=j.session&&String(j.session.state||'').toLowerCase()==='match'; if((_pi.isParty&&_pi.code)||_inMatch) fetchPlayers(false); }catch{} }catch(e){ logErr(`[AUDIO] stats parse err ${e.message}`); apiOnline=false; if(forceRender) renderInfoPanel(); } } else { apiOnline=false; if(forceRender) renderInfoPanel(); } try{ updateApiDot(); }catch{} },
            onerror: ()=>{ if(!fresh(mySeq)) return; apiOnline=false; if(forceRender) renderInfoPanel(); try{ updateApiDot(); }catch{} },
            ontimeout: ()=>{ if(!fresh(mySeq)) return; apiOnline=false; if(forceRender) renderInfoPanel(); try{ updateApiDot(); }catch{} }});
          return;
        }catch{}
      }
      fetch(url, {method:'GET'}).then(r=>r.text().then(t=>{ if(!fresh(mySeq)) return; if(r.ok){ try{ const j=JSON.parse(t); statsCache=j; logInfo(`[AUDIO] stats fetched (fetch) lobby_music=${String(j.lobby_music_url||'').slice(-30)} state=${j.session? j.session.state:'none'}`); try{ feedLog('info', {audio:'stats-fetch', hasMusic:!!j.lobby_music_url, state:j.session? j.session.state:null}); }catch{} try{ _reconcileServerState(j.session); }catch{} apiOnline=true; try{ _hbFails=0; startHeartbeat(); }catch{} if(j.skin) GMSet('bnc_skin', j.skin); if(j.game_username) GMSet('bnc_game_username', j.game_username); if(j.game_id) GMSet('bnc_game_id', j.game_id); if(j.discord_username) { discordUsername=j.discord_username; GMSet('bnc_discord_username', discordUsername); } renderInfoPanel(); try{ const _pi2=getPartyInfo(j.session); const _inMatch2=j.session&&String(j.session.state||'').toLowerCase()==='match'; if((_pi2.isParty&&_pi2.code)||_inMatch2) fetchPlayers(false); }catch{} }catch(e){ logErr(`[AUDIO] stats parse err (fetch) ${e.message}`); apiOnline=false; if(forceRender) renderInfoPanel(); } } else { apiOnline=false; if(forceRender) renderInfoPanel(); } try{ updateApiDot(); }catch{} }).catch(()=>{ if(!fresh(mySeq)) return; apiOnline=false; if(forceRender) renderInfoPanel(); try{ updateApiDot(); }catch{} })).catch(()=>{ if(!fresh(mySeq)) return; apiOnline=false; if(forceRender) renderInfoPanel(); try{ updateApiDot(); }catch{} });
    };
    doFetch(true);
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
  setTimeout(()=>{ ensureLobbyAudio(); createInfoPanel(); renderInfoPanel(); if(drawerOpen){ startStatsPoll(); if(drawerTab==='players') startPlayersPoll(); } if(sessionId){ startBgLobbyPoll(); try{ startHeartbeat(); }catch{} } try{ clipStart(); }catch{} try{ logInfo(`[RANKED] companion v${BNC_VERSION} boot`); }catch{} }, 700);
  // refresh when session established after login
  const _origStoreSession = storeSession;
  // wrap storeSession to trigger render
  // monkey-patch via interval check on sessionId change
  let _lastSid = sessionId;
  setInterval(()=>{ if(sessionId!==_lastSid){ _lastSid=sessionId; renderInfoPanel(); if(drawerOpen){ fetchStats(false); if(drawerTab==='players') fetchPlayers(false); } else if(sessionId){ startBgLobbyPoll(); try{ startHeartbeat(); }catch{} fetchStats(false); } try{ clipStart(); }catch{} } }, 1000);
  setInterval(()=>{ try{
    if(!_clip.started) clipStart();
    else if(!_clip.rec && !_clip.finalizing) clipRestart();
    else if(_clip.started && !_clip.finalizing && !_clip.pendingTimer && clipCanvasChanged()){ try{ logInfo('[CLIPS] canvas resized â€” restarting segment'); }catch{} clipRestart(); }
  }catch{} }, 15000);

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
    const pts=(rk.points==null ? 0 : rk.points);
    const color=rk.milestone_color || '#fff';
    // Honesty: a pending seal shows as approximate instead of implying
    // server-confirmed truth (the number can still move at seal).
    const pend=!!_rankedPendingSeal;
    el.innerHTML=pend
      ? `<div style="font-size:23px;font-weight:800;color:#fff">${Number(pts).toLocaleString('en-US')}</div><div style="font-size:10px;font-weight:700;color:#9ca3af;letter-spacing:0.4px">&#8776; seal pending</div>`
      : `<div style="font-size:23px;font-weight:800;color:#fff">${Number(pts).toLocaleString('en-US')}</div>`;
    el.style.display='flex';
    if(_lastRankedPts!=null && pts!==_lastRankedPts){
      el.style.borderColor=color;
      el.style.boxShadow=`0 0 18px ${color}`;
      try{ clearTimeout(_rankedFlashT); }catch{}
      _rankedFlashT=setTimeout(()=>{ try{ el.style.borderColor='rgba(255,255,255,0.12)'; el.style.boxShadow='none'; }catch{} }, 700);
    }
    _lastRankedPts=pts;
  }

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
    // the JKVUD session) â€” say so loudly instead of failing silent.
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
  // lobby-state only â€” but it SURVIVES match end: after the last seal the
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
    if(sig!==_lastLobbySig){
      _lastLobbySig=sig;
      try{ logInfo(`[RANKED] lobby-card -> ${show?'SHOW':'HIDE'} ranked=${rk?rk.rid+' '+rk.points+'pts/'+rk.milestone:'none'} session=${sess?sess.state:'none'}`); }catch{}
    }
    if(!show){ el.style.display='none'; return; }
    const color=rk.milestone_color || '#fff';
    const icon=rk.milestone_icon_url
      ? `<img src="${String(rk.milestone_icon_url).replace(/"/g,'')}" alt="" style="width:80px;height:80px;object-fit:contain" onerror="this.style.display='none'" />`
      : '';
    const ptsRaw=Number(rk.points||0);
    // Server-confirmed totals; a pending seal shows as approximate.
    const pts=ptsRaw.toLocaleString('en-US')+(pend?' &#8776;':'');
    const ms=String(rk.milestone||'Unranked').replace(/</g,'&lt;');
    const season=(sc && sc.arena && sc.arena.season!=null) ? `Nectar Ranked Season ${sc.arena.season}` : 'Nectar Ranked';
    const code=String(rk.short_code||'').replace(/</g,'&lt;');
    const players=(sess && sess.players_count!=null) ? Number(sess.players_count) : null;
    let roomLine=code;
    if(players!=null) roomLine+=` Â· ${players} player${players===1?'':'s'}`;
    // Room exists but was never started â€” seals can't fire. Same card plus
    // an unmissable amber prompt for the host.
    const notStarted=String(rk.status||'').toLowerCase()==='lobby';
    const sleepingNote=rankedEnabled()?'':'<div style="font-size:11px;font-weight:700;color:#f87171;margin-top:2px">RANKED SLEEPING - matches will not seal</div>';
    const startLine=notStarted ? `<div style="font-size:12px;font-weight:800;color:#f59e0b;margin-top:4px">NOT STARTED â€” host: /ranked start</div>` : '';
    el.innerHTML=`<div style="font-size:13px;font-weight:800;color:#fff">${season}</div>${icon}<div style="font-size:22px;font-weight:800;color:${color}">${pts} (${ms})</div><div style="font-size:11px;color:#9ca3af">${roomLine}</div>${startLine}${sleepingNote}`;
    el.style.display='flex';
  }

  // --- RANKED POINTS TOASTS: Victory / Elimination / Placement on seals ---
  // Fires on a match-version bump with a matching last_result. The label uses
  // the same priority as Comp: wins up -> VICTORY, kills up -> ELIMINATION,
  // otherwise PLACEMENT. A result write can trail its version bump, so keep
  // the edge pending until that matching row arrives.
  // --- RANKED POINTS TOAST QUEUE: lossless FIFO, coalescing, exactly-once ---
  // Identical title+delta within 60s coalesce into a xN badge; each seal
  // carries a key (rid:v) fired at most once per session.
  let _prevRankSnap=null, _pendingRankToast=null;
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
        if(q.title===t && q.delta===d && now-q.at<60000){
          q.count=(q.count||1)+1; q.at=now;
          try{ logInfo(`[RANKED] points-toast coalesced "${t}" ${d>=0?'+':''}${d} x${q.count}`); }catch{}
          return;
        }
      }
      const item={title:t, delta:d, at:now, count:1, dwell:Number(opts.dwell)||3500, end:!!opts.pinnedLast, key:opts.key||null};
      if(item.end) _ptsToastQueue.push(item);   // session summary always last
      else{
        let i=_ptsToastQueue.length;
        while(i>0 && _ptsToastQueue[i-1].end) i--;
        _ptsToastQueue.splice(i,0,item);
      }
      while(_ptsToastQueue.length>8){           // overflow: drop oldest non-summary
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
    const gain=delta>=0;
    const el=document.createElement('div');
    Object.assign(el.style,{position:'fixed', left:'0', top:'50%', transform:'translate(-120%,-50%)', transition:'transform 300ms cubic-bezier(0.22,1,0.36,1)', zIndex:'2147483641', background:'#0f0f0f', border:'1px solid rgba(255,255,255,0.12)', borderLeft:'3px solid '+(gain?'#10b981':'#ef4444'), borderRadius:'0 12px 12px 0', padding:'12px 18px 12px 14px', fontFamily:'Inter, system-ui, sans-serif', pointerEvents:'none', boxShadow:'8px 0 32px rgba(0,0,0,0.45)'});
    el.innerHTML=`<div style="font-size:15px;font-weight:800;color:#fff">${title.replace(/</g,'&lt;')}</div><div style="font-size:22px;font-weight:800;color:${gain?'#10b981':'#ef4444'}">${gain?'+':''}${Number(delta).toLocaleString('en-US')} Points</div>`;
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
    if(!Number.isFinite(gained) || gained===0) return;
    const winsUp=(snap.wins!=null && pending.prev.wins!=null)
      ? snap.wins>pending.prev.wins
      : lr.won===true;
    const killsUp=(snap.kills!=null && pending.prev.kills!=null)
      ? snap.kills>pending.prev.kills
      : Number(lr.kills)>0;
    const label=winsUp ? 'VICTORY' : (killsUp ? 'ELIMINATION' : 'PLACEMENT');
    // Include the post-seal total so a later score correction is a distinct,
    // deterministic event while duplicate polls remain deduped by the queue.
    const sealKey=`${snap.rid}:${snap.v}:${snap.pts}`;
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
    if(!IS_GAME_FRAME) return;
    const rk=liveRanked();
    const inMatch=effInMatch()===true;
    if(!rk || !inMatch) return;
    const rid=String(rk.rid||'');
    if(!rid || _rankedStartShown[rid]) return;
    _rankedStartShown[rid]=true;
    const mver=Number(rk.match_version)||0;
    if(mver>1) return;  // joined mid-session: first match already passed
    if(document.getElementById('bnc-ranked-start')) return;
    try{ logInfo(`[RANKED] start-overlay SHOW rid=${rid}`); }catch{}
    const ov=document.createElement('div');
    ov.id='bnc-ranked-start';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483643', background:'rgba(0,0,0,0.72)', display:'flex', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif', cursor:'pointer'});
    ov.innerHTML=`<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:16px;padding:28px;width:420px;max-width:92vw;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,0.5)"><div style="font-size:20px;font-weight:800;color:#fff">This is a Ranked Match</div><div style="font-size:13px;color:#e5e7eb;line-height:1.6;margin-top:10px">Kills 4–18 by opponent strength, less after the 2nd Â· Win scales with lobby Â· Loss −5 to −32 by tier</div><div style="font-size:11px;color:#6b7280;margin-top:14px">click anywhere to close</div></div>`;
    ov.addEventListener('click', ()=> _dismissRankedStart());
    try{ (document.body||document.documentElement).appendChild(ov); }catch{ return; }
    _rankedStartTimer=setTimeout(()=> _dismissRankedStart(), 5000);
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

  // --- RANKED PANEL (Phase 8): F9 â€” milestones map + ranked leaderboard ---
  const RANKED_ASSETS='https://mnpwvwjqawmuqmickteu.supabase.co/storage/v1/object/public/assets';
  const RANKED_TIERS=[
    {name:'Unranked', min:0, fee:0, color:'#6b7280'},
    {name:'Bronze', min:200, fee:10, color:'#CD7F32'},
    {name:'Silver', min:500, fee:25, color:'#C0C0C0'},
    {name:'Gold', min:1000, fee:50, color:'#FFD700'},
    {name:'Platinum', min:1500, fee:75, color:'#E5E7EB'},
    {name:'Diamond', min:2000, fee:100, color:'#B9F2FF'},
    {name:'Emerald', min:3000, fee:150, color:'#10b981'},
    {name:'Champion', min:4000, fee:200, color:'#f59e0b'},
    {name:'Master', min:5000, fee:250, color:'#9333EA'},
    {name:'Grandmaster', min:7500, fee:300, color:'#ef4444'},
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
    ov.innerHTML=`<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:14px;width:700px;max-width:94vw;height:500px;max-height:90vh;display:flex;flex-direction:column;overflow:hidden"><div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.08)"><div><b style="font-size:16px;color:#fff">Nectar Ranked</b><div id="bnc-ranked-season" style="font-size:11px;color:#9ca3af;margin-top:2px">Season â€”</div></div><button id="bnc-ranked-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px">Close</button></div><div style="display:flex;gap:8px;padding:12px 18px 0 18px"><button id="bnc-ranked-tab-ms" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Milestones</button><button id="bnc-ranked-tab-lb" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Leaderboard</button></div><div id="bnc-ranked-body" style="flex:1;overflow:auto;padding:16px 18px"></div></div>`;
    (document.body||document.documentElement).appendChild(ov);
    ov.addEventListener('click', (e)=>{ if(e.target===ov) toggleRankedPanel(false); });
    ov.querySelector('#bnc-ranked-close').onclick=()=> toggleRankedPanel(false);
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
      const sess=statsCache.session;
      return !!(sess && String(sess.state||'').toLowerCase()==='match');
    }catch{ return false; }
  }
  function toggleRankedPanel(force){
    if(typeof rankedEnabled==='function' && !rankedEnabled()){ showToast('Ranked is sleeping - streaks are live', false); try{ logInfo('[RANKED] panel sleeping'); }catch{} return; }
    createRankedPanel();
    const isHidden=rankedPanel.style.display==='none' || !rankedPanel.style.display;
    const show=typeof force==='boolean' ? force : isHidden;
    // Panel is ranked-session only: blocked outside one, and locked mid-match
    // inside one (leaderboard/milestones would be a live aid).
    if(show && !liveRanked()){ showToast('Ranked panel is only available during a ranked session', true); try{ logInfo('[RANKED] panel blocked - not in ranked session'); }catch{} return; }
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
      el.textContent=`Season ${season}`+(left?` Â· Resets in ${left}`:'');
      el.title=reset?`Resets at ${reset}`:'';
    } else {
      el.textContent='Season â€”';
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
      const summary=`<div style="display:flex;align-items:center;gap:12px;background:#171717;border:1px solid ${tier.color};border-radius:10px;padding:12px 14px;margin-bottom:16px"><img src="${tier.icon}" alt="" style="width:${summaryPx}px;height:${summaryPx}px;object-fit:contain;flex-shrink:0" onerror="this.style.display='none'" /><div style="flex:1;min-width:0"><div style="font-size:13px;font-weight:700;color:#fff">${tier.name} Â· ${Number(pts).toLocaleString('en-US')} pts</div><div style="height:6px;background:rgba(255,255,255,0.10);border-radius:999px;margin-top:8px;overflow:hidden"><div style="height:100%;width:${pct}%;background:${tier.color};border-radius:999px"></div></div><div style="font-size:11px;color:rgba(255,255,255,0.56);margin-top:6px">${toNext} (${pct}%)</div></div></div>`;
      const cards=RANKED_TIERS.map(t=>{
        const isHere=t.name===here;
        const px=RANKED_ICON_PX(t.name);
        return `<div style="background:#171717;border:1px solid ${isHere?t.color:'rgba(255,255,255,0.10)'};border-radius:10px;padding:14px 8px 12px 8px;text-align:center"><div style="height:72px;display:flex;align-items:center;justify-content:center"><img src="${t.icon}" alt="${t.name}" style="width:${px}px;height:${px}px;object-fit:contain" onerror="this.style.display='none'" /></div><div style="font-size:12px;font-weight:700;color:${t.color};margin-top:10px">${t.name}</div><div style="font-size:11px;color:rgba(255,255,255,0.56);margin-top:2px">${Number(t.min).toLocaleString('en-US')}+</div><div style="font-size:11px;color:rgba(255,255,255,0.36)">Fee ${t.fee}</div>${isHere?`<div style="font-size:11px;font-weight:700;color:#fff;margin-top:8px">â— You are here</div>`:''}</div>`;
      }).join('');
      body.innerHTML=`${summary}<div style="display:grid;grid-template-columns:repeat(5,1fr);grid-auto-rows:1fr;gap:12px">${cards}</div><div style="font-size:11px;color:rgba(255,255,255,0.36);text-align:center;margin-top:12px">Entry fees burn on every ranked start â€” higher tier, steeper entry.</div>`;
    } else {
      renderRankedLb(rankedLbCache ? rankedLbCache.rows : null);
    }
  }
  function renderRankedLb(rows){
    if(!rankedPanel) return;
    const body=rankedPanel.querySelector('#bnc-ranked-body');
    if(!body) return;
    if(!rows){
      body.innerHTML=`<div style="text-align:center;color:#9ca3af;font-size:13px;padding:40px 0">Loading leaderboardâ€¦</div>`;
      return;
    }
    if(!rows.length){
      body.innerHTML=`<div style="text-align:center;color:#9ca3af;font-size:13px;padding:40px 0">No ranked players yet â€” be the first.</div>`;
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
  // Privileged GET for the F9 leaderboard (explicit user action â€” the only
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
    ov.innerHTML=`<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:14px;width:640px;max-width:94vw;height:480px;max-height:90vh;display:flex;flex-direction:column;overflow:hidden"><div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.08)"><div><b id="bnc-host11-title" style="font-size:16px;color:#fff">Host roster</b><div id="bnc-host11-sub" style="font-size:11px;color:#9ca3af;margin-top:2px">â€”</div></div><div><button id="bnc-host11-refresh" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px;margin-right:6px">Refresh</button><button id="bnc-host11-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px">Close</button></div></div><div id="bnc-host11-body" style="flex:1;overflow:auto;padding:16px 18px;font-size:13px;color:#e5e7eb"></div></div>`;
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
    if(tid==null){ if(body) body.innerHTML='<span style="color:#9ca3af">No tourney context â€” join the tourney room first.</span>'; return; }
    if(body) body.innerHTML='<span style="color:#9ca3af">Loadingâ€¦</span>';
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
    if(sub) sub.textContent=(t.name||'Tourney')+' Â· '+(t.status||'');
    const st=(_hostTourneyCache||{}).standings||[];
    if(!st.length){ body.innerHTML='<span style="color:#9ca3af">No sealed matches yet.</span>'; return; }
    const prm=_hostPrCache||{};
    let h='<table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="color:#9ca3af;text-align:left"><th>#</th><th>Player</th><th>Pts</th><th>K</th><th>W</th><th>Avg</th><th>PR</th></tr>';
    st.forEach((r,i)=>{
      const nm=(r&&r.name)||'?'; const did=String((r&&r.discord_id)||'');
      const pr=prm[did];
      h+=`<tr style="border-top:1px solid rgba(255,255,255,0.06)"><td>${i+1}</td><td><b style="color:#fff">${String(nm).replace(/</g,'&lt;')}</b></td><td>${r.points||0}</td><td>${r.kills||0}</td><td>${r.wins||0}</td><td>${r.avg_placement!=null?r.avg_placement:'â€”'}</td><td>${pr!=null?pr:'â€”'}</td></tr>`;
    });
    body.innerHTML=h+'</table>';
  }
  function toggleHost11Panel(force){
    createHost11Panel();
    const isHidden=host11Panel.style.display==='none'||!host11Panel.style.display;
    const show=typeof force==='boolean'?force:isHidden;
    if(show && !hostPanelsAllowed()){ showToast('Host roster needs a tourney room', true); return; }
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
    if(!ctx){ if(body) body.innerHTML='<span style="color:#9ca3af">No ranked context â€” join the ranked room first.</span>'; return; }
    if(body) body.innerHTML='<span style="color:#9ca3af">Loadingâ€¦</span>';
    const rk=((statsCache||{}).ranked)||{};
    const mver=rk.match_version;
    const sub=document.getElementById('bnc-host11-sub');
    if(sub) sub.textContent=`${ctx.code||'Ranked'} Â· ${ctx.status||''} Â· ${ctx.rid}`;
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
      +`<span style="color:#e5e7eb"> Â· ${m.sealed?'sealed':'live'}${m.winner_discord_id?' Â· winner crowned':''} Â· ${board.length} playing</span></div>`;
    if(!board.length){ body.innerHTML=h+'<span style="color:#9ca3af">No banked events yet this match.</span>'; return; }
    h+='<table style="width:100%;border-collapse:collapse;font-size:13px"><tr style="color:#9ca3af;text-align:left"><th>#</th><th>Player</th><th>K</th><th>Plc</th><th>Match</th><th>Season</th></tr>';
    board.forEach((r,i)=>{
      const nm=String((r&&r.name)||'?').replace(/</g,'&lt;');
      const did=String((r&&r.discord_id)||'');
      const crown=(r&&r.won)?' ðŸ‘‘':'';
      const season=lb[did];
      h+=`<tr style="border-top:1px solid rgba(255,255,255,0.06)"><td>${i+1}</td><td><b style="color:#fff">${nm}</b>${crown}</td><td>${(r&&r.kills)||0}</td><td>${(r&&r.placement)!=null?r.placement:'â€”'}</td><td>${(r&&r.points)||0}</td><td>${season!=null?season:'â€”'}</td></tr>`;
    });
    body.innerHTML=h+'</table>';
  }
  function createHost12Panel(){
    if(document.getElementById('bnc-host12')){ host12Panel=document.getElementById('bnc-host12'); return; }
    const ov=document.createElement('div');
    ov.id='bnc-host12';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483646', background:'rgba(0,0,0,0.64)', display:'none', opacity:'0', transition:'opacity 180ms ease-out', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif'});
    ov.innerHTML=`<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:14px;width:560px;max-width:94vw;max-height:90vh;display:flex;flex-direction:column;overflow:hidden"><div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.08)"><div><b id="bnc-host12-title" style="font-size:16px;color:#fff">Host controls</b><div id="bnc-host12-sub" style="font-size:11px;color:#9ca3af;margin-top:2px">â€”</div></div><button id="bnc-host12-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px">Close</button></div><div id="bnc-host12-body" style="padding:16px 18px;font-size:13px;color:#e5e7eb;overflow:auto"></div><div id="bnc-host12-status" style="padding:8px 18px;font-size:12px;color:#9ca3af;border-top:1px solid rgba(255,255,255,0.08)"></div></div>`;
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
    if(sub) sub.textContent=(t.name||'Tourney')+(m?(' Â· match v'+m.version+' ('+m.status+')'):' Â· no open match');
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
    q('h12-winGo').onclick=()=>{ const mm=needMatch(); if(!mm) return; const raw=q('h12-winner').value; const did=raw?resolveDid(raw):null; if(raw&&!did){ host12Say('Unknown player â€” use the F11 name or id.', true); return; } hostApiPost('/api/comp/match/winner',{match_id:mm.id,winner_discord_id:did},(j)=>{ host12Say(j&&!j.error?('Winner set'+(j.placements?' (+P1)':'')):('Failed: '+((j&&j.error)||'no response')), !(j&&!j.error)); fetchHostTourney(); }); };
    q('h12-sealGo').onclick=()=>{ const mm=needMatch(); if(!mm) return; hostApiPost('/api/comp/match/seal',{match_id:mm.id},(j)=>{ host12Say(j&&!j.error?'Sealed.':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error)); fetchHostTourney(); }); };
    q('h12-openGo').onclick=()=>{ const tid=hostTourneyId(); if(tid==null){ host12Say('No tourney context.', true); return; } hostApiPost('/api/comp/match/open',{tourney_id:tid},(j)=>{ host12Say(j&&!j.error?('Opened v'+j.version+'.'):'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error)); fetchHostTourney(); }); };
    q('h12-rmGo').onclick=()=>{ const mm=needMatch(); if(!mm) return; const did=resolveDid(q('h12-rm').value); if(!did){ host12Say('Unknown player.', true); return; } hostApiPost('/api/comp/match/remove',{match_id:mm.id,discord_id:did},(j)=>{ host12Say(j&&!j.error?'Removed (ignored in placements).':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error)); fetchHostTourney(); }); };
    q('h12-addGo').onclick=()=>{ const mm=needMatch(); if(!mm) return; const did=resolveDid(q('h12-rm').value); if(!did){ host12Say('Unknown player.', true); return; } hostApiPost('/api/comp/match/add-back',{match_id:mm.id,discord_id:did},(j)=>{ host12Say(j&&!j.error?'Added back.':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error)); fetchHostTourney(); }); };
  }
  function toggleHost12Panel(force){
    createHost12Panel();
    const isHidden=host12Panel.style.display==='none'||!host12Panel.style.display;
    const show=typeof force==='boolean'?force:isHidden;
    if(show && !hostPanelsAllowed()){ showToast('Host controls need a tourney room', true); return; }
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
    if(sub) sub.textContent=ctx?`${ctx.code||'Ranked'} Â· ${ctx.status||''} Â· ${ctx.rid}`:'â€”';
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
      host12Say('Backing outâ€¦');
      hostApiPost('/api/ranked/room/backout',{room_id:c.rid,discord_id:d},(j)=>{
        host12Say(j&&!j.error?'Backed out â€” room is lobby. Restart via /ranked start.':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error));
      });
    };
    q('h12-rEnd').onclick=()=>{
      const c=needCtx(); if(!c) return;
      const d=did(); if(!d){ host12Say('Link your Discord account first.', true); return; }
      host12Say('Endingâ€¦');
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
        host12Say('Setting winnerâ€¦');
        hostApiPost('/api/ranked/match/winner',{room_id:c.rid,match_version:mv,discord_id:d,winner_discord_id:wdid},(j)=>{
          host12Say(j&&!j.error?'Winner set â€” ledger recalculated.':'Failed: '+((j&&j.error)||'no response'), !(j&&!j.error));
        });
      };
      if(!raw){ send(null); return; }
      if(/^\d+$/.test(raw)){ send(raw); return; }
      rankedApiGet('/api/ranked/match?room_id='+encodeURIComponent(c.rid)+'&match_version='+encodeURIComponent(mv),(j)=>{
        try{
          const b=(j&&j.board)||[]; const low=raw.toLowerCase();
          const hit=b.find(r=>String(r.discord_id)===raw)||b.find(r=>String(r.name||'').toLowerCase()===low);
          if(!hit){ host12Say('Unknown player â€” use the F11 name or id.', true); return; }
          send(String(hit.discord_id));
        }catch{ host12Say('Resolve failed.', true); }
      });
    };
    q('h12-rRecalc').onclick=()=>{
      const c=needCtx(); if(!c) return;
      const d=did(); if(!d){ host12Say('Link your Discord account first.', true); return; }
      const mv=lastSealedMver(); if(mv==null){ host12Say('No sealed match yet.', true); return; }
      host12Say('Recalculatingâ€¦');
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
  // --- LEADERBOARD PANEL: Alt+Shift+L â€” all players, any time ---
  // Standalone overlay (same card pattern as the ranked panel): sortable
  // columns (points/kills/wins/K-D/playtime), paginated, Esc/backdrop closes.
  // Insights + Friends tabs plug into lbTab/renderLbPanel in later slices.
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
  // physical keystroke (defaultPrevented at window capture â€” see diagnosis
  // above, which no element-level handler can undo). One âŒ¨ toggle per input,
  // grid sits below its row, Enter triggers the input's own action. Never throws.
  function attachVkbd(input, onEnter){
    try{
      if(!input || input.dataset.bncVkbd) return;
      input.dataset.bncVkbd='1';
      const toggle=document.createElement('button');
      toggle.textContent='âŒ¨';
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
      [['space',' ',4],['âŒ«','\b',2],['Enter','\n',2],['Hide','',2]].forEach(([label,ch,flex])=>{
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
  // lobby holding no code). No server call â€” the next pID join re-resolves
  // the same code fresh via fetchCompRoom. Never throws.
  function resetCompRoomLocal(){
    try{ _compRoom=null; }catch{}
    try{ _compHudData=null; }catch{}
    try{ _publicRoom=false; }catch{}
    try{ updateTourneyHud(); }catch{}
    try{ showToast('Comp room cleared (F7) â€” re-set or rejoin the code', false); }catch{}
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
      // (or tag missing) â€” show what we know until stats confirm or deny.
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
    const escS=(v)=> String(v==null?'â€”':v).replace(/</g,'&lt;');
    const pts=d.points!=null?Number(d.points).toLocaleString('en-US'):'â€”';
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
    try{ if(typeof _inCompRoom==='function' && _inCompRoom()){ el.style.display='none'; return; } }catch{}
    try{ if(typeof liveRanked==='function' && liveRanked()){ el.style.display='none'; return; } }catch{}
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
    const escS=(v)=> String(v==null?'—':v).replace(/</g,'&lt;');
    const pts=d.points!=null?Number(d.points).toLocaleString('en-US'):'—';
    // bare counter like comp, gold accent for ranked identity
    el.innerHTML=`<div style="font-size:23px;font-weight:800;color:#FFD700">${escS(pts)}</div>`;
    el.style.display='flex';
    try{ updateRtToast(d); }catch{}
  }
  // --- RT TOASTS: same rule as comp (wins -> VICTORY, kills ->
  // ELIMINATION, else PLACEMENT), own baseline per tourney. Shared queue.
  function updateRtToast(d){
    const snap=(d && d.tourney_id!=null)
      ? {tid:String(d.tourney_id), pts:Number(d.points)||0, kills:Number(d.kills)||0, wins:Number(d.wins)||0}
      : {tid:'', pts:0, kills:0, wins:0};
    const prev=_prevRtSnap;
    _prevRtSnap=snap;
    if(!prev) return;  // first sighting: baseline only
    if(!IS_GAME_FRAME) return;  // track baseline everywhere, toast in game only
    if(!snap.tid || snap.tid!==prev.tid) return;  // left room / new tourney: re-baseline silently
    const delta=snap.pts-prev.pts;
    if(!(delta>0)) return;
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
      const st=sess&&String(sess.state||'').toLowerCase();
      let inMatch=false;
      try{ inMatch=!!_optimisticState.inMatch; }catch{}
      if(!inMatch) inMatch=(st==='match');
      if(inMatch) return {ok:false, code:null, why:'Invites off mid-match'};
      return {ok:true, code:String(code).toUpperCase(), why:''};
    }catch{ return {ok:false, code:null, why:'Unavailable'}; }
  }
  // Plain JSON POST (no session tokens â€” friends/party API is discord_id-keyed).
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
  // ---- CLIPS (Postparty for BuildNow GG, drawer-only MVP) ----
  // Rolling canvas capture: MediaRecorder on the Unity WebGL canvas keeps the
  // last N seconds in memory; kill/win/hotkey slices it into a stored clip.
  // Local-first (IndexedDB, no server) so the F4 > Clips tab works offline.
  // Never throws (game loop must survive recorder failures).
  const CLIP_DEF={enabled:true, len:30, fps:15, bitrate:1500000, autoKill:true, autoDeath:true, autoWin:true, after:5, audio:true};
  function clipCfg(){
    try{
      return {
        enabled: GMGet('bnc_clip_enabled', CLIP_DEF.enabled),
        len: Number(GMGet('bnc_clip_len', CLIP_DEF.len))||CLIP_DEF.len,
        fps: Number(GMGet('bnc_clip_fps', CLIP_DEF.fps))||CLIP_DEF.fps,
        bitrate: Number(GMGet('bnc_clip_bitrate', CLIP_DEF.bitrate))||CLIP_DEF.bitrate,
        autoKill: GMGet('bnc_clip_autokill', CLIP_DEF.autoKill),
        autoDeath: GMGet('bnc_clip_autodeath', CLIP_DEF.autoDeath),
        autoWin: GMGet('bnc_clip_autowin', CLIP_DEF.autoWin),
        after: Number(GMGet('bnc_clip_after', CLIP_DEF.after))||0,
        audio: GMGet('bnc_clip_audio', CLIP_DEF.audio)!==false,
      };
    }catch{ return Object.assign({}, CLIP_DEF); }
  }
  // Only encoder-affecting keys restart the recorder (changing Aftermath or
  // auto-clip toggles must NOT wipe the buffered seconds).
  function clipSet(k, v){ try{ GMSet(k, v); }catch{} try{ if(k==='bnc_clip_len'||k==='bnc_clip_fps'||k==='bnc_clip_bitrate'||k==='bnc_clip_enabled'||k==='bnc_clip_audio') clipRestart(); }catch{} try{ if(typeof renderLbClips==='function' && lbTab==='clips') renderLbClips(); }catch{} }
  function clipPickMime(withAudio){
    // Prefer hardware-accelerated codecs first (H.264 mp4), VP8 over VP9
    // (VP9 software encode is what tanked FPS: 60 -> 20 at full-res).
    // With audio we need an AV mux: AAC-in-mp4 first, then opus-in-webm.
    try{
      const cands=withAudio
        ? ['video/mp4;codecs="avc1.42E01E,mp4a.40.2"','video/webm;codecs=vp8,opus','video/webm;codecs=vp9,opus','video/webm','video/mp4']
        : ['video/mp4;codecs="avc1.42E01E"','video/mp4','video/webm;codecs=vp8','video/webm','video/webm;codecs=vp9'];
      for(const m of cands){ try{ if(typeof MediaRecorder!=='undefined' && MediaRecorder.isTypeSupported(m)) return m; }catch{} }
    }catch{}
    return '';
  }
  function clipExt(mime){ try{ if(String(mime||'').includes('mp4')) return 'mp4'; }catch{} return 'webm'; }
  let _clip={rec:null, stream:null, chunks:[], segTimer:null, segStartedAt:0, finalizing:false, started:false, lastErr:'', mime:'', withAudio:false, recStartedAt:0, killAt:0, winAt:0, winKey:'',     pendingTimer:null, pendingTrigger:null, lastKill:null, deathAt:0, cw:0, ch:0};
  function clipFindCanvas(){
    try{
      const cs=document.querySelectorAll('canvas');
      if(!cs || !cs.length) return null;
      let best=null, bestArea=0;
      for(const c of cs){ try{ const a=(c.width||0)*(c.height||0); if(a>bestArea){ bestArea=a; best=c; } }catch{} }
      return best;
    }catch{ return null; }
  }
  // ---- CLIPS AUDIO TAP (game sound in recordings) ----
  // Unity WebGL plays all sound through realtime AudioContexts ending at
  // ctx.destination. We override that getter with a transparent GainNode
  // passthrough: game -> proxy -> speakers, plus proxy -> MediaStream.
  // Sample-accurate, no latency, no double audio. Nodes connected BEFORE this
  // runs keep pointing at the real destination (missed), but every sound that
  // reads ctx.destination afterwards â€” virtually all SFX â€” flows through.
  // disconnect(realDest) from game code is remapped to the proxy so sounds can
  // still be stopped. All try/caught: on failure clips fall back to silent
  // video, game audio is never broken.
  const _clipCtxs=new Set();
  const _clipTaps=(typeof WeakMap!=='undefined')?new WeakMap():null;
  let _clipAudioPatched=false;
  function clipPatchAudio(){
    if(_clipAudioPatched) return true;
    try{
      if(typeof AudioNode==='undefined') return false;
      const protos=[];
      try{ if(typeof BaseAudioContext!=='undefined'&&BaseAudioContext.prototype&&protos.indexOf(BaseAudioContext.prototype)<0) protos.push(BaseAudioContext.prototype); }catch{}
      try{ if(typeof AudioContext!=='undefined'&&AudioContext.prototype&&protos.indexOf(AudioContext.prototype)<0) protos.push(AudioContext.prototype); }catch{}
      try{ const wac=window.webkitAudioContext; if(wac&&wac.prototype&&protos.indexOf(wac.prototype)<0) protos.push(wac.prototype); }catch{}
      let ok=false;
      for(const p of protos){
        try{
          if(p.__bncClipTap){ ok=true; continue; }
          let realGet=null, owner=p;
          while(owner){ const d=Object.getOwnPropertyDescriptor(owner,'destination'); if(d&&d.get){ realGet=d.get; break; } owner=Object.getPrototypeOf(owner); }
          if(!realGet) continue;
          Object.defineProperty(p,'destination',{configurable:true,enumerable:false,get:function(){
            try{
              const ctx=this;
              try{ _clipCtxs.add(ctx); }catch{}
              let t=null;
              try{ t=_clipTaps?_clipTaps.get(ctx):ctx.__bncClipTap; }catch{}
              if(!t){
                const real=realGet.call(ctx);
                const msd=ctx.createMediaStreamDestination();
                const proxy=ctx.createGain(); proxy.gain.value=1;
                try{ proxy.connect(real); }catch{}
                try{ proxy.connect(msd); }catch{}
                t={proxy:proxy, real:real, msd:msd};
                try{ if(_clipTaps) _clipTaps.set(ctx,t); else ctx.__bncClipTap=t; }catch{}
              }
              return t.proxy;
            }catch(e){ try{ return realGet.call(this); }catch(e2){ return null; } }
          }});
          try{ p.__bncClipTap=true; }catch{}
          ok=true;
        }catch{}
      }
      try{
        if(!AudioNode.prototype.__bncClipDisc){
          const origDisc=AudioNode.prototype.disconnect;
          AudioNode.prototype.disconnect=function(){
            try{
              const a=Array.prototype.slice.call(arguments);
              if(a.length&&a[0]&&typeof a[0]==='object'){
                let t=null;
                try{ t=this.context&&(_clipTaps?_clipTaps.get(this.context):this.context.__bncClipTap); }catch{}
                if(t&&a[0]===t.real) a[0]=t.proxy;
              }
              return origDisc.apply(this,a);
            }catch(e){ try{ return origDisc.apply(this,arguments); }catch(e2){} }
          };
          try{ AudioNode.prototype.__bncClipDisc=true; }catch{}
        }
      }catch{}
      _clipAudioPatched=ok;
      return ok;
    }catch{ return false; }
  }
  function clipGameAudioTrack(){
    try{
      let found=null;
      _clipCtxs.forEach((ctx)=>{
        if(found) return;
        try{
          if(typeof OfflineAudioContext!=='undefined'&&ctx instanceof OfflineAudioContext) return;
          let t=null;
          try{ t=_clipTaps?_clipTaps.get(ctx):ctx.__bncClipTap; }catch{}
          if(!t){
            try{ void ctx.destination; }catch{}
            try{ t=_clipTaps?_clipTaps.get(ctx):ctx.__bncClipTap; }catch{}
          }
          if(t&&t.msd&&t.msd.stream){
            const trs=t.msd.stream.getAudioTracks()||[];
            for(const tr of trs){ if(tr&&tr.readyState==='live'){ found=tr; break; } }
          }
        }catch{}
      });
      return found;
    }catch{ return null; }
  }
  // Canvas resizes (window resize, fullscreen, render-scale change) rewrite the
  // encoder's codec description mid-segment â€” Chrome's avc1 warning, and the
  // resulting footage corrupts. Detect + restart the segment on new geometry.
  function clipCanvasChanged(){
    try{
      const c=clipFindCanvas();
      if(!c||!_clip.cw||!_clip.ch) return false;
      const w=c.width||0, h=c.height||0;
      return w>0&&h>0&&(w!==_clip.cw||h!==_clip.ch);
    }catch{ return false; }
  }
  // Instant resize guard (the 15s poll is backup): a canvas geometry change
  // mid-segment rewrites the encoder's codec description â€” Chrome's avc1
  // warning â€” and corrupts everything after it. Restart immediately so no
  // saved clip ever spans a resize. During a kill aftermath wait, finalize
  // early instead: a shorter playable clip beats a full corrupted one.
  let _clipRO=null;
  function clipWatchResize(canvas){
    try{
      if(_clipRO){ try{ _clipRO.disconnect(); }catch{} _clipRO=null; }
      if(!canvas||typeof ResizeObserver==='undefined') return;
      let lastW=canvas.width||0, lastH=canvas.height||0;
      _clipRO=new ResizeObserver(()=>{
        try{
          const w=canvas.width||0, h=canvas.height||0;
          if(!(w>0&&h>0)) return;
          if(w===lastW&&h===lastH) return;
          lastW=w; lastH=h;
          try{ _clip.cw=w; _clip.ch=h; }catch{}
          if(!_clip.started||_clip.finalizing) return;
          if(_clip.pendingTimer){
            try{ clearTimeout(_clip.pendingTimer); _clip.pendingTimer=null; }catch{}
            try{ logInfo('[CLIPS] resize during aftermath â€” finalizing early'); }catch{}
            saveClip(_clip.pendingTrigger||'kill');
            return;
          }
          try{ logInfo('[CLIPS] canvas resized â€” restarting segment'); }catch{}
          clipRestart();
        }catch{}
      });
      try{ _clipRO.observe(canvas); }catch{}
    }catch{}
  }
  // Playability self-check: decode the first frame before storing. A broken
  // mux (mid-segment resize, bad audio interleave) then becomes a clear
  // "discarded" + status reason instead of a black file in the library.
  function clipVerifyPlayable(blob){
    return new Promise((res)=>{
      let done=false, url='', v=null;
      const finish=(ok)=>{
        if(done) return; done=true;
        try{ if(v){ try{ v.pause(); }catch{} try{ v.removeAttribute('src'); }catch{} try{ v.load(); }catch{} try{ v.remove(); }catch{} } }catch{}
        try{ if(url) URL.revokeObjectURL(url); }catch{}
        res(ok);
      };
      try{
        url=URL.createObjectURL(blob);
        v=document.createElement('video');
        v.muted=true; v.preload='auto';
        v.onloadeddata=()=>finish(true);
        v.onerror=()=>finish(false);
        v.src=url;
        setTimeout(()=>finish(false), 6000);
      }catch(e){ finish(false); }
    });
  }
  function clipRestart(){
    try{ clipStop(); }catch{}
    try{ clipStart(); }catch{}
  }
  function clipStart(){
    try{
      const cfg=clipCfg();
      if(!cfg.enabled) return;
      if(!IS_GAME_FRAME) return;
      if(typeof MediaRecorder==='undefined') { _clip.lastErr='MediaRecorder unsupported'; return; }
      if(_clip.started) return;
      const canvas=clipFindCanvas();
      if(!canvas){ _clip.lastErr='No game canvas found'; return; }
      try{ _clip.cw=canvas.width||0; _clip.ch=canvas.height||0; }catch{}
      try{ clipWatchResize(canvas); }catch{}
      let stream=null;
      try{ stream=canvas.captureStream(Number(cfg.fps)||15); }
      catch(e){ try{ stream=canvas.captureStream(); }catch(e2){ _clip.lastErr='captureStream failed'; return; } }
      if(!stream){ _clip.lastErr='captureStream empty'; return; }
      try{ clipPatchAudio(); }catch{}
      // Mix game audio in: canvas video track + tap audio track, one stream.
      // No live audio track (patch late / sound off / disabled) = video-only.
      let withAudio=false;
      try{
        const vt=(stream.getVideoTracks()||[])[0]||null;
        if(cfg.audio&&vt){
          const at=clipGameAudioTrack();
          if(at){ stream=new MediaStream([vt, at]); withAudio=true; }
        }
      }catch{}
      _clip.withAudio=withAudio;
      _clip.stream=stream; _clip.lastErr='';
      _clip.started=true; _clip.recStartedAt=Date.now();
      if(!clipStartSegment()){ try{ stream.getVideoTracks().forEach(t=>{ try{ t.stop(); }catch{} }); }catch{} _clip.stream=null; _clip.withAudio=false; _clip.started=false; return; }
      try{
        if(!clipStart._visHooked){
          clipStart._visHooked=true;
          document.addEventListener('visibilitychange', ()=>{ try{ if(document.hidden) clipPause(true); else clipPause(false); }catch{} });
        }
      }catch{}
    }catch{}
  }
  function clipPause(p){
    try{
      const r=_clip.rec;
      if(!r) return;
      if(p && r.state==='recording'){ try{ r.pause(); }catch{} }
      else if(!p && r.state==='paused'){ try{ r.resume(); }catch{} }
    }catch{}
  }
  // Segmented recording: each segment opens with a fresh keyframe + headers,
  // so finalizing it with stop() yields a self-contained playable file.
  // Slicing chunks out of a running stream (the old ring-buffer approach)
  // starts mid-GOP with no reference frame â€” macroblock garbage like the
  // 1:09 corrupted mp4. Never build files that way; always finalize.
  function clipStartSegment(){
    try{
      if(!_clip.stream) return false;
      const cfg=clipCfg();
      const mime=clipPickMime(!!_clip.withAudio);
      const opts={videoBitsPerSecond:Number(cfg.bitrate)||1500000};
      if(_clip.withAudio) opts.audioBitsPerSecond=128000;
      if(mime) opts.mimeType=mime;
      let rec=null;
      try{ rec=new MediaRecorder(_clip.stream, opts); }catch(e){ try{ rec=new MediaRecorder(_clip.stream); }catch(e2){ _clip.lastErr='Recorder init failed'; return false; } }
      _clip.rec=rec; _clip.chunks=[]; _clip.mime=mime||''; _clip.segStartedAt=Date.now(); _clip.finalizing=false;
      rec.ondataavailable=(e)=>{ try{ if(e.data && e.data.size>0) _clip.chunks.push(e.data); }catch{} };
      rec.onerror=()=>{ try{ _clip.lastErr='Recorder error'; }catch{} };
      try{ rec.start(1000); }catch(e){ _clip.lastErr='Recorder start failed'; return false; }
      try{ if(_clip.segTimer) clearTimeout(_clip.segTimer); }catch{}
      try{ _clip.segTimer=setTimeout(()=>{ try{ clipRotate(); }catch{} }, Math.max(10, Math.min(90, Number(cfg.len)||30))*1000); }catch{}
      return true;
    }catch{ return false; }
  }
  function clipRotate(){
    // Silent rotation at the buffer boundary: discard, start fresh.
    try{
      const r=_clip.rec;
      if(!r || r.state==='inactive'){ try{ if(_clip.started) clipStartSegment(); }catch{} return; }
      _clip.finalizing=true;
      try{
        r.onstop=()=>{ try{ if(_clip.rec!==r) return; _clip.rec=null; _clip.chunks=[]; _clip.finalizing=false; }catch{} try{ if(_clip.started && !clipStartSegment()) clipRetrySoon(); }catch{} };
      }catch{}
      try{ r.stop(); }catch(e){ try{ _clip.finalizing=false; }catch{} }
    }catch{}
  }
  function clipRetrySoon(){
    try{ if(_clip.segTimer) clearTimeout(_clip.segTimer); }catch{}
    try{ _clip.segTimer=setTimeout(()=>{ try{ if(_clip.started && !_clip.rec) clipStartSegment(); }catch{} }, 5000); }catch{}
  }
  function clipFinalize(){
    // Stop now and resolve with the finished playable blob (up to `len`
    // seconds ending at the trigger). Container is closed properly by stop().
    return new Promise((res)=>{
      try{
        const r=_clip.rec;
        if(!r || r.state==='inactive' || _clip.finalizing){ res(null); return; }
        _clip.finalizing=true;
        try{ if(_clip.segTimer) clearTimeout(_clip.segTimer); }catch{}
        const mime=(r.mimeType)||_clip.mime||clipPickMime(!!_clip.withAudio)||'video/webm';
        let done=false;
        const finish=()=>{ if(done) return; done=true; try{ if(_clip.rec!==r) return; _clip.rec=null; _clip.chunks=[]; _clip.finalizing=false; }catch{} try{ if(_clip.started && !_clip.rec && !clipStartSegment()) clipRetrySoon(); }catch{} };
        try{ r.onstop=()=>{ try{ const blob=new Blob(_clip.chunks.slice(), {type:mime}); finish(); res({blob, mime}); }catch(e){ finish(); res(null); } }; }catch{ finish(); res(null); return; }
        try{ r.requestData(); }catch{}
        setTimeout(()=>{ try{ r.stop(); }catch(e){ finish(); res(null); } }, 250);
        setTimeout(()=>{ if(!done){ finish(); res(null); } }, 4000);
      }catch{ res(null); }
    });
  }
  function clipStop(){
    try{ if(_clipRO){ _clipRO.disconnect(); _clipRO=null; } }catch{}
    try{ if(_clip.pendingTimer){ clearTimeout(_clip.pendingTimer); _clip.pendingTimer=null; } }catch{}
    try{ if(_clip.segTimer){ clearTimeout(_clip.segTimer); _clip.segTimer=null; } }catch{}
    try{ if(_clip.rec && _clip.rec.state!=='inactive'){ try{ _clip.rec.onstop=null; _clip.rec.stop(); }catch{} } }catch{}; _clip.rec=null;
    try{ if(_clip.stream){ _clip.stream.getVideoTracks().forEach(t=>{ try{ t.stop(); }catch{} }); } }catch{}
    _clip.rec=null; _clip.stream=null; _clip.chunks=[]; _clip.finalizing=false; _clip.withAudio=false; _clip.started=false;
  }
  function clipReady(){ try{ return !!(_clip.started && _clip.rec && _clip.rec.state==='recording' && !_clip.finalizing && (Date.now()-(_clip.segStartedAt||0))>3000); }catch{ return false; } }
  function clipDb(){
    return new Promise((res)=>{
      try{
        const rq=indexedDB.open('bnc-clips', 1);
        rq.onupgradeneeded=()=>{ try{ rq.result.createObjectStore('clips', {keyPath:'id'}); }catch{} };
        rq.onsuccess=()=>res(rq.result);
        rq.onerror=()=>res(null);
      }catch{ res(null); }
    });
  }
  async function clipStore(meta, blob){
    try{
      const db=await clipDb();
      if(!db) return null;
      const row={id:'c'+Date.now().toString(36)+Math.floor(Math.random()*1e6).toString(36), ts:Date.now(), trigger:meta.trigger||'manual', mime:blob.type||'video/webm', size:blob.size||0, room:meta.room||null, mv:meta.mv||null, blob};
      await new Promise((ok, no)=>{ try{ const tx=db.transaction('clips','readwrite'); tx.objectStore('clips').put(row); tx.oncomplete=ok; tx.onerror=no; }catch(e){ no(e); } });
      // Cap library at 20 newest (30s@1.5Mbps â‰ˆ 5.6MB each â€” 50 would eat ~280MB).
      try{
        const all=await new Promise((res)=>{ try{ const tx=db.transaction('clips','readonly'); const q=tx.objectStore('clips').getAll(); q.onsuccess=()=>res(q.result||[]); q.onerror=()=>res([]); }catch{ res([]); } });
        if(all.length>20){
          all.sort((a,b)=>(a.ts||0)-(b.ts||0));
          const drop=all.slice(0, all.length-20);
          await new Promise((res)=>{ try{ const tx=db.transaction('clips','readwrite'); const st=tx.objectStore('clips'); drop.forEach(r=>{ try{ st.delete(r.id); }catch{} }); tx.oncomplete=res; tx.onerror=res; }catch{ res(); } });
        }
      }catch{}
      try{ db.close(); }catch{}
      return row;
    }catch{ return null; }
  }
  async function clipList(){
    try{
      const db=await clipDb();
      if(!db) return [];
      const rows=await new Promise((res)=>{ try{ const tx=db.transaction('clips','readonly'); const q=tx.objectStore('clips').getAll(); q.onsuccess=()=>res(q.result||[]); q.onerror=()=>res([]); }catch{ res([]); } });
      try{ db.close(); }catch{}
      rows.sort((a,b)=>(b.ts||0)-(a.ts||0));
      return rows.slice(0, 20);
    }catch{ return []; }
  }
  async function clipDelete(id){
    try{
      const db=await clipDb();
      if(!db) return;
      await new Promise((res)=>{ try{ const tx=db.transaction('clips','readwrite'); tx.objectStore('clips').delete(id); tx.oncomplete=res; tx.onerror=res; }catch{ res(); } });
      try{ db.close(); }catch{}
    }catch{}
  }
  async function clipClearAll(){
    try{
      const db=await clipDb();
      if(!db) return;
      await new Promise((res)=>{ try{ const tx=db.transaction('clips','readwrite'); tx.objectStore('clips').clear(); tx.oncomplete=res; tx.onerror=res; }catch{ res(); } });
      try{ db.close(); }catch{}
    }catch{}
  }
  function clipRoomCtx(){
    try{
      const sess=statsCache&&statsCache.session;
      return {room:(sess&&sess.short_code)||null, mv:(sess&&sess.match_version)||null};
    }catch{ return {room:null, mv:null}; }
  }
  // Auto-clips (kill/win) wait `after` seconds before finalizing, so the clip
  // keeps rolling through the aftermath (death cam, reactions) instead of
  // cutting dead at the trigger frame. Manual F8 stays instant. While waiting,
  // rotation is held off so the segment can't split mid-aftermath.
  function clipScheduleAuto(trigger){
    try{
      const cfg=clipCfg();
      if(!cfg.enabled) return;
      if(_clip.finalizing || _clip.pendingTimer) return;
      if(!clipReady()){ saveClip(trigger); return; }
      const wait=Math.max(0, Math.min(10, Number(cfg.after)||0));
      if(wait<=0){ saveClip(trigger); return; }
      try{ if(_clip.segTimer){ clearTimeout(_clip.segTimer); _clip.segTimer=null; } }catch{}
      _clip.pendingTrigger=trigger;
      try{ _clip.pendingTimer=setTimeout(()=>{ try{ _clip.pendingTimer=null; }catch{} saveClip(_clip.pendingTrigger||trigger); }, wait*1000); }catch{}
      try{ if(lbTab==='clips') renderLbClips(); }catch{}
    }catch{}
  }
  async function saveClip(trigger){
    try{
      try{ if(_clip.pendingTimer){ clearTimeout(_clip.pendingTimer); _clip.pendingTimer=null; } }catch{}
      const cfg=clipCfg();
      if(!cfg.enabled){ try{ showToast('Clipping is off (F4 > Clips > Enable)', true); }catch{} return null; }
      if(!clipReady()){
        try{ clipStart(); }catch{}
        if(!clipReady()){ try{ showToast('Clip not ready yet â€” ' + (_clip.lastErr||'recordingâ€¦ try again in a few seconds'), true); }catch{} return null; }
      }
      const out=await clipFinalize();
      if(!out || !out.blob || !out.blob.size){ try{ showToast('Clip not ready â€” try again in a few seconds', true); }catch{} return null; }
      const blob=out.blob, mime=out.mime;
      const plays=await clipVerifyPlayable(blob);
      if(!plays){
        try{ showToast('Clip corrupted â€” discarded, keep playing', true); }catch{}
        try{ if(_clip.lastKill&&trigger!=='manual') _clip.lastKill.decision='corrupted'; }catch{}
        try{ if(lbTab==='clips') renderLbClips(); }catch{}
        return null;
      }
      const ctx=clipRoomCtx();
      const row=await clipStore({trigger, room:ctx.room, mv:ctx.mv}, blob);
      try{ if(row&&_clip.lastKill&&trigger!=='manual') _clip.lastKill.decision='saved'; }catch{}
      try{ showToast('Clip saved â€” F4 > Clips to watch', false); }catch{}
      try{ if(lbTab==='clips') fetchLbClips(); }catch{}
      return row;
    }catch{ return null; }
  }
  function clipOnKill(killer, victim, isKill){
    try{
      const stored=String(GMGet('bnc_game_username','')||'').trim();
      const note=(decision)=>{ try{ _clip.lastKill={killer:String(killer||'?'), victim:String(victim||'?'), me:stored||'?', at:Date.now(), decision}; }catch{} };
      if(!isKill){ note('knock-ignored'); return; }
      const cfg=clipCfg();
      if(!cfg.enabled){ note('clipping-off'); return; }
      // Match on EITHER form: raw names ("cis6led" vs "cis6led") or normalized
      // (strips color tags + "dc: " prefix, same as the ranked kill toast).
      // No single canonical form is assumed â€” whichever side matches, clips.
      const rawMe=stored.toLowerCase();
      const rawK=String(killer||'').replace(/<[^>]*>/g,'').trim().toLowerCase();
      const rawV=String(victim||'').replace(/<[^>]*>/g,'').trim().toLowerCase();
      const meN=_normName(stored).trim().toLowerCase();
      const kN=_normName(killer||'').trim().toLowerCase();
      const vN=_normName(victim||'').trim().toLowerCase();
      const amKiller=((meN&&kN&&meN===kN)||(rawMe&&rawK&&rawMe===rawK))||!stored;
      const amVictim=((meN&&vN&&meN===vN)||(rawMe&&rawV&&rawMe===rawV));
      const now=Date.now();
      if(amKiller){
        if(!cfg.autoKill){ note('auto-off'); return; }
        if(now-_clip.killAt<8000){ note('debounced'); return; }
        _clip.killAt=now;
        note('queued');
        clipScheduleAuto('kill');
        return;
      }
      if(amVictim){
        if(!cfg.autoDeath){ note('auto-off'); return; }
        if(now-_clip.deathAt<8000){ note('debounced'); return; }
        _clip.deathAt=now;
        note('queued');
        clipScheduleAuto('death');
        return;
      }
      note('not-your-kill');
    }catch{}
  }
  function clipOnWin(){
    try{
      const cfg=clipCfg();
      if(!cfg.enabled || !cfg.autoWin) return;
      const now=Date.now();
      if(now-_clip.winAt<30000) return;
      _clip.winAt=now;
      clipScheduleAuto('win');
    }catch{}
  }
  let lbPanel=null, lbSort='points', lbOrder='desc', lbOffset=0, lbRows=[], lbCount=0, lbLoading=false, _lbFadeT=null;
  let lbTab='board', lbIns=null, lbInsLoading=false; // 'board' | 'insights' | 'friends' | 'events' | 'clips'
  let lbClips=[], lbClipsLoading=false, lbClipsNote='';
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
    ov.innerHTML=`<div id="bnc-lb-card" style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:14px;width:700px;max-width:94vw;height:500px;max-height:90vh;display:flex;flex-direction:column;overflow:hidden"><div style="display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid rgba(255,255,255,0.08)"><div><b style="font-size:16px;color:#fff">Leaderboard</b><div id="bnc-lb-sub" style="font-size:11px;color:#9ca3af;margin-top:2px">All players</div></div><button id="bnc-lb-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 10px;cursor:pointer;font-size:12px">Close</button></div><div style="display:flex;gap:8px;padding:12px 18px 0 18px"><button id="bnc-lb-tab-board" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Leaderboard</button><button id="bnc-lb-tab-ins" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Insights</button><button id="bnc-lb-tab-fr" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Friends</button><button id="bnc-lb-tab-ev" style="flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer">Events</button></div><div id="bnc-lb-sorts" style="display:flex;gap:8px;padding:12px 18px 0 18px;flex-wrap:wrap"></div><div id="bnc-lb-body" style="flex:1;overflow:auto;padding:16px 18px"></div><div id="bnc-lb-foot" style="display:flex;justify-content:space-between;align-items:center;padding:10px 18px;border-top:1px solid rgba(255,255,255,0.08)"><button id="bnc-lb-prev" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:5px 14px;cursor:pointer;font-size:12px">â€¹ Prev</button><span id="bnc-lb-page" style="font-size:11px;color:#9ca3af"></span><button id="bnc-lb-next" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:5px 14px;cursor:pointer;font-size:12px">Next â€º</button></div></div>`;
    (document.body||document.documentElement).appendChild(ov);
    // visible focus ring on panel text inputs (proves focus landed)
    try{
      if(!document.getElementById('bnc-lb-input-style')){
        const st=document.createElement('style');
        st.id='bnc-lb-input-style';
        st.textContent='#bnc-fr-q:focus,#bnc-chat-in:focus{outline:2px solid #fff !important;border-color:#fff !important;}';
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
    ov.querySelector('#bnc-lb-tab-ins').onclick=()=>{ lbTab='insights'; try{ stopLbChat(); }catch{} try{ untrapRelease(); }catch{} renderLbPanel(); fetchLbIns(); };
    ov.querySelector('#bnc-lb-tab-fr').onclick=()=>{ lbTab='friends'; renderLbPanel(); fetchLbFriends(); };
    ov.querySelector('#bnc-lb-tab-ev').onclick=()=>{ lbTab='events'; lbEventDetail=null; try{ stopLbChat(); }catch{} try{ untrapRelease(); }catch{} renderLbPanel(); fetchLbEvents(); };
    try{
      const tabsRow=ov.querySelector('#bnc-lb-tab-ev').parentElement;
      if(tabsRow && !ov.querySelector('#bnc-lb-tab-clips')){
        const b=document.createElement('button');
        b.id='bnc-lb-tab-clips'; b.textContent='Clips';
        b.style.cssText='flex:1;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer';
        b.onclick=()=>{ lbTab='clips'; try{ stopLbChat(); }catch{} try{ untrapRelease(); }catch{} renderLbPanel(); fetchLbClips(); };
        tabsRow.appendChild(b);
      }
    }catch{}
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
      if(lbTab==='insights') fetchLbIns(); else if(lbTab==='friends') fetchLbFriends(); else if(lbTab==='events') fetchLbEvents(); else if(lbTab==='clips') fetchLbClips(); else fetchLb();
    } else {
      try{ lbPanel.style.opacity='0'; }catch{}
      try{ stopLbChat(); }catch{}
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
    tabBtn('bnc-lb-tab-ins', lbTab==='insights');
    tabBtn('bnc-lb-tab-fr', lbTab==='friends');
    tabBtn('bnc-lb-tab-ev', lbTab==='events');
    tabBtn('bnc-lb-tab-clips', lbTab==='clips');
    // profile view replaces the board content (sorts + pagination hidden)
    if(lbTab==='board' && lbProfile){ renderLbProfile(); return; }
    const sorts=document.getElementById('bnc-lb-sorts');
    if(sorts) sorts.style.display = lbTab==='board' ? 'flex' : 'none';
    const foot=document.getElementById('bnc-lb-foot');
    if(foot) foot.style.display = lbTab==='board' ? 'flex' : 'none';
    if(lbTab==='insights'){ renderLbIns(); return; }
    if(lbTab==='friends'){ renderLbFriends(); return; }
    if(lbTab==='events'){ renderLbEvents(); return; }
    if(lbTab==='clips'){ renderLbClips(); return; }
    if(sorts){
      sorts.innerHTML=LB_SORTS.map(([v,label])=>{
        const active = lbSort===v;
        return `<button data-lb-sort="${v}" style="flex:1;min-width:90px;padding:7px 0;border-radius:999px;font-size:12px;font-weight:700;cursor:pointer;border:1px solid ${active?'#fff':'rgba(255,255,255,0.12)'};background:${active?'#fff':'#171717'};color:${active?'#000':'#9ca3af'}">${label}${active?(lbOrder==='desc'?' â†“':' â†‘'):''}</button>`;
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
    if(page) page.textContent = lbRows.length ? `Showing ${lbOffset+1}â€“${lbOffset+lbRows.length}` : 'No players yet';
    const sub=document.getElementById('bnc-lb-sub');
    if(sub) sub.textContent = `All players Â· sorted by ${(LB_SORTS.find(s=>s[0]===lbSort)||[,'?'])[1]}`;
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
    const esc=(v)=> String(v==null?'â€”':v).replace(/</g,'&lt;');
    const sess=(m)=>{
      if(!m) return 'â€”';
      const res=m.win?'W':(m.defeat?'L':'â€“');
      return `${m.kills||0}K/${m.deaths||0}D ${res} (${esc(fmtGmDrawer(m.gm))})`;
    };
    if(sub) sub.textContent='Your gameplay insights';
    if(lbInsLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading insightsâ€¦</div>`; return; }
    if(!lbIns){
      let did=null;
      try{ did=(statsCache&&statsCache.discord_id)||GMGet('bnc_discord_id',null)||null; }catch{}
      body.innerHTML=!did
        ? `<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Link your account first â€” open the player info drawer to link.</div>`
        : `<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">No insights yet â€” play a few matches first.</div>`;
      return;
    }
    const card=(label,val)=>`<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 12px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:12px"><span style="color:#9ca3af">${label}</span><span style="color:#fff;font-weight:700;text-align:right">${val}</span></div>`;
    const fear=lbIns.biggest_fear, farm=lbIns.biggest_free_kill;
    const mateI=lbIns.fav_teammate;
    const mateLine=mateI&&mateI.player?`${esc(mateI.player)} (${mateI.shared||0} shared)`:'Not yet found';
    const form=(lbIns.recent_form||[]).slice(0,5).map((m,i)=>`<div style="font-size:12px;color:#e5e7eb;padding:5px 12px">${i+1}. ${esc(sess(m))}</div>`).join('') || `<div style="font-size:12px;color:#9ca3af;padding:5px 12px">No recent matches</div>`;
    const mastery=(lbIns.mode_mastery||[]).slice(0,4).map(m=>`<div style="font-size:12px;color:#e5e7eb;padding:5px 12px;display:flex;justify-content:space-between"><span>${esc(fmtGmDrawer(m.gm))} Â· ${m.matches} matches</span><span>${esc(m.kd)} K/D Â· ${Math.round((m.win_rate||0)*100)}% W</span></div>`).join('');
    const styleTip=(typeof PLAYSTYLE_TIPS!=='undefined' && PLAYSTYLE_TIPS[lbIns.playstyle]) || 'Playstyle from kills/match, K/D and win rate.';
    body.innerHTML=`<div style="display:flex;flex-direction:column;gap:6px">`
      + `<div style="font-size:13px;font-weight:800;color:#fff;text-align:center;padding:4px 0">Playstyle: <span title="${esc(styleTip)}" style="text-decoration:underline dotted;cursor:help">${esc(lbIns.playstyle)}</span> <span style="color:#6b7280;font-weight:400">Â· ${lbIns.matches_analyzed||0} matches</span></div>`
      + card('ðŸ”¥ Best session', esc(sess(lbIns.best_session)))
      + card('ðŸ§Š Worst session', esc(sess(lbIns.worst_session)))
      + card('ðŸ˜± Biggest fear', fear&&fear.player?`${esc(fear.player)} (${fear.deaths} deaths)`:'â€”')
      + card('ðŸŽ¯ Biggest free kill', farm&&farm.player?`${esc(farm.player)} (${farm.kills} kills)`:'â€”')
      + card('ðŸŽ® Fav mode', esc(fmtGmDrawer(lbIns.fav_game_mode)))
      + card('ðŸ¤ Best teammate', mateLine)
      + card('ðŸ§¤ Clutch wins (â‰¤1 kill)', esc(lbIns.clutch_factor))
      + `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">RECENT FORM</div>` + form
      + (mastery?`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">MODE MASTERY</div>`+mastery:'')
      + `</div>`;
  }
  // --- FRIENDS TAB: request / accept / decline / invite-to-party / join ---
  // Invite is gated on holding a live private pID (myLiveCode); join copies
  // the code â€” the game needs manual entry, no auto-join is possible.
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
      // reading marks them read server-side â€” refresh the unread badges
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
    lbChatTimer=setInterval(()=>{ try{ if(lbTab==='friends' && lbChatWith && lbPanel && lbPanel.style.display!=='none') fetchLbChat(); }catch{} }, 5000);
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
    btn.disabled=true; btn.textContent='â€¦';
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
    const esc=(v)=> String(v==null?'â€”':v).replace(/</g,'&lt;');
    if(sub) sub.textContent='Friends & party invites';
    const did=myDiscordId();
    if(!did){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Link your account first â€” open the player info drawer to link.</div>`; return; }
    if(lbFriendsLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading friendsâ€¦</div>`; return; }
    if(!lbFriends && !lbInvites){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Couldn't load friends â€” server unreachable?</div>`; return; }
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
    // row() takes pre-built SAFE html for the title (dot/badge markup) â€”
    // escape dynamic text BEFORE composing (esc() here would print tags).
    const row=(titleHtml,sub2,controls)=>`<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:8px 12px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:12px"><div style="min-width:0"><div style="color:#fff;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${titleHtml}</div>${sub2?`<div style="color:#6b7280;font-size:11px">${esc(sub2)}</div>`:''}</div><div style="display:flex;gap:6px;flex-shrink:0;align-items:center">${controls}</div></div>`;
    const nm=(e)=> e.discord_username || e.game_username || String(e.discord_id||'').slice(0,6);
    let html=`<div style="display:flex;flex-direction:column;gap:6px">`;
    // incoming party invites first (join = copy code)
    const incoming=(lbInvites&&lbInvites.incoming)||[];
    if(incoming.length){
      html+=`<div style="font-size:11px;font-weight:800;color:#f59e0b;letter-spacing:.06em;padding:4px 12px 0">PARTY INVITES â€” TAP JOIN, THEN ENTER THE CODE IN-GAME</div>`;
      incoming.forEach((inv,i)=>{
        const from=inv.from_discord_username || inv.from_game_username || String(inv.from_discord_id||'').slice(0,6);
        html+=row(`ðŸ“¨ ${esc(from)} â†’ room ${esc(inv.code||'?')}`, null, `<button data-join="${i}" style="background:#f59e0b;color:#000;border:0;border-radius:6px;padding:4px 12px;cursor:pointer;font-size:11px;font-weight:800;white-space:nowrap">Join</button>`);
      });
    }
    // friends + presence + invite/chat buttons
    const friends=(lbFriends&&lbFriends.friends)||[];
    const unreadTotal=Number((lbUnread&&lbUnread.total)||0);
    if(sub) sub.textContent='Friends & party invites'+(unreadTotal>0?` Â· ${unreadTotal} unread`:'');
    html+=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">FRIENDS (${friends.length})${code?` Â· YOUR CODE: ${esc(code)}`:' Â· HOLD A PRIVATE ROOM TO INVITE'}</div>`;
    html+= friends.length ? friends.map(f=>{
      const p=f.presence||{detail:'offline'};
      const dot=`<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${presDot(p.detail)};margin-right:6px" title="${esc(presText(f))}"></span>`;
      const sub2=[presText(f), (f.game_username&&f.discord_username)?f.game_username:null].filter(Boolean).join(' Â· ');
      const gate=canPartyInvite();
      return row(dot+esc(nm(f))+unreadBadge(f.discord_id), sub2,
        `<button data-chat="${esc(f.discord_id)}" title="Chat" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:4px 10px;cursor:pointer;font-size:11px;font-weight:700;white-space:nowrap">Chat</button>`
        +btn('inv:'+f.discord_id, 'Invite', gate.ok, gate.ok?`Invite to room ${gate.code}`:gate.why));
    }).join('')
      : `<div style="font-size:12px;color:#9ca3af;padding:5px 12px">No friends yet â€” search below to add some.</div>`;
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
        html+=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">NEARBY â€” IN YOUR ROOM (${_near.length})</div>`;
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
      <div style="display:flex;gap:6px;padding:0 12px"><input id="bnc-fr-q" placeholder="Discord or game nameâ€¦" value="" style="flex:1;background:#1f2937;border:1px solid rgba(255,255,255,0.12);border-radius:8px;padding:7px 10px;color:#e5e7eb;font-size:12px;outline:none;min-width:0"><button id="bnc-fr-go" style="background:#fff;color:#000;border:0;border-radius:8px;padding:7px 14px;cursor:pointer;font-size:12px;font-weight:800">Search</button></div>
      <div id="bnc-fr-results" style="display:flex;flex-direction:column;gap:6px"></div>`;
    html+=`</div>`;
    body.innerHTML=html;
    // wire search
    const q=document.getElementById('bnc-fr-q'), go=document.getElementById('bnc-fr-go');
    const doSearch=()=>{
      const v=(q&&q.value||'').trim();
      if(v.length<2){ try{ showToast('Type at least 2 characters', true); }catch{} return; }
      const box=document.getElementById('bnc-fr-results');
      if(box) box.innerHTML=`<div style="font-size:12px;color:#9ca3af;padding:4px 12px">Searchingâ€¦</div>`;
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
        copyText(inv.code, `Code ${inv.code} copied â€” enter it in a private room`);
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
    if(sub) sub.textContent='Chat â€” '+name;
    const did=myDiscordId();
    // Poll refresh while typing: the input is alive and focused â€” update ONLY
    // the message list so the draft and focus survive. Full rebuilds happen
    // on open/send, where losing focus is correct.
    const liveIn=document.getElementById('bnc-chat-in');
    if(liveIn && document.activeElement===liveIn){
      const box=document.getElementById('bnc-chat-msgs');
      if(box){
        box.innerHTML=(lbChat||[]).map(m=>{
          const mine=String(m.from_discord_id)===String(did);
          return `<div style="align-self:${mine?'flex-end':'flex-start'};max-width:80%;background:${mine?'#fff':'#1f2937'};color:${mine?'#000':'#e5e7eb'};border-radius:10px;padding:6px 10px;font-size:12px;word-break:break-word">${esc(m.body)}</div>`;
        }).join('') || `<div style="color:#9ca3af;font-size:12px;text-align:center">No messages yet â€” say hi.</div>`;
        box.scrollTop=box.scrollHeight;
      }
      return;
    }
    const msgs=(lbChat||[]).map(m=>{
      const mine=String(m.from_discord_id)===String(did);
      return `<div style="align-self:${mine?'flex-end':'flex-start'};max-width:80%;background:${mine?'#fff':'#1f2937'};color:${mine?'#000':'#e5e7eb'};border-radius:10px;padding:6px 10px;font-size:12px;word-break:break-word">${esc(m.body)}</div>`;
    }).join('');
    body.innerHTML=`<div style="display:flex;flex-direction:column;gap:6px;height:100%">`
      + `<div style="padding:0 12px"><button id="bnc-chat-back" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:4px 12px;cursor:pointer;font-size:11px;font-weight:700">â€¹ Back</button></div>`
      + `<div id="bnc-chat-msgs" style="flex:1;overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding:6px 12px;min-height:200px;max-height:46vh">${lbChatLoading?`<div style="color:#9ca3af;font-size:12px;text-align:center">Loadingâ€¦</div>`:((msgs||`<div style="color:#9ca3af;font-size:12px;text-align:center">No messages yet â€” say hi.</div>`))}</div>`
      + `<div style="display:flex;gap:6px;padding:0 12px"><input id="bnc-chat-in" maxlength="500" placeholder="Message ${esc(name)}â€¦" style="flex:1;background:#1f2937;border:1px solid rgba(255,255,255,0.12);border-radius:8px;padding:8px 10px;color:#e5e7eb;font-size:12px;outline:none;min-width:0"><button id="bnc-chat-send" style="background:#fff;color:#000;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:12px;font-weight:800">Send</button></div>`
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
      // refocus only right after OUR send (never on poll refresh â€” reading
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
  function fetchLbEventDetail(id){
    if(!lbPanel) return;
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
    const esc=(v)=> String(v==null?'â€”':v).replace(/</g,'&lt;');
    if(lbEventDetail || lbEventLoading){ renderLbEventDetail(body, esc, sub); return; }
    if(sub) sub.textContent='Tournament events';
    if(lbEventsLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading eventsâ€¦</div>`; return; }
    const list=lbEvents||[];
    const chips=[['all','All'],['scheduled','Scheduled'],['running','Running'],['ended','Ended']];
    const rows=list.filter(t=>lbEventFilter==='all' || String(t.status||'').toLowerCase()===lbEventFilter);
    const chipHtml=chips.map(([v,label])=>{
      const active=lbEventFilter===v;
      return `<button data-ev-filter="${v}" style="flex:1;min-width:70px;padding:6px 0;border-radius:999px;font-size:11px;font-weight:700;cursor:pointer;border:1px solid ${active?'#fff':'rgba(255,255,255,0.12)'};background:${active?'#fff':'#171717'};color:${active?'#000':'#9ca3af'}">${label}</button>`;
    }).join('');
    let html=`<div style="display:flex;gap:6px;margin-bottom:10px">${chipHtml}</div>`;
    if(!rows.length){
      const hint=lbEventFilter==='running' ? 'No running events â€” check scheduled.' : lbEventFilter==='ended' ? 'No ended events yet.' : 'No events yet â€” hosts create them with /tourney-create.';
      html+=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">${hint}</div>`;
    } else {
      html+=rows.map(t=>{
        const m=evStatusMeta(t.status);
        const prog=(t.series_len||0)>0 ? `${t.sealed||0}/${t.series_len}` : `${t.sealed||0} played`;
        return `<div data-ev-open="${t.id}" title="Open event" style="display:flex;gap:10px;align-items:center;padding:9px 12px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:12px;cursor:pointer;margin-bottom:6px"><span style="width:9px;height:9px;border-radius:50%;background:${m.color};flex-shrink:0" title="${m.label}"></span><div style="flex:1;min-width:0"><div style="color:#fff;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">#${t.id} ${esc(t.name)}</div><div style="color:#6b7280;font-size:11px">${m.label} Â· ${prog} matches Â· ${t.players||0} players</div></div><span style="color:#6b7280;font-size:14px">â€º</span></div>`;
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
    if(lbEventLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loading eventâ€¦</div>`; return; }
    if(!lbEventDetail){
      body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Couldn't load this event.</div>
        <div style="text-align:center"><button id="bnc-ev-back" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:5px 14px;cursor:pointer;font-size:12px;font-weight:700">â€¹ Back</button></div>`;
      const bb=document.getElementById('bnc-ev-back');
      if(bb) bb.onclick=()=>{ lbEventDetail=null; renderLbEvents(); };
      return;
    }
    const t=lbEventDetail.tourney||{};
    const m=evStatusMeta(t.status);
    if(sub) sub.textContent=`Event #${t.id}`;
    const card=(label,val)=>`<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;padding:9px 12px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;font-size:12px"><span style="color:#9ca3af">${label}</span><span style="color:#fff;font-weight:700;text-align:right">${val}</span></div>`;
    // Scoring readout from the tourney points table (WIN owns P1: 1st dropped).
    const evOrd=(n)=>{ try{ n=Number(n); const s=['th','st','nd','rd']; const v=n%100; return (n+(s[(v-20)%10]||s[v]||s[0])).toUpperCase(); }catch{ return 'â€”'; } };
    let scoringHtml='';
    try{
      const pt=(t&&t.points_table)||{};
      const pl=pt.placement||[10,7,5,3,1];
      const parts=[`WIN = ${pt.win!=null?pt.win:3} PTS`, `KILLS = ${pt.kill!=null?pt.kill:1} PTS`];
      pl.forEach((p,i)=>{ if(i===0) return; parts.push(`${evOrd(i+1)} = ${p} PTS`); });
      scoringHtml=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">SCORING</div>`
        + `<div style="font-size:12px;color:#e5e7eb;padding:5px 12px;line-height:1.7">${parts.map(esc).join(' Â· ')}</div>`;
    }catch{}
    const players=lbEventDetail.players||[];
    const standings=lbEventDetail.standings||[];
    const matches=lbEventDetail.matches||[];
    const boards=lbEventDetail.match_boards||[];
    const boardFor=(v)=>{ const b=boards.find(x=>x&&(x.version===v)); return (b&&b.rows)||[]; };
    let html=`<div style="display:flex;flex-direction:column;gap:6px">`
      + `<div style="display:flex;align-items:center;gap:10px"><button id="bnc-ev-back" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px;font-weight:700;flex-shrink:0">â€¹ Back</button><b style="font-size:15px;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0">#${t.id} ${esc(t.name)}</b><span style="font-size:10px;font-weight:800;color:${m.color}">${m.label.toUpperCase()}</span></div>`
      + card('Series', `${t.sealed||0}/${t.series_len||'âˆž'} matches`)
      + (t.code?card('Room code', `<span style="font-family:monospace">${esc(t.code)}</span>`):'')
      + scoringHtml
      + `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">LEADERBOARD (${standings.length})</div>`
      + (standings.length ? standings.slice(0,15).map((r,i)=>`<div style="font-size:12px;color:#e5e7eb;padding:5px 12px;display:flex;justify-content:space-between"><span>${i+1}. ${esc(r.name||'?')}</span><span><b>${r.points||0}</b> pts Â· ${r.kills||0}K Â· ${r.wins||0}W${r.avg_placement!=null?' Â· '+r.avg_placement+' avg':''}</span></div>`).join('') : `<div style="font-size:12px;color:#9ca3af;padding:5px 12px">No sealed matches yet.</div>`)
      + `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">MATCHES (${matches.length})</div>`
      + (matches.length ? matches.map(mm=>{
          const rows=boardFor(mm.version);
          return `<div style="background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:8px 12px"><div style="font-size:12px;color:#fff;font-weight:700">Match v${mm.version} <span style="color:#6b7280;font-weight:400">Â· winner ${esc(mm.winner||'â€”')}</span></div>`
            + (rows.length?rows.slice(0,5).map((r,i)=>`<div style="font-size:11px;color:#9ca3af;display:flex;justify-content:space-between;padding:2px 0"><span>${i+1}. ${esc(r.name||'?')}</span><span>${r.points||0} pts</span></div>`).join(''):'')
            + `</div>`;
        }).join('') : `<div style="font-size:12px;color:#9ca3af;padding:5px 12px">No matches sealed.</div>`)
      + `<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:8px 12px 0">PLAYERS (${players.length})</div>`
      + `<div style="font-size:12px;color:#e5e7eb;padding:5px 12px;line-height:1.7">${players.length?players.slice(0,40).map(p=>esc(p.name||'?')).join(' Â· '):'No registrations yet.'}</div>`
      + `</div>`;
    body.innerHTML=html;
    const back=document.getElementById('bnc-ev-back');
    if(back) back.onclick=()=>{ lbEventDetail=null; renderLbEvents(); };
  }
  function fetchLbClips(){
    if(!lbPanel) return;
    lbClipsLoading=true; renderLbClips();
    clipList().then((rows)=>{
      lbClipsLoading=false; lbClips=rows||[];
      if(lbTab==='clips') renderLbClips();
    }).catch(()=>{ lbClipsLoading=false; lbClips=[]; if(lbTab==='clips') renderLbClips(); });
  }
  function renderLbClips(){
    const body=document.getElementById('bnc-lb-body');
    if(!body) return;
    const sub=document.getElementById('bnc-lb-sub');
    if(sub) sub.textContent='My clips â€” stored in this browser';
    const esc=(v)=> String(v==null?'â€”':v).replace(/</g,'&lt;');
    const cfg=clipCfg();
    const mimeShort=(()=>{ try{ const m=(_clip.rec&&_clip.rec.mimeType)||_clip.mime||''; let s='?'; if(m.includes('avc1')||m.includes('mp4')) s='mp4/h264'; else if(m.includes('vp8')) s='vp8'; else if(m.includes('vp9')) s='vp9'; else s=m.split(';')[0]||'?'; if(m.includes('mp4a')) s+='+aac'; else if(m.includes('opus')) s+='+opus'; return s; }catch{ return '?'; } })();
    const audioBit=_clip.withAudio?' Â· +audio':(cfg.audio?' Â· no-audio':'');
    const heavy=cfg.fps>=30 && cfg.bitrate>=2500000;
    const segAge=Math.max(0,Math.round((Date.now()-(_clip.segStartedAt||Date.now()))/1000));
    const recState=_clip.pendingTimer ? `Saving ${(_clip.pendingTrigger||'clip')} clipâ€¦` : (clipReady() ? `REC seg ${segAge}/${cfg.len}s Â· ${cfg.fps}fps ${mimeShort}${audioBit}` : (_clip.lastErr||'startingâ€¦'));
    const dot=clipReady() ? '#ef4444' : '#6b7280';
    const sel=(id, val, opts)=>`<select id="${id}" style="background:#171717;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;font-size:11px;padding:4px 6px">`+opts.map(([v,l])=>`<option value="${v}"${String(v)===String(val)?' selected':''}>${l}</option>`).join('')+`</select>`;
    const chk=(id, on, label)=>`<label style="display:flex;align-items:center;gap:6px;font-size:11px;color:#e5e7eb;cursor:pointer"><input type="checkbox" id="${id}"${on?' checked':''} style="accent-color:#fff"> ${label}</label>`;
    let html=`<div style="display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap">`
      + `<button id="bnc-clip-now" style="flex:1;min-width:120px;background:#fff;color:#000;border:0;border-radius:8px;padding:8px 0;font-size:12px;font-weight:800;cursor:pointer">â— Clip now (F8)</button>`
      + `<span style="display:flex;align-items:center;gap:6px;font-size:11px;color:#9ca3af"><span style="width:8px;height:8px;border-radius:50%;background:${dot};display:inline-block"></span>${esc(recState)}</span></div>`;
    try{
      const lk=_clip.lastKill;
      if(lk&&lk.at){
        const ago=Math.max(0,Math.round((Date.now()-lk.at)/1000));
        const why={'queued':'clip queued','saved':'clip saved','not-your-kill':'skipped (not your kill)','knock-ignored':'skipped (knock)','debounced':'skipped (too soon)','auto-off':'auto-clip off','clipping-off':'clipping off','corrupted':'discarded (corrupted)'}[lk.decision]||lk.decision||'';
        const you=lk.decision==='not-your-kill' ? ' (you: '+(lk.me||'?').slice(0,24)+')' : '';
        html+=`<div style="font-size:11px;color:#6b7280;margin:-2px 0 10px">Last killfeed: ${esc(lk.killer)} â†’ ${esc(lk.victim)} Â· ${ago}s ago Â· ${esc(why)}${esc(you)}</div>`;
      }
    }catch{}
    if(lbClipsLoading){ body.innerHTML=html+`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:20px 0">Loading clipsâ€¦</div>`; return; }
    const list=lbClips||[];
    if(!list.length){
      html+=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:24px 0">No clips yet â€” press F8 or get a kill.<br><span style="font-size:11px">Clips capture up to the last ${cfg.len}s ending at the trigger.</span></div>`;
    } else {
      html+=`<div style="display:flex;justify-content:space-between;align-items:center;margin:4px 0 8px"><span style="font-size:11px;color:#6b7280">${list.length} clip${list.length===1?'':'s'} Â· ${(list.reduce((a,r)=>a+(r.size||0),0)/1048576).toFixed(1)} MB</span><button id="bnc-clips-clear" style="background:none;border:0;color:#6b7280;font-size:11px;cursor:pointer;text-decoration:underline">Clear all</button></div>`;
      html+=list.map((r)=>{
        const d=new Date(r.ts||Date.now());
        const when=d.toLocaleString('en-US',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
        const badge={kill:'#ef4444',death:'#f59e0b',win:'#10b981',manual:'#6b7280'}[r.trigger]||'#6b7280';
        return `<div data-clip="${r.id}" style="background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:10px 12px;margin-bottom:8px">`
          + `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><span style="font-size:10px;font-weight:800;color:#fff;background:${badge};border-radius:4px;padding:2px 6px">${esc((r.trigger||'manual').toUpperCase())}</span><span style="font-size:11px;color:#9ca3af">${esc(when)} Â· ${((r.size||0)/1024).toFixed(0)} KB${r.room?' Â· '+esc(r.room):''}</span></div>`
          + `<video data-clip-vid="${r.id}" preload="metadata" muted playsinline controls style="width:100%;max-height:220px;background:#000;border-radius:6px"></video>`
          + `<div style="display:flex;gap:8px;margin-top:8px"><button data-clip-dl="${r.id}" style="flex:1;background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:6px 0;font-size:11px;font-weight:700;cursor:pointer">Download</button><button data-clip-del="${r.id}" style="background:none;border:1px solid rgba(255,255,255,0.12);color:#9ca3af;border-radius:6px;padding:6px 12px;font-size:11px;cursor:pointer">Delete</button></div></div>`;
      }).join('');
    }
    html+=`<div style="font-size:11px;font-weight:800;color:#6b7280;letter-spacing:.06em;padding:10px 0 4px">CLIPPING SETTINGS</div>`
      + `<div style="display:flex;flex-direction:column;gap:8px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:12px">`
      + `<div style="display:flex;gap:12px;flex-wrap:wrap">${chk('bnc-clip-enabled', cfg.enabled, 'Enable clipping')}${chk('bnc-clip-autokill', cfg.autoKill, 'Auto-clip my kills')}${chk('bnc-clip-autodeath', cfg.autoDeath, 'Auto-clip my deaths')}${chk('bnc-clip-autowin', cfg.autoWin, 'Auto-clip wins')}${chk('bnc-clip-audio', cfg.audio, 'Game audio')}</div>`
      + `<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:11px;color:#9ca3af"><span>Buffer</span>${sel('bnc-clip-len', cfg.len, [[15,'15s'],[30,'30s'],[45,'45s'],[60,'60s']])}<span>FPS</span>${sel('bnc-clip-fps', cfg.fps, [[10,'10'],[15,'15 âœ“'],[30,'30'],[60,'60']])}<span>Quality</span>${sel('bnc-clip-bitrate', cfg.bitrate, [[1500000,'Low âœ“'],[2500000,'Medium'],[5000000,'High']])}<span>Aftermath</span>${sel('bnc-clip-after', cfg.after, [[0,'0s'],[3,'3s'],[5,'5s âœ“'],[8,'8s']])}</div>`
      + (heavy?`<div style="font-size:11px;color:#eab308">High FPS + quality can drop game FPS â€” try 15 FPS / Low if the game stutters.</div>`:'')
      + `<div style="font-size:11px;color:#6b7280">Hotkey: F8 (change in <a href="#" id="bnc-clip-keys" style="color:#e5e7eb">Shortcuts menu</a>) Â· clips stay in this browser until downloaded. Clips saved before v2.23.2 are corrupted â€” delete them.</div>`
      + `</div>`;
    body.innerHTML=html;
    const now=document.getElementById('bnc-clip-now');
    if(now) now.onclick=()=>{ try{ saveClip('manual'); }catch{} };
    const clr=document.getElementById('bnc-clips-clear');
    if(clr) clr.onclick=()=>{ if(!confirm('Delete all clips?')) return; clipClearAll().then(()=>fetchLbClips()); };
    const keys=document.getElementById('bnc-clip-keys');
    if(keys) keys.onclick=(e)=>{ try{ e.preventDefault(); toggleKeybinds(); }catch{} };
    const bind=(id, fn)=>{ const el=document.getElementById(id); if(el) el.onchange=fn; };
    bind('bnc-clip-enabled', (e)=>clipSet('bnc_clip_enabled', e.target.checked));
    bind('bnc-clip-autokill', (e)=>clipSet('bnc_clip_autokill', e.target.checked));
    bind('bnc-clip-autodeath', (e)=>clipSet('bnc_clip_autodeath', e.target.checked));
    bind('bnc-clip-autowin', (e)=>clipSet('bnc_clip_autowin', e.target.checked));
    bind('bnc-clip-audio', (e)=>clipSet('bnc_clip_audio', e.target.checked));
    bind('bnc-clip-len', (e)=>clipSet('bnc_clip_len', Number(e.target.value)||30));
    bind('bnc-clip-fps', (e)=>clipSet('bnc_clip_fps', Number(e.target.value)||15));
    bind('bnc-clip-bitrate', (e)=>clipSet('bnc_clip_bitrate', Number(e.target.value)||1500000));
    bind('bnc-clip-after', (e)=>{ const v=Number(e.target.value); clipSet('bnc_clip_after', isNaN(v)?5:Math.max(0,Math.min(10,v))); });
    try{
      const byId={}; (lbClips||[]).forEach(r=>{ byId[r.id]=r; });
      body.querySelectorAll('video[data-clip-vid]').forEach(v=>{
        const r=byId[v.dataset.clipVid];
        if(r && r.blob){ try{ v.src=URL.createObjectURL(r.blob); }catch{} }
      });
      body.querySelectorAll('[data-clip-dl]').forEach(b=>{
        b.onclick=()=>{
          const r=byId[b.dataset.clipDl];
          if(!r || !r.blob) return;
          try{
            const a=document.createElement('a');
            a.href=URL.createObjectURL(r.blob);
            a.download=`buildnow-${r.trigger||'clip'}-${r.ts||Date.now()}.${clipExt(r.mime||(r.blob&&r.blob.type))}`;
            (document.body||document.documentElement).appendChild(a); a.click(); a.remove();
          }catch{}
        };
      });
      body.querySelectorAll('[data-clip-del]').forEach(b=>{
        b.onclick=()=>{ clipDelete(b.dataset.clipDel).then(()=>fetchLbClips()); };
      });
    }catch{}
  }
  function renderLbBody(){
    const body=document.getElementById('bnc-lb-body');
    if(!body) return;
    const esc=(v)=> String(v==null?'â€”':v).replace(/</g,'&lt;');    if(lbLoading){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">Loadingâ€¦</div>`; return; }
    if(!lbRows.length){ body.innerHTML=`<div style="color:#9ca3af;font-size:12px;text-align:center;padding:30px 0">No players yet â€” play a match first.</div>`; return; }
    const prMode=(typeof lbSort!=='undefined' && lbSort==='pr');
    const rows=lbRows.map(r=>{
      const name=r.game_username || r.discord_username || 'â€”';
      const kd=(r.kd==null)?'â€”':r.kd;
      const pt=prMode ? (r.tourney_pr==null?'â€”':r.tourney_pr) : ((r.points==null)?'â€”':Number(r.points).toLocaleString('en-US'));
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
    const esc=(v)=> String(v==null?'â€”':v).replace(/</g,'&lt;').replace(/"/g,'&quot;');
    const r=lbProfile;
    const name=r.game_username || r.discord_username || 'â€”';
    const meDid=(typeof myDiscordId==='function' ? myDiscordId() : null);
    const canAddFriend=!!(r.discord_id && meDid && String(r.discord_id)!==String(meDid));
    const skinId=(r.skin!=null && String(r.skin).trim()!=='' && String(r.skin).toLowerCase()!=='nan') ? r.skin : 'cct_0_default';
    const skinUrl=skinUrlDrawer(skinId);
    const ins=lbProfileIns;
    const box=(label,val,tip)=>`<div title="${esc(tip||label)}" style="background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px;padding:10px 12px;min-width:0"><div style="font-size:10px;font-weight:800;color:#6b7280;letter-spacing:.06em">${esc(label)}</div><div style="font-size:15px;font-weight:800;color:#fff;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${val}</div></div>`;
    const sess=(m)=>{
      if(!m) return 'â€”';
      const res=m.win?'W':(m.defeat?'L':'â€“');
      return `${m.kills||0}K/${m.deaths||0}D ${res} Â· ${esc(fmtGmDrawer(m.gm))}`;
    };
    const mate=ins&&ins.fav_teammate;
    const mateVal=!ins?'â€¦':(mate&&mate.player?`${esc(mate.player)} <span style="color:#6b7280;font-weight:400">(${mate.shared} shared)</span>`:'Not yet found');
    const styleVal=!ins?'â€¦':esc(ins.playstyle||'â€”');
    const styleTip=!ins?'':(PLAYSTYLE_TIPS[ins.playstyle]||'Playstyle from kills/match, K/D and win rate.');
    const boxes=[
      box('Points', r.points!=null?Number(r.points).toLocaleString('en-US'):'â€”'),
      box('Kills', esc(r.kills)),
      box('Deaths', esc(r.deaths)),
      box('K/D', esc(r.kd)),
      box('Wins', esc(r.wins)),
      box('Playtime', esc(fmtPlaytimeDrawer(r.playtime_global))),
      box('Last online', esc(fmtLastSeen(r.last_seen)), r.last_seen ? ('Last seen ' + r.last_seen) : 'No heartbeat yet'),
      box('Playstyle', styleVal, styleTip),
      box('Best session', !ins?'â€¦':esc(sess(ins.best_session))),
      box('Fav mode', !ins?'â€¦':esc(fmtGmDrawer(ins.fav_game_mode))),
      box('Best teammate', mateVal, 'Most shared team-mode matches. Needs duo/team history.'),
      box('Biggest fear', !ins?'â€¦':((ins.biggest_fear&&ins.biggest_fear.player)?`${esc(ins.biggest_fear.player)} (${ins.biggest_fear.deaths})`:'â€”')),
    ].join('');
    body.innerHTML=`<div style="display:flex;flex-direction:column;gap:10px;height:100%">`
      + `<div style="display:flex;align-items:center;gap:10px"><button id="bnc-prof-back" style="background:#1f2937;color:#e5e7eb;border:1px solid rgba(255,255,255,0.12);border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px;font-weight:700">â€¹ Back</button><b style="font-size:16px;color:#fff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0">${esc(name)}</b>${r.rank!=null?`<span style="font-size:11px;color:#6b7280">#${esc(r.rank)}</span>`:''}${canAddFriend?`<button id="bnc-prof-add" style="background:#fff;color:#000;border:0;border-radius:6px;padding:5px 12px;cursor:pointer;font-size:12px;font-weight:800;flex-shrink:0">Add friend</button>`:''}</div>`
      + `<div style="flex:1;display:flex;gap:10px;min-height:0;overflow:auto">`
      + `<div style="width:190px;flex-shrink:0;background:#0a0a0a;border:1px solid rgba(255,255,255,0.08);border-radius:12px;display:flex;align-items:center;justify-content:center;align-self:stretch;padding:12px">${skinUrl?`<img src="${skinUrl}" style="max-width:100%;max-height:100%;object-fit:contain;image-rendering:pixelated" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div style="display:none;font-size:40px">â–£</div>`:`<div style="font-size:40px">â–£</div>`}</div>`
      + `<div style="flex:1;display:grid;grid-template-columns:1fr 1fr;gap:8px;align-content:start;min-width:0">${lbProfileLoading?`<div style="grid-column:1/-1;color:#9ca3af;font-size:12px;text-align:center;padding:16px 0">Loading insightsâ€¦</div>`:boxes}</div>`
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
  const PLAYSTYLE_TIPS={
    Aggressor:'Drops into fights: 3+ kills per match with a strong K/D.',
    Anchor:'Holds ground: solid K/D and wins half or more of the matches.',
    Support:'Plays for the team: fewer kills but a steady win rate.',
    Wildcard:'Unpredictable mix â€” every lobby is different.',
  };

  // --- KEYBINDS / SETTINGS MENU (Ctrl+Shift+K) ---
  let keybindOverlay=null;
  function createKeybindOverlay(){
    if(document.getElementById('bnc-keys')) return;
    const ov=document.createElement('div');
    ov.id='bnc-keys';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483646', background:'rgba(0,0,0,0.54)', display:'none', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif'});
    ov.innerHTML = `<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:12px;padding:20px;min-width:360px;max-width:520px;color:#e5e7eb"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><b style="font-size:15px;color:#fff">BuildNow Companion - Shortcuts & Settings</b><button id="bnc-keys-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 8px;cursor:pointer">Close</button></div><div id="bnc-keys-list" style="display:flex;flex-direction:column;gap:6px;font-size:12px"></div><div style="display:flex;gap:8px;margin-top:12px"><button id="bnc-keys-open-drawer" style="flex:1;padding:7px 0;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:#171717;color:#e5e7eb;font-size:12px;font-weight:700;cursor:pointer">Open player info</button><button id="bnc-keys-open-ranked" style="flex:1;padding:7px 0;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:#171717;color:#e5e7eb;font-size:12px;font-weight:700;cursor:pointer">Open ranked panel</button><button id="bnc-keys-open-lb" style="flex:1;padding:7px 0;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:#171717;color:#e5e7eb;font-size:12px;font-weight:700;cursor:pointer">Open leaderboard</button></div><div style="margin-top:8px;font-size:11px;color:#9ca3af"><code>&#96;</code> - This menu (rebindable like any row; Ctrl+Shift+K still works)</div><div style="margin-top:10px;font-size:11px;opacity:0.7">Panels open with one key: <b>F2</b> player info Â· <b>F4</b> leaderboard Â· <b>F9</b> ranked. Mac: F-keys work as-is (on small MacBooks hold <b>Fn</b>). Combos with Ctrl/âŒ˜ also work via Change.</div><div style="margin-top:10px"><button id="bnc-keys-reset" style="width:100%;padding:7px 0;border-radius:8px;border:1px solid rgba(255,255,255,0.12);background:#171717;color:#e5e7eb;font-size:12px;font-weight:700;cursor:pointer">Reset all shortcuts to defaults</button></div><div id="bnc-dev-row" style="margin-top:14px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.08);font-size:11px;opacity:0.7">Settings: <label style="cursor:pointer"><input type="checkbox" id="bnc-dev-toggle"> Dev mode (feed)</label></div></div>`;
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
          showToast('Shortcuts reset: F2 player info Â· F4 leaderboard Â· F9 ranked', false);
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
    btn.textContent='Pressâ€¦';
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
      if(warn){ hint.textContent='âš  '+warn; return; }  // stay capturing
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
    if (isModalOrInputFocused() || isTypingKey()) return;
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

  // Ranked UI removed â€” ranked is not ready. (Host mode toggle removed too;
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
  // session â€” these counters let the dot + logs tell them apart. Never raise.
  let lastCaptureOkAt=0, _capFails=0, _capSeen=0, _lastCapWarn=0;
  function captureAgeMs(){ try{ return lastCaptureOkAt? Date.now()-lastCaptureOkAt : -1; }catch{ return -1; } }
  // Footer dot truth: Offline (no api) / Login (no session) / Online (capture
  // fresh <90s) / Idle (hook alive, no essential packets â€” normal lobby idle)
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
      if(w>=2 && w>_lastStreakWin) showToast(`ðŸ”¥ Win streak x${w}`, false);
      else if(d>=2 && d>_lastStreakDaily) showToast(`ðŸ“… Daily streak x${d}`, false);
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
    // ranked tracking (comp-copy): ONLY on MY join/create (226/227 out):
    // pID present -> track the code, pid-less (public matchmaking) ->
    // ranked mode OFF. Auth (230/231) never touches the leave guard
    // (mid-match re-auths must recover); MY leave op (254 out) always
    // hides ranked UI + sets the guard. Incoming 226/227
    // (server responses/echoes) never touch tracking. Incoming Leave
    // events (254 in = someone else left) only refresh the player list —
    // they must NOT hide my ranked UI.
    // Incoming Leave events (254 in = someone else left) only refresh the
    // player list â€” they must NOT hide my ranked UI.
    // Any Leave event (254 in = someone left queue/match) and any of MY
    // room changes -> prompt player-list refresh (server prunes on Leave).
    try{
      if((type===2||type===3)&&(code===230||code===231)){ schedulePlayersRefresh('auth'); try{ noteLocalRoom(false); }catch{} try{ _publicRoom=false; _compRoom=null; _compHudData=null; updateTourneyHud(); resetRtRoomLocal(); }catch{} }
      else if((type===2||type===3)&&code===254&&dir==='out'){ schedulePlayersRefresh('leave-op'); try{ noteLocalRoom(false); }catch{} try{ _publicRoom=false; _compRoom=null; _compHudData=null; updateTourneyHud(); resetRtRoomLocal(); }catch{} try{ disableRanked('leave-op'); }catch{} }
      else if(type===4&&code===254&&dir==='in') schedulePlayersRefresh('leave-ev');
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
      // responses â€” hook for the instant win toast + stats pull (no-op untracked).
      // (Ranked stG detection removed; stG still drives flush priority + clips below.)
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

    // Optimistic: detect Authenticate (opCode 230 = 0xE6) outgoing â†’ lobby.
    // First auth = login: set lobby state silently (never touch audio â€” it's
    // either already starting or will start via stats). Later auths = back to
    // lobby â†’ normal path, which unpauses if we flipped out of a match.
    if(dir==='out' && type===2 && code===230) {
      _authCount++;
      if(_authCount === 1) {
        _applyOptimisticCurScn('MainLobby', {silent:true});
      } else {
        _applyOptimisticCurScn('MainLobby');
      }
    }

    // Optimistic: detect killfeed (type=4, code=101) â†’ increment kills/deaths immediately
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
              try{ clipOnKill(killer, victim, isKill); }catch{}
              // True optimism: toast + delta NOW, no server roundtrip.
              // Kills only (knocks don't seal). Death is silent here â€”
              // the seal toast covers it.
              // (Ranked optimistic delta removed with detection.)
            }
          }
        } else {
          // No known verb: never stay silent â€” raw strings diagnose new formats.
          try{ console.log(`%c[BNC]%c KILL-RAW seq=${rec.seq} strings=${JSON.stringify(strings.slice(0,4))}`, 'color:#f59e0b;font-weight:900',''); }catch{}
        }
      } catch(e) {}
    }

    delete rec._isEssential; delete rec._type; delete rec._code;
    queue.push(rec);
    feedLog('capture', {type, code, dir, label:rec.label, seq:rec.seq, hex:rawHex.slice(0,12)+'..', queued:queue.length});
    // Priority lane: kills + match markers skip the batch timer â€” the
    // toast/seal path needs them now, not in 250ms. Everything else batches.
    try{
      const _isKill=(type===4 && code===101);
      const _isMarker=((type===4 && code===253) || ((type===2||type===3)&&(code===226||code===227))) && sniffStg(rawHex)!=null;
      try{ if(_isMarker && sniffStg(rawHex)===3) clipOnWin(); }catch{}
      if(_isKill || _isMarker){ flush(); return; }
    }catch{}
    scheduleFlush();
  }
  function ensureHex(rec){ if(rec.rawHex) return rec.rawHex; try{ rec.rawHex=hexOf(rec._bytes); delete rec._bytes; }catch{ rec.rawHex=''; } return rec.rawHex; }
  function scheduleFlush(){ if(flushTimer) return; if(queue.length>=BATCH_MAX) flush(); else flushTimer=setTimeout(flush, FLUSH_MS); }
  // Heartbeat: every 5s send an empty test-cap batch so the server session
  // stays warm (_cached_at slides, stats/presence stay fresh) through quiet
  // stretches (idle lobby, mobile players). Records carry NO rawHex, so the
  // server skips them before engine.feed AND the processor â€” zero effect on
  // kills/wins/versions/seals â€” and NO req_token, so no rotation is burned.
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
      apiPost('/api/capture', { sessionId: captureSessionId, records: [{hb:1, t:now}], _quiet:true, _noLiveness:true, _noToken:true, _noWipe:true }, (ok)=>{
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
  function startHeartbeat(){ try{ if(_hbTimer) return; _hbTimer=setInterval(sendHeartbeat, 5000); }catch{} }
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
    const payload={ sessionId: captureSessionId, records: batch };
    try{ _lastRealFlushAt=Date.now(); }catch{}
    flushInFlight=true;
    try{ _flushStartAt=Date.now(); }catch{}
    feedLog('flush', {count:batch.length, queued:queue.length});
    apiPost('/api/capture', payload, (ok, txt)=>{
      flushInFlight=false;
      if(ok){
        try{ lastCaptureOkAt=Date.now(); _capFails=0; }catch{}
        try{
          const j=JSON.parse(txt);
          if(j.new_req_token) { storeSession({new_req_token:j.new_req_token, new_counter:j.new_counter}); logInfo(`capture token rotated ${String(j.new_req_token).slice(0,4)}â€¦`); feedLog('ok', {rotated:j.new_req_token.slice(0,4)}); }
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
            // stats polls during play â€” drive ranked UI off them (throttled
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
          if(String(txt).toLowerCase().includes('session expired') && sessionId){
            // 2-day session expiry: drop session (login token kept) and
            // re-validate silently; the token prompt only appears on
            // explicit rejection inside silentRevalidate.
            logInfo('[AUTH] session expired - refreshing');
            try{ clearSessionOnly(); }catch{}
            try{ showToast('Session expired - refreshing...', false); }catch{}
            try{ silentRevalidate('expired'); }catch{}
          }
        } else {
          queue=batch.concat(queue).slice(0,BATCH_MAX*4);
        }
      }
      if(queue.length) flushTimer=setTimeout(flush, 40);
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
      Proto[hookKey+'_proto']=true; logSys(`prototype patches on ${w===window?'window':'unsafeWindow'} âœ“`);
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
        logOk(`HOOKED Photon WSS â†’ ${url}`);
        try{ ws.binaryType='arraybuffer'; }catch{}
        ws.addEventListener('open',()=>logOk(`WSS OPEN ${labelForUrl(url)}`));
        ws.addEventListener('close',(ev)=>logSys(`WSS CLOSE ${labelForUrl(url)} code=${ev.code}`));
        return ws;
      }
      HookedWS.prototype=OrigWS.prototype;
      HookedWS.CONNECTING=OrigWS.CONNECTING; HookedWS.OPEN=OrigWS.OPEN; HookedWS.CLOSING=OrigWS.CLOSING; HookedWS.CLOSED=OrigWS.CLOSED;
      HookedWS[hookKey]=true; w.WebSocket=HookedWS;
      logSys(`WebSocket hook installed on ${w===window?'window':'unsafeWindow'} âœ“`);
    }catch(e){ logErr(`hook failed: ${e.message}`); }
  }
  try{
    const api={
      get sessionId(){return captureSessionId;},
      get queueLen(){return queue.length;},
      flush:()=>flush(),
      clearToken:()=>{ cleanTokenStorage(); showToast('Token cleared',false); },
      get collect(){ return { enabled: collectMode, count: collectedPackets.length, packets: collectedPackets }; },
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
    logSys('ready - BuildNow Companion active (project-nectar-6n70.onrender.com) Â· COLLECT Ctrl+Shift+C/S/L/R');
  }catch{}
})();
