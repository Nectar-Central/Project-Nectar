"""
34 — Collect: merge two capture files for longer test
"""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
files=[Path("logs/photon-capture-1.json"), Path("logs/photon-capture-2.json")]
merged=[]
for f in files:
    if f.exists():
        merged.extend(json.loads(f.read_text())["packets"])
print(f"merged {len(merged)} packets")
Path("logs/merged.json").write_text(json.dumps({"meta":{"merged":True},"packets":merged}))
print("wrote logs/merged.json")
