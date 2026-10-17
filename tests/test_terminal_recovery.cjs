'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const net = require('node:net');
const { EventEmitter } = require('node:events');
const { execFileSync } = require('node:child_process');
const { recoveryGuard, verifyRecoveryInputs, createServer, TerminalBroker, initializeOwnedDisplay, requireRefusedRecoveryPort, ownBrokerIdentity, recoveryOwnerState, validOwnerIdentity, parseOwnerLock } = require('../terminal/terminal_broker.cjs');

function fixture(t, withLock = true) {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'pron-recovery-test-')));
  t.after(() => fs.rmSync(directory, { recursive: true }));
  const filename = path.join(directory, 'terminal.json');
  const reference = path.join(directory, 'reference.json');
  const maintenance = `${filename}.recovery.lock`;
  fs.writeFileSync(filename, JSON.stringify({ url: 'http://127.0.0.1:39001', pid: 123, token: 'fixture-not-real-0123456789', version: '0.3.0' }));
  fs.writeFileSync(reference, JSON.stringify({ descriptor_file: filename }));
  fs.writeFileSync(maintenance, JSON.stringify({ pid: 1, nonce: 'test-maintenance' }));
  if (withLock) fs.writeFileSync(`${filename}.lock`, '123');
  const snapshot = name => {
    const stats = fs.statSync(name, { bigint: true });
    return { hash: crypto.createHash('sha256').update(fs.readFileSync(name)).digest('hex'),
      identity: { ino: String(stats.ino), size: String(stats.size), mtime_ns: String(stats.mtimeNs) } };
  };
  const descriptor = snapshot(filename), ref = snapshot(reference), maint = snapshot(maintenance);
  const lock = withLock ? snapshot(`${filename}.lock`) : null;
  const guard = { descriptor_file: filename, descriptor_sha256: descriptor.hash, descriptor_identity: descriptor.identity,
    reference_file: reference, reference_sha256: ref.hash, reference_identity: ref.identity,
    maintenance_sha256: maint.hash, maintenance_identity: maint.identity,
    lock_sha256: lock?.hash ?? null, lock_identity: lock?.identity ?? null,
    pid: 123, recovery_id: 'a'.repeat(32) };
  const refresh = () => {
    const nextDescriptor = snapshot(filename), nextLock = fs.existsSync(`${filename}.lock`) ? snapshot(`${filename}.lock`) : null;
    Object.assign(guard, { descriptor_sha256: nextDescriptor.hash, descriptor_identity: nextDescriptor.identity,
      lock_sha256: nextLock?.hash ?? null, lock_identity: nextLock?.identity ?? null });
  };
  return { filename, reference, maintenance, guard, refresh, valid: () => recoveryGuard(filename, JSON.stringify(guard), reference) };
}

test('recovery child accepts exact bounded guard but not altered schema, path, generation, or identities', t => {
  const f = fixture(t);
  assert.deepEqual(f.valid(), f.guard);
  assert.equal(recoveryGuard(f.filename, ''), null);
  for (const change of [{ descriptor_file: 'relative.json' }, { reference_file: 'unrelated.json' }, { pid: 0 }, { pid: 2147483648 },
    { recovery_id: 'not-a-generation' }, { descriptor_identity: { ino: 1, size: '2', mtime_ns: '3' } }, { token: 'not accepted' },
    { lock_sha256: null }]) {
    assert.throws(() => recoveryGuard(f.filename, JSON.stringify({ ...f.guard, ...change }), f.reference), /authorization is invalid/);
  }
});

test('only affirmative old PID death and exact original descriptor/reference/lease permit recovery', t => {
  const f = fixture(t);
  assert.equal(verifyRecoveryInputs(f.filename, f.valid(), { pidState: () => 'dead' }).pid, 123);
  for (const state of ['alive', 'unknown']) {
    assert.throws(() => verifyRecoveryInputs(f.filename, f.valid(), { pidState: () => state }), /recovery refused/);
  }
});

