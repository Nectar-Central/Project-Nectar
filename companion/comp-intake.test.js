'use strict';
// node --test companion/comp-intake.test.js
const { test } = require('node:test');
const assert = require('node:assert/strict');

const comp = require('./comp-intake');

function stg(v) {
  return { ev: { evName: 'PropertiesChanged', evCode: 253, params: [
    { keyName: 'Properties', value: { value: { stG: { value: v } } } },
  ] } };
}
function joinEv(actorNr, name) {
  return { ev: { evName: 'Join', evCode: 255, params: [
    { keyName: 'ActorNr', value: { value: actorNr } },
    { keyName: 'Properties', value: { value: { 255: { value: name } } } },
  ] } };
}
function leaveEv(actorNr) {
  return { ev: { evName: 'Leave', evCode: 254, params: [
    { keyName: 'ActorNr', value: { value: actorNr } },
  ] } };
}
function kfEv(text) {
  return { ev: { evName: 'Killfeed', evCode: 101, params: [
    { keyName: 'Data', value: { value: text } },
  ] } };
}
function alvEv(actorNr, alive) {
  return { ev: { evName: 'PropertiesChanged', evCode: 253, params: [
    { keyName: 'ActorNr', value: { value: actorNr } },
    { keyName: 'Properties', value: { value: { alv: { value: alive } } } },
  ] } };
}

function memSink() {
  const saved = [];
  return {
    saved,
    async saveMatch(m) { saved.push(m); return saved.length; },
    async nextVersion() { return saved.length; },
    async resolveNames(names) {
      const out = {};
      for (const n of (names || [])) out[n] = `did:${n}`;
      return out;
    },
  };
}

test('stG 1 opens, events bank, stG 3 seals with version', async () => {
  const sink = memSink();
  const t = comp.createTracker({ sink, tourneyId: 0, log: () => {} });
  t.handle(null, joinEv(1, 'Alice'));
  t.handle(null, joinEv(2, 'Bob'));
  t.handle(null, stg(1));
  t.handle(null, kfEv('<color>Alice</color> killed <color>Bob</color>'));
  t.handle(null, alvEv(2, false));
  t.handle(null, leaveEv(2));
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 1);
  const m = sink.saved[0];
  assert.equal(m.version, 0);
  assert.equal(m.tourneyId, 0);
  const kinds = m.events.map((e) => e.etype);
  assert.deepEqual(kinds, ['kill', 'alv', 'leave']);
  assert.equal(m.events[0].actorName, 'Alice');
  assert.equal(m.events[0].targetName, 'Bob');
  assert.equal(m.events[1].alive, false);
});

test('stG 2 ignored, versions increment', async () => {
  const sink = memSink();
  const t = comp.createTracker({ sink, tourneyId: 3, log: () => {} });
  t.handle(null, stg(1));
  t.handle(null, stg(2));
  t.handle(null, kfEv('A knocked B'));
  t.handle(null, stg(3));
  t.handle(null, stg(1));
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 2);
  assert.equal(sink.saved[0].version, 0);
  assert.equal(sink.saved[1].version, 1);
  assert.equal(sink.saved[0].events[0].etype, 'knock');
});

test('manual open/seal without stG', async () => {
  const sink = memSink();
  const t = comp.createTracker({ sink, tourneyId: 0, log: () => {} });
  t.openManual();
  t.handle(null, kfEv('A killed B'));
  await t.sealManual();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 1);
  assert.equal(sink.saved[0].manual, true);
});

test('no tourney buffers, setTourney flushes', async () => {
  const sink = memSink();
  const t = comp.createTracker({ sink, log: () => {} });
  t.handle(null, stg(1));
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 0);
  assert.equal(t.status().buffered, 1);
  await t.setTourney(7);
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 1);
  assert.equal(sink.saved[0].tourneyId, 7);
});

