'use strict';
// No real program/SSH is started. Every spawn below is a fake sink.
const test = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const { TerminalBroker, createServer, directRootIdentity } = require('../terminal_broker.cjs');

function fixture() {
  const calls = [], writes = [], kills = []; let output;
  const fake = { pid: 123, process: 'codex', write: data => writes.push(data), resize() {}, kill() { kills.push(123); },
    onData: callback => { output = callback; }, onExit() {} };
  const broker = new TerminalBroker({ spawn: (file, args, options) => { calls.push({ file, args, options }); return fake; },
    spawnRemote: () => { throw Error('no SSH'); }, probe: async () => ({ state: 'agent', command: 'codex' }) });
  return { broker, calls, writes, kills, output: data => output(data) };
}

test('direct local program preserves argument boundaries and does not wrap in a shell', async () => {
  const f = fixture();
  const args = ['resume', '11111111-1111-4111-8111-111111111111', '-c', 'value="a b; $()"'];
  const body = { program: process.execPath, args, host: 'local', transport: 'local', cwd: __dirname };
  const created = await f.broker.create(body);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].file, process.execPath);
  assert.deepEqual(f.calls[0].args, args);
  const replay = await f.broker.create({ ...body, session_id: created.id });
  assert.equal(replay.id, created.id);
  assert.equal(f.calls.length, 1);
  await assert.rejects(f.broker.create({ ...body, session_id: created.id, args: ['other'] }), error => error.code === 'session_conflict');
});

test('direct launch rejects SSH, relative executables and non-string or oversized arguments before spawning', async () => {
  const f = fixture();
  const valid = { program: process.execPath, args: [], cwd: __dirname, transport: 'local' };
  for (const change of [{ transport: 'ssh', target: 'fake' }, { program: 'codex' }, { args: 'string' },
    { args: [1] }, { args: ['\0'] }, { program: process.execPath + '\0' }, { args: Array(201).fill('x') },
    { args: ['x'.repeat(32769)] }, { args: Array(10).fill('x'.repeat(20000)) }]) {
    await assert.rejects(f.broker.create({ ...valid, ...change }), error => error.code === 'invalid_program');
  }
  assert.equal(f.calls.length, 0);
});

test('human busy is a boolean snapshot/output capability and never reveals the draft', async () => {
  const f = fixture();
  const created = await f.broker.create({ cwd: __dirname });
  await f.broker.input(created.id, { origin: 'ui', data: 'private draft' });
  let snapshot = await f.broker.snapshot(created.id);
  assert.equal(snapshot.human_busy, true);
  assert.equal(JSON.stringify(snapshot).includes('private draft'), false);
  f.broker.get(created.id).humanLeaseUntil = 0;
  assert.equal((await f.broker.output(created.id, snapshot.current_seq)).human_busy, true);
  await f.broker.input(created.id, { origin: 'ui', data: '\x03' });
  f.broker.get(created.id).humanLeaseUntil = 0;
  snapshot = await f.broker.snapshot(created.id);
  assert.equal(snapshot.human_busy, false);
});

test('conversation ownership and resume pickers block ordinary task prose', async () => {
  for (const screen of ['This conversation is open in another app\r\nClose it there and press R to continue here.\r\nr retry · f fork · esc exit',
    'Resume a session\r\nSearch conversations\r\nEnter to select · Esc to exit']) {
    const f = fixture();
    const created = await f.broker.create({ cwd: __dirname, rows: 30 });
    f.output(screen);
    await assert.rejects(f.broker.prompt(created.id, { text: 'must not send', request_key: 'menu' }), error => error.code === 'waiting_for_agent');
    assert.equal((await f.broker.snapshot(created.id)).agent_ready, false);
    assert.deepEqual(f.writes, []);
  }
});

test('new double-angle selector cannot turn a cropped startup menu into task input', async () => {
  for (const screen of ['Background server has incompatible feature settings\r\n» 1. Run without daemon this time',
    'Update available\r\n» 1. Update now\r\n  2. Skip']) {
    const f = fixture();
    const created = await f.broker.create({ cwd: __dirname, rows: 5, cols: 100 });
    f.output(screen);
    await assert.rejects(f.broker.prompt(created.id, { text: 'must not send', request_key: 'double-angle-menu' }), error => error.code === 'waiting_for_agent');
    assert.equal((await f.broker.snapshot(created.id)).blocked_reason, 'setup_menu');
    assert.deepEqual(f.writes, []);
  }
});

