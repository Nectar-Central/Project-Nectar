"""
02 — Fast pre-filter: should we send this packet to server?
Checks only 3 bytes, no full decrypt.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_api import Photon

samples = ['f302e601', 'f404ad01', 'f3046502']
for hx in samples:
    print(hx[:8], 'essential?', Photon.is_essential(hx))
