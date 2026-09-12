'use strict';

/**
 * Comp intake — host-side match recorder for tourneys/scrims.
 *
 * Consumes decoded packets ({rec, msg}, the exact shape updatePresenceFromPacket
 * in decryptor.js receives) and banks matches into Supabase bngc_comp_* tables:
 *
 *   stG == 1  -> open a match (per room)
 *   stG == 3  -> seal the open match (kills/knocks/alv/leaves included)
 *   stG == 2  -> respawn time, ignored
 *
 * Roster: actorNr -> name from Join ev 255 (255/skid props) + ActorList seeds.
 * Discord mapping: game_username -> discord_id via bngc_users at seal time;
 * twin names (2+ actors, same name, unresolvable) stay null — never guess.
 *
 * Zero dependencies (global fetch for Supabase REST). Never throws into the
 * caller: every entry point is wrapped. Pure logic (tracker + mapping +
 * killfeed parse) is unit-tested via node --test (see comp-intake.test.js).
 */

const STG_START = 1;    // open match
const STG_RESPAWN = 2;  // ignored
const STG_END = 3;      // seal match

const DEFAULT_POINTS = { kill: 1, win: 3, placement: [10, 7, 5, 3, 1] };

function stripTags(s) {
  try {
    return String(s == null ? '' : s).replace(/<[^>]*>/g, '').trim();
  } catch { return ''; }
}

function normName(n) {
  const s = stripTags(n);
  if (/^dc:/i.test(s)) return s.slice(3).trim();
  return s;
}

// Minimal killfeed parse (mirrors decryptor.js parseKillfeedText contract):
// "A killed B" / "A eliminated B" -> kill, "A knocked B" -> knock.
function parseKillfeed(raw) {
  const t = stripTags(raw);
  if (!t) return { type: 'other', killer: null, victim: null };
  let m = t.match(/^(.+?)\s+(killed|eliminated)\s+(.+)$/i);
  if (m) return { type: 'kill', killer: m[1].trim(), victim: m[3].trim() };
  m = t.match(/^(.+?)\s+knocked\s+(.+)$/i);
  if (m) return { type: 'knock', killer: m[1].trim(), victim: m[2].trim() };
  return { type: 'other', killer: null, victim: null };
}

function num(v) {
  try {
    const n = Number(v && typeof v === 'object' && 'value' in v ? v.value : v);
    return Number.isFinite(n) ? n : null;
  } catch { return null; }
}

function paramVal(params, key) {
  try {
    const p = (params || []).find((x) => x && x.keyName === key);
    if (!p || !p.value) return undefined;
    return p.value && typeof p.value === 'object' && 'value' in p.value ? p.value.value : p.value;
  } catch { return undefined; }
}

// ---------------------------------------------------------------------------
// Tracker (pure except sink calls, which are fire-and-forget safe)
// ---------------------------------------------------------------------------

