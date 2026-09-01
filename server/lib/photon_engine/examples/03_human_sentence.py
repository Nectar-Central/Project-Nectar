"""
03 — Human sentence: one-liner for logs
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_api import Photon

for hx in ['f302e601', 'f404ad01']:
    print(Photon.human(hx))
