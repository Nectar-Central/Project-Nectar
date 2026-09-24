#!/usr/bin/env python3
"""
Nectar Buildnow Presence — decryptor + rich presence in system tray, headless background.
Bundle with PyInstaller to .exe.
"""
import os, sys, subprocess, threading, time, webbrowser, json
from pathlib import Path

# hide console on Windows
if sys.platform == "win32":
    import ctypes
    try: ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    except: pass

# Single instance: a second launch would fight over ports 8765+ and double-push
# to Discord. The named mutex also lets the Inno Setup installer detect a
# running tray on upgrade/uninstall (AppMutex=BuildNowTraySingleInstance).
if sys.platform == "win32":
    try:
        _single = ctypes.windll.kernel32.CreateMutexW(None, False, "BuildNowTraySingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        pass

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    print("pip install pystray pillow requests")
    sys.exit(1)

import requests, shutil

# PyInstaller frozen → files in sys._MEIPASS
if getattr(sys, 'frozen', False):
    BASE = Path(sys._MEIPASS) / "companion"
else:
    BASE = Path(__file__).resolve().parent.parent  # companion/
ROOT = BASE
DEC = ROOT / "decryptor.js"
# Rich presence: the Python bridge the userscript pushes to (F4 > Presence).
# The legacy node script stays on disk as a fallback if the import fails.
PRES = ROOT / "rich_presence.py"
PRES_NODE = ROOT / "discord-presence.js"
LOG = Path.home() / ".buildnow-tray.log"
ICON_SIZE = 64

def make_icon():
    img = Image.new("RGBA", (ICON_SIZE, ICON_SIZE), (0,0,0,0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4,4,60,60], radius=14, fill=(124,58,237))
    d.text((18,18), "BN", fill="white", font=None)
    return img

def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f: f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except: pass

def find_node():
    for c in ["node", "node.exe"]:
        p = shutil.which(c)
        if p: return p
    for p in [r"C:\Program Files\nodejs\node.exe", os.path.expandvars(r"%ProgramFiles%\nodejs\node.exe"), r"C:\Program Files (x86)\nodejs\node.exe"]:
        if Path(p).exists(): return p
    return None

def ensure_modules():
    # when frozen, node needs NODE_PATH to find bundled node_modules
    if getattr(sys, 'frozen', False):
        mod = Path(sys._MEIPASS) / "node_modules"
        if mod.exists():
            os.environ["NODE_PATH"] = str(mod) + os.pathsep + os.environ.get("NODE_PATH","")
            log(f"NODE_PATH set to {mod}")

def run_node(script, env_extra=None):
    node = find_node()
    if not node:
        log("node not found in PATH — install from https://nodejs.org")
        return None
    if not Path(script).exists():
        log(f"script not found: {script}")
        return None
    env = os.environ.copy()
    if env_extra: env.update(env_extra)
    creation = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    log(f"start {node} {script}")
    # log stdout/stderr to file instead of DEVNULL so we see why 8765 fails
    lf = open(LOG, "a", encoding="utf-8")
    try:
        return subprocess.Popen(
            [node, str(script)], cwd=str(ROOT.parent if ROOT.parent.exists() else Path.cwd()),
            stdout=lf, stderr=subprocess.STDOUT, creationflags=creation, env=env
        )
    except Exception as e:
        log(f"popen failed {e}")
        return None

dec_proc = None
pres_proc = None
pres_mod = None    # rich_presence module when the in-process bridge is running
pres_bridge = None # Bridge instance
pres_server = None # its ThreadingHTTPServer
pres_port = None   # the port the bridge actually bound (it walks 8765->8769)

def start_services():
    global dec_proc, pres_proc
    ensure_modules()
    try:
        r = requests.get("http://127.0.0.1:8765/status", timeout=1)
        if r.ok:
            log("8765 already listening — skipping decryptor start")
            dec_proc = None
        else:
            dec_proc = run_node(DEC)
    except:
        dec_proc = run_node(DEC)
    time.sleep(1.2)
    # check decryptor actually up
    try:
        r = requests.get("http://127.0.0.1:8765/status", timeout=1.5)
        if not r.ok: log(f"decryptor status {r.status_code} {r.text[:200]}")
        else: log("decryptor OK")
    except Exception as e:
        log(f"decryptor not reachable: {e} — check log at {LOG} and that node is installed")
    if not start_presence():
        try:
            pres_proc = run_node(PRES_NODE)
            log(f"presence start {pres_proc} (node fallback)")
        except Exception as e:
            log(f"presence start failed {e}")

def start_presence():
    """Run the rich presence bridge in-process.

    In-process rather than subprocess: a PyInstaller build has no python
    executable to spawn, and the bridge is a small stdlib HTTP server. It binds
    8765 when free; if the decryptor already owns that port it walks forward to
    8766+ and the userscript's auto-detect follows it.
    """
    global pres_mod, pres_bridge, pres_server, pres_port
    try:
        if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
        import rich_presence as _rp
        pres_bridge, pres_server, port = _rp.serve_in_thread()
        pres_mod = _rp
        pres_port = port
        log(f"presence bridge listening on {_rp.http_url(port)}")
        return True
    except Exception as e:
        pres_mod, pres_bridge, pres_server = None, None, None
        log(f"presence bridge failed: {e}")
        return False

def stop_services():
    global pres_mod, pres_bridge, pres_server
    import signal
    if pres_bridge is not None:
        try: pres_bridge.clear("tray stop")          # clears the Discord activity
        except Exception: pass
        try: pres_mod.stop_server(pres_server)
        except Exception: pass
        pres_mod, pres_bridge, pres_server = None, None, None
    for p in (dec_proc, pres_proc):
        if not p: continue
        try:
            # try graceful SIGINT so discord-presence can clearActivity()
            try: p.send_signal(signal.SIGINT)
            except: p.terminate()
            # give it 1.5s to clear
            try: p.wait(timeout=1.5)
            except: p.terminate()
        except: pass
    # if decryptor still has presence, force clear via stale timeout — also try direct clear
    try:
        # fire-and-forget clear via dummy presence idle
        requests.post("http://127.0.0.1:8765/clear", timeout=1)
    except: pass

def fetch_presence(port=None):
    """The bridge's own status: v2 answers with {activity, mode, discord, ...}.

    Falls back to the legacy node bridge's flat shape so an older setup still
    reports something useful instead of "-".
    """
    ports = [port] if port else ([pres_port] if pres_port else []) + [8765]
    for p in ports:
        try:
            r = requests.get(f"http://127.0.0.1:{p}/presence", timeout=2)
            j = r.json()
            if not isinstance(j, dict):
                continue
            if j.get("service"):
                return j
            return j.get("presence") or j
        except Exception:
            continue
    return None

def presence_summary(j):
    """Status line for the tray tooltip/notification."""
    if not isinstance(j, dict):
        return None
    activity = j.get("activity") or {}
    if activity:
        return f"{activity.get('details','-')} | {activity.get('state','-')}"
    if j.get("gameMode") is not None:   # legacy node bridge
        return f"{j.get('gameMode','-')} | {j.get('status','-')} • {j.get('playersCount',0)}/{j.get('maxPlayers',12)}"
    return "no activity (bridge idle)"

def on_show(icon, item):
    j = fetch_presence()
    line = presence_summary(j)
    if j and line:
        try: icon.notify(line, "Nectar Buildnow Presence")
        except: pass
    else:
        try: icon.notify(f"Presence bridge not answering on 8765 — see {LOG}", "Nectar Buildnow Presence — error")
        except: pass
    # also open log on status click if failed
    if not j:
        try: os.startfile(str(LOG))
        except: pass

def on_open_logs(icon, item):
    log_dir = ROOT / "captures"
    try: os.startfile(str(log_dir))
    except: webbrowser.open(str(log_dir))

def on_quit(icon, item):
    icon.stop()
    stop_services()
    os._exit(0)

def on_restart(icon, item):
    stop_services()
    time.sleep(0.5)
    start_services()
    icon.notify("Restarted decryptor + presence", "Nectar Buildnow Presence")

def setup_tray():
    icon = pystray.Icon("Nectar Buildnow Presence", make_icon(), "Nectar Buildnow Presence")
    icon.menu = pystray.Menu(
        pystray.MenuItem("Status", on_show),
        pystray.MenuItem("Open captures", on_open_logs),
        pystray.MenuItem("Restart services", on_restart),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    # run services before tray loop
    threading.Thread(target=start_services, daemon=True).start()
    icon.run()

if __name__ == "__main__":
    setup_tray()
