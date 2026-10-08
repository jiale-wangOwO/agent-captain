'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const zlib = require('node:zlib');
const { TerminalBroker, createServer, shellQuote, classifyCommand, remoteBridgeCommand } = require('../terminal_broker.cjs');

function fakeBroker(options = {}) {
  const writes = [];
  let output;
  const fake = {
    pid: 123, process: 'powershell.exe', write: value => writes.push(value),
    resize() {}, kill() {}, onData: callback => { output = callback; }, onExit() {},
  };
  const broker = new TerminalBroker({ spawn: () => fake, probe: async () => ({ state: 'agent', command: 'codex.exe' }), ...options });
  return { broker, writes, output: value => output(value) };
}
async function session(fixture) {
  return fixture.broker.create({ host: 'local', transport: 'local', cwd: __dirname, cols: 80, rows: 10 });
}

test('prompt delivery is atomic and retries do not send a second task', async () => {
  const fixture = fakeBroker();
  const created = await session(fixture);
  const body = { text: 'first line\nsecond line', request_key: 'request-1' };
  const [one, two] = await Promise.all([fixture.broker.prompt(created.id, body), fixture.broker.prompt(created.id, body)]);
  assert.equal(one.state, 'submitted');
  assert.equal(two.deduplicated, true);
  assert.deepEqual(fixture.writes, ['\x1b[200~first line\nsecond line\x1b[201~\r']);
  await assert.rejects(fixture.broker.prompt(created.id, { ...body, text: 'different task' }), error => error.code === 'request_conflict');
});

test('user partial input blocks CEO even after the short typing lease expires', async () => {
  const fixture = fakeBroker();
  const created = await session(fixture);
  await fixture.broker.input(created.id, { origin: 'ui', data: 'unfinished human question' });
  fixture.broker.get(created.id).humanLeaseUntil = 0;
  await assert.rejects(fixture.broker.prompt(created.id, { text: 'CEO message', request_key: 'request-2' }), error => error.code === 'human_busy');
  assert.equal(fixture.writes.length, 1);
  await fixture.broker.input(created.id, { origin: 'ui', data: '\x03' });
  fixture.broker.get(created.id).humanLeaseUntil = 0;
  await fixture.broker.prompt(created.id, { text: 'CEO message', request_key: 'request-2' });
  assert.equal(fixture.writes.length, 3);
});

test('CEO cannot submit task text to a shell or an agent approval screen', async () => {
  const fixture = fakeBroker({ probe: async () => ({ state: 'shell', command: 'powershell.exe' }) });
  const created = await session(fixture);
  await assert.rejects(fixture.broker.prompt(created.id, { text: 'fix bug', request_key: 'safety-1' }), error => error.code === 'agent_not_started');
  fixture.broker.probe = async () => ({ state: 'agent', command: 'codex.exe' });
  fixture.output('Would you like to run the following command?\r\n  1. Yes, proceed\r\n');
  await assert.rejects(fixture.broker.prompt(created.id, { text: 'fix bug', request_key: 'safety-1' }), error => error.code === 'waiting_for_agent');
  assert.equal(fixture.writes.length, 0);
  const view = await fixture.broker.snapshot(created.id);
  assert.equal(view.agent_ready, false);
  assert.equal(view.blocked_reason, 'approval');
});

test('bracketed multiline paste and navigation cannot accidentally clear a human draft', async () => {
  const fixture = fakeBroker();
  const created = await session(fixture);
  await fixture.broker.input(created.id, { origin: 'ui', data: '\x1b[20' });
  await fixture.broker.input(created.id, { origin: 'ui', data: '0~unfinished multiline\n\x1b[201~\x1b[H\b' });
  fixture.broker.get(created.id).humanLeaseUntil = 0;
  await assert.rejects(fixture.broker.prompt(created.id, { text: 'CEO message', request_key: 'paste-1' }), error => error.code === 'human_busy');
});

test('authentication prompt near top of a tall screen blocks task injection', async () => {
  const fixture = fakeBroker();
  const created = await fixture.broker.create({ host: 'local', transport: 'local', cwd: __dirname, cols: 80, rows: 30 });
  fixture.output('Sign in with ChatGPT\r\n  1. Continue\r\n');
  await assert.rejects(fixture.broker.prompt(created.id, { text: 'CEO message', request_key: 'login-1' }), error => error.code === 'waiting_for_agent');
});

test('headless screen interprets cursor movement and output resets after ring eviction', async () => {
  const fixture = fakeBroker({ maxOutput: 24 });
  const created = await session(fixture);
  fixture.output('old text\r\n');
  fixture.output('\x1b[2J\x1b[Hcurrent screen');
  fixture.output('\r\nlast visible line');
  const snapshot = await fixture.broker.snapshot(created.id);
  assert.ok(snapshot.screen.includes('current screen'));
  assert.ok(snapshot.screen.includes('last visible line'));
  assert.ok(!snapshot.screen.includes('old text'));
  assert.equal(snapshot.current_seq, 3);
  const reset = await fixture.broker.output(created.id, 0);
  assert.equal(reset.reset, true);
  assert.equal(reset.seq, snapshot.current_seq);
  assert.ok(reset.ansi.includes('current screen'));
  assert.equal((await fixture.broker.output(created.id, reset.seq)).data, '');
});

