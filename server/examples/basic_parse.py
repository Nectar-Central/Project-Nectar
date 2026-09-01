"""
basic_parse.py — Simplest use case: parse one hex packet.

You have a hex string from WSS (e.g. from userscript records[i].rawHex)
and you just want to know what it is, with real field names.

No session, no state, no DB.

Run either way:
  python -m server.examples.basic_parse
  python server/examples/basic_parse.py
"""

import sys
from pathlib import Path

# Allow running as `python server/examples/basic_parse.py` directly
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.lib.photon_api import Photon
from server.lib.photon_engine.enums import GameMode, PacketType

# Example hex — truncated, will be UNKNOWN, but shows the pattern
# In real use, this comes from rec["rawHex"] in POST /api/capture
hex_samples = [
    "f302e601",  # truncated Authenticate (will parse as UNKNOWN, but is_essential will be True)
]

for hx in hex_samples:
    # One-liner: hex -> human sentence or None if noise
    print(f"Human: {Photon.human(hx)}")

    # Full packet object with real names
    pkt = Photon.parse(hx)

    print(f"  raw_hex: {pkt.raw_hex[:16]}...")
    print(f"  packet_type: {pkt.packet_type}")  # e.g. PacketType.AUTHENTICATE, not 230
    print(f"  game_mode: {pkt.game_mode} -> {pkt.gm_human}")
    print(f"  is_kill: {pkt.is_kill}, killer={pkt.killer}, victim={pkt.victim}")
    print(f"  is_useful: {pkt.is_useful}, sentence: {pkt.sentence}")
    print()

# Fast pre-filter (no parse, just 3-byte check) — use before sending to server
print("Is essential?", Photon.is_essential("f302e601"))  # True for auth
print("Is essential?", Photon.is_essential("f3046502"))  # depends on code
