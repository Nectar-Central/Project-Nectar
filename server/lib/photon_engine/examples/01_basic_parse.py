"""
01 — Basic parse: hex → PhotonPacket
Run: python -m server.lib.photon_engine.examples.01_basic_parse
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_api import Photon

hex_str = 'f302e601'  # truncated — in prod comes from rec["rawHex"]
pkt = Photon.parse(hex_str)
print(f'type={pkt.packet_type.value} kill={pkt.is_kill} sentence={pkt.sentence}')
print('dict:', pkt.to_dict())
