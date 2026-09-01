"""
11 — Death vs kill: compare killer/victim to view.me.username
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from server.lib.photon_engine.models import Player, SessionView

sid='sess_kd'
view = SessionView(session_id=sid)
view.me = Player(actor_nr=1, username='jew6led', is_self=True)
view.players[1]=view.me

pkt_dict = {'packet':'kill','is_kill':True,'killer':'Tiktok: angelseg_','victim':'jew6led','kill':{'instigator':'Tiktok: angelseg_','instigated':'jew6led','event_type':'killed'}}
if pkt_dict['victim']==view.me.username:
    print('you died')
elif pkt_dict['killer']==view.me.username:
    print('you killed')
