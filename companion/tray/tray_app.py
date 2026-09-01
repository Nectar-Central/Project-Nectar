#!/usr/bin/env python3
"""
BuildNow Tray — decryptor + rich presence in system tray, headless background.
Bundle with PyInstaller to .exe.
"""
import os, sys, subprocess, threading, time, webbrowser, json
from pathlib import Path

# hide console on Windows
if sys.platform == "win32":
    import ctypes
    try: ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)
    except: pass

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
PRES = ROOT / "discord-presence.js"
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
    try:
        pres_proc = run_node(PRES)
        log(f"presence start {pres_proc}")
    except Exception as e:
        log(f"presence start failed {e}")

def stop_services():
    import signal
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

def fetch_presence():
    try:
        r = requests.get("http://127.0.0.1:8765/presence", timeout=2)
        j = r.json()
        p = j.get("presence") or j
        return p
    except: return None

def on_show(icon, item):
    p = fetch_presence()
    if p:
        try: icon.notify(f"{p.get('gameMode','-')} | {p.get('status','-')} • {p.get('playersCount',0)}/{p.get('maxPlayers',12)}", "BuildNow Presence")
        except: pass
    else:
        try: icon.notify(f"Decryptor not on 8765 — see {LOG}", "BuildNow Tray — error")
        except: pass
    # also open log on status click if failed
    if not p:
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
    icon.notify("Restarted decryptor + presence", "BuildNow Tray")

def setup_tray():
    icon = pystray.Icon("BuildNow Tray", make_icon(), "BuildNow — Tray (decryptor + presence headless)")
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