test('twins fail closed in mapping', () => {
  const out = comp.mapNamesToDiscord(['Bob', 'Bob'], { Bob: 'did:1' });
  assert.equal(out.Bob, null);
  const out2 = comp.mapNamesToDiscord(['Alice'], { Alice: 'did:1' });
  assert.equal(out2.Alice, 'did:1');
});

test('killfeed parse', () => {
  assert.deepEqual(comp.parseKillfeed('A killed B'), { type: 'kill', killer: 'A', victim: 'B' });
  assert.deepEqual(comp.parseKillfeed('A eliminated B'), { type: 'kill', killer: 'A', victim: 'B' });
  assert.deepEqual(comp.parseKillfeed('A knocked B'), { type: 'knock', killer: 'A', victim: 'B' });
  assert.equal(comp.parseKillfeed('zone closing').type, 'other');
});

test('never throws on garbage', () => {
  const t = comp.createTracker({ sink: memSink(), log: () => {} });
  t.handle(null, null);
  t.handle(null, {});
  t.handle(null, { ev: { evCode: 253, params: null } });
  t.handle(null, { ev: { evCode: 101, params: [{ keyName: 'Data', value: {} }] } });
  assert.equal(t.status().sealed, 0);
});

function joinReq(name) {
  return { req: { opName: 'JoinRoom', opCode: 226, params: [
    { keyName: 'PlayerProperties', value: { value: { 255: { value: name } } } },
  ] } };
}

test('non-playing host kills and alv skipped', async () => {
  const sink = memSink();
  const t = comp.createTracker({ sink, tourneyId: 0, log: () => {} });
  t.handle({ dir: 'out' }, joinReq('HostGuy'));
  // flip to spectating (as setTourney would from the tourney row)
  t._state.hostPlaying = false;
  t.handle(null, stg(1));
  t.handle(null, kfEv('HostGuy killed Alice'));
  t.handle(null, kfEv('Alice killed Bob'));
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 1);
  const kinds = sink.saved[0].events.map((e) => (e.actorName || '') + ':' + e.etype);
  assert.deepEqual(kinds, ['Alice:kill']);
});

test('playing host (default) keeps everything', async () => {
  const sink = memSink();
  const t = comp.createTracker({ sink, tourneyId: 0, log: () => {} });
  t.handle({ dir: 'out' }, joinReq('HostGuy'));
  t.handle(null, stg(1));
  t.handle(null, kfEv('HostGuy killed Alice'));
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved[0].events.length, 1);
});

function joinRoomPid(pid) {
  return { req: { opName: 'JoinRoom', opCode: 226, params: [
    { keyName: 'RoomName', value: { value: 'uuid-room-1' } },
    { keyName: 'GameProperties', value: { value: { pID: { value: pid } } } },
  ] } };
}
function joinRandomRoomNoPid() {
  return { req: { opName: 'JoinRandomRoom', opCode: 225, params: [
    { keyName: 'RoomName', value: { value: 'uuid-random-9' } },
  ] } };
}
function codedSink(code) {
  const sink = memSink();
  sink.getTourney = async () => ({ id: 4, host_playing: true, code });
  sink.nextVersion = async () => 0;
  return sink;
}

test('pID room auto-tracks the open tourney with that code', async () => {
  let tracked = null;
  const sink = memSink();
  sink.tourneyByCode = async (code) => (code === 'ABCDE' ? 4 : null);
  const t = comp.createTracker({ sink, log: () => {} });
  t.handle({ dir: 'out' }, joinRoomPid('ABCDE'));
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(t.status().tourneyId, 4);
  assert.equal(t.status().room, 'ABCDE');
});

test('ambiguous or missing code never auto-tracks', async () => {
  const sink = memSink();
  sink.tourneyByCode = async () => null;
  const t = comp.createTracker({ sink, log: () => {} });
  t.handle({ dir: 'out' }, joinRoomPid('ZZZ99'));
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(t.status().tourneyId, null);
  assert.equal(t.status().room, 'ZZZ99');
});