test('descriptor or pointer replacement between parent check and child startup fails closed', t => {
  const f = fixture(t);
  const before = fs.readFileSync(f.filename);
  fs.writeFileSync(f.filename, Buffer.concat([before, Buffer.from('\n')]));
  assert.throws(() => verifyRecoveryInputs(f.filename, f.guard, { pidState: () => 'dead' }), /recovery refused/);
  fs.writeFileSync(f.filename, before);
  fs.writeFileSync(f.reference, '{}');
  assert.throws(() => verifyRecoveryInputs(f.filename, f.guard, { pidState: () => 'dead' }), /recovery refused/);
});

test('same bytes in a replaced descriptor inode do not satisfy recovery CAS', t => {
  const f = fixture(t);
  fs.renameSync(f.filename, `${f.filename}.old`);
  fs.writeFileSync(f.filename, fs.readFileSync(`${f.filename}.old`));
  assert.throws(() => verifyRecoveryInputs(f.filename, f.guard, { pidState: () => 'dead' }), /recovery refused/);
});

test('maintenance lease or broker lock changes are rejected without deleting either file', t => {
  const f = fixture(t);
  fs.writeFileSync(`${f.filename}.lock`, '456');
  assert.throws(() => verifyRecoveryInputs(f.filename, f.guard, { pidState: () => 'dead' }), /recovery refused/);
  assert.equal(fs.readFileSync(`${f.filename}.lock`, 'utf8'), '456');
  fs.writeFileSync(f.maintenance, 'new owner');
  assert.throws(() => verifyRecoveryInputs(f.filename, f.guard, { pidState: () => 'dead' }), /recovery refused/);
  assert.equal(fs.readFileSync(f.maintenance, 'utf8'), 'new owner');
});

test('previously absent lock cannot be silently reclaimed if another owner claims it', t => {
  const f = fixture(t, false);
  assert.equal(verifyRecoveryInputs(f.filename, f.guard, { pidState: () => 'dead' }).pid, 123);
  fs.writeFileSync(`${f.filename}.lock`, '999');
  assert.throws(() => verifyRecoveryInputs(f.filename, f.guard, { pidState: () => 'dead' }), /recovery refused/);
});

test('shared OS helper records the real current Node birth as a decimal string and exact broker script', () => {
  const identity = ownBrokerIdentity();
  assert.equal(typeof identity.birth, 'string');
  assert.equal(identity.executable.toLowerCase(), process.execPath.toLowerCase());
  assert.equal(identity.script, path.resolve(__dirname, '../terminal/terminal_broker.cjs'));
  assert.equal(recoveryOwnerState({ pid: process.pid, owner_identity: identity }), 'alive');
  assert.equal(recoveryOwnerState({ pid: process.pid, owner_identity: { ...identity, birth: 'win-filetime:1' } }), 'dead');
  assert.equal(recoveryOwnerState({ pid: process.pid }), 'unknown');
});

test('layout migration accepts only exact canonical or legacy checkout broker identities', () => {
  const canonical = ownBrokerIdentity();
  const legacy = { ...canonical, script: path.resolve(__dirname, '../terminal_broker.cjs') };
  assert.equal(validOwnerIdentity(canonical), true);
  assert.equal(validOwnerIdentity(legacy), true);
  assert.equal(recoveryOwnerState({ pid: process.pid, owner_identity: legacy }), 'alive');
  for (const script of [path.join(os.tmpdir(), 'terminal_broker.cjs'), path.resolve(__dirname, 'terminal_broker.cjs'), 'terminal_broker.cjs']) {
    assert.equal(validOwnerIdentity({ ...canonical, script }), false);
    let queried = false;
    assert.equal(recoveryOwnerState({ pid: process.pid, owner_identity: { ...canonical, script } }, {
      query() { queried = true; return { state: 'alive' }; },
    }), 'unknown');
    assert.equal(queried, false);
  }
  const queries = [];
  assert.equal(recoveryOwnerState({ pid: process.pid, owner_identity: legacy }, {
    query(args) { queries.push(args); return { state: 'alive' }; },
  }), 'alive');
  assert.equal(queries[0].at(-1), legacy.script);
});