function createTracker(opts) {
  opts = opts || {};
  const sink = opts.sink || null;
  const log = opts.log || (() => {});
  const st = {
    tourneyId: opts.tourneyId != null ? opts.tourneyId : null,
    tourneyCode: null, // 5-char room code of the tracked tourney (null = codeless/manual: no room gate)
    room: null,
    publicRoom: false, // pid-less JoinRandomRoom: public matchmaking, comp checks off until the next real join
    roster: new Map(), // actorNr -> {name}
    selfName: null,    // local player (first outbound PlayerProperties 255)
    hostPlaying: true, // refreshed from the tourney row on setTourney
    openMatch: null,   // {version, room, openedAt, events[]}
    nextVersion: 0,
    sealed: 0,
    dropped: 0,
    pending: [],       // sealed matches waiting for a tourney id (cap)
  };

  function rosterName(nr) {
    try {
      const e = st.roster.get(Number(nr));
      return e ? e.name : null;
    } catch { return null; }
  }

  function isHost(name) {
    try {
      return !!(st.selfName && name && normName(name) === st.selfName);
    } catch { return false; }
  }

  function ensureActor(nr, name) {
    try {
      nr = Number(nr);
      if (!Number.isFinite(nr)) return;
      const cur = st.roster.get(nr);
      if (!cur) st.roster.set(nr, { name: name || `Player ${nr}` });
      else if (name && cur.name.startsWith('Player ')) cur.name = name;
      else if (name && !cur.name) cur.name = name;
    } catch {}
  }

  function setRoom(room) {
    try {
      room = room ? String(room) : null;
      if (room && room !== st.room) {
        st.room = room;
        st.publicRoom = false; // a real join ends public state
        st.roster = new Map();
        log(`comp room -> ${room.slice(0, 8)}`);
        // auto-tourney: an open tourney tagged with this room code takes
        // over tracking — no COMP_TOURNEY_ID needed per night. Only when the
        // code maps to exactly one open tourney; manual Track always wins
        // (it runs after and overwrites).
        if (/^[A-Z0-9]{5}$/i.test(room) && sink && typeof sink.tourneyByCode === 'function') {
          const code = room.toUpperCase();
          Promise.resolve()
            .then(() => sink.tourneyByCode(code))
            .then((tid) => {
              if (tid == null) return;
              if (st.tourneyId !== null && String(st.tourneyId) === String(tid)) return;
              log(`comp room ${code} belongs to tourney #${tid} — auto-tracking`);
              return setTourney(Number(tid));
            })
            .catch(() => {});
        }
      }
    } catch {}
  }

  function openMatch(manual) {
    try {
      if (st.openMatch) return st.openMatch;
      // series complete: no new auto-opens (manual blanks bypass).
      if (!manual && st.seriesLen > 0 && st.seriesSealed >= st.seriesLen) {
        log(`comp match suppressed: series complete (${st.seriesSealed}/${st.seriesLen})`);
        return null;
      }
      // room gate: a coded tourney only scores its own room. Playing
      // elsewhere (public matchmaking, another room) must not open — and
      // therefore never seal — into the tracked tourney. Codeless tourneys
      // and explicit manual opens bypass the gate. Unknown room fails open
      // (missed join packet must not eat a real match).
      if (!manual && st.publicRoom) {
        log('comp match suppressed: public room');
        return null;
      }
      if (!manual && st.tourneyCode && st.room && String(st.room).toUpperCase() !== st.tourneyCode) {
        log(`comp match suppressed: room ${st.room} != tourney room ${st.tourneyCode}`);
        return null;
      }
      st.openMatch = {
        version: st.nextVersion,
        room: st.room,
        manual: !!manual,
        openedAt: new Date().toISOString(),
        events: [],
        leaves: [], // actorNames seen leaving (event 254) — auto-removed at seal
      };
      log(`match v${st.openMatch.version} OPEN${manual ? ' (manual)' : ''} room=${(st.room || '?').slice(0, 8)}${st.tourneyCode ? ' code=' + st.tourneyCode : ''}`);
      return st.openMatch;
    } catch { return null; }
  }

  function pushEvent(etype, fields) {
    try {
      if (!st.openMatch) return;
      st.openMatch.events.push(Object.assign({ etype, t: new Date().toISOString() }, fields || {}));
    } catch {}
  }

  async function sealMatch(manual) {
    let m = null;
    try {
      m = st.openMatch;
      st.openMatch = null;
      if (!m) return null;
      st.nextVersion = Math.max(st.nextVersion, m.version + 1);
    } catch { return null; }
    // public room: drop the stale open match instead of sealing it with
    // public-match events (left the tourney room without a clean stG 3).
    // Manual seals bypass.
    if (!manual && st.publicRoom) {
      try { log(`comp seal suppressed: public room (dropping stale v${m.version})`); } catch {}
      return null;
    }
    if (st.tourneyId == null) {
      try {
        st.pending.push(m);
        if (st.pending.length > 50) st.pending.shift();
        log(`match v${m.version} sealed, buffered (no tourney set)${st.tourneyCode ? ' code=' + st.tourneyCode : ''}`);
      } catch {}
      return m;
    }
    try {
      if (sink && typeof sink.saveMatch === 'function') {
        await sink.saveMatch({
          tourneyId: st.tourneyId,
          version: m.version,
          room: m.room,
          manual: !!manual || !!m.manual,
          events: m.events,
          leaves: m.leaves || [],
        });
        st.sealed++;
        try { st.seriesSealed = (st.seriesSealed || 0) + 1; } catch {}
        // summary log: kills + winner
        const kills = m.events.filter(e => e.etype === 'kill').length;
        const alive = m.events.filter(e => e.etype === 'alv' && !e.alive);
        const winner = alive.length > 0 ? alive[alive.length - 1].actorName : null;
        log(`match v${m.version} SEALED — kills=${kills} winner=${winner || '?'}`);
      } else {
        st.dropped++;
      }
    } catch (e) {
      try { st.dropped++; log(`comp seal failed v${m.version}: ${e && e.message}`); } catch {}
    }
    return m;
  }

  function onStg(v) {
    try {
      const n = num(v);
      log(`stG raw=${JSON.stringify(v)} parsed=${n}`);
      if (n === STG_START) openMatch(false);
      else if (n === STG_END && st.openMatch) void sealMatch(false);
      // STG_RESPAWN (2) and everything else: ignored by design
    } catch (e) { log(`stG error: ${e && e.message}`); }
  }

  function handle(rec, msg) {
    try {
      if (!msg) return;
      try {
        const kind = msg.req ? `req:${msg.req.opName}` : msg.ev ? `ev:${msg.ev.evCode}` : '?';
        log(`handle ${kind} dir=${rec && rec.dir}`);
      } catch {}
      // room tracking from create/join ops
      if (msg.req && (msg.req.opName === 'CreateRoom' || msg.req.opName === 'JoinRoom' || msg.req.opName === 'JoinRandomRoom')) {
        const isRandom = msg.req.opName === 'JoinRandomRoom';
        const rn = paramVal(msg.req.params, 'RoomName');
        const code = rn && typeof rn === 'object' && 'value' in rn ? rn.value : rn;
        // Prefer the 5-char pID (private rooms): it matches tourney codes
        // and server short_codes. Falls back to RoomName.
        let pid = null;
        try {
          const gp = paramVal(msg.req.params, 'GameProperties') || {};
          const raw = gp && gp.pID !== undefined ? gp.pID : null;
          const v = raw && typeof raw === 'object' && 'value' in raw ? raw.value : raw;
          if (typeof v === 'string' && /^[A-Z0-9]{5}$/i.test(v.trim())) pid = v.trim().toUpperCase();
        } catch {}
        if (pid) setRoom(pid);
        // JoinRandomRoom without a pID is public matchmaking: ignore it
        // completely (no RoomName fallback) AND mark the public state so
        // auto open/seal stay off until the next real join. A random uuid
        // would flip the tracker out of the tourney room and wipe the
        // roster for a room that can never score.
        else if (isRandom) {
          st.publicRoom = true;
          log('comp public room - comp checks off');
        }
        else if (code) setRoom(String(code));
        // self identity: the FIRST outbound PlayerProperties 255 is this box.
        // (Host-skip for non-playing hosts keys off this name.)
        if (rec && rec.dir === 'out' && !st.selfName) {
          try {
            const pp = paramVal(msg.req.params, 'PlayerProperties');
            const nm = pp && pp['255'] && pp['255'].value !== undefined ? stripTags(pp['255'].value) : null;
            if (nm) {
              st.selfName = normName(nm) || nm;
              log(`comp self = ${st.selfName}`);
            }
          } catch {}
        }
      }
      if (msg.resp) {
        const al = paramVal(msg.resp.params, 'ActorList');
        const arr = al && al.value !== undefined ? al.value : al;
        if (Array.isArray(arr)) arr.forEach((nr) => ensureActor(nr, null));
      }
      if (!msg.ev) return;
      const code = msg.ev.evCode;
      if (code === 255) { // Join
        const actorNr = num(paramVal(msg.ev.params, 'ActorNr'));
        const props = paramVal(msg.ev.params, 'Properties') || paramVal(msg.ev.params, 'PlayerProperties') || {};
        const name = props ? stripTags(props['255'] && props['255'].value !== undefined ? props['255'].value : props['255']) : null;
        if (actorNr != null) ensureActor(actorNr, name || null);
      } else if (code === 254) { // Leave
        const actorNr = num(paramVal(msg.ev.params, 'ActorNr'));
        const nm = actorNr != null ? rosterName(actorNr) : null;
        pushEvent('leave', { actorNr, actorName: nm });
        // auto-remove: remember the leaver so seal excludes them from the
        // roster (host can add back via /tourney add-back or dashboard).
        try {
          if (st.openMatch && nm) {
            st.openMatch.leaves = st.openMatch.leaves || [];
            if (!st.openMatch.leaves.includes(nm)) st.openMatch.leaves.push(nm);
          }
        } catch {}
      } else if (code === 253) { // Properties: stG / alv / curScn...
        const props = paramVal(msg.ev.params, 'Properties') || {};
        try { log(`253 props keys=${props && typeof props === 'object' ? Object.keys(props).join(',') : typeof props}`); } catch {}
        if (props && typeof props === 'object') {
          if ('stG' in props) onStg(props.stG && props.stG.value !== undefined ? props.stG.value : props.stG);
          if ('alv' in props) {
            const actorNr = num(paramVal(msg.ev.params, 'ActorNr')) ?? num(paramVal(msg.ev.params, 'TargetActorNr'));
            const nm = actorNr != null ? rosterName(actorNr) : null;
            if (!st.hostPlaying && isHost(nm)) return; // spectating host: no alv rows
            const alive = props.alv && props.alv.value !== undefined ? !!props.alv.value : !!props.alv;
            pushEvent('alv', { actorNr, actorName: nm, alive });
          }
        }
      } else if (code === 101) { // Killfeed
        let text = null;
        for (const p of (msg.ev.params || [])) {
          try {
            const v = p && p.value && typeof p.value === 'object' && 'value' in p.value ? p.value.value : p && p.value;
            if (typeof v === 'string' && v.trim()) { text = v; break; }
          } catch {}
        }
        if (text) {
          const kf = parseKillfeed(text);
          if (kf.type === 'kill' || kf.type === 'knock') {
            // non-playing host spectates: their own kills/knocks are admin
            // noise, never scoreboard rows (tourney.host_playing == false).
            if (!st.hostPlaying && isHost(kf.killer)) return;
            // resolve actor numbers by name for the record (best effort)
            let actorNr = null;
            try {
              for (const [nr, e] of st.roster) {
                if (normName(e.name) && normName(e.name) === normName(kf.killer)) { actorNr = nr; break; }
              }
            } catch {}
            pushEvent(kf.type === 'kill' ? 'kill' : 'knock',
              { actorNr, actorName: kf.killer, targetName: kf.victim });
            try { log(`kill ${kf.killer} → ${kf.victim}`); } catch {}
          }
        }
      }
    } catch {}
  }

  async function setTourney(id) {
    try {
      st.tourneyId = id;
      st.nextVersion = 0;
      st.hostPlaying = true;
      st.tourneyCode = null;
      st.seriesLen = 0;
      st.seriesSealed = 0;
      if (sink && typeof sink.nextVersion === 'function' && id != null) {
        try { st.nextVersion = await sink.nextVersion(id); } catch {}
      }
      if (sink && typeof sink.getTourney === 'function' && id != null) {
        try {
          const t = await sink.getTourney(id);
          if (t && t.host_playing === false) st.hostPlaying = false;
          if (t && t.code && /^[A-Z0-9]{5}$/i.test(String(t.code).trim())) {
            st.tourneyCode = String(t.code).trim().toUpperCase();
          }
          if (t && Number(t.series_len) > 0) st.seriesLen = Number(t.series_len);
        } catch {}
        try {
          if (sink && typeof sink.sealedCount === 'function') {
            st.seriesSealed = await sink.sealedCount(id);
          }
        } catch {}
      }
      // flush buffered matches
      const pend = st.pending.splice(0, st.pending.length);
      for (const m of pend) {
        try {
          if (sink && typeof sink.saveMatch === 'function') {
            await sink.saveMatch({ tourneyId: id, version: m.version, room: m.room, manual: !!m.manual, events: m.events, leaves: m.leaves || [] });
            st.sealed++;
          }
        } catch {}
      }
      return true;
    } catch { return false; }
  }

  function status() {
    try {
      return {
        tourneyId: st.tourneyId, tourneyCode: st.tourneyCode, room: st.room,
        public: st.publicRoom,
        open: st.openMatch ? { version: st.openMatch.version, events: st.openMatch.events.length, manual: !!st.openMatch.manual } : null,
        nextVersion: st.nextVersion, sealed: st.sealed, dropped: st.dropped,
        buffered: st.pending.length, roster: st.roster.size,
      };
    } catch { return {}; }
  }

  // A preset tourney id (COMP_TOURNEY_ID) loads its row immediately so the
  // room gate, host flag and version counter apply from the first packet.
  // setTourney never rejects (false on failure), so this is fire-and-forget.
  if (st.tourneyId != null) {
    try { setTourney(st.tourneyId); } catch {}
  }

  return { handle, status, setTourney, openManual: () => openMatch(true), sealManual: () => sealMatch(true), _state: st };
}