test('private broker health advertises new features without a protocol version bump', async () => {
  const f = fixture();
  const server = createServer(f.broker, 'fake-private-capability');
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const response = await fetch(`http://127.0.0.1:${server.address().port}/health`, { headers: { Authorization: 'Bearer fake-private-capability' } });
    const result = await response.json();
    assert.equal(result.version, '0.3.0');
    assert.deepEqual(result.features, ['direct_program', 'input_busy', 'idle_close']);
  } finally {
    await new Promise(resolve => server.close(resolve));
  }
});

test('idle handoff checks input again inside the close queue without killing a changed terminal', async () => {
  const f = fixture();
  const created = await f.broker.create({ cwd: __dirname });
  // Avoid awaiting a queued write from within the probe: human draft bookkeeping
  // happens synchronously even when its PTY write waits behind this operation.
  f.broker.probe = async () => {
    f.broker.input(created.id, { origin: 'ui', data: 'new draft' });
    return { state: 'agent', command: 'codex' };
  };
  await assert.rejects(f.broker.close(created.id, { require_idle: true }), error => error.code === 'human_busy');
  assert.equal(f.broker.get(created.id).connected, true);
  assert.deepEqual(f.kills, []);
  await f.broker.get(created.id).writeQueue;
  f.broker.probe = async () => ({ state: 'agent', command: 'codex' });
  await f.broker.input(created.id, { origin: 'ui', data: '\x03' });
  f.broker.get(created.id).humanLeaseUntil = 0;
  assert.equal((await f.broker.close(created.id, { require_idle: true })).closed, true);
  assert.deepEqual(f.kills, [123]);
});

async function nativeFixture(platform, options = {}) {
  const program = platform === 'win32' ? path.resolve(__dirname, 'codex.exe') : '/fake/codex';
  const calls = [], writes = []; let output;
  const fake = { pid: 123, process: path.basename(program), write: data => writes.push(data), resize() {}, kill() {},
    onData: callback => { output = callback; }, onExit() {} };
  const metadata = { pid: 123, name: 'codex.exe', executable: program, expectedExecutable: program,
    startTicks: 1234567, pgid: 123, tpgid: 123 };
  const broker = new TerminalBroker({ platform, spawn: () => fake,
    resolveProgramPath: value => value,
    runCommand: async (file, args) => {
      calls.push({ file, args });
      return { stdout: platform === 'win32' ? JSON.stringify(metadata) : '123 1 123 123 codex\n' };
    }, readRootMetadata: () => ({ ...metadata }), ...options });
  const created = await broker.create({ program, args: ['resume', 'exact-session'], cwd: __dirname });
  return { broker, metadata, calls, writes, id: created.id, current: broker.get(created.id), output: data => output(data) };
}

test('Windows direct native root is proven by filtered exact executable/PID/creation-time and accepts new » input UI', async () => {
  const f = await nativeFixture('win32');
  f.output('Codex\r\n» Ask Codex to do anything');
  const snapshot = await f.broker.snapshot(f.id);
  assert.equal(snapshot.agent_ready, true);
  assert.equal(snapshot.command, 'codex.exe');
  assert.equal(f.calls.length, 1);
  assert.match(f.calls[0].args.at(-1), /-Filter "ProcessId=123"/);
  assert.match(f.calls[0].args.at(-1), /-Property ProcessId,Name,ExecutablePath,CreationDate/);
  assert.doesNotMatch(f.calls[0].args.at(-1), /CommandLine|ParentProcessId|\$all/);
  assert.equal(JSON.stringify(snapshot).includes(f.metadata.executable), false, 'Proof executable remains internal metadata');
  assert.equal((await f.broker.prompt(f.id, { text: 'authorized test', request_key: 'native-root' })).state, 'submitted');
  assert.equal(f.calls.length, 3, 'Task delivery uses two additional fresh identity probes');
  assert.deepEqual(f.writes, ['\x1b[200~authorized test\x1b[201~\r']);
});

test('Windows direct root exact path, PID, name and creation-time failures cannot be excused by a Codex screen', async () => {
  for (const change of [{ executable: 'C:\\other\\codex.exe' }, { pid: 456 }, { name: 'node.exe' },
    { startTicks: 0 }, { startTicks: Number.MAX_SAFE_INTEGER + 1 }]) {
    const f = await nativeFixture('win32');
    Object.assign(f.metadata, change);
    f.output('Codex\r\n» Ask anything');
    const view = await f.broker.snapshot(f.id);
    assert.equal(view.agent_ready, false);
    assert.equal(view.state, 'unknown');
    await assert.rejects(f.broker.prompt(f.id, { text: 'not allowed', request_key: 'bad-root' }), error => error.code === 'agent_not_confirmed');
    assert.deepEqual(f.writes, []);
  }
});

