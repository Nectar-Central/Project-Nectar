"""
35 — Collect: convert JSON capture to YAML logs via logger
"""
import sys, json, yaml
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.logger import log_engine_json, log_engine_yaml

p=Path("logs/photon-capture-123.json")
if p.exists():
    for rec in json.loads(p.read_text())["packets"][:3]:
        log_engine_json("replay", "sess_yaml", rawHex=rec["rawHex"][:8])
        log_engine_yaml("replay", "sess_yaml", rawHex=rec["rawHex"][:8])
else:
    log_engine_json("demo","sess_yaml", msg="no file")