// ---------------------------------------------------------------------------
// Discord mapping (pure): game names -> discord_ids, twins fail closed
// ---------------------------------------------------------------------------

function mapNamesToDiscord(names, usersByGame) {
  // usersByGame: {game_username: discord_id} (may hold several dids per name
  // only if the caller passes arrays — duplicate accounts never auto-map).
  const out = {};
  try {
    const seen = {};
    for (const n of (names || [])) {
      const k = normName(n);
      if (!k) continue;
      seen[k] = (seen[k] || 0) + 1;
    }
    for (const n of (names || [])) {
      const k = normName(n);
      if (!k || out[n] !== undefined) continue;
      if ((seen[k] || 0) > 1) { out[n] = null; continue; } // twins: no guess
      let did = null;
      try {
        for (const [g, d] of Object.entries(usersByGame || {})) {
          if (normName(g) === k) {
            if (Array.isArray(d)) { did = null; break; } // duplicate accounts
            did = did === null ? d : (did === d ? d : null);
            if (did === null && d !== did) break;
          }
        }
      } catch { did = null; }
      out[n] = did || null;
    }
  } catch {}
  return out;
}

// ---------------------------------------------------------------------------
// Supabase REST sink (fetch only, zero-dep). Reads env lazily so tests and
// the decryptor never need credentials at import time.
// ---------------------------------------------------------------------------