test('Windows direct native process replacement between fresh probes is denied without using cached name or screen', async () => {
  const f = await nativeFixture('win32');
  let samples = 0;
  f.broker.runCommand = async () => ({ stdout: JSON.stringify({ ...f.metadata, startTicks: ++samples === 1 ? 1234567 : 9999999 }) });
  f.output('Codex\r\n» Ask anything');
  await assert.rejects(f.broker.prompt(f.id, { text: 'must stay with original process', request_key: 'native-replaced' }), error => error.code === 'agent_not_confirmed');
  assert.equal(samples, 2);
  assert.deepEqual(f.writes, []);
  assert.equal(f.current.state, 'unknown');
});

test('Windows native root still needs real input UI and shell cursor or startup menu still blocks it', async () => {
  for (const [screen, state, reason] of [
    ['Starting Codex', 'unknown', null],
    ['Codex\r\n» Ask anything\r\nPS C:\\project> ', 'shell', null],
    ['Background server has incompatible feature settings\r\n1. Run without daemon this time\r\n2. Restart with these settings\r\n» 3. Cancel', 'agent', 'setup_menu'],
    ['This conversation is open in another app\r\nClose it there and press R to continue here.\r\n» retry', 'agent', 'session_locked'],
  ]) {
    const f = await nativeFixture('win32');
    f.output(screen);
    const view = await f.broker.snapshot(f.id);
    assert.equal(view.state, state);
    assert.equal(view.agent_ready, false);
    assert.equal(view.blocked_reason, reason);
    await assert.rejects(f.broker.prompt(f.id, { text: 'must not write into this screen', request_key: 'native-ui-fence' }));
    assert.deepEqual(f.writes, []);
  }
});

test('new Windows » marker does not turn generic node or a background agent with a shell cursor into a ready agent', async () => {
  for (const [names, screen] of [
    [['node.exe'], 'Codex mentioned as text\r\n» Ask anything'],
    [['codex.exe'], 'Codex\r\n» Ask anything\r\nPS C:\\project> '],
  ]) {
    const f = await nativeFixture('win32');
    f.current.program = path.resolve(__dirname, 'node.exe');
    f.current.pty.process = 'node.exe';
    f.broker.runCommand = async () => ({ stdout: JSON.stringify(names) });
    f.output(screen);
    assert.equal((await f.broker.snapshot(f.id)).agent_ready, false);
    await assert.rejects(f.broker.prompt(f.id, { text: 'not a foreground agent', request_key: 'generic-node' }));
    assert.deepEqual(f.writes, []);
    assert.equal(f.current.directRootProof, undefined);
  }
});

test('Linux directly launched native root is included only with current foreground group and exact /proc identity', async () => {
  const f = await nativeFixture('linux');
  f.output('Codex\r\n» Ask anything');
  const view = await f.broker.snapshot(f.id);
  assert.equal(view.agent_ready, true);
  assert.equal(f.current.agentIdentity.pid, 123);
  assert.equal((await f.broker.prompt(f.id, { text: 'foreground only', request_key: 'linux-root' })).state, 'submitted');
  assert.deepEqual(f.writes, ['\x1b[200~foreground only\x1b[201~\r']);
});

test('Linux native root not in foreground or with changed executable/start ticks fails closed even if children share an agent name', async () => {
  for (const change of ['background', 'path', 'start']) {
    const f = await nativeFixture('linux');
    f.output('Codex\r\n» Ask anything');
    if (change === 'background') f.broker.runCommand = async () => ({ stdout: '123 1 123 456 codex\n456 123 456 456 codex\n' });
    if (change === 'path') f.metadata.executable = '/other/codex';
    if (change === 'start') {
      await f.broker.snapshot(f.id);
      f.metadata.startTicks++;
      f.current.lastProbe = 0;
    }
    assert.equal((await f.broker.snapshot(f.id)).agent_ready, false);
    await assert.rejects(f.broker.prompt(f.id, { text: 'must stay with owned root', request_key: 'linux-fence' }));
    assert.deepEqual(f.writes, []);
  }
});

test('pure direct root helper never accepts node scripts or non-owned/native process identities', () => {
  const session = { transport: 'local', program: 'C:\\Codex\\codex.exe', pty: { pid: 123 } };
  const meta = { pid: 123, name: 'codex.exe', startTicks: 123456, executable: 'c:\\codex\\CODEX.exe', expectedExecutable: session.program };
  assert.equal(directRootIdentity(session, meta, 'win32').pid, 123);
  for (const change of [{ program: 'C:\\Codex\\node.exe' }, { program: 'C:\\Codex\\codex.cmd' }, { transport: 'ssh' }]) {
    assert.equal(directRootIdentity({ ...session, ...change }, meta, 'win32'), null);
  }
  assert.equal(directRootIdentity(session, { ...meta, expectedExecutable: 'C:\\Codex\\codex.js' }, 'win32'), null);
});
