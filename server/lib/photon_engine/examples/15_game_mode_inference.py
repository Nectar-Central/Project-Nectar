"""
15 — GameMode inference from curScn or gm
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_api import GameMode

for s in ['MainWorld','ZoneWars','Lategame','WildWest','Boxfights 2v2']:
    print(s, '->', GameMode.from_curscn(s))
