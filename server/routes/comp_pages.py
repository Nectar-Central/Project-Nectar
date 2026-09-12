"""
comp_pages.py — Python-first comp dashboard + admin panel + decryptor-compat.

Replaces the Node split-brain (companion/decryptor.js serves HTML on :8765,
companion/comp-dashboard.html fetches SRV + local Node URLs). Everything here
is same-origin against this FastAPI app:

  GET  /comp/dashboard  -> dashboard (tourneys, detail, match edit, unmapped/link)
  GET  /admin           -> admin panel (hosts, regs/ban, table, seal/winner)
  GET  /comp/status     -> live bridge status (open matches, counts)
  POST /capture         -> decryptor-compat alias for POST /api/capture
  WS   /ws              -> decryptor-compat socket (POST fallback recommended;
                          accepts {sessionId, records} frames, feeds engine)

All data flows through /api/comp/* (server/routes/comp.py) and
POST /api/capture (server/routes/capture.py). Node decryptor is retired;
the Tampermonkey userscript should point LOCAL_API at this server.
"""

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from ..lib.supabase_client import supabase, db_io

router = APIRouter()

from pathlib import Path as _Path


def _load_dash_html() -> str:
    try:
        return (_Path(__file__).with_name("comp_dashboard.html")).read_text(encoding="utf-8")
    except Exception:
        return "<html><body>dashboard file missing</body></html>"


# Copied layout from companion/comp-dashboard.html (same Tahoma / blue
# gradient / fieldset / tab styling). Only the backend calls target this
# Python server: SRV defaults to location.origin, the tracker reads
# GET /comp/status, Track/Open/Seal use POST /api/comp/* (the bridge
# auto-records by room code).
_DASH_HTML = _load_dash_html()


