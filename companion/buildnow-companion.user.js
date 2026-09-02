// ==UserScript==
// @name         BuildNow Companion Live Capture (game-frame only)
// @namespace    buildnow-companion.local
// @version      2.3.0
// @description  BuildNow Companion v2.3 — two-tier lock (default/admin), enforced login, no guest, full logs + stG.
// @author       @cis6led
// @match        https://buildnow-gg.game-files.crazygames.com/*
// @all-frames   true
// @run-at       document-start
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
// @connect      vercel.app
// @connect      *.vercel.app
// @connect      supabase.co
// @connect      *.supabase.co
// ==/UserScript==

(function () {
  'use strict';
  const HREF = (() => { try { return location.href; } catch { return ''; } })();
  const IS_GAME_FRAME = HREF.includes('buildnow-gg.game-files.crazygames.com');
  if (!IS_GAME_FRAME) return;

  const GMGet = (k,d=null)=>{ try{ return typeof GM_getValue!=='undefined'? GM_getValue(k,d) : d; }catch{ return d; } };
  const GMSet = (k,v)=>{ try{ if(typeof GM_setValue!=='undefined') GM_setValue(k,v); }catch{} };
  const GMDel = (k)=>{ try{ if(typeof GM_deleteValue!=='undefined') GM_deleteValue(k); else if(typeof GM_setValue!=='undefined') GM_setValue(k, undefined); }catch{} };
  // Enforce login: if user sets bnc_disabled, script is inert — play normally
  if (GMGet('bnc_disabled', false)) {
    console.log('%c[BNC] disabled — play normally (reenable via GMSet bnc_disabled false)', 'color:#6b7280');
    return;
  }

  // api-compbng.vercel.app is primary. Local dev override: GMSet('bnc_api_base','http://127.0.0.1:8000') or 'https://api-compbng.vercel.app'
  const VERCEL_API = GMGet('bnc_api_base', GMGet('bn_api_base', null)) || null;
  const SERVER_API = 'https://api-compbng.vercel.app';
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

  // session restore — login required, no guest capture
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
        cleanTokenStorage(); hasWelcomed=false; setTimeout(()=>{ showTokenPrompt(); showToast('Session expired — enter new token', true); }, 800);
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
      logErr(`apiPost ${path} skipped — no session`);
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
                logInfo(`replay detected for ${path} — retry`);
                apiQueue.unshift({path, body: {...origBody, _retried:true}, cb, retried:true});
                apiBusy = false; setTimeout(processApiQueue, 30); return;
              }
            }
            if(j.error && String(j.error).toLowerCase().includes('invalid')){
              if(j.current){ currentReqToken=j.current; GMSet('bnc_req_token', currentReqToken); logInfo(`mismatch sync token → ${String(currentReqToken).slice(0,4)}…`); if(!retried){ logInfo(`mismatch retry ${path}`); apiQueue.unshift({path, body: {...origBody, _retried:true}, cb, retried:true}); apiBusy=false; setTimeout(processApiQueue,30); return; } }
              else { logErr(`req_token rejected — clearing session`); cleanTokenStorage(); }
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
            onload: r=> { const ok=r.status>=200&&r.status<300; (ok?logOk:logErr)(`API ${ok?'OK':'ERR'} ${u} → ${r.status} ${String(r.responseText).slice(0,200)}`); wrapOk(ok, r.responseText); },
            onerror: (e)=>{ wrapFail(`API GM_xhr error → ${u} ${e&&e.error||''}`); },
            ontimeout: ()=>{ wrapFail(`API timeout (15s) → ${u}`); }});
          return true;
        }catch(e){ wrapFail(`API GM throw ${e.message}`); return false; }
      }
      try{
        fetch(u,{method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)}).then(r=>r.text().then(t=>{ const ok=r.ok; (ok?logOk:logErr)(`API fetch ${ok?'OK':'ERR'} ${u} → ${r.status} ${t.slice(0,200)}`); wrapOk(ok,t); }).catch(()=>wrapFail(`fetch catch ${u}`))).catch((e)=>{ wrapFail(`API fetch fail ${u} ${e.message}`); });
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
      if(sessionId) showToast(`BuildNow Companion — Linked${discordUsername? ' as '+discordUsername:''}`, false);
      else if(loginToken) showToast('BuildNow Companion — Ready', false);
      else showToast('BuildNow Companion — Login required', true);
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
  function showLockedPanel(reason, scope){
    if(document.getElementById('bnc-locked')) return;
    const isAdmin = String(scope||'').toLowerCase()==='admin';
    const title = isAdmin ? 'Admin-locked' : 'Account locked';
    const help = isAdmin ? 'This is an <b style="color:#fff">admin lock</b> — only an admin can run <b style="color:#fff">/adminunlock</b> on you. Disable the tracker to play normally.' : 'Use <b style="color:#fff">/unlock</b> in Discord or ask an admin. Disable the tracker to play normally.';
    const wrap=document.createElement('div');
    wrap.id='bnc-locked';
    Object.assign(wrap.style,{position:'fixed',inset:'0',zIndex:'2147483647',background:'rgba(0,0,0,0.82)',display:'flex',alignItems:'center',justifyContent:'center',padding:'24px',fontFamily:'Inter, system-ui, sans-serif'});
    const card=document.createElement('div');
    Object.assign(card.style,{background:'#0a0a0a',border:'1px solid #991b1b',borderRadius:'14px',padding:'28px',width:'420px',textAlign:'center',boxShadow:'0 8px 32px rgba(239,68,68,0.25)'});
    card.innerHTML = `<div style="font-size:44px;margin-bottom:8px">${isAdmin?'⛔':'🔒'}</div><div style="font-size:18px;font-weight:800;color:#fff;margin-bottom:6px">${title}</div><div style="font-size:13px;color:#fca5a5;margin-bottom:6px;word-break:break-word">${String(reason||'Token reuse or sharing detected — friend account use is blocked.').replace(/</g,'&lt;')}</div><div style="font-size:12px;color:#9ca3af;margin-bottom:16px">${help}</div><div style="display:flex;gap:8px;justify-content:center"><button id="bnc-locked-disable" style="flex:1;height:40px;border:0;border-radius:8px;background:#fff;color:#000;font-weight:700;cursor:pointer">Disable & Reload</button><button id="bnc-locked-retry" style="flex:1;height:40px;border:1px solid #374151;border-radius:8px;background:#1f2937;color:#e5e7eb;font-weight:600;cursor:pointer">Retry</button></div><div style="margin-top:10px;font-size:11px;color:#6b7280">BNC is disabled while locked — no capture or API calls.</div>`;
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
      showToast('BNC disabled — reload to play normally', false);
      setTimeout(()=>{ try{ window.top.location.reload(); }catch{ location.reload(); } }, 600);
    };
    document.getElementById('bnc-locked-retry').onclick=()=>{
      wrap.remove();
      ['keydown','keyup','keypress'].forEach(ev=> window.removeEventListener(ev, blocker, true));
      location.reload();
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
    // before the game sees it. We do NOT preventDefault — let the input handle it.
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
      // do not call preventDefault — input needs Backspace/Delete/Enter/Arrows
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

    // Backdrop click closes — but not input/card clicks
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
        if(!ok){ msg.textContent='Token incorrect — try again'; submitBtn.textContent='Link'; submitBtn.style.opacity='1'; linking=false; showToast('Token incorrect',true); return; }
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
        showToast('Linked! Reloading…',false);
        setTimeout(()=>{ try{ window.top.location.reload(); }catch{ location.reload(); } }, 400);
      });
    };

    disableBtn.onclick=()=>{
      GMSet('bnc_disabled', true);
      document.removeEventListener('keydown', escHandler, true); closeModal();
      showToast('BNC disabled — reload to play normally', false);
      setTimeout(()=>{ try{ window.top.location.reload(); }catch{ location.reload(); } }, 500);
    };
    submitBtn.onclick=doSubmit;
    // Enter submits, Backspace/Delete must work — handled by guard not blocking default
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
  const ESSENTIAL_OPS=new Set([225,226,227,230,231]);
  const ESSENTIAL_EVS=new Set([101,253,254,255]);
  const DROP_EVS_NOISY=new Set([200,201,202,204,206]);
  const BATCH_MAX=40, FLUSH_MS=250;

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

  // Hotkeys: Ctrl+Shift+C = toggle collect, etc — disable when modal/input focused
  function isModalOrInputFocused(){
    const ae = document.activeElement;
    return !!(ae && (ae.id === 'bnc-token-field' || ae.closest && ae.closest('#bnc-login-wrap')) || document.getElementById('bnc-login-wrap')?.contains(ae));
  }
  document.addEventListener('keydown', (e)=>{
    if (isModalOrInputFocused()) return;
    if(e.ctrlKey && e.shiftKey){
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

  // --- LOG FEED (bottom, passthrough + pause + decrypted hex) — dev only, hidden by default in v2 ---
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
  feedBar.innerHTML = `<span style="opacity:0.5">BNC feed</span><button id="bnc-feed-pause" style="background:#1f2937;color:#e5e7eb;border:1px solid #374151;border-radius:4px;padding:0 6px;cursor:pointer">${feedPaused?'▶ Resume':'⏸ Pause'}</button><span style="opacity:0.5">Ctrl+Shift+F toggle · Ctrl+Shift+P pause · hover to scroll</span>`;
  const feedEl = document.createElement('div');
  feedEl.id = 'bnc-log-feed';
  Object.assign(feedEl.style, {
    maxHeight: '130px', background: 'rgba(0,0,0,0.88)', color: '#e5e7eb',
    fontFamily: 'monospace', fontSize: '10px', lineHeight: '13px',
    padding: '4px 6px', overflowY: 'auto', overflowX: 'hidden', pointerEvents: 'auto',
    borderTop: '1px solid rgba(255,255,255,0.08)', backdropFilter: 'blur(2px)'
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
      // no truncation — full payload, but cap single field at 4000 chars to avoid DOM kill
      let s;
      if(typeof v==='object' && v!==null) s = JSON.stringify(v);
      else s = String(v);
      if(s.length>4000) s = s.slice(0,4000)+' …(+'+(s.length-4000)+' chars)';
      // escape html
      s = s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      return `${k}=${s}`;
    }).join(' ');
    const line = `<div style="white-space:pre-wrap;word-break:break-all"><span style="opacity:0.45">${ts}</span> <span style="color:${color};font-weight:700">${evt}</span> <span style="opacity:0.9">${fieldStr}</span></div>`;
    feedLines.push(line);
    if(feedLines.length>300) feedLines.shift();
    const wasAtBottom = feedEl.scrollTop + feedEl.clientHeight >= feedEl.scrollHeight - 20;
    feedEl.innerHTML = feedLines.join('');
    if(wasAtBottom) feedEl.scrollTop = feedEl.scrollHeight;
  }
  function feedInfo(msg){ feedLog('info', {msg}); }
  document.addEventListener('keydown', (e)=>{
    if (isModalOrInputFocused()) return;
    const k=(e.key||'').toUpperCase();
    if(e.ctrlKey && e.shiftKey && k==='F'){
      e.preventDefault(); e.stopPropagation();
      feedEnabled = !feedEnabled;
      GMSet('bnc_feed_enabled', feedEnabled);
      feedWrap.style.display = feedEnabled ? 'block' : 'none';
      showToast(feedEnabled ? 'Feed ON' : 'Feed OFF', false);
    } else if(e.ctrlKey && e.shiftKey && k==='P'){
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
  }, 800);

  // --- PLAYER INFO PANEL (v2: skin, name, discord, game_id, stats, state, game_mode, zone, playtime) ---
  let infoPanel = null, infoUpdateTimer=null;
  function createInfoPanel(){
    if(document.getElementById('bnc-info-panel')) return;
    infoPanel = document.createElement('div');
    infoPanel.id='bnc-info-panel';
    Object.assign(infoPanel.style, {position:'fixed', top:'8px', right:'8px', zIndex:'2147483645', width:'260px', background:'rgba(0,0,0,0.92)', color:'#e5e7eb', border:'1px solid rgba(255,255,255,0.12)', borderRadius:'10px', padding:'10px 12px', fontFamily:'Inter, system-ui, sans-serif', fontSize:'12px', lineHeight:'1.4', backdropFilter:'blur(6px)', display:'none'});
    infoPanel.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px"><span style="font-weight:700;color:#fff">Player</span><span style="opacity:0.6;font-size:10px">Ctrl+Shift+I</span><button id="bnc-info-close" style="background:transparent;border:0;color:#9ca3af;cursor:pointer">✕</button></div><div id="bnc-info-body" style="opacity:0.9">Loading…</div>`;
    (document.body||document.documentElement).appendChild(infoPanel);
    infoPanel.querySelector('#bnc-info-close').onclick=()=>{ infoPanel.style.display='none'; GMSet('bnc_info_open', false); };
    // draggable
    let drag=false, sx=0, sy=0, ox=0, oy=0;
    const hdr = infoPanel.firstChild;
    hdr.style.cursor='move';
    hdr.addEventListener('mousedown', (e)=>{ drag=true; sx=e.clientX; sy=e.clientY; const r=infoPanel.getBoundingClientRect(); ox=r.left; oy=r.top; e.preventDefault(); });
    document.addEventListener('mousemove', (e)=>{ if(!drag) return; infoPanel.style.left = (ox + e.clientX - sx)+'px'; infoPanel.style.top = (oy + e.clientY - sy)+'px'; infoPanel.style.right='auto'; });
    document.addEventListener('mouseup', ()=> drag=false);
  }
  function renderInfoPanel(){
    if(!infoPanel) createInfoPanel();
    const body = document.getElementById('bnc-info-body');
    if(!body) return;
    const uname = GMGet('bnc_game_username', discordUsername || '-') || '-';
    const gid = GMGet('bnc_game_id', '-') || '-';
    const dname = discordUsername || '-';
    const gm = GMGet('bnc_session_gm', '-') || '-';
    const skin = GMGet('bnc_skin', '-') || '-';
    const discord = discordUsername ? `${discordUsername} (${discordId? discordId.slice(0,6)+'…' : '-'})` : '-';
    const nameLine = `<div><b style="color:#fff">${uname}</b> <span style="opacity:0.6">${gid!=='-'? '('+gid.slice(0,8)+'…)' : ''}</span></div>`;
    const sub = `<div style="opacity:0.7">Discord: ${discord} · GM: ${gm} · Skin: ${String(skin).slice(0,22)}</div>`;
    const hint = `<div style="margin-top:6px;opacity:0.6;font-size:10px">Stats & insights from /stats in Discord</div>`;
    body.innerHTML = nameLine + sub + hint;
  }
  function toggleInfoPanel(){
    if(!infoPanel) createInfoPanel();
    const isOpen = infoPanel.style.display !== 'none';
    if(isOpen){ infoPanel.style.display='none'; GMSet('bnc_info_open', false); }
    else { renderInfoPanel(); infoPanel.style.display='block'; GMSet('bnc_info_open', true); }
  }
  if(GMGet('bnc_info_open', false)){ setTimeout(()=>{ createInfoPanel(); renderInfoPanel(); if(infoPanel) infoPanel.style.display='block'; }, 1200); }

  // --- KEYBINDS / SETTINGS MENU (Ctrl+Shift+K) ---
  let keybindOverlay=null;
  function createKeybindOverlay(){
    if(document.getElementById('bnc-keys')) return;
    const ov=document.createElement('div');
    ov.id='bnc-keys';
    Object.assign(ov.style,{position:'fixed', inset:'0', zIndex:'2147483646', background:'rgba(0,0,0,0.54)', display:'none', alignItems:'center', justifyContent:'center', padding:'24px', fontFamily:'Inter, system-ui, sans-serif'});
    ov.innerHTML = `<div style="background:#111;border:1px solid rgba(255,255,255,0.12);border-radius:12px;padding:20px;min-width:360px;max-width:520px;color:#e5e7eb"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><b style="font-size:15px;color:#fff">BuildNow Companion — Shortcuts & Settings</b><button id="bnc-keys-close" style="background:#1f2937;color:#e5e7eb;border:0;border-radius:6px;padding:4px 8px;cursor:pointer">Close</button></div><div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;line-height:1.6"><div><code>Ctrl+Shift+C</code> — Collect toggle<br><code>Ctrl+Shift+S</code> — Save collected<br><code>Ctrl+Shift+L</code> — Load file<br><code>Ctrl+Shift+R</code> — Replay</div><div><code>Ctrl+Shift+F</code> — Feed (dev only)<br><code>Ctrl+Shift+P</code> — Pause feed<br><code>Ctrl+Shift+I</code> — Player info<br><code>Ctrl+Shift+K</code> — This menu</div></div><div style="margin-top:14px;padding-top:10px;border-top:1px solid rgba(255,255,255,0.08);font-size:11px;opacity:0.7">Settings: <label style="cursor:pointer"><input type="checkbox" id="bnc-dev-toggle"> Dev mode (feed)</label> · <label style="cursor:pointer"><input type="checkbox" id="bnc-info-auto"> Show player info on start</label></div></div>`;
    (document.body||document.documentElement).appendChild(ov);
    ov.addEventListener('click', (e)=>{ if(e.target===ov) ov.style.display='none'; });
    ov.querySelector('#bnc-keys-close').onclick=()=> ov.style.display='none';
    const devT = ov.querySelector('#bnc-dev-toggle');
    devT.checked = !!GMGet('bnc_dev_mode', false);
    devT.onchange=()=>{ GMSet('bnc_dev_mode', devT.checked); showToast(devT.checked? 'Dev mode ON':'Dev mode OFF', false); if(devT.checked){ feedEnabled=true; feedWrap.style.display='block'; } else feedWrap.style.display='none'; };
    const autoT = ov.querySelector('#bnc-info-auto');
    autoT.checked = !!GMGet('bnc_info_open', false);
    autoT.onchange=()=> GMSet('bnc_info_open', autoT.checked);
    keybindOverlay=ov;
  }
  function toggleKeybinds(){ if(!keybindOverlay) createKeybindOverlay(); keybindOverlay.style.display = keybindOverlay.style.display==='none' || !keybindOverlay.style.display ? 'flex' : 'none'; }
  // Hotkeys: Ctrl+Shift+I info panel, Ctrl+Shift+K keybinds (guard modal focus)
  document.addEventListener('keydown', (e)=>{
    if (isModalOrInputFocused()) return;
    if(e.ctrlKey && e.shiftKey){
      const k=(e.key||'').toUpperCase();
      if(k==='I'){ e.preventDefault(); e.stopPropagation(); toggleInfoPanel(); }
      else if(k==='K'){ e.preventDefault(); e.stopPropagation(); toggleKeybinds(); }
    }
  }, true);

  // --- RANKED HUD (only when joining private room with short_code / pID) ---
  let rankedHUD=null; let rankedShort=null;
  function createRankedHUD(){
    if(document.getElementById('bnc-ranked')) return;
    rankedHUD=document.createElement('div');
    rankedHUD.id='bnc-ranked';
    Object.assign(rankedHUD.style,{position:'fixed', top:'52px', left:'50%', transform:'translateX(-50%)', zIndex:'2147483645', background:'linear-gradient(135deg,#7c3aed 0%,#4f46e5 100%)', color:'#fff', padding:'6px 14px', borderRadius:'999px', fontFamily:'Inter, system-ui, sans-serif', fontSize:'12px', fontWeight:'800', letterSpacing:'0.3px', display:'none', boxShadow:'0 4px 16px rgba(124,58,237,0.45)', border:'1px solid rgba(255,255,255,0.12)'});
    rankedHUD.textContent='RANKED — private lobby';
    (document.body||document.documentElement).appendChild(rankedHUD);
  }
  function showRankedHUD(code){
    if(!rankedHUD) createRankedHUD();
    rankedShort=code;
    rankedHUD.textContent = `RANKED — ${code}`;
    rankedHUD.style.display='block';
    // also update info panel gm hint
    if(code) GMSet('bnc_session_gm', 'ranked_'+code);
    setTimeout(()=>{ /* keep visible */ }, 0);
  }
  function hideRankedHUD(){ if(rankedHUD) rankedHUD.style.display='none'; rankedShort=null; }
  // expose for API capture response to trigger
  window.__BNC_SHOW_RANKED__ = showRankedHUD;
  window.__BNC_HIDE_RANKED__ = hideRankedHUD;

  function isPhotonUrl(url){ if(!url) return false; const u=String(url); if(u.includes(APP_ID)) return true; for(const h of PHOTON_HOSTS) if(u.toLowerCase().includes(h)) return true; if(u.includes('wss://')&&u.includes('exitgames')) return true; return false; }
  function shortUrl(url){ return String(url).replace(/^wss?:\/\//,'').replace(/\?libversion.*$/,'').slice(0,80); }
  function labelForUrl(url){ const u=String(url); if(u.includes('ns.exitgames.com')) return 'NameServer'; if(u.includes('gcams')){ const m=u.match(/gcams(\d+)/); return m?`GCAMS${m[1]}`:'GCAMS'; } if(u.includes('exitgames')) return 'Photon'; return 'WS'; }
  function hexOf(bytes){ let s=''; for(let i=0;i<bytes.length;i++) s+=(bytes[i]<16?'0':'')+bytes[i].toString(16); return s; }
  function isEssentialBytes(bytes){
    if(!bytes||bytes.length<3) return false;
    const type=bytes[1]; const code=bytes[2];
    if(type===6||type===7) return false;
    if(type===2||type===3) return ESSENTIAL_OPS.has(code);
    if(type===4){ if(DROP_EVS_NOISY.has(code)) return false; return ESSENTIAL_EVS.has(code); }
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
      if(Date.now()-lastCaptureBlockedLog>5000){ lastCaptureBlockedLog=Date.now(); logInfo('capture skipped — no session (login required)'); }
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
              // full decrypted payload — no truncation
              const fields = {packet:p.packet || p.packet_type, seq:p.seq, dir:p.dir, human:p.human || ''};
              if(p.killer || p.victim) { fields.killer=p.killer; fields.victim=p.victim; }
              if(p.player_name) fields.player=p.player_name;
              if(p.zone_time!=null) fields.zone_time=p.zone_time;
              if(p.zone_phase!=null) fields.zone_phase=p.zone_phase;
              if(p.short_code) fields.code=p.short_code;
              if(p.room_gm) fields.gm=p.room_gm;
              if(p.room_id) fields.room_id=p.room_id.slice(0,12);
              if(p.match_version!=null) fields.ver=p.match_version;
              // stG change — explicit log (full props, no truncation)
              try{
                const stg = dec && (dec.properties ? dec.properties.stG : dec.stG);
                const lastStg = p._lastStg !== undefined ? p._lastStg : null;
                if(stg!=null) fields.stG = stg;
                // engine already updates last_stg; we also emit dedicated stg line when present
                if(dec && dec.properties && 'stG' in dec.properties){
                  feedLog('stg', {seq:p.seq, stG: dec.properties.stG, gm: p.room_gm || p.game_mode || '', room: (p.room_id||'').slice(0,8), curScn: p.cur_scn || '', human: p.human || '', full: dec.properties});
                }
              }catch{}
              // full gpb — no slice
              if(dec && Object.keys(dec).length) fields.gpb = dec;
              // full hex — no truncation
              feedLog('decrypted', fields);
              feedLog('hex', {seq:p.seq, hex:p.rawHex||''});
              // Ranked HUD: only show when joining private room with short_code (5-char code) — not every packet
              if(p.short_code && /^[A-Z0-9]{5}$/i.test(String(p.short_code))){ showRankedHUD(String(p.short_code).toUpperCase()); }
              else if(p.room_gm && String(p.room_gm).toLowerCase().includes('ranked') && p.short_code){ showRankedHUD(String(p.short_code).toUpperCase()); }
            }
            // Also check session for private ranked room
            try{
              const sess = j.session;
              if(sess && sess.rooms){
                for(const rv of Object.values(sess.rooms)){ if(rv.short_code && /^[A-Z0-9]{5}$/i.test(rv.short_code)){ showRankedHUD(rv.short_code); break; } }
              }
            }catch{}
          }
        }catch(e){ feedLog('decrypted', {err:e.message}); }
        feedLog('sent', {count:batch.length, ok:true});
      } else {
        let _lockScope='default'; try{ const _j=JSON.parse(txt||'{}'); if(_j.lock_scope) _lockScope=_j.lock_scope; if(_j.is_admin_locked) _lockScope='admin'; }catch{}
        const lower = String(txt).toLowerCase();
        if(lower.includes('locked') || lower.includes('account locked')){
          isLocked = true; queue=[]; flushTimer=null;
          logErr(`account locked — blocking game ${String(txt).slice(0,120)} scope=${_lockScope}`);
          showLockedPanel(String(txt).toLowerCase().includes('admin')? 'Account admin-locked' : 'Account locked', _lockScope);
          return;
        }
        logErr(`capture failed — requeue ${batch.length} txt=${String(txt).slice(0,120)}`);
        feedLog('err', {count:batch.length, ok:false, txt:String(txt).slice(0,120)});
        if(String(txt).includes('Invalid req_token') || String(txt).includes('expired')){
          queue=[]; feedLog('err', {msg:'token error — re-link'});
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
            if(peekHeader(data)&&!isEssentialHeader(peekHeader(data))) return origSend.call(this,data);
            const b=toBytes(data); if(b) captureOut(b,u);
            else if(typeof Blob!=='undefined'&&data instanceof Blob) data.arrayBuffer().then(ab=>{ const bb=toBytes(ab); if(bb) captureOut(bb,u); }).catch(()=>{});
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
    logSys('ready — BuildNow Companion active (api-compbng.vercel.app) · COLLECT Ctrl+Shift+C/S/L/R');
  }catch{}
})();