test('tourneyByCode matches any non-ended status (v232 lifecycle)', async () => {
  // regression: the filter was status=eq.open, but 'open' no longer exists
  // (created/scheduled/started/ended) so auto-track never fired.
  const calls = [];
  const realFetch = globalThis.fetch;
  const oldUrl = process.env.SUPABASE_URL;
  const oldKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
  process.env.SUPABASE_URL = 'https://example.supabase.co';
  process.env.SUPABASE_SERVICE_ROLE_KEY = 'dummy';
  globalThis.fetch = async (url) => {
    calls.push(String(url));
    return { ok: true, text: async () => JSON.stringify([{ id: 7 }]) };
  };
  try {
    const sink = comp.createSupabaseSink();
    assert.equal(await sink.tourneyByCode('GZ8GU'), 7);
    assert.equal(calls.length, 1);
    assert.match(calls[0], /status=neq\.ended/);
    assert.doesNotMatch(calls[0], /status=eq\.open/);
    // ambiguous: two rows -> null, never guess
    globalThis.fetch = async () => ({ ok: true, text: async () => JSON.stringify([{ id: 7 }, { id: 8 }]) });
    assert.equal(await sink.tourneyByCode('GZ8GU'), null);
  } finally {
    globalThis.fetch = realFetch;
    if (oldUrl === undefined) delete process.env.SUPABASE_URL; else process.env.SUPABASE_URL = oldUrl;
    if (oldKey === undefined) delete process.env.SUPABASE_SERVICE_ROLE_KEY; else process.env.SUPABASE_SERVICE_ROLE_KEY = oldKey;
  }
});

test('JoinRandomRoom without pID is ignored', async () => {
  const sink = memSink();
  const t = comp.createTracker({ sink, log: () => {} });
  t.handle({ dir: 'out' }, joinRoomPid('ABCDE'));
  t.handle(null, joinEv(1, 'Alice'));
  assert.equal(t.status().room, 'ABCDE');
  assert.equal(t.status().roster, 1);
  t.handle({ dir: 'out' }, joinRandomRoomNoPid());
  assert.equal(t.status().room, 'ABCDE'); // untouched, no uuid flip
  assert.equal(t.status().roster, 1); // roster not wiped
});

test('coded tourney only scores its own room', async () => {
  const sink = codedSink('ABCDE');
  const t = comp.createTracker({ sink, log: () => {} });
  await t.setTourney(4);
  assert.equal(t.status().tourneyCode, 'ABCDE');
  // foreign room: stG boundaries open and seal nothing
  t.handle({ dir: 'out' }, joinRoomPid('ZZZ99'));
  t.handle(null, stg(1));
  assert.equal(t.status().open, null);
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 0);
  // own room: opens and seals into the tourney
  t.handle({ dir: 'out' }, joinRoomPid('ABCDE'));
  t.handle(null, stg(1));
  assert.ok(t.status().open);
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 1);
  assert.equal(sink.saved[0].tourneyId, 4);
});

test('manual open bypasses the room gate', async () => {
  const sink = codedSink('ABCDE');
  const t = comp.createTracker({ sink, log: () => {} });
  await t.setTourney(4);
  t.handle({ dir: 'out' }, joinRoomPid('ZZZ99'));
  t.openManual();
  assert.ok(t.status().open);
  await t.sealManual();
  assert.equal(sink.saved.length, 1);
});

test('codeless tourney scores any room', async () => {
  const sink = codedSink(null);
  const t = comp.createTracker({ sink, log: () => {} });
  await t.setTourney(5);
  assert.equal(t.status().tourneyCode, null);
  t.handle({ dir: 'out' }, joinRoomPid('ZZZ99'));
  t.handle(null, stg(1));
  assert.ok(t.status().open);
});

