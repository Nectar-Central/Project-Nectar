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