function sbEnv() {
  try {
    const url = (process.env.SUPABASE_URL || '').replace(/\/$/, '');
    const key = process.env.SUPABASE_SERVICE_ROLE_KEY || '';
    if (!url || !key) return null;
    return { url, key };
  } catch { return null; }
}

async function sbReq(method, path, body) {
  const env = sbEnv();
  if (!env) throw new Error('SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing');
  const res = await fetch(`${env.url}/rest/v1${path}`, {
    method,
    headers: {
      apikey: env.key,
      Authorization: `Bearer ${env.key}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`supabase ${res.status} ${await res.text().catch(() => '')}`.slice(0, 200));
  const txt = await res.text();
  return txt ? JSON.parse(txt) : null;
}

function createSupabaseSink() {
  return {
    async getTourney(tourneyId) {
      try {
        const rows = await sbReq('GET', `/bngc_comp_tourneys?select=id,host_playing,code,series_len&id=eq.${encodeURIComponent(tourneyId)}&limit=1`);
        return (rows && rows[0]) || null;
      } catch { return null; }
    },
    async sealedCount(tourneyId) {
      try {
        const rows = await sbReq('GET', `/bngc_comp_matches?select=id&tourney_id=eq.${encodeURIComponent(tourneyId)}&status=in.(sealed,manual)&limit=500`);
        return (rows || []).length;
      } catch { return 0; }
    },
    async tourneyByCode(code) {
      // exactly one NON-ENDED tourney carrying this room code -> its id, else null
      // (ambiguous or absent: caller keeps current tracking, never guesses).
      // NOTE: statuses are created/scheduled/started/ended since v232 —
      // 'open' no longer exists, so filter neq ended, not eq open.
      try {
        const rows = await sbReq('GET', `/bngc_comp_tourneys?select=id&code=eq.${encodeURIComponent(String(code || '').toUpperCase())}&status=neq.ended&limit=2`);
        if (rows && rows.length === 1 && rows[0].id != null) return Number(rows[0].id);
        return null;
      } catch { return null; }
    },
    async nextVersion(tourneyId) {
      try {
        const rows = await sbReq('GET', `/bngc_comp_matches?select=version&tourney_id=eq.${encodeURIComponent(tourneyId)}&order=version.desc&limit=1`);
        const top = rows && rows[0] ? Number(rows[0].version) : -1;
        return Number.isFinite(top) ? top + 1 : 0;
      } catch { return 0; }
    },
    async resolveNames(names) {
      // batch game_username lookup (PostgREST in-list)
      const uniq = [...new Set((names || []).map(normName).filter(Boolean))].slice(0, 100);
      if (!uniq.length) return {};
      const list = uniq.map((n) => `"${n.replace(/"/g, '')}"`).join(',');
      const rows = await sbReq('GET', `/bngc_users?select=discord_id,game_username&game_username=in.(${encodeURIComponent(list)})`);
      const byGame = {};
      for (const r of (rows || [])) {
        if (r && r.game_username && r.discord_id) byGame[r.game_username] = r.discord_id;
      }
      return mapNamesToDiscord(names, byGame);
    },
    async saveMatch({ tourneyId, version, room, manual, events, leaves }) {
      const names = [];
      for (const e of (events || [])) {
        if (e.actorName) names.push(e.actorName);
        if (e.targetName) names.push(e.targetName);
      }
      for (const n of (leaves || [])) names.push(n);
      let mapping = {};
      try { mapping = await this.resolveNames(names); } catch {}
      const mrows = await sbReq('POST', '/bngc_comp_matches',
        { tourney_id: tourneyId, version, room_id: room, status: manual ? 'manual' : 'sealed', sealed_at: new Date().toISOString() });
      const mid = mrows && mrows[0] && mrows[0].id;
      if (!mid) throw new Error('match insert returned no id');
      const erows = (events || []).map((e) => ({
        match_id: mid,
        etype: e.etype,
        actor_nr: e.actorNr != null ? e.actorNr : null,
        actor_name: e.actorName || null,
        target_name: e.targetName || null,
        discord_id: (e.actorName && mapping[e.actorName]) || null,
        alive: e.alive != null ? !!e.alive : null,
      }));
      // chunked insert (100/req)
      for (let i = 0; i < erows.length; i += 100) {
        await sbReq('POST', '/bngc_comp_events', erows.slice(i, i + 100));
      }
      // auto-removed leavers: resolve leave names -> dids, store on the match
      // (best effort; pre-migration DBs ignore the column).
      try {
        const removed = [...new Set((leaves || []).map((n) => mapping[n]).filter(Boolean))];
        if (removed.length) {
          await sbReq('PATCH', `/bngc_comp_matches?id=eq.${encodeURIComponent(mid)}`, { removed_discord_ids: removed });
        }
      } catch {}
      return mid;
    },
  };
}

module.exports = {
  STG_START, STG_RESPAWN, STG_END, DEFAULT_POINTS,
  createTracker, mapNamesToDiscord, parseKillfeed, normName, stripTags,
  createSupabaseSink, sbEnv,
};