_ADMIN_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Panel (Python)</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#111827;color:#e5e7eb}
header{background:#7c2d12;padding:12px 18px}header b{font-size:16px}
main{max-width:1000px;margin:0 auto;padding:14px}
fieldset{border:1px solid #44403c;border-radius:6px;padding:12px;margin-bottom:12px;background:#1c1917}
legend{color:#fb923c;font-weight:700;padding:0 6px}
button{background:#ea580c;border:0;border-radius:4px;padding:6px 12px;font-weight:700;cursor:pointer;color:#fff}
button.ghost{background:#44403c}
input{background:#0c0a09;color:#e5e7eb;border:1px solid #57534e;border-radius:4px;padding:6px 8px}
table{border-collapse:collapse;width:100%;font-size:13px}
th{background:#9a3412;padding:6px;text-align:left}td{padding:5px 6px;border-bottom:1px solid #44403c}
.dim{color:#a8a29e}.bad{color:#f87171}a{color:#fdba74}
#log{font-family:monospace;font-size:12px}
</style></head><body>
<header><b>Admin Panel</b> <span class="dim">python · same-origin · <a href="/comp/dashboard">dashboard</a></span></header>
<main>
<fieldset><legend>Registrations (accept / ban)</legend>
<div>Tourney <input id="tid" size="6"> <button id="regs">Load regs</button></div>
<div id="reglist" class="dim">—</div></fieldset>
<fieldset><legend>Hosts + table + calculate</legend>
<div>Tourney <input id="hid" size="6"> hosts(comma dids) <input id="hosts" size="30"> <button id="sethosts">Set hosts</button> <button id="calc" class="ghost">Calculate</button></div>
<div id="calcOut" class="dim"></div></fieldset>
<fieldset><legend>Log</legend><div id="log"></div></fieldset>
</main>
<script>
'use strict';
const $=id=>document.getElementById(id);
function log(m,err){const el=$('log');const d=document.createElement('div');if(err)d.className='bad';d.textContent=new Date().toLocaleTimeString()+' '+m;el.prepend(d);}
async function api(p,o){const r=await fetch(p,o);const j=await r.json().catch(()=>({}));if(!r.ok)throw new Error(j.error||r.status);return j;}
$('regs').onclick=async()=>{const tid=$('tid').value;try{const j=await api('/api/comp/regs?tourney_id='+tid+'&status=accepted');$('reglist').innerHTML=(j.regs||[]).map(r=>'<div>#'+r.id+' '+r.team_label+' cap='+r.captain_id+' <button data-ban="'+r.id+'">ban</button></div>').join('')||'<span class=dim>none</span>';}catch(e){log('regs: '+e.message,1);}};
document.addEventListener('click',async e=>{const b=e.target.closest('button[data-ban]');if(!b)return;try{await api('/api/comp/reg/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reg_id:Number(b.dataset.ban),status:'banned'})});log('banned reg '+b.dataset.ban);$('regs').click();}catch(err){log('ban: '+err.message,1);}});
$('sethosts').onclick=async()=>{try{const dids=$('hosts').value.split(',').map(s=>s.trim()).filter(Boolean);await api('/api/comp/tourney/hosts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tourney_id:Number($('hid').value),host_discord_ids:dids})});log('hosts set');}catch(e){log('hosts: '+e.message,1);}};
$('calc').onclick=async()=>{try{const j=await api('/api/comp/calculate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({tourney_id:Number($('hid').value)})});$('calcOut').textContent=JSON.stringify(j).slice(0,1000);}catch(e){log('calc: '+e.message,1);}};
</script></body></html>"""


@router.get("/comp/dashboard", response_class=HTMLResponse)
async def comp_dashboard():
    """Python comp dashboard (same-origin). Replaces Node :8765 /comp/dashboard."""
    return HTMLResponse(_DASH_HTML)


@router.get("/comp/", response_class=HTMLResponse)
async def comp_dashboard_alias():
    return HTMLResponse(_DASH_HTML)


@router.get("/admin", response_class=HTMLResponse)
async def admin_panel():
    """Python admin panel (same-origin). No admin*.html existed before; this is canonical."""
    return HTMLResponse(_ADMIN_HTML)


@router.get("/comp/status")
async def comp_status():
    """Live bridge status: open/sealed matches + unmapped event count. Never 500s on blips."""
    if not supabase:
        return {"ok": False, "error": "Server not configured", "open_matches": 0}
    try:
        mres = await db_io(lambda: supabase.table("bngc_comp_matches").select(
            "id,status").in_("status", ["open", "manual", "sealed"]).limit(2000).execute())
        rows = mres.data or []
        n_open = sum(1 for r in rows if (r or {}).get("status") in ("open", "manual"))
        n_sealed = sum(1 for r in rows if (r or {}).get("status") == "sealed")
    except Exception:
        n_open, n_sealed = -1, -1
    try:
        eres = await db_io(lambda: supabase.table("bngc_comp_events").select(
            "id").is_("discord_id", "null").limit(1000).execute())
        n_unmapped = len(eres.data or [])
    except Exception:
        n_unmapped = -1
    return {
        "ok": True,
        "service": "comp-bridge (python)",
        "open_matches": n_open,
        "sealed_matches": n_sealed,
        "unmapped_events": n_unmapped,
        "note": "bridge: short_code → tourney → open → seal on stG phase end; -1 = transient DB blip",
    }


@router.post("/capture")
async def capture_alias(request: Request):
    """Decryptor-compat alias: Node POST /capture {sessionId, records:[{seq,t,dir,url,rawHex}]}.

    Normalizes to /api/capture and forwards internally so the Tampermonkey
    userscript works unchanged against Python (set LOCAL_API to this server).
    """
    from .capture import capture_handler
    from fastapi.background import BackgroundTasks

    # capture_handler reads authSessionId/token + sessionId/records — shape matches.
    return await capture_handler(request, BackgroundTasks())


@router.websocket("/ws")
async def ws_capture(ws: WebSocket):
    """Decryptor-compat socket. Accepts JSON frames {sessionId, records:[...]}.

    Preferred path remains POST /api/capture (or POST /capture); WS exists so
    old userscript WS transports connect and get a clear migration message
    plus best-effort ingest of forwarded frames.
    """
    await ws.accept()
    try:
        await ws.send_json({"ok": True, "service": "comp-bridge (python)",
                            "hint": "POST /api/capture preferred; WS frames accepted"})
        while True:
            try:
                msg = await ws.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                try:
                    await ws.send_json({"ok": False, "error": "invalid frame; send {sessionId, records}"})
                except Exception:
                    break
                continue
            try:
                records = msg.get("records") if isinstance(msg, dict) else None
                if not isinstance(records, list):
                    await ws.send_json({"ok": False, "error": "records required"})
                    continue
                # Best-effort engine feed without auth (auth happens on POST path).
                from .capture import engine as _engine
                sid = str(msg.get("sessionId") or msg.get("session_id") or "ws")
                for rec in records:
                    try:
                        raw = rec.get("rawHex") or rec.get("hex") or ""
                        if raw:
                            _engine.feed(sid, raw)
                    except Exception:
                        continue
                await ws.send_json({"ok": True, "ingested": len(records)})
            except Exception as exc:
                try:
                    await ws.send_json({"ok": False, "error": str(exc)[:120]})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        try:
            await ws.close()
        except Exception:
            pass