test('legacy launcher records its exact invoked path without starting a broker on require', () => {
  const canonical = path.resolve(__dirname, '../terminal/terminal_broker.cjs');
  const legacy = path.resolve(__dirname, '../terminal_broker.cjs');
  const script = `const broker=require(${JSON.stringify(canonical)});process.stdout.write(JSON.stringify(broker.ownBrokerIdentity()));`;
  // Inert local child only: no main(), descriptor publication, session or SSH.
  const identity = JSON.parse(execFileSync(process.execPath, ['-e', script, legacy], {
    encoding: 'utf8', windowsHide: true, timeout: 10000,
  }));
  assert.equal(identity.script, legacy);
  assert.equal(identity.executable.toLowerCase(), process.execPath.toLowerCase());
  assert.equal(validOwnerIdentity(identity), true);
});

test('birth-bearing descriptor and private lease must prove the same dead owner, not merely an equal PID', t => {
  const f = fixture(t);
  const identity = ownBrokerIdentity();
  const descriptor = JSON.parse(fs.readFileSync(f.filename));
  descriptor.owner_identity = identity;
  fs.writeFileSync(f.filename, JSON.stringify(descriptor));
  fs.writeFileSync(`${f.filename}.lock`, JSON.stringify({ pid: 123, owner_identity: identity }));
  f.refresh();
  const inspected = [];
  const ownerState = owner => { inspected.push(owner); return 'dead'; };
  assert.equal(verifyRecoveryInputs(f.filename, f.valid(), { ownerState }).pid, 123);
  assert.equal(inspected.length, 2);
  assert.deepEqual(inspected.map(owner => owner.owner_identity), [identity, identity]);
  for (const state of ['alive', 'unknown']) {
    assert.throws(() => verifyRecoveryInputs(f.filename, f.valid(), { ownerState: () => state }), /recovery refused/);
  }
  fs.writeFileSync(`${f.filename}.lock`, JSON.stringify({ pid: 123, owner_identity: { ...identity, birth: 'win-filetime:1' } }));
  f.refresh();
  assert.throws(() => verifyRecoveryInputs(f.filename, f.valid(), { ownerState }), /recovery refused/);
  assert.equal(parseOwnerLock(fs.readFileSync(`${f.filename}.lock`)).owner_identity.birth, 'win-filetime:1');
});

test('recovery child cannot claim its own lease using the current PID but another process birth', t => {
  const f = fixture(t);
  const identity = ownBrokerIdentity();
  fs.writeFileSync(`${f.filename}.lock`, JSON.stringify({ pid: process.pid, owner_identity: { ...identity, birth: 'win-filetime:1' } }));
  assert.throws(() => verifyRecoveryInputs(f.filename, f.guard,
    { ownedLock: true, ownerState: () => 'dead', currentOwnerIdentity: identity }), /recovery refused/);
  fs.writeFileSync(`${f.filename}.lock`, JSON.stringify({ pid: process.pid, owner_identity: identity }));
  assert.equal(verifyRecoveryInputs(f.filename, f.guard,
    { ownedLock: true, ownerState: () => 'dead', currentOwnerIdentity: identity }).pid, 123);
});

test('after lock acquisition recovery validates only its own lease plus original descriptor and pointer', t => {
  const f = fixture(t);
  fs.writeFileSync(`${f.filename}.lock`, String(process.pid));
  assert.equal(verifyRecoveryInputs(f.filename, f.guard, { ownedLock: true, pidState: () => 'dead' }).pid, 123);
  fs.writeFileSync(`${f.filename}.lock`, 'other lease');
  assert.throws(() => verifyRecoveryInputs(f.filename, f.guard, { ownedLock: true, pidState: () => 'dead' }), /recovery refused/);
});

test('authenticated health publishes the public recovery generation but initializes no sessions', async t => {
  const generation = 'b'.repeat(32);
  const broker = new TerminalBroker({ spawn: () => { throw new Error('No work session may start'); } });
  const server = createServer(broker, 'fixture-not-real-0123456789', generation);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => server.close());
  const url = `http://127.0.0.1:${server.address().port}`;
  const response = await fetch(`${url}/health`, { headers: { Authorization: 'Bearer fixture-not-real-0123456789' } });
  const health = await response.json();
  assert.equal(health.recovery_id, generation);
  assert.equal(health.version, '0.3.0');
  assert.equal(broker.sessions.size, 0);
  assert.equal((await fetch(`${url}/health`)).status, 401);
});