test('headless terminal answers cursor queries without a browser or a human typing lease', async () => {
  const fixture = fakeBroker();
  const created = await session(fixture);
  fixture.output('\x1b[6n');
  await fixture.broker.flushScreen(fixture.broker.get(created.id));
  await fixture.broker.get(created.id).writeQueue;
  assert.deepEqual(fixture.writes, ['\x1b[1;1R']);
  assert.equal(fixture.broker.get(created.id).humanLineLength, 0);
  assert.equal(fixture.broker.get(created.id).humanLeaseUntil, 0);
});

test('delta polling updates foreground metadata without probing every frame', async () => {
  let count = 0;
  let command = 'bash';
  const fixture = fakeBroker({ probe: async () => { count++; return { command, state: classifyCommand(command) }; } });
  const created = await session(fixture);
  const shell = await fixture.broker.output(created.id, 0);
  assert.equal(shell.state, 'shell');
  assert.equal(shell.status, 'shell');
  assert.equal(shell.connected, true);
  assert.equal(shell.agent_ready, false);
  assert.equal(shell.cols, 80);
  assert.equal(shell.rows, 10);
  command = 'codex';
  await Promise.all(Array.from({ length: 8 }, () => fixture.broker.output(created.id, shell.seq)));
  assert.equal(count, 1);
  fixture.broker.get(created.id).lastProbe = 0;
  const agent = await fixture.broker.output(created.id, shell.seq);
  assert.equal(agent.command, 'codex');
  assert.equal(agent.status, 'agent');
  assert.equal(agent.agent_ready, true);
  assert.equal(count, 2);
  fixture.broker.get(created.id).connected = false;
  fixture.broker.get(created.id).exitCode = 0;
  const closed = await fixture.broker.output(created.id, agent.seq);
  assert.equal(closed.connected, false);
  assert.equal(closed.status, 'exited');
  assert.equal(closed.exit_code, 0);
  assert.equal(count, 2);
});

test('resize parses remote reflow at new geometry and restores dimensions on failure', async () => {
  const fixture = fakeBroker();
  const created = await session(fixture);
  const current = fixture.broker.get(created.id);
  current.pty.resize = async (cols, rows) => {
    assert.equal(current.terminal.cols, cols);
    assert.equal(current.terminal.rows, rows);
    fixture.output('\x1b[2J\x1b[H' + '0123456789'.repeat(6));
    await fixture.broker.flushScreen(current);
  };
  await fixture.broker.resize(created.id, { cols: 100, rows: 12 });
  const resized = await fixture.broker.snapshot(created.id);
  assert.ok(resized.screen.includes('0123456789'.repeat(6)));
  current.pty.resize = async () => { throw new Error('remote resize failed'); };
  await assert.rejects(fixture.broker.resize(created.id, { cols: 50, rows: 8 }), /remote resize failed/);
  assert.equal(current.terminal.cols, 100);
  assert.equal(current.terminal.rows, 12);
  assert.equal(current.cols, 100);
  assert.equal(current.rows, 12);
});

test('uncertain SSH resize pauses human and CEO input until verified resize redraw succeeds', async () => {
  const fixture = fakeBroker();
  const created = await fixture.broker.create({ host: '316', transport: 'ssh', target: 'fake316', cwd: '/tmp', cols: 80, rows: 10 });
  const current = fixture.broker.get(created.id);
  current.pty.resize = async () => { throw new Error('resize result lost after kernel update'); };
  await assert.rejects(fixture.broker.resize(created.id, { cols: 100, rows: 20 }), /result lost/);
  const unknown = await fixture.broker.snapshot(created.id);
  assert.equal(unknown.geometry_unknown, true);
  assert.equal(unknown.blocked_reason, 'geometry_unknown');
  assert.equal(unknown.state, 'unknown');
  assert.equal(unknown.agent_ready, false);
  assert.equal(unknown.cols, 100, 'Do not pretend old dimensions were restored without confirmation');
  await assert.rejects(fixture.broker.input(created.id, { origin: 'ui', data: 'unsafe draft' }), error => error.code === 'geometry_unknown');
  await assert.rejects(fixture.broker.prompt(created.id, { text: 'unsafe task', request_key: 'unsafe-geometry' }), error => error.code === 'geometry_unknown');
  assert.equal(fixture.writes.length, 0);
  current.pty.resize = async () => { fixture.output('\x1b[2J\x1b[HCodex\r\n› '); };
  await fixture.broker.resize(created.id, { cols: 100, rows: 20 });
  assert.equal((await fixture.broker.snapshot(created.id)).geometry_unknown, false);
});

