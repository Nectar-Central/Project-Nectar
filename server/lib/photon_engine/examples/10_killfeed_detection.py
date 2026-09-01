"""
10 — Killfeed: pkt.is_kill / pkt.killer / pkt.victim
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_api import Photon

pkt = Photon.parse('f4046501')  # dummy
if pkt.is_kill:
    print(f'{pkt.killer} killed {pkt.victim}')
else:
    print('not a kill, type', pkt.packet_type)
    print('to_dict:', pkt.to_dict())