for (const stage of ['constructor', 'listen']) test(`${stage} startup failure releases only its exact child lease and preserves old descriptor/reference`, async t => {
  const f = fixture(t);
  const descriptorBefore = fs.readFileSync(f.filename), referenceBefore = fs.readFileSync(f.reference);
  const lockfile = `${f.filename}.lock`;
  fs.writeFileSync(lockfile, String(process.pid));
  let closed = false;
  const server = new EventEmitter();
  server.listen = () => queueMicrotask(() => server.emit('error', Object.assign(new Error('fixture listen failure'), { code: 'EACCES' })));
  server.close = callback => { closed = true; callback(); };
  const options = { verify: (filename, guard, state) => verifyRecoveryInputs(filename, guard, { ...state, pidState: () => 'dead' }),
    portProof: async () => {}, brokerFactory: () => { if (stage === 'constructor') throw new Error('fixture constructor failure'); return { sessions: new Map() }; },
    serverFactory: () => server };
  await assert.rejects(initializeOwnedDisplay(f.filename, lockfile, f.guard, options), new RegExp(`fixture ${stage} failure`));
  assert.equal(fs.existsSync(lockfile), false);
  assert.deepEqual(fs.readFileSync(f.filename), descriptorBefore);
  assert.deepEqual(fs.readFileSync(f.reference), referenceBefore);
  assert.equal(closed, stage === 'listen');
});

test('startup cleanup does not remove a replaced lease even if its text contains the same PID', async t => {
  const f = fixture(t);
  const lockfile = `${f.filename}.lock`;
  fs.writeFileSync(lockfile, String(process.pid));
  await assert.rejects(initializeOwnedDisplay(f.filename, lockfile, null, {
    brokerFactory: () => {
      fs.renameSync(lockfile, `${lockfile}.original`);
      fs.writeFileSync(lockfile, String(process.pid));
      throw new Error('fixture concurrent replacement');
    },
  }), /fixture concurrent replacement/);
  assert.equal(fs.readFileSync(lockfile, 'utf8'), String(process.pid));
  assert.equal(fs.readFileSync(`${lockfile}.original`, 'utf8'), String(process.pid));
});

test('recovery port proof waits up to four seconds but accepts only affirmative refusal', async t => {
  for (const outcome of ['ECONNREFUSED', 'timeout', 'connect', 'ETIMEDOUT', 'ECONNRESET', 'EACCES']) {
    await t.test(outcome, async child => {
      const probe = new EventEmitter();
      let budget, expire, timer;
      probe.setTimeout = (milliseconds, callback) => {
        budget = milliseconds; expire = callback; timer = setTimeout(callback, milliseconds); return probe;
      };
      probe.destroy = () => { clearTimeout(timer); probe.destroyed = true; return probe; };
      child.mock.method(net, 'createConnection', options => {
        assert.deepEqual(options, { host: '127.0.0.1', port: 39001 });
        return probe;
      });
      const pending = requireRefusedRecoveryPort('http://127.0.0.1:39001');
      const checked = outcome === 'ECONNREFUSED' ? pending : assert.rejects(pending, /not affirmatively offline; recovery refused/);
      assert.equal(budget, 4000);
      if (outcome === 'timeout') expire();
      else if (outcome === 'connect') probe.emit('connect');
      else if (outcome === 'ECONNREFUSED') {
        // Match the observed Windows loopback RST arriving after about 2s.
        await new Promise(resolve => setTimeout(resolve, 2050));
        probe.emit('error', Object.assign(new Error('fixture refusal'), { code: outcome }));
      } else probe.emit('error', Object.assign(new Error('fixture unknown'), { code: outcome }));
      await checked;
      assert.equal(probe.destroyed, true);
    });
  }
});
