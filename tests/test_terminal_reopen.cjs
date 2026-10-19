'use strict';
// Fake owned children, fake SSH replies and temporary loopback HTTP only.
// No real terminal, model, remote command, process kill or personal state.
const test = require('node:test');
const assert = require('node:assert/strict');
const zlib = require('node:zlib');
const { TerminalBroker, createServer, remoteCloseCommand } = require('../terminal/terminal_broker.cjs');

const ID = 'ceohub-0123456789abcdef01234567';
const identity = { name: ID, session: '$17', created: 1792000000, pane: '%21', pane_pid: 1234,
  pane_start_ticks: 9876, boot_id: '12345678-1234-1234-1234-123456789abc', server_pid: 4321 };
const expectedRemote = { exists: true, identity };
function fixture(options = {}) {
  const calls = [], kills = [];
  let exit;
  const child = { pid: 123, process: 'shell', write() {}, resize() {}, onData() {},
    onExit(callback) { exit = callback; }, kill() { kills.push(123); if (options.exitOnKill !== false) exit({ exitCode: 0 }); } };
  const broker = new TerminalBroker({ spawn: () => child, spawnRemote: () => child,
    probe: async () => ({ state: 'shell', command: 'bash' }), queryDisplayIdentity: () => ({ state: 'unknown' }),
    closeExitTimeout: 10, runCommand: async (...args) => { calls.push(args); if (options.runCommand) return options.runCommand(...args);
      return { stdout: 'CEO_HUB_TERMINAL_CLOSE_RESULT=' + JSON.stringify({ ok: true, terminal_id: ID, closed_verified: true }) + '\n' }; } });
  return { broker, calls, kills, child, exit: () => exit({ exitCode: 0 }) };
}
const route = transport => ({ require_verified: true, expected_host: transport === 'ssh' ? 'fake-remote' : 'local',
  expected_transport: transport, expected_target: transport === 'ssh' ? 'fake-ssh' : '',
  ...(transport === 'ssh' ? { expected_remote: expectedRemote } : {}) });
async function create(f, transport = 'local') {
  return f.broker.create({ session_id: ID, host: transport === 'ssh' ? 'fake-remote' : 'local', transport,
    target: transport === 'ssh' ? 'fake-ssh' : '', cwd: __dirname });
}

test('strict local close waits for owned-child exit, removes only that record and prevents old-ID recreation', async () => {
  const f = fixture();
  await create(f);
  const result = await f.broker.close(ID, route('local'));
  assert.deepEqual(result, { id: ID, closed: true, closed_verified: true, record_removed: true,
    local_display_exit_verified: true, missing_record: false });
  assert.equal(f.broker.sessions.has(ID), false);
  assert.deepEqual(f.kills, [123]);
  assert.deepEqual(await f.broker.close(ID, route('local')), result);
  assert.deepEqual(f.kills, [123], 'Duplicate close cannot kill a later process');
  await assert.rejects(create(f), error => error.code === 'terminal_closed');
  await assert.rejects(f.broker.input(ID, { data: 'stale input' }), error => error.code === 'terminal_not_found');
});

test('kill return without an observed owned-child exit stays unknown and leaves the record fenced', async () => {
  const f = fixture({ exitOnKill: false });
  await create(f);
  await assert.rejects(f.broker.close(ID, route('local')), error => error.code === 'terminal_close_unknown');
  assert.equal(f.broker.sessions.has(ID), true);
  assert.equal(f.broker.describe(f.broker.get(ID)).agent_ready, false);
  assert.equal(f.broker.describe(f.broker.get(ID)).blocked_reason, 'terminal_close_unknown');
  await assert.rejects(f.broker.input(ID, { data: 'stale' }), error => error.code === 'terminal_closing');
  await assert.rejects(f.broker.close(ID, route('local')), error => error.code === 'terminal_close_unknown');
  assert.deepEqual(f.kills, [123], 'Unknown result is never repeated');
  await assert.rejects(f.broker.close(ID, {}), error => error.code === 'terminal_close_unknown');
  assert.deepEqual(f.kills, [123], 'Legacy close cannot bypass the replacement fence');
});

test('an already observed original child exit is valid without another kill', async () => {
  const f = fixture(); await create(f); f.exit();
  assert.equal((await f.broker.close(ID, route('local'))).closed_verified, true);
  assert.deepEqual(f.kills, []);
});

test('SSH close requires exact remote receipt before ending its display and removing the record', async () => {
  const f = fixture(); await create(f, 'ssh');
  const result = await f.broker.close(ID, route('ssh'));
  assert.equal(result.remote_closed_verified, true);
  assert.equal(result.local_display_exit_verified, true);
  assert.equal(f.calls.length, 1);
  assert.ok(f.calls[0][1].includes('ControlMaster=no'));
  assert.ok(f.calls[0][1].includes('ControlPersist=no'));
  assert.equal(f.calls[0][1].at(-2), 'fake-ssh');
  assert.deepEqual(f.kills, [123]);
  assert.equal(f.broker.sessions.has(ID), false);
});