test('public room suppresses auto open and seal', async () => {
  const sink = codedSink('ABCDE');
  const t = comp.createTracker({ sink, log: () => {} });
  await t.setTourney(4);
  t.handle({ dir: 'out' }, joinRandomRoomNoPid());
  assert.equal(t.status().public, true);
  t.handle(null, stg(1));
  assert.equal(t.status().open, null);
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 0);
  // manual still works in public rooms (explicit host action)
  t.openManual();
  assert.ok(t.status().open);
  await t.sealManual();
  assert.equal(sink.saved.length, 1);
});

test('public seal drops a stale open match', async () => {
  const sink = codedSink('ABCDE');
  const t = comp.createTracker({ sink, log: () => {} });
  await t.setTourney(4);
  t.handle({ dir: 'out' }, joinRoomPid('ABCDE'));
  t.handle(null, stg(1));
  assert.ok(t.status().open);
  // left for public matchmaking without a clean end
  t.handle({ dir: 'out' }, joinRandomRoomNoPid());
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 0);
  assert.equal(t.status().open, null);
});

test('coded join clears public state', async () => {
  const sink = codedSink('ABCDE');
  const t = comp.createTracker({ sink, log: () => {} });
  await t.setTourney(4);
  t.handle({ dir: 'out' }, joinRandomRoomNoPid());
  assert.equal(t.status().public, true);
  t.handle({ dir: 'out' }, joinRoomPid('ABCDE'));
  assert.equal(t.status().public, false);
  t.handle(null, stg(1));
  assert.ok(t.status().open);
});

test('same-room rejoin after lobby hop clears public state', async () => {
  // host backout: ABCDE -> pid-less lobby -> same ABCDE. Tracking must resume
  // without a reload (publicRoom stuck true used to suppress opens forever).
  const sink = codedSink('ABCDE');
  const t = comp.createTracker({ sink, log: () => {} });
  await t.setTourney(4);
  t.handle({ dir: 'out' }, joinRoomPid('ABCDE'));
  t.handle(null, stg(1));
  assert.ok(t.status().open);
  await t.sealManual();
  assert.equal(sink.saved.length, 1);
  t.handle({ dir: 'out' }, joinRandomRoomNoPid());
  assert.equal(t.status().public, true);
  t.handle({ dir: 'out' }, joinRoomPid('ABCDE'));
  assert.equal(t.status().public, false);
  t.handle(null, stg(1));
  assert.ok(t.status().open);
});

// ---------------------------------------------------------------------------
// Ranked tracker (comp-merge recorder)
// ---------------------------------------------------------------------------

comp.rankedListRows = comp.rankedListRows || comp.rankedEventRows;

function memRankedSink(row) {
  const saved = [];
  const sealed = {};
  return {
    saved, sealed,
    roomRow: row || { id: 'R-X', short_code: 'ABCDE', host_status: 'is_host_playing' },
    async rankedRoomByCode(code) { return code === 'ABCDE' ? 'R-X' : null; },
    async getRankedRoom(rid) { return rid === 'R-X' ? this.roomRow : null; },
    async nextRankedVersion() { return saved.length; },
    async sealedRanked(rid, v) { return !!sealed[`${rid}:${v}`]; },
    async saveRankedMatch(m) { saved.push(m); return m.events.length; },
  };
}

function rankedJoinReq(pid, self) {
  const params = [{ keyName: 'GameProperties', value: { value: { pID: { value: pid } } } }];
  if (self) params.push({ keyName: 'PlayerProperties', value: { value: { 255: { value: self } } } });
  return { req: { opName: 'JoinRoom', params } };
}

test('ranked: stG 1 opens, banks, stG 3 seals with rid+version', async () => {
  const sink = memRankedSink();
  const t = comp.createRankedTracker({ sink, log: () => {} });
  await t.trackRid('R-X');
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle(null, joinEv(1, 'Host'));
  t.handle(null, joinEv(2, 'Guest'));
  t.handle(null, stg(1));
  assert.ok(t.status().open);
  t.handle(null, kfEv('Host killed Guest'));
  t.handle(null, alvEv(2, false));
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 1);
  const m = sink.saved[0];
  assert.equal(m.rid, 'R-X');
  assert.equal(m.version, 0);
  assert.deepEqual(m.events.map((e) => e.etype), ['kill', 'alv']);
  assert.equal(t.status().sealed, 1);
});

