// ==UserScript==
// @name         Nectar Buildnow Companion
// @namespace    https://github.com/Nectar-Central
// @version      2.13.0
// @description  Nectar Buildnow Companion v2.13.0 — tracker plus loading-screen branding (all CrazyGames frames).
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
// @connect      project-nectar.onrender.com
// @connect      vercel.app
// @connect      *.vercel.app
// @connect      supabase.co
// @connect      *.supabase.co
// ==/UserScript==

(function () {
  'use strict';
  const BNC_VERSION = '2.13.0';
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

  // project-nectar.onrender.com is primary. Local dev override: GMSet('bnc_api_base','http://127.0.0.1:8000')
  const VERCEL_API = GMGet('bnc_api_base', GMGet('bn_api_base', null)) || null;
  const SERVER_API = 'https://project-nectar.onrender.com';
  const LOCAL_API  = 'http://127.0.0.1:8000';
  function resolveApiBase(){
    if (VERCEL_API && String(VERCEL_API).startsWith('http')) return String(VERCEL_API).replace(/\/$/,'');
    // allow localhost override via ?api= local or GM value
    try { if (location.search.includes('api=local')) return LOCAL_API; } catch {}
    return SERVER_API;
  }
  function cleanTokenStorage(){
    ['bnc_token','bn_token','bnc_guest','bn_guest','bnc_discord_username','bn_discord_username','bnc_discord_id','bn_discord_id','bnc_first_seen','bn_first_seen','bnc_game_username','bn_game_username','bnc_game_id','bn_game_id','bnc_welcomed','bn_welcomed','bnc_session_gm','bn_session_gm','bnc_session_id','bn_session_id','bnc_req_secret','bn_req_secret','bnc_req_token','bn_req_token','bnc_req_counter','bn_req_counter'].forEach(GMDel);
    loginToken=null; discordUsername=null; hasWelcomed=false;
    sessionId=null; currentReqToken=null; reqCounter=0; discordId=null;
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
    if(j.session_id) { loginToken=null; GMDel('bnc_token'); GMDel('bn_token'); GMDel('bnc_guest'); GMDel('bn_guest'); }
    GMDel('bnc_req_secret'); GMDel('bn_req_secret');
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
  // NOT on the top page. Let the rebrand run in every frame — it's idempotent.
  // Only the full tracker is gated to the game frame.
  try{ bncRebrandBoot(); bncRebrandOnce(); }catch(e){}

  // ---- Only the full tracker runs inside the game frame ----
  if (!IS_GAME_FRAME) return;

  // session restore - login required, no guest capture
  if(sessionId && currentReqToken){
    logInfoEarly(`session restored ${String(sessionId).slice(0,8)}… counter=${reqCounter}`);
  } else if(loginToken){
    setTimeout(()=> apiPost('/api/validate-token', {token: loginToken}, (ok, txt)=>{
      if(!ok){
        let reason=''; let scope='default'; try{ const _p=JSON.parse(txt||'{}'); reason=_p.error||''; scope=_p.lock_scope||(_p.is_admin_locked?'admin':'default'); }catch{}
        if(String(reason).toLowerCase().includes('locked')){
          showLockedPanel(reason || 'Account locked', scope);
          return;
        }
        cleanTokenStorage(); hasWelcomed=false; setTimeout(()=>{ showTokenPrompt(); showToast('Session expired - enter new token', true); }, 800);
      }
      else { try{ const j=JSON.parse(txt); if(j.discord_username) { discordUsername=j.discord_username; GMSet('bnc_discord_username', discordUsername); } if(j.game_username) GMSet('bnc_game_username', j.game_username); if(j.session_id) storeSession(j); }catch{} }
    }), 600);
  }

  // queue: one at a time to avoid req_token reuse
  let apiQueue = [];
  let apiBusy = false;
  function apiPost(path, body, cb){
    const clean={...body}; delete clean._retried;
    apiQueue.push({path, body: clean, cb, retried: !!body._retried});
    processApiQueue();
  }
  function processApiQueue(){
    if(apiBusy || !apiQueue.length) return;
    apiBusy = true;
    const {path, body: origBody, cb, retried} = apiQueue.shift();
    let body = {...origBody};
    if(path!=='/api/validate-token' && sessionId && currentReqToken){
      if(path==='/api/capture'){
        body = { ...body, authSessionId: sessionId, discord_id: discordId, req_token: currentReqToken };
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
                apiQueue.unshift({path, body: {...origBody, _retried:true}, cb, retried:true});
                apiBusy = false; setTimeout(processApiQueue, 30); return;
              }
            }
            if(j.error && String(j.error).toLowerCase().includes('invalid')){
              if(j.current){ currentReqToken=j.current; GMSet('bnc_req_token', currentReqToken); logInfo(`mismatch sync token → ${String(currentReqToken).slice(0,4)}…`); if(!retried){ logInfo(`mismatch retry ${path}`); apiQueue.unshift({path, body: {...origBody, _retried:true}, cb, retried:true}); apiBusy=false; setTimeout(processApiQueue,30); return; } }
              else { logErr(`req_token rejected - clearing session`); cleanTokenStorage(); }
            }
          }catch{}
        }
        try{ cb(ok, txt); }catch{}
        apiBusy = false; setTimeout(processApiQueue, 10);
      };
      const wrapFail = (msg)=>{ logErr(msg); try{ cb(false, msg); }catch{} apiBusy=false; setTimeout(processApiQueue,20); };
      if(useGM && hasGM){
        try{
          GM({ method:'POST', url:u, headers:{'Content-Type':'application/json'}, data: JSON.stringify(body), timeout:15000,
            onload: r=> { const ok=r.status>=200&&r.status<300; const txt=r.responseText||''; const isZone=txt.includes('"zone"'); (ok?logOk:logErr)(`API ${ok?'OK':'ERR'} ${u} → ${r.status} ${String(txt).slice(0, isZone?1200:600)}`); if(isZone){ try{ const j=JSON.parse(txt); const zps=(j.packets||[]).filter(p=> (p.packet||p.packet_type)==='zone'); if(zps.length){ logGroup(`API zone batch ${zps.length} packets`, ()=>{ zps.forEach(zp=> console.log('zone packet', zp, 'decrypted', zp.decrypted_hex||zp.raw)); }); } }catch{} } wrapOk(ok, r.responseText); },
            onerror: (e)=>{ wrapFail(`API GM_xhr error → ${u} ${e&&e.error||''}`); },
            ontimeout: ()=>{ wrapFail(`API timeout (15s) → ${u}`); }});
          return true;
        }catch(e){ wrapFail(`API GM throw ${e.message}`); return false; }
      }
      try{
        fetch(u,{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(r=>r.text().then(t=>{ const ok=r.ok; const isZone=t.includes('"zone"'); (ok?logOk:logErr)(`API fetch ${ok?'OK':'ERR'} ${u} → ${r.status} ${t.slice(0, isZone?1200:600)}`); if(isZone){ try{ const j=JSON.parse(t); const zps=(j.packets||[]).filter(p=> (p.packet||p.packet_type)==='zone'); if(zps.length){ logGroup(`API zone batch ${zps.length} packets`, ()=>{ zps.forEach(zp=> console.log('zone packet', zp, 'decrypted', zp.decrypted_hex||zp.raw)); }); } }catch{} } wrapOk(ok,t); }).catch(()=>wrapFail(`fetch catch ${u}`))).catch((e)=>{ wrapFail(`API fetch fail ${u} ${e.message}`); });
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
    inMatch: false,      // derived: true if curScn != MainLobby
    lastUpdateAt: 0,     // timestamp of last optimistic update
  };

  function _applyOptimisticCurScn(curScn, opts) {
    if (!curScn || curScn === _optimisticState.curScn) return;
    const wasInMatch = _optimisticState.inMatch;
    _optimisticState.curScn = curScn;
    _optimisticState.inMatch = curScn !== 'MainLobby';
    _optimisticState.lastUpdateAt = Date.now();
    logSys(`[OPTIMISTIC] curScn=${curScn} inMatch=${_optimisticState.inMatch}`);
    const silent = opts && opts.silent;
    // Audio rule: pause ONLY on scene flip (lobby<->match). Same-side scene
    // changes and silent sets never touch audio (prevents stop+restart cycles).
    if (!silent && wasInMatch !== _optimisticState.inMatch) {
      try { updateLobbyMusic(); } catch {}
    }
    // fast drawer update
    if (wasInMatch !== _optimisticState.inMatch) {
      try { renderInfoPanel(); } catch {}
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
  // once optimistic is stale (>10s — server trails live packets ~2-5s), which heals
  // stuck flags even when idle (no capture batches flowing). Returns true on flip.
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
      const optimisticFresh = (Date.now() - (_optimisticState.lastUpdateAt || 0)) < 10000;
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

  function _applyOptimisticKillfeed(killer, victim) {
    const myName = GMGet('bnc_game_username', '') || '';
    if (!myName) return;
    const myNorm = _normName(myName);
    const killerNorm = _normName(killer);
    const victimNorm = _normName(victim);
    if (!statsCache) return;
    if (myNorm && killerNorm && myNorm === killerNorm) {
      statsCache.kills = (statsCache.kills || 0) + 1;
      logSys(`[OPTIMISTIC] KILL +1 → ${statsCache.kills} victim=${victim}`);
      try { renderInfoPanel(); } catch {}
    } else if (myNorm && victimNorm && myNorm === victimNorm) {
      statsCache.deaths = (statsCache.deaths || 0) + 1;
      logSys(`[OPTIMISTIC] DEATH +1 → ${statsCache.deaths} killer=${killer}`);
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
  document.addEventListener('keydown', (e)=>{
    if (isModalOrInputFocused()) return;
    if(modKey(e) && e.shiftKey){
      const k=(e.key||'').toUpperCase();
      if(k==='C'){ e.preventDefault(); e.stopPropagation(); toggleCollectMode(); }
      else if(k==='S'){ e.preventDefault(); e.stopPropagation(); downloadCollected(); }
      else if(k==='L'){ e.preventDefault(); e.stopPropagation(); document.getElementById('bnc-file-input')?.click(); }
      else if(k==='R'){ e.preventDefault(); e.stopPropagation(); replayCollected(); }
    }
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
  feedBar.innerHTML = `<span style="opacity:0.5">BNC feed - zone/stG/alv full</span><button id="bnc-feed-pause" style="background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:4px;padding:0 6px;cursor:pointer">${feedPaused?'▶ Resume':'⏸ Pause'}</button><button id="bnc-feed-expand" style="background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:4px;padding:0 6px;cursor:pointer">⤢ Expand</button><span style="opacity:0.5">Ctrl/Cmd+Shift+F toggle · Ctrl/Cmd+Shift+P pause · hover to scroll · drag top edge to resize</span>`;
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
  document.addEventListener('keydown', (e)=>{
    if (isModalOrInputFocused()) return;
    const k=(e.key||'').toUpperCase();
    if(modKey(e) && e.shiftKey && k==='F'){
      e.preventDefault(); e.stopPropagation();
      feedEnabled = !feedEnabled;
      GMSet('bnc_feed_enabled', feedEnabled);
      feedWrap.style.display = feedEnabled ? 'block' : 'none';
      showToast(feedEnabled ? 'Feed ON' : 'Feed OFF', false);
    } else if(modKey(e) && e.shiftKey && k==='P'){
      e.preventDefault(); e.stopPropagation();
      feedPaused = !feedPaused;
      GMSet('bnc_feed_paused', feedPaused);
      const btn=document.getElementById('bnc-feed-pause');
      if(btn) btn.textContent = feedPaused ? '▶ Resume' : '⏸ Pause';
      feedLog('info', {msg: feedPaused ? 'paused (scrollable)' : 'resumed'});
      showToast(feedPaused ? 'Feed paused' : 'Feed resumed', false);
    }
  }, true);
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
  function skinUrlDrawer(id){ if(!id || id==='-' ) return null; return `https://buildnow-api.netlify.app/skins/${encodeURIComponent(String(id).trim())}.png`; }
  // keybind for drawer - customizable via button in drawer
  function getDrawerKeybind(){
    try{
      const raw=GMGet('bnc_drawer_keybind', null);
      if(raw && typeof raw==='object' && raw.key) return raw;
      if(typeof raw==='string' && raw){
        try{ const j=JSON.parse(raw); if(j && j.key) return j; }catch{}
        // legacy string like "Ctrl+Shift+I" (or "Cmd+Shift+I" on Mac)
        const parts=String(raw).split('+');
        const kb={ctrl:false,shift:false,alt:false,meta:false,key:'I'};
        for(const p of parts){ const l=p.trim().toLowerCase(); if(l==='ctrl'||l==='control') kb.ctrl=true; else if(l==='cmd'||l==='meta'||l==='command') kb.meta=true; else if(l==='shift') kb.shift=true; else if(l==='alt') kb.alt=true; else if(p.trim()) kb.key=p.trim().toUpperCase(); }
        return kb;
      }
    }catch{}
    return {ctrl:true, shift:true, alt:false, key:'I'};
  }
  function formatKeybind(kb){ const a=[]; if(kb.meta) a.push('Cmd'); if(kb.ctrl) a.push('Ctrl'); if(kb.shift) a.push('Shift'); if(kb.alt) a.push('Alt'); a.push(String(kb.key||'').toUpperCase()); return a.join('+'); }
  function isDrawerKeybind(e){
    const kb=getDrawerKeybind();
    const k=(e.key||'').toUpperCase();
    // normalize: for letter keys compare uppercase, for others compare as is
    const want=k===String(kb.key).toUpperCase();
    return want && modKey(e)===!!(kb.ctrl||kb.meta) && !!e.shiftKey===!!kb.shift && !!e.altKey===!!kb.alt;
  }
  let _keybindCapturing=false;
  let apiOnline=false;

  let infoPanel=null, infoUpdateTimer=null, statsCache=null, statsPollTimer=null, drawerBackdrop=null;
  // Drawer always starts closed on page load (first open) — never auto-open from storage.
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
  let playersCache=null, playersPollTimer=null, playersLoading=false;
  let lobbyAudio=null;
  let bgLobbyPollTimer=null;
  let _audioPlaying=false;
  let _audioPlayPending=false; // play() in flight (buffering) — blocks duplicate play() calls
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
      try{ const resume=()=>{ logInfo('[AUDIO] click resume try'); try{ const rs = statsCache && statsCache.session; const rsLobby = !_optimisticState.inMatch && (!rs || String(rs.state||'').toLowerCase()!=='match'); if(lobbyAudio && !_audioPlaying && !_audioPlayPending && rsLobby && lobbyAudio.paused && lobbyAudio.src) { _audioPlayPending=true; lobbyAudio.play().then(()=> logOk('[AUDIO] resume playing')).catch(e=> { _audioPlayPending=false; logErr(`[AUDIO] resume blocked ${e.message||e}`); }); } }catch(e){ logErr(`[AUDIO] resume err ${e.message}`);} }; document.addEventListener('click', resume, {once:true}); }catch{}
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
        <button id="bnc-drawer-close" style="width:28px;height:28px;border:1px solid rgba(255,255,255,0.12);background:#1a1a1a;color:#9ca3af;border-radius:8px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:14px">✕</button>
      </div>
      <div id="bnc-drawer-body" style="flex:1;overflow-y:auto;overflow-x:hidden;padding:8px 20px 16px 20px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;scrollbar-width:thin">
        <div style="opacity:0.7;font-size:12px">Loading…</div>
      </div>
      <div id="bnc-drawer-footer" style="padding:10px 14px;border-top:1px solid rgba(255,255,255,0.06);display:flex;justify-content:space-between;align-items:center;flex-shrink:0;font-size:11px;color:#6b7280">
        <span id="bnc-drawer-version">v${BNC_VERSION}</span>
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
          // in flight, and we're in lobby (optimistic AND server — server trails, so both).
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
    const base=resolveApiBase();
    const url=base + '/api/players?session_id=' + encodeURIComponent(sessionId);
    const GM = typeof GM_xmlhttpRequest!=='undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
    const onDone=(ok, txt)=>{
      playersLoading=false;
      if(ok){
        try{
          const j=JSON.parse(txt);
          playersCache=j;
          if(drawerTab==='players') renderInfoPanel();
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
  // centered drawer - skin top, username, discord - game_id, state - gm, separator, stats, footer, keybind button + tabs
  function renderInfoPanel(){
    if(!infoPanel) createInfoPanel();
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
    const wins = sc? sc.wins : null;
    const defeats = sc? sc.defeats : null;
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
      ? `<img src="${skinUrl}" alt="${String(skin).slice(0,24)}" style="width:112px;height:112px;object-fit:contain;background:radial-gradient(140px 90px at 50% 20%, rgba(255,255,255,0.06), transparent 70%), #111;border:1px solid rgba(255,255,255,0.08);border-radius:16px;image-rendering:pixelated;flex-shrink:0" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'" /><div style="display:none;width:112px;height:112px;align-items:center;justify-content:center;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:16px;font-size:32px;flex-shrink:0">▣</div>`
      : `<div style="width:112px;height:112px;display:flex;align-items:center;justify-content:center;background:#111;border:1px solid rgba(255,255,255,0.08);border-radius:16px;font-size:32px;flex-shrink:0">▣</div>`;
    const kbLabel = formatKeybind(getDrawerKeybind());
    const row = (label,val)=> `<div style="display:flex;justify-content:space-between;align-items:center;width:100%;max-width:240px;font-size:13px"><span style="color:#9ca3af">${label}</span><span style="font-weight:700;color:#fff">${val==null? '-' : val}</span></div>`;
    const hasGm = gmLive && gmLive !== '-' && gmLive !== '-' && gmLive.toLowerCase() !== 'unknown' && gmLive.toLowerCase() !== 'lobby';
    const stateGmLine = hasGm && stateLabel.toLowerCase() !== gmLive.toLowerCase() ? `${stateLabel} - ${gmLive}` : stateLabel;
    const smallRow = (label,val)=> `<div style="display:flex;justify-content:space-between;align-items:center;width:100%;max-width:240px;font-size:11px"><span style="color:#9ca3af">${label}</span><span style="font-weight:600;color:#fff">${val==null? '-' : val}</span></div>`;
    const perGmRow = hasGm && perGmCurrent!=null ? smallRow('Playtime ('+gmLive+')', fmtPlaytimeDrawer(perGmCurrent)) : '';
    // Empty account (no game username and no stats yet): placeholder instead of blank rows
    const _gmUname = GMGet('bnc_game_username', null);
    const noStats = (kills==null) && (!_gmUname || _gmUname==='-');
    const emptyStatsHtml = `<div style="display:flex;flex-direction:column;gap:8px;align-items:center;width:100%;max-width:240px;padding:18px 14px;background:#171717;border:1px dashed rgba(255,255,255,0.16);border-radius:12px;text-align:center"><div style="font-size:26px">📊</div><div style="font-size:14px;font-weight:800;color:#fff">No stats yet</div><div style="font-size:12px;color:#9ca3af;line-height:1.5">Play 1-2 matches to get your account ready.<br>Kills, deaths, wins and skins will show up here.</div></div>`;
    const statsRowsHtml = noStats ? emptyStatsHtml : `
          ${row('Kills', kills)}
          ${row('Deaths', deaths)}
          ${row('Wins', wins)}
          ${row('Defeats', defeats)}
          ${row('Playtime', pGlobal!=null? fmtPlaytimeDrawer(pGlobal):null)}
          ${perGmRow}`;
    const isLobbyTab = stateLabel === 'In Lobby';
    if(isLobbyTab){
      // clean up stale player list when back in lobby (fixes not refreshing)
      if(playersCache){ playersCache=null; }
      if(drawerTab==='players'){
        drawerTab='stats';
        try{ GMSet('bnc_drawer_tab', drawerTab); }catch{}
        stopPlayersPoll();
      }
    }
    // tab bar - hide Players when in lobby per request
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
          const nameLine = `<div style="font-size:13px;font-weight:700;color:#fff;word-break:break-word">${titleName}${lateBadge}</div>`;
          const gidFull = pl.game_id!=null ? String(pl.game_id) : 'NaN';
          const discordVal = pl.discord_username!=null ? String(pl.discord_username) : 'NaN';
          const detailLine = `Discord: ${toNaN(discordVal)} · Game ID: ${toNaN(gidFull)}`;
          const detailDiv = `<div style="font-size:11px;color:#9ca3af;word-break:break-word;white-space:normal;line-height:1.35">${detailLine}</div>`;
          return `<div style="display:flex;gap:10px;align-items:center;width:100%;max-width:300px;padding:8px 10px;background:#171717;border:1px solid rgba(255,255,255,0.06);border-radius:10px"><div style="flex-shrink:0">${img}</div><div style="flex:1;min-width:0;text-align:left;display:flex;flex-direction:column;gap:2px">${nameLine}${detailDiv}</div></div>`;
        }).join('');
        playersHtml = header + `<div style="display:flex;flex-direction:column;gap:6px;align-items:center;width:100%;max-height:46vh;overflow-y:auto;padding-right:4px">${rows}</div>`;
      }
    }
    body.innerHTML = tabBar + (drawerTab==='stats' ? statsHtml : `<div style="display:flex;flex-direction:column;align-items:center;width:100%;padding:8px 0">${playersHtml}</div>`);
    // tab handlers
    const tsBtn=document.getElementById('bnc-tab-stats');
    const tpBtn=document.getElementById('bnc-tab-players');
    if(tsBtn) tsBtn.onclick=()=> switchDrawerTab('stats');
    if(tpBtn) tpBtn.onclick=()=> switchDrawerTab('players');
    // footer api dot
    const dot=document.getElementById('bnc-drawer-dot');
    const apiTxt=document.getElementById('bnc-drawer-api-text');
    if(dot) dot.style.background = apiOnline? '#10b981' : '#6b7280';
    if(apiTxt) apiTxt.textContent = apiOnline? 'Online' : 'Offline';
    // keybind button handler (only in stats tab)
    const kbBtn=document.getElementById('bnc-keybind-set');
    const kbHint=document.getElementById('bnc-keybind-hint');
    if(kbBtn){
      kbBtn.onclick=()=>{
        if(_keybindCapturing) return;
        _keybindCapturing=true;
        kbBtn.textContent='Press new keybind…';
        if(kbHint) kbHint.textContent='Press any combo (e.g. Ctrl+Shift+K, or Cmd+Shift+K on Mac). Esc to cancel.';
        const handler=(e)=>{
          if(e.key==='Escape'){ _keybindCapturing=false; kbBtn.textContent='Set open keybind: '+formatKeybind(getDrawerKeybind()); if(kbHint) kbHint.textContent='Cancelled.'; document.removeEventListener('keydown', handler, true); return; }
          if(['Control','Shift','Alt','Meta'].includes(e.key)) return;
          e.preventDefault(); e.stopPropagation();
          const nb={ctrl:!!e.ctrlKey, meta:!!e.metaKey, shift:!!e.shiftKey, alt:!!e.altKey, key:String(e.key).toUpperCase()};
          if(!nb.key) return;
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
    const volSlider=document.getElementById('bnc-volume-slider');
    const volLabel=document.getElementById('bnc-volume-label');
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
        logInfo(`[REBRAND] loading branding ${bncRebrandEl.checked?'on — applying now':'off'}`);
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
    const base=resolveApiBase();
    const qs = sessionId ? `session_id=${encodeURIComponent(sessionId)}` : `discord_id=${encodeURIComponent(discordId)}`;
    const url = base + '/api/stats?' + qs;
    const GM = typeof GM_xmlhttpRequest!=='undefined' ? GM_xmlhttpRequest : (typeof unsafeWindow!=='undefined' && unsafeWindow.GM_xmlhttpRequest) || null;
    const doFetch = (useGM)=>{
      if(useGM && GM){
        try{
          GM({method:'GET', url, headers:{'Content-Type':'application/json'}, timeout:8000,
            onload: r=>{ if(r.status>=200&&r.status<300){ try{ const j=JSON.parse(r.responseText); statsCache=j; logInfo(`[AUDIO] stats fetched lobby_music=${String(j.lobby_music_url||'').slice(-30)} state=${j.session? j.session.state:'none'}`); try{ feedLog('info', {audio:'stats', hasMusic:!!j.lobby_music_url, state:j.session? j.session.state:null}); }catch{} apiOnline=true; if(j.skin) GMSet('bnc_skin', j.skin); if(j.game_username) GMSet('bnc_game_username', j.game_username); if(j.game_id) GMSet('bnc_game_id', j.game_id); if(j.discord_username) { discordUsername=j.discord_username; GMSet('bnc_discord_username', discordUsername); } try{ _reconcileServerState(j.session); }catch{} renderInfoPanel(); }catch(e){ logErr(`[AUDIO] stats parse err ${e.message}`); apiOnline=false; if(forceRender) renderInfoPanel(); } } else { apiOnline=false; if(forceRender) renderInfoPanel(); } try{ const d=document.getElementById('bnc-drawer-dot'); const t=document.getElementById('bnc-drawer-api-text'); if(d) d.style.background=apiOnline?'#10b981':'#6b7280'; if(t) t.textContent=apiOnline?'Online':'Offline'; }catch{} },
            onerror: ()=>{ apiOnline=false; if(forceRender) renderInfoPanel(); try{ const d=document.getElementById('bnc-drawer-dot'); const t=document.getElementById('bnc-drawer-api-text'); if(d) d.style.background='#6b7280'; if(t) t.textContent='Offline'; }catch{} },
            ontimeout: ()=>{ apiOnline=false; if(forceRender) renderInfoPanel(); try{ const d=document.getElementById('bnc-drawer-dot'); const t=document.getElementById('bnc-drawer-api-text'); if(d) d.style.background='#6b7280'; if(t) t.textContent='Offline'; }catch{} }});
          return;
        }catch{}
      }
      fetch(url, {method:'GET'}).then(r=>r.text().then(t=>{ if(r.ok){ try{ const j=JSON.parse(t); statsCache=j; logInfo(`[AUDIO] stats fetched (fetch) lobby_music=${String(j.lobby_music_url||'').slice(-30)} state=${j.session? j.session.state:'none'}`); try{ feedLog('info', {audio:'stats-fetch', hasMusic:!!j.lobby_music_url, state:j.session? j.session.state:null}); }catch{} try{ _reconcileServerState(j.session); }catch{} apiOnline=true; if(j.skin) GMSet('bnc_skin', j.skin); if(j.game_username) GMSet('bnc_game_username', j.game_username); if(j.game_id) GMSet('bnc_game_id', j.game_id); if(j.discord_username) { discordUsername=j.discord_username; GMSet('bnc_discord_username', discordUsername); } renderInfoPanel(); }catch(e){ logErr(`[AUDIO] stats parse err (fetch) ${e.message}`); apiOnline=false; if(forceRender) renderInfoPanel(); } } else { apiOnline=false; if(forceRender) renderInfoPanel(); } try{ const d=document.getElementById('bnc-drawer-dot'); const tt=document.getElementById('bnc-drawer-api-text'); if(d) d.style.background=apiOnline?'#10b981':'#6b7280'; if(tt) tt.textContent=apiOnline?'Online':'Offline'; }catch{} }).catch(()=>{ apiOnline=false; if(forceRender) renderInfoPanel(); try{ const d=document.getElementById('bnc-drawer-dot'); const tt=document.getElementById('bnc-drawer-api-text'); if(d) d.style.background='#6b7280'; if(tt) tt.textContent='Offline'; }catch{} })).catch(()=>{ apiOnline=false; if(forceRender) renderInfoPanel(); try{ const d=document.getElementById('bnc-drawer-dot'); const tt=document.getElementById('bnc-drawer-api-text'); if(d) d.style.background='#6b7280'; if(tt) tt.textContent='Offline'; }catch{} });
    };
    doFetch(true);
  }
  function startStatsPoll(){
    stopStatsPoll();
    fetchStats(false);
    // Poll every 30s when in-match (we have optimistic state), 15s in lobby
    const interval = _optimisticState.inMatch ? 30000 : 15000;
    statsPollTimer=setInterval(()=> fetchStats(false), interval);
  }
  function stopStatsPoll(){ if(statsPollTimer){ clearInterval(statsPollTimer); statsPollTimer=null; } }
  // auto-create drawer shell immediately so handle is visible + hidden audio tag that auto plays
  setTimeout(()=>{ ensureLobbyAudio(); createInfoPanel(); renderInfoPanel(); if(drawerOpen){ startStatsPoll(); if(drawerTab==='players') startPlayersPoll(); } if(sessionId) startBgLobbyPoll(); }, 700);
  // refresh when session established after login
  const _origStoreSession = storeSession;
  // wrap storeSession to trigger render
  // monkey-patch via interval check on sessionId change
  let _lastSid = sessionId;
  setInterval(()=>{ if(sessionId!==_lastSid){ _lastSid=sessionId; renderInfoPanel(); if(drawerOpen){ fetchStats(false); if(drawerTab==='players') fetchPlayers(false); } else if(sessionId){ startBgLobbyPoll(); fetchStats(false); } } }, 1000);

  // --- KEYBINDS / SETTINGS MENU (Ctrl+Shift+K) ---
  let keybindOverlay=null;
  function createKeybindOverlay(){
    if(document.getElementById('bnc-keys')) return;
    const ov=document.createElement('div');
    ov.id='bnc-keys';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483646', background:'rgba(0,0,0,0.54)', display:'none', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif'});
    ov.innerHTML = `<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:12px;padding:20px;min-width:360px;max-width:520px;color:#e5e7eb"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><b style="font-size:15px;color:#fff">BuildNow Companion - Shortcuts & Settings</b><button id="bnc-keys-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 8px;cursor:pointer">Close</button></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;line-height:1.6"><div><code>Ctrl+Shift+C</code> - Collect toggle<br><code>Ctrl+Shift+S</code> - Save collected<br><code>Ctrl+Shift+L</code> - Load file<br><code>Ctrl+Shift+R</code> - Replay</div><div><code>Ctrl+Shift+F</code> - Feed (dev only)<br><code>Ctrl+Shift+P</code> - Pause feed<br><code id="bnc-keys-drawer-label">Ctrl+Shift+I</code> - Player info<br><code>Ctrl+Shift+K</code> - This menu</div></div><div style="margin-top:10px;font-size:11px;opacity:0.7">Mac tip: use ⌘ instead of Ctrl — Cmd+Shift+&lt;key&gt; works the same.</div><div style="margin-top:14px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.08);font-size:11px;opacity:0.7">Settings: <label style="cursor:pointer"><input type="checkbox" id="bnc-dev-toggle"> Dev mode (feed)</label></div></div>`;
    (document.body||document.documentElement).appendChild(ov);
    ov.addEventListener('click', (e)=>{ if(e.target===ov) ov.style.display='none'; });
    ov.querySelector('#bnc-keys-close').onclick=()=> ov.style.display='none';
    const devT = ov.querySelector('#bnc-dev-toggle');
    devT.checked = !!GMGet('bnc_dev_mode', false);
    devT.onchange=()=>{ GMSet('bnc_dev_mode', devT.checked); showToast(devT.checked? 'Dev mode ON':'Dev mode OFF', false); if(devT.checked){ feedEnabled=true; feedWrap.style.display='block'; } else feedWrap.style.display='none'; };
    try{ const dl=ov.querySelector('#bnc-keys-drawer-label'); if(dl) dl.textContent=formatKeybind(getDrawerKeybind()); }catch{}
    keybindOverlay=ov;
  }
  function toggleKeybinds(){ if(!keybindOverlay) createKeybindOverlay(); const isHidden=keybindOverlay.style.display==='none' || !keybindOverlay.style.display; keybindOverlay.style.display = isHidden ? 'flex' : 'none'; if(isHidden){ try{ const dl=keybindOverlay.querySelector('#bnc-keys-drawer-label'); if(dl) dl.textContent=formatKeybind(getDrawerKeybind()); }catch{} } }
  // Hotkeys: drawer (customizable) + Ctrl+Shift+K keybinds (guard modal focus)
  document.addEventListener('keydown', (e)=>{
    if(_keybindCapturing) return;
    if (isModalOrInputFocused()) return;
    if(isDrawerKeybind(e)){ e.preventDefault(); e.stopPropagation(); toggleInfoPanel(); return; }
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
  let seq=0, queue=[], flushTimer=null, flushInFlight=false;
  let lastCaptureBlockedLog=0;

  function enqueue(dir, bytes, url){
    if(isLocked) return;
    if(!bytes||!bytes.length) return;
    if(!sessionId || !currentReqToken){
      if(Date.now()-lastCaptureBlockedLog>5000){ lastCaptureBlockedLog=Date.now(); logInfo('capture skipped - no session (login required)'); }
      return;
    }
    const isEssential = isEssentialBytes(bytes);
    const rawHex = hexOf(bytes);
    const type = bytes[1], code = bytes[2];
    const rec = { seq:++seq, t:Date.now(), dir, url: shortUrl(url), fullUrl: String(url).slice(0,256), label: labelForUrl(url), rawHex, _isEssential: isEssential, _type:type, _code:code };
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
          }
        }
      } catch(e) {}
    }

    // Optimistic: detect Authenticate (opCode 230 = 0xE6) outgoing → lobby.
    // First auth = login: set lobby state silently (never touch audio — it's
    // either already starting or will start via stats). Later auths = back to
    // lobby → normal path, which unpauses if we flipped out of a match.
    if(dir==='out' && type===2 && code===230) {
      _authCount++;
      if(_authCount === 1) {
        _applyOptimisticCurScn('MainLobby', {silent:true});
      } else {
        _applyOptimisticCurScn('MainLobby');
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
            if(hex[i] === '07') { // String type
              i++;
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
              _applyOptimisticKillfeed(killer, victim);
            }
          }
        }
      } catch(e) {}
    }

    delete rec._isEssential; delete rec._type; delete rec._code;
    queue.push(rec);
    feedLog('capture', {type, code, dir, label:rec.label, seq:rec.seq, hex:rawHex.slice(0,12)+'..', queued:queue.length});
    scheduleFlush();
  }
  function ensureHex(rec){ if(rec.rawHex) return rec.rawHex; try{ rec.rawHex=hexOf(rec._bytes); delete rec._bytes; }catch{ rec.rawHex=''; } return rec.rawHex; }
  function scheduleFlush(){ if(flushTimer) return; if(queue.length>=BATCH_MAX) flush(); else flushTimer=setTimeout(flush, FLUSH_MS); }
  function flush(){
    if(isLocked) { flushTimer=null; queue=[]; return; }
    if(!queue.length){ flushTimer=null; return; }
    if(!sessionId || !currentReqToken){ flushTimer=null; queue=[]; return; }
    if(flushInFlight){ if(!flushTimer) flushTimer=setTimeout(flush, FLUSH_MS); return; }
    flushTimer=null;
    const batch=queue.splice(0,BATCH_MAX);
    for(const r of batch) ensureHex(r);
    const payload={ sessionId: captureSessionId, records: batch };
    flushInFlight=true;
    feedLog('flush', {count:batch.length, queued:queue.length});
    apiPost('/api/capture', payload, (ok, txt)=>{
      flushInFlight=false;
      if(ok){
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
            if(_reconcileServerState(j.session)) {
              try { renderInfoPanel(); } catch {}
            }
          }
        }catch(e){ feedLog('decrypted', {err:e.message}); }
        feedLog('sent', {count:batch.length, ok:true});
      } else {
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
            // 2-day session expiry: drop session (keep login token), re-validate for a
            // fresh session; fall back to the token prompt when revalidation fails.
            logInfo('[AUTH] session expired - refreshing');
            sessionId=null; currentReqToken=null; reqCounter=0;
            try{ GMDel('bnc_session_id'); GMDel('bn_session_id'); GMDel('bnc_req_token'); GMDel('bn_req_token'); GMDel('bnc_req_counter'); GMDel('bn_req_counter'); }catch{}
            if(loginToken){
              showToast('Session expired - refreshing...', false);
              apiPost('/api/validate-token', {token: loginToken}, (ok2, txt2)=>{
                if(!ok2){ cleanTokenStorage(); setTimeout(()=>{ showTokenPrompt(); showToast('Session expired - enter new token', true); }, 800); }
              });
            } else {
              cleanTokenStorage(); setTimeout(()=>{ showTokenPrompt(); showToast('Session expired - enter new token', true); }, 800);
            }
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
        ws.addEventListener('open',()=>logOk(`WSS OPEN ${labelForUrl(url)}`));
        ws.addEventListener('close',(ev)=>logSys(`WSS CLOSE ${labelForUrl(url)} code=${ev.code}`));
        return ws;
      }
      HookedWS.prototype=OrigWS.prototype;
      HookedWS.CONNECTING=OrigWS.CONNECTING; HookedWS.OPEN=OrigWS.OPEN; HookedWS.CLOSING=OrigWS.CLOSING; HookedWS.CLOSED=OrigWS.CLOSED;
      HookedWS[hookKey]=true; w.WebSocket=HookedWS;
      logSys(`WebSocket hook installed on ${w===window?'window':'unsafeWindow'} ✓`);
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
    logSys('ready - BuildNow Companion active (project-nectar.onrender.com) · COLLECT Ctrl+Shift+C/S/L/R');
  }catch{}
})();
