'use strict';
// All children and process observations are fake. No SSH or worker input.
const test = require('node:test');
const assert = require('node:assert/strict');
const { TerminalBroker } = require('../terminal/terminal_broker.cjs');

const ID = 'ceohub-0123456789abcdef01234567';
const BODY = { session_id: ID, host: 'spark', transport: 'ssh', target: 'fake-spark', cwd: '/first-marker', cols: 80, rows: 30 };
const proof = number => ({ state: 'alive', birth: `win-filetime:${number}`, executable: 'C:\\Windows\\System32\\OpenSSH\\ssh.exe' });
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function child(pid) {
  let data, exit;
  const writes = [];
  return { pid, writes, write(value) { writes.push(value); }, resize() {}, kill() {},
    onData(callback) { data = callback; }, onExit(callback) { exit = callback; },
    output(value) { data(value); }, exited(exitCode = 0) { exit({ exitCode }); } };
}
function fixture(options = {}) {
  const children = [child(12345), child(12346)];
  let spawns = 0;
  const broker = new TerminalBroker({ platform: 'win32', spawnRemote: () => children[spawns++],
    queryDisplayIdentity: pid => proof(pid), probe: async () => ({ state: 'unknown', command: '' }), ...options });
  return { broker, children, get spawns() { return spawns; } };
}

test('SSH birth identity is frozen immediately after child ready, not refreshed by describe', async () => {
  const ready = deferred(), calls = [], observed = proof(12345);
  const f = fixture({ queryDisplayIdentity: pid => { calls.push(pid); return observed; } });
  f.children[0].ready = ready.promise;
  const pending = f.broker.create(BODY);
  assert.deepEqual(calls, []);
  ready.resolve();
  const created = await pending;
  assert.deepEqual(calls, [12345]);
  assert.deepEqual(created.display_identity, { birth: observed.birth, executable: observed.executable });
  observed.birth = 'win-filetime:99999';
  assert.equal(f.broker.describe(f.broker.get(ID)).display_identity.birth, 'win-filetime:12345');
  assert.equal(calls.length, 1);
  const snapshot = f.broker.describe(f.broker.get(ID));
  snapshot.display_identity.birth = 'win-filetime:77777';
  assert.equal(f.broker.describe(f.broker.get(ID)).display_identity.birth, 'win-filetime:12345');
});

test('unknown, malformed, dead or non-SSH birth observations never block the display but give no strict proof', async () => {
  for (const observed of [{ state: 'unknown' }, { state: 'dead' }, { ...proof(1), birth: 'unknown' },
    { ...proof(1), executable: 'C:\\Windows\\System32\\cmd.exe' }, { ...proof(1), executable: 'ssh.exe' },
    { ...proof(1), executable: 'C:\\bad\nssh.exe' }]) {
    const f = fixture({ queryDisplayIdentity: () => observed });
    const created = await f.broker.create(BODY);
    assert.equal(created.connected, true);
    assert.equal(created.display_identity, null);
  }
  const f = fixture({ queryDisplayIdentity: () => { throw new Error('observation unavailable'); } });
  assert.equal((await f.broker.create(BODY)).display_identity, null);
});

test('a changed child PID cannot reuse the frozen SSH birth proof', async () => {
  const f = fixture(); await f.broker.create(BODY);
  f.children[0].pid = 99999;
  assert.equal(f.broker.describe(f.broker.get(ID)).display_identity, null);
});

