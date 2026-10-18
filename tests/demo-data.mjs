import test from 'node:test';
import assert from 'node:assert/strict';
import { snapshot, spectrum, events, csv } from '../demo/data.mjs';

test('normal samples are deterministic and below the illustrative threshold', () => {
  assert.deepEqual(snapshot('normal', 0), snapshot('normal', 0));
  assert.equal(snapshot('normal', 0).length, 4);
  assert(snapshot('normal', 10).every(c => !c.stale && !c.warning));
});
test('rising scenario affects only the pump drive-end channel', () => {
  const rising = snapshot('rising', 0), normal = snapshot('normal', 0);
  assert(rising[0].warning);
  assert.deepEqual(rising.slice(1), normal.slice(1));
  assert(spectrum(rising[0], 'rising')[25].value > spectrum(rising[0], 'rising', true)[25].value);
  assert.equal(events('rising', 0)[0].state, 'Warning');
});
test('missing sensor never fabricates readings through the data gap', () => {
  const channel = snapshot('missing', 6)[2];
  assert(channel.stale);
  assert.equal(channel.captureMinute, 54);
  assert.equal(channel.ageMinutes, 11);
  assert.equal(channel.value, snapshot('missing', 0)[2].value);
  assert(channel.history.slice(-11).every(p => p.value === null));
  assert.equal(channel.history.at(-12).value, channel.value);
  assert(csv(channel).includes('synthetic_demo'));
  assert(csv(channel).split('\r\n').at(-2).includes('CH3,,10Hz-1kHz'));
  assert.equal(events('missing', 6)[0].state, 'Stale');
});
test('invalid replay state is rejected', () => {
  assert.throws(() => snapshot('unexpected', 0));
  assert.throws(() => snapshot('toString', 0));
  assert.throws(() => snapshot('normal', -1));
});
