"""
23 — Collect: load file via Ctrl+Shift+L
Userscript fileInput #bn-file-input reads JSON → collectedPackets
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
# Handle file load: handleFileLoad(file) → JSON.parse → collectedPackets = data.packets
p = Path("logs/photon-capture-123.json")
if p.exists():
    data = json.loads(p.read_text())
    print(f"Loaded {len(data['packets'])} packets, meta={data['meta']}")
else:
    print("Place a captures file in logs/ then Ctrl+Shift+L in game")
