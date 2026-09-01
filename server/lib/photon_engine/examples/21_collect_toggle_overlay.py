"""
21 — Collect: toggle via Ctrl+Shift+C (badge)
Userscript stores packets in collectedPackets[] while collectMode=true.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
# No python needed — this is userscript hotkey:
# Ctrl+Shift+C → toggleCollectMode() → badge "🔴 COLLECT 123"
# See photon-sniffer.user.js:toggleCollectMode
print("Toggle: Ctrl+Shift+C | badge at #bn-collect-badge shows count")