test('reattaching resets original attachment metadata and old child callbacks cannot overwrite the new display', async () => {
  const f = fixture(); await f.broker.create(BODY);
  const current = f.broker.get(ID);
  current.state = 'agent'; current.command = 'codex'; current.agentIdentity = { kind: 'codex' };
  current.lastProbe = Date.now(); current.directRootProof = { kind: 'codex' };
  f.children[0].exited();
  const created = await f.broker.create({ ...BODY, cwd: '/new-strict-marker' });
  assert.equal(created.cwd, '/new-strict-marker');
  assert.equal(created.state, 'unknown'); assert.equal(created.command, '');
  assert.equal(current.agentIdentity, null); assert.equal(current.directRootProof, null); assert.equal(current.lastProbe, 0);
  assert.equal(created.display_identity.birth, 'win-filetime:12346');
  f.children[1].output('current output');
  const seq = current.seq;
  f.children[0].output('stale output'); f.children[0].exited(255);
  assert.equal(current.seq, seq); assert.equal(current.connected, true); assert.equal(current.exitCode, null);
  await f.broker.flushScreen(current);
  assert.equal(current.ring.some(chunk => chunk.data.includes('stale output')), false);
});

test('old foreground result cannot change a new attachment or clear its pending probe', async () => {
  const old = deferred(), fresh = deferred(); let probes = 0;
  const f = fixture({ probe: () => ++probes === 1 ? old.promise : fresh.promise });
  await f.broker.create(BODY); const current = f.broker.get(ID);
  const pendingOld = f.broker.refreshForeground(current, true);
  f.children[0].exited(); await f.broker.create({ ...BODY, cwd: '/new-marker' });
  const pendingFresh = f.broker.refreshForeground(current, true), freshProbe = current.probing;
  old.resolve({ command: 'codex', state: 'agent', identity_verified: true, agent_kind: 'codex',
    agent_pid: 321, agent_start_ticks: 456, foreground_pgid: 321 });
  await pendingOld;
  assert.equal(current.state, 'unknown'); assert.equal(current.agentIdentity, null); assert.equal(current.lastProbe, 0);
  assert.equal(current.probing, freshProbe);
  fresh.resolve({ command: 'bash', state: 'shell' }); await pendingFresh;
  assert.equal(current.state, 'shell'); assert.equal(current.command, 'bash'); assert.equal(current.probing, null);
});

test('old foreground rejection cannot replace the freshly verified attachment state', async () => {
  const old = deferred(); let probes = 0;
  const f = fixture({ probe: () => ++probes === 1 ? old.promise : Promise.resolve({ command: 'bash', state: 'shell' }) });
  await f.broker.create(BODY); const current = f.broker.get(ID);
  const pendingOld = f.broker.refreshForeground(current, true);
  f.children[0].exited(); await f.broker.create(BODY); await f.broker.refreshForeground(current, true);
  const lastProbe = current.lastProbe;
  old.reject(new Error('old connection failed')); await pendingOld;
  assert.equal(current.state, 'shell'); assert.equal(current.command, 'bash'); assert.equal(current.lastProbe, lastProbe);
});

test('a concurrent create cannot spawn another SSH while the original child is starting', async () => {
  const ready = deferred(), f = fixture(); f.children[0].ready = ready.promise;
  const first = f.broker.create(BODY);
  await assert.rejects(f.broker.create(BODY), error => error.code === 'session_attaching');
  assert.equal(f.spawns, 1); ready.resolve(); await first;
});

test('old queued protocol replies are never sent into a replacement SSH child', async () => {
  const f = fixture(); await f.broker.create(BODY);
  const current = f.broker.get(ID), blocked = deferred(); current.writeQueue = blocked.promise;
  f.children[0].output('\x1b[6n'); await f.broker.flushScreen(current);
  f.children[0].exited(); await f.broker.create(BODY);
  blocked.resolve(); await current.writeQueue;
  assert.deepEqual(f.children[0].writes, []); assert.deepEqual(f.children[1].writes, []);
});

test('pending old frames finish parsing before a replacement child can receive protocol replies', async () => {
  const f = fixture(); await f.broker.create(BODY);
  f.children[0].output('\x1b[6n'); f.children[0].exited();
  const pending = f.broker.create(BODY);
  assert.equal(f.spawns, 1, 'Do not spawn the replacement before pending old frames have parsed');
  await pending;
  const current = f.broker.get(ID); await f.broker.flushScreen(current); await current.writeQueue;
  assert.deepEqual(f.children[0].writes, []); assert.deepEqual(f.children[1].writes, []);
});