test('ranked: room gate suppresses foreign rooms, unbound suppresses all', async () => {
  const sink = memRankedSink();
  const t = comp.createRankedTracker({ sink, log: () => {} });
  t.handle(null, stg(1)); // never bound: no open
  assert.equal(t.status().open, null);
  await t.trackRid('R-X');
  t.handle({ dir: 'out' }, rankedJoinReq('OTHER', 'Host'));
  t.handle(null, stg(1));
  assert.equal(t.status().open, null); // bound code ABCDE != OTHER
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 0);
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle(null, stg(1));
  assert.ok(t.status().open);
});

test('ranked: auto-binds a live room by code', async () => {
  const sink = memRankedSink();
  const t = comp.createRankedTracker({ sink, log: () => {} });
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(t.status().rid, 'R-X');
  assert.equal(t.status().code, 'ABCDE');
});

test('ranked: already-sealed version is dropped, not banked', async () => {
  const sink = memRankedSink();
  sink.sealed['R-X:0'] = true;
  const t = comp.createRankedTracker({ sink, log: () => {} });
  await t.trackRid('R-X');
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle(null, stg(1));
  t.handle(null, kfEv('Host killed Guest'));
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 0);
  assert.equal(t.status().sealed, 0);
});

test('ranked: hosting_only host rows skipped, others banked', async () => {
  const sink = memRankedSink({ id: 'R-X', short_code: 'ABCDE', host_status: 'hosting_only' });
  const t = comp.createRankedTracker({ sink, log: () => {} });
  await t.trackRid('R-X');
  assert.equal(t.status().hostingOnly, true);
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle(null, joinEv(1, 'Host'));
  t.handle(null, joinEv(2, 'Guest'));
  t.handle(null, stg(1));
  t.handle(null, kfEv('Host killed Guest'));
  t.handle(null, kfEv('Guest knocked Host'));
  t.handle(null, alvEv(1, false));
  t.handle(null, alvEv(2, false));
  await t.sealManual();
  assert.equal(sink.saved.length, 1);
  const kinds = sink.saved[0].events.map((e) => `${e.etype}:${e.actorName}`);
  // host kill + host alv skipped; guest knock + guest alv kept
  assert.deepEqual(kinds, ['knock:Guest', 'alv:Guest']);
});

test('ranked: versions increment per seal', async () => {
  const sink = memRankedSink();
  const t = comp.createRankedTracker({ sink, log: () => {} });
  await t.trackRid('R-X');
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle(null, stg(1));
  t.handle(null, stg(3));
  t.handle(null, stg(1));
  await t.sealManual();
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 2);
  assert.equal(sink.saved[0].version, 0);
  assert.equal(sink.saved[1].version, 1);
});

test('ranked: public room suppresses an automatic seal', async () => {
  const sink = memRankedSink();
  const t = comp.createRankedTracker({ sink, log: () => {} });
  await t.trackRid('R-X');
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle(null, stg(1));
  assert.ok(t.status().open);
  t.handle({ dir: 'out' }, joinRandomRoomNoPid());
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 25));
  assert.equal(t.status().public, true);
  assert.equal(sink.saved.length, 0);
});

test('ranked: coded rejoin clears public state even when the code is unchanged', async () => {
  const sink = memRankedSink();
  const t = comp.createRankedTracker({ sink, log: () => {} });
  await t.trackRid('R-X');
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle({ dir: 'out' }, joinRandomRoomNoPid());
  assert.equal(t.status().public, true);
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  assert.equal(t.status().public, false);
});