test('SSH timeout, malformed receipt and another target identity never count as closure', async () => {
  for (const reply of [null, { ok: true, terminal_id: ID }, { ok: true, terminal_id: 'other', closed_verified: true },
    { ok: false, terminal_id: ID, closed_verified: true }, { ok: true, terminal_id: ID, closed_verified: true, extra: true }]) {
    const f = fixture({ runCommand: async () => { if (reply === null) throw Error('SSH handshake timed out');
      return { stdout: 'CEO_HUB_TERMINAL_CLOSE_RESULT=' + JSON.stringify(reply) }; } });
    await create(f, 'ssh');
    await assert.rejects(f.broker.close(ID, route('ssh')), error => error.code === 'terminal_close_unknown');
    assert.equal(f.broker.sessions.has(ID), true);
    assert.deepEqual(f.kills, []);
    assert.equal(f.calls.length, 1);
  }
});

test('a missing local record is not proof of local process absence', async () => {
  const f = fixture();
  await assert.rejects(f.broker.close(ID, route('local')), error => error.code === 'terminal_close_unknown');
  assert.deepEqual(f.kills, []); assert.deepEqual(f.calls, []);
});

test('missing SSH record still requires exact remote proof and returns its different local-evidence boundary', async () => {
  const f = fixture();
  const result = await f.broker.close(ID, { ...route('ssh'), expected_remote: { exists: false } });
  assert.equal(result.closed_verified, true);
  assert.equal(result.remote_closed_verified, true);
  assert.equal(result.local_display_exit_verified, false);
  assert.equal(result.missing_record, true);
  assert.equal(f.calls.length, 1); assert.deepEqual(f.kills, []);
});

test('route mismatches and incomplete remote proof fail before any destructive action', async () => {
  const f = fixture(); await create(f, 'ssh');
  for (const body of [{ ...route('ssh'), expected_target: 'another-ssh' },
    { ...route('ssh'), expected_remote: { exists: true, identity: { ...identity, boot_id: undefined } } },
    { ...route('ssh'), expected_remote: { exists: false, identity } }]) {
    await assert.rejects(f.broker.close(ID, body), error => ['session_conflict', 'invalid_close_identity'].includes(error.code));
  }
  assert.deepEqual(f.kills, []); assert.deepEqual(f.calls, []);
});

test('portable close bootstrap has exact expected proof, not a shell kill-prefix fallback', () => {
  const command = remoteCloseCommand(ID, expectedRemote);
  const encoded = /base64\.b64decode\([^A-Za-z0-9]+([A-Za-z0-9+/=]+)[^)]*\)/.exec(command)?.[1];
  assert.ok(encoded);
  const helper = zlib.inflateSync(Buffer.from(encoded, 'base64')).toString('utf8');
  assert.ok(helper.includes('def valid_identity('));
  assert.ok(helper.includes('"if-shell", "-F"'));
  assert.ok(helper.includes('pane_start_ticks'));
  assert.ok(helper.includes('boot_id'));
  assert.ok(!command.includes(' || ! tmux has-session'));
  assert.ok(command.includes("'--expected'"));
  assert.ok(remoteCloseCommand(ID, { exists: false }).includes("'--expected-absent'"));
});

test('HTTP strict-close generation gate blocks stale ownership before touching its terminal', async t => {
  const f = fixture(); await create(f);
  const server = createServer(f.broker, 'inert-terminal-test-capability', 'a'.repeat(32));
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const url = `http://127.0.0.1:${server.address().port}/sessions/${ID}/close`;
  const response = await fetch(url, { method: 'POST', headers: { Authorization: 'Bearer inert-terminal-test-capability',
    'Content-Type': 'application/json' }, body: JSON.stringify({ ...route('local'), expected_owner: { pid: process.pid, recovery_id: 'b'.repeat(32) } }) });
  assert.equal(response.status, 409);
  assert.equal((await response.json()).error, 'terminal_owner_changed');
  assert.deepEqual(f.kills, []); assert.equal(f.broker.sessions.has(ID), true);
});

test('HTTP replacement-create generation gate cannot create a shell under a substituted broker owner', async t => {
  const f = fixture();
  const generation = 'a'.repeat(32);
  const server = createServer(f.broker, 'inert-terminal-test-capability', generation);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const url = `http://127.0.0.1:${server.address().port}/sessions/create`;
  const send = body => fetch(url, { method: 'POST', headers: { Authorization: 'Bearer inert-terminal-test-capability',
    'Content-Type': 'application/json' }, body: JSON.stringify({ session_id: ID, transport: 'local', cwd: __dirname, ...body }) });
  for (const owner of [null, { pid: process.pid + 1, recovery_id: generation },
    { pid: process.pid, recovery_id: 'b'.repeat(32) }, { pid: process.pid, recovery_id: generation, extra: true }]) {
    const response = await send({ expected_owner: owner });
    assert.equal(response.status, 409);
    assert.equal((await response.json()).error, 'terminal_owner_changed');
    assert.equal(f.broker.sessions.size, 0);
  }
  const response = await send({ expected_owner: { pid: process.pid, recovery_id: generation } });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).id, ID);
  assert.equal(f.broker.sessions.size, 1);
});
