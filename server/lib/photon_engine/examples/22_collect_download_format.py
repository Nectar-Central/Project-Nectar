"""
22 — Collect: download format (Ctrl+Shift+S)
JSON: {meta:{capturedAt,game,totalPackets,sessionId}, packets:[{rawHex,seq,dir,url,t,label}]}
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
sample = {"meta":{"capturedAt":"2026-09-01T01:36:11Z","game":"BuildNow GG","totalPackets":2,"sessionId":"sess_1a0"},"packets":[{"rawHex":"f302e601","seq":1,"dir":"out","url":"ns.exitgames.com"},{"rawHex":"f404ad01","seq":2,"dir":"in","url":"gcams1.exitgames.com"}]}
print(json.dumps(sample, indent=2))
print("Saved as logs/photon-capture-<ts>.json via downloadCollected()")