test('confirmed SSH rollback restores known dimensions but still requires a clean redraw', async () => {
  const fixture = fakeBroker();
  const created = await fixture.broker.create({ host: '316', transport: 'ssh', target: 'fake316', cwd: '/tmp', cols: 80, rows: 10 });
  const current = fixture.broker.get(created.id);
  current.pty.resize = async () => {
    const error = new Error('remote operation failed; rollback verified');
    error.geometryConfirmed = true; error.actualCols = 80; error.actualRows = 10;
    throw error;
  };
  await assert.rejects(fixture.broker.resize(created.id, { cols: 100, rows: 20 }), /rollback verified/);
  assert.equal(current.cols, 80);
  assert.equal(current.rows, 10);
  assert.equal(current.geometryUnknown, true);
});

test('SSH bootstrap uses the inherited-descriptor bridge and safely quotes directory data', () => {
  const sessionId = 'ceohub-0123456789abcdef01234567', bridgeId = '1234567890abcdef12345678';
  const cwd = "/tmp/project'quoted;still a path";
  const command = remoteBridgeCommand('attach', { sessionId, bridgeId, cols: 73, rows: 33, cwd });
  assert.ok(command.includes(shellQuote(cwd)));
  assert.ok(command.includes("'--bridge-id'"));
  assert.ok(command.includes("'attach'"));
  assert.ok(command.includes('zlib.decompress'));
  assert.ok(!command.includes('stty -F'));
  const encoded = /base64\.b64decode\([^A-Za-z0-9]+([A-Za-z0-9+/=]+)[^)]*\)/.exec(command)?.[1];
  assert.ok(encoded, 'Standalone helper is encoded, not installed or fetched remotely');
  const helper = zlib.inflateSync(Buffer.from(encoded, 'base64')).toString('utf8');
  assert.ok(helper.includes('subprocess.Popen(["tmux", "-u", "attach-session"'), 'UTF-8 display must be explicit even if SSH LANG is absent');
  assert.ok(helper.includes('apply_winsize(0, cols, rows)'), 'Resize uses the original inherited fd 0');
  assert.ok(helper.includes('SO_PEERCRED'), 'Private socket verifies the caller UID');
  assert.ok(command.length < 20000, 'Compressed standalone helper fits the Windows command line');
  assert.throws(() => remoteBridgeCommand('attach', { sessionId: 'user-existing-session', bridgeId, cols: 73, rows: 33 }), /Invalid owned/);
  assert.throws(() => remoteBridgeCommand('resize', { sessionId, bridgeId, cols: 10, rows: 33 }), /Expected an integer/);
});

test('plugin raw input requires an idempotency key and retries keep exact bytes', async () => {
  const fixture = fakeBroker();
  const created = await session(fixture);
  await assert.rejects(fixture.broker.input(created.id, { origin: 'plugin', data: '\x03' }), error => error.code === 'missing_request_key');
  const input = { origin: 'plugin', request_key: 'stop-1', data: '\x03' };
  await fixture.broker.input(created.id, input);
  assert.equal((await fixture.broker.input(created.id, input)).deduplicated, true);
  assert.deepEqual(fixture.writes, ['\x03']);
});

test('broker token stays backend-only and cross-origin browser requests are rejected', async t => {
  const fixture = fakeBroker();
  const server = createServer(fixture.broker, 'test-token');
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  const url = `http://127.0.0.1:${server.address().port}/sessions`;
  assert.equal((await fetch(url)).status, 401);
  assert.equal((await fetch(url, { headers: { Authorization: 'Bearer test-token', Origin: 'http://127.0.0.1:8766' } })).status, 403);
  assert.equal((await fetch(url, { headers: { Authorization: 'Bearer test-token' } })).status, 200);
});

test('owned remote session ID cannot be reused for another machine', async () => {
  const fixture = fakeBroker();
  const first = await fixture.broker.create({ transport: 'ssh', target: 'unit-test-host', host: '316', cwd: '/tmp' });
  await assert.rejects(fixture.broker.create({ transport: 'ssh', target: 'another-server', host: 'other', session_id: first.id }), error => error.code === 'session_conflict');
  await assert.rejects(fixture.broker.create({ transport: 'ssh', target: '-oProxyCommand=bad' }), error => error.code === 'invalid_target');
  await assert.rejects(fixture.broker.create({ transport: 'ssh', target: 'unit-test-host', session_id: 'user-existing-shell' }), error => error.code === 'invalid_session_id');
  assert.equal(shellQuote("/tmp/a'b"), "'/tmp/a'\\''b'");
  assert.equal(classifyCommand('codex.exe'), 'agent');
  assert.equal(classifyCommand('bash'), 'shell');
});