test('ranked: capped series blocks automatic opens but manual bypasses', async () => {
  const sink = memRankedSink({ id: 'R-X', short_code: 'ABCDE', host_status: 'is_host_playing', max_matches: 1 });
  const t = comp.createRankedTracker({ sink, log: () => {} });
  await t.trackRid('R-X');
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle(null, stg(1)); t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 25));
  assert.equal(t.status().seriesSealed, 1);
  t.handle(null, stg(1));
  assert.equal(t.status().open, null);
  assert.ok(t.openManual());
});

test('ranked: a seal buffers while its bound room is temporarily absent', async () => {
  const sink = memRankedSink();
  const t = comp.createRankedTracker({ sink, log: () => {} });
  await t.trackRid('R-X');
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle(null, stg(1));
  await t.trackRid(null);
  await t.sealManual();
  assert.equal(t.status().buffered, 1);
  await t.trackRid('R-X');
  assert.equal(t.status().buffered, 0);
  assert.equal(sink.saved.length, 1);
});

// v250 seal rails: per-seal idempotency keys (seal_id + event_seq) that the
// partial unique index on bngc_ranked_events absorbs retries with.
test('ranked: seal rows carry seal_id and sequential event_seq', async () => {
  const sink = memRankedSink();
  const t = comp.createRankedTracker({ sink, log: () => {} });
  await t.trackRid('R-X');
  t.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t.handle(null, joinEv(1, 'Host'));
  t.handle(null, joinEv(2, 'Guest'));
  t.handle(null, stg(1));
  t.handle(null, kfEv('Host killed Guest'));
  t.handle(null, alvEv(2, false));
  t.handle(null, stg(3));
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(sink.saved.length, 1);
  const m = sink.saved[0];
  assert.ok(m.sealId, 'seal must carry a sealId');
  // resolveNames in memSink maps every name -> did:<name>; rows carry the keys
  const rows = comp.rankedListRows(m);
  assert.equal(rows.length, m.events.length);
  rows.forEach((row, i) => {
    assert.equal(row.match_version, m.version);
    assert.equal(row.seal_id, m.sealId);
    assert.equal(row.event_seq, i); // stable position in the immutable array
  });
  // distinct sealIds per match: an independent seal never collides with the index
  const t2 = comp.createRankedTracker({ sink: memRankedSink(), log: () => {} });
  await t2.trackRid('R-X');
  t2.handle({ dir: 'out' }, rankedJoinReq('ABCDE', 'Host'));
  t2.handle(null, stg(1));
  const firstSealId = t2._state.openMatch && t2._state.openMatch.sealId;
  assert.ok(firstSealId, 'fresh open carries its own sealId');
  assert.notEqual(firstSealId, m.sealId);
});

test('ranked: re-saving the same sealed match replays identical keys', async () => {
  // Contract for the DB rail: a retry (network blip, host tracker + engine
  // bridge both sealing) must produce byte-identical (room_id, match_version,
  // seal_id, event_seq) keys, which is what makes the unique index a no-op
  // instead of a duplicate.
  const m = {
    rid: 'R-X', version: 3, room: 'ABCDE', manual: false,
    sealId: 'test-seal-1234',
    events: [
      { etype: 'kill', actorNr: 1, actorName: 'Host', targetName: 'Guest' },
      { etype: 'alv', actorNr: 2, actorName: 'Guest', alive: false },
    ],
    leaves: [],
  };
  const a = comp.rankedListRows(m);
  const b = comp.rankedListRows(m);
  assert.deepEqual(a, b);
  assert.deepEqual(a.map((r) => [r.room_id, r.match_version, r.seal_id, r.event_seq]),
    [['R-X', 3, 'test-seal-1234', 0], ['R-X', 3, 'test-seal-1234', 1]]);
  // no sealId -> legacy shape (keys omitted; pre-v250 DBs unaffected)
  const legacy = comp.rankedListRows({ rid: 'R-Y', version: 1, events: m.events, sealId: null });
  assert.ok(legacy.every((r) => !('seal_id' in r) && !('event_seq' in r)));
});
