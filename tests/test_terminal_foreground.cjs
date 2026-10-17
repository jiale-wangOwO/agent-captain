'use strict';
// Fake terminal sink only: no SSH process, PTY, model, or real agent message.
const test = require('node:test');
const assert = require('node:assert/strict');
const { TerminalBroker } = require('../terminal/terminal_broker.cjs');

async function fixture(result) {
  const writes = [], stats = { probes: 0 }; let output;
  const fake = { pid: 123, process: 'ssh', write: text => writes.push(text), resize() {}, kill() {},
    onData: callback => { output = callback; }, onExit() {} };
  const broker = new TerminalBroker({ spawnRemote: () => fake,
    probe: async () => { stats.probes++; return result; } });
  const created = await broker.create({ host: '316', transport: 'ssh', target: 'fake316', cwd: '/tmp', cols: 80, rows: 30 });
  const current = broker.get(created.id);
  return { broker, writes, current, id: created.id, stats, output: text => output(text) };
}

const codexViaNode = { command: 'node', state: 'agent', agent_kind: 'codex', agent_pid: 201, agent_start_ticks: 10201,
  foreground_pgid: 200, identity_verified: true, reason: 'verified_foreground_codex' };

const daemonSetupMenu = 'Background server has incompatible feature settings\r\n'
  + 'requires api_key_model_discovery disabled\r\n'
  + '  1. Run without daemon this time\r\n'
  + '  2. Restart with these settings\r\n'
  + '› 3. Cancel\r\n';

const updateSetupMenu = 'Update available · 0.160.0 → 0.160.1\r\n'
  + '  1. Update now (runs `npm install -g @openai/codex`)\r\n'
  + '› 2. Skip\r\n'
  + '  3. Skip until next version\r\n';

test('verified foreground node-launcher Codex accepts ordinary task once without relabeling generic node', async () => {
  const f = await fixture(codexViaNode), body = { text: '检查这个项目的进展。', request_key: 'node-task-1' };
  const result = await f.broker.prompt(f.id, body);
  assert.equal(result.state, 'submitted');
  const replay = await f.broker.prompt(f.id, body);
  assert.equal(replay.deduplicated, true);
  assert.deepEqual(f.writes, ['\x1b[200~检查这个项目的进展。\x1b[201~\r']);
  assert.equal(f.current.command, 'node');
  assert.equal(f.current.state, 'agent');
  assert.equal(f.stats.probes, 2, 'Verified PID/start-time/group must be checked again immediately before task input');
});

test('plain foreground node has no permission to receive ordinary agent task prose', async () => {
  const f = await fixture({ command: 'node', state: 'unknown', reason: 'not_agent' });
  await assert.rejects(f.broker.prompt(f.id, { text: 'fix this bug', request_key: 'plain-node' }), error => error.code === 'agent_not_confirmed');
  assert.deepEqual(f.writes, []);
});

test('foreground shell remains guarded even when a background Codex exists', async () => {
  const f = await fixture({ command: 'bash', state: 'shell', reason: 'foreground_shell' });
  await assert.rejects(f.broker.prompt(f.id, { text: 'fix this bug', request_key: 'shell-background' }), error => error.code === 'agent_not_started');
  assert.deepEqual(f.writes, []);
});

test('process-race or parse failure does not reuse a previously trusted Codex state', async () => {
  const f = await fixture(codexViaNode);
  await f.broker.refreshForeground(f.current, true);
  f.broker.probe = async () => { throw new Error('invalid or changed process metadata'); };
  await assert.rejects(f.broker.prompt(f.id, { text: 'must not be delivered', request_key: 'probe-race' }), error => error.code === 'agent_not_confirmed');
  assert.deepEqual(f.writes, []);
  assert.equal(f.current.state, 'unknown');
});

test('a timed-out foreground check clears readiness until a fresh process proof succeeds', async () => {
  const f = await fixture(codexViaNode);
  await f.broker.refreshForeground(f.current, true);
  assert.equal(f.broker.describe(f.current).agent_ready, true);
  let checks = 0;
  f.broker.probe = async () => {
    checks++;
    throw Object.assign(new Error('foreground query exceeded its budget'), { code: 'ETIMEDOUT', killed: true });
  };
  await assert.rejects(f.broker.prompt(f.id, { text: 'do not reuse the old proof', request_key: 'timeout-fence' }),
    error => error.code === 'agent_not_confirmed');
  assert.equal(f.current.state, 'unknown');
  assert.equal(f.current.agentIdentity, null);
  assert.equal(f.broker.describe(f.current).agent_ready, false);
  assert.equal(checks, 1);
  assert.deepEqual(f.writes, []);

  f.broker.probe = async () => { checks++; return { ...codexViaNode, agent_pid: 301, agent_start_ticks: 10301 }; };
  await f.broker.refreshForeground(f.current, true);
  assert.equal(checks, 2, 'Recovery requires a new identity query, not restoration of a cached ready state');
  assert.equal(f.broker.describe(f.current).agent_ready, true);
  assert.equal(f.current.agentIdentity.pid, 301);
  assert.equal(f.current.agentIdentity.startTicks, 10301);
  assert.deepEqual(f.writes, [], 'Checking and recovering readiness never sends task input');
});

test('a timeout during the final fresh identity check prevents task input', async () => {
  const f = await fixture(codexViaNode);
  let checks = 0;
  f.broker.probe = async () => {
    if (++checks === 1) return codexViaNode;
    throw Object.assign(new Error('final foreground query timed out'), { code: 'ETIMEDOUT', killed: true });
  };
  await assert.rejects(f.broker.prompt(f.id, { text: 'must not be delivered', request_key: 'final-timeout-fence' }),
    error => error.code === 'agent_not_confirmed');
  assert.equal(checks, 2);
  assert.equal(f.current.state, 'unknown');
  assert.equal(f.current.agentIdentity, null);
  assert.equal(f.broker.describe(f.current).agent_ready, false);
  assert.deepEqual(f.writes, []);
});

test('last-moment PID, start time, or foreground group change prevents submitting into a replacement process', async () => {
  for (const changed of [{ agent_pid: 999 }, { agent_start_ticks: 99999 }, { foreground_pgid: 999 }]) {
    const f = await fixture(codexViaNode);
    let calls = 0;
    f.broker.probe = async () => ++calls === 1 ? codexViaNode : { ...codexViaNode, ...changed };
    await assert.rejects(f.broker.prompt(f.id, { text: 'must stay with selected worker', request_key: 'identity-race' }), error => error.code === 'agent_not_confirmed');
    assert.equal(calls, 2);
    assert.deepEqual(f.writes, []);
  }
});

test('concurrent read clearing identity during first screen flush cannot skip the final fresh probe', async () => {
  const f = await fixture(codexViaNode);
  const flush = f.broker.flushScreen.bind(f.broker);
  let flushes = 0, probes = 0;
  f.broker.probe = async () => ++probes === 1 ? codexViaNode : { command: 'bash', state: 'shell', identity_verified: false, reason: 'foreground_shell' };
  f.broker.flushScreen = async session => {
    await flush(session);
    if (++flushes === 1) { session.state = 'shell'; session.agentIdentity = null; }
  };
  await assert.rejects(f.broker.prompt(f.id, { text: 'must not become shell input', request_key: 'first-flush-race' }), error => error.code === 'agent_not_confirmed');
  assert.equal(probes, 2, 'Capture the first fresh proof before awaiting screen parsing; final verification cannot be made conditional on a mutable cache');
  assert.deepEqual(f.writes, []);
});

test('concurrent identity mutation during second screen flush is rejected immediately before PTY write', async () => {
  for (const change of ['clear_identity', 'change_pid', 'return_to_shell']) {
    const f = await fixture(codexViaNode);
    const flush = f.broker.flushScreen.bind(f.broker);
    let flushes = 0;
    f.broker.flushScreen = async session => {
      await flush(session);
      if (++flushes === 2) {
        if (change === 'clear_identity') session.agentIdentity = null;
        else if (change === 'change_pid') session.agentIdentity = { ...session.agentIdentity, pid: 999 };
        else session.state = 'shell';
      }
    };
    await assert.rejects(f.broker.prompt(f.id, { text: 'must stay with verified worker', request_key: 'second-flush-race' }), error => error.code === 'agent_not_confirmed');
    assert.equal(f.stats.probes, 2);
    assert.deepEqual(f.writes, []);
  }
});

test('login screen appearing during final identity probe still blocks task input', async () => {
  const f = await fixture(codexViaNode); let calls = 0;
  f.broker.probe = async () => {
    if (++calls === 2) f.output('Sign in with ChatGPT\r\n1. Continue\r\n');
    return codexViaNode;
  };
  await assert.rejects(f.broker.prompt(f.id, { text: 'must not become login input', request_key: 'late-login' }), error => error.code === 'waiting_for_agent');
  assert.deepEqual(f.writes, []);
});

test('human input during the final identity probe takes precedence without queue deadlock', async () => {
  const f = await fixture(codexViaNode); let calls = 0, humanInput;
  f.broker.probe = async () => {
    if (++calls === 2) humanInput = f.broker.input(f.id, { origin: 'ui', data: 'human draft' });
    return codexViaNode;
  };
  await assert.rejects(f.broker.prompt(f.id, { text: 'CEO task', request_key: 'late-human' }), error => error.code === 'human_busy');
  await humanInput;
  assert.deepEqual(f.writes, ['human draft']);
});

test('login or model-choice menu still blocks verified node-launcher Codex task input', async () => {
  for (const screen of ['Sign in with ChatGPT\r\n 1. Continue\r\n', 'Choose a model\r\n 1. Provider A\r\n']) {
    const f = await fixture(codexViaNode);
    f.output(screen);
    await assert.rejects(f.broker.prompt(f.id, { text: 'do work', request_key: 'login-fence' }), error => error.code === 'waiting_for_agent');
    assert.deepEqual(f.writes, []);
  }
});

test('daemon compatibility startup menu is not ready for task prose regardless of selected option', async () => {
  const screens = [daemonSetupMenu,
    daemonSetupMenu.replace('  2. Restart', '› 2. Restart').replace('› 3. Cancel', '  3. Cancel'),
    '\x1b[?1049hBACKGROUND SERVER HAS INCOMPATIBLE\r\nFEATURE SETTINGS\r\n'
      + 'requires api_key_model_discovery disabled\r\n'
      + '❯ 1. Run without the daemon\r\nthis time\r\n'
      + '  2. Restart with these settings\r\n  3. Cancel\r\n'];
  for (const screen of screens) {
    const f = await fixture(codexViaNode);
    f.output(screen);
    const view = await f.broker.snapshot(f.id);
    assert.equal(view.state, 'agent', 'Process identity remains Codex while its startup menu blocks readiness');
    assert.equal(view.agent_ready, false);
    assert.equal(view.blocked_reason, 'setup_menu');
    await assert.rejects(f.broker.prompt(f.id, { text: 'do work', request_key: 'daemon-startup' }), error => error.code === 'waiting_for_agent');
    assert.deepEqual(f.writes, [], 'Neither tasks nor startup-menu selections are sent automatically');
  }
});

test('daemon startup menu appearing during final identity probe still blocks task input', async () => {
  const f = await fixture(codexViaNode); let calls = 0;
  f.broker.probe = async () => {
    if (++calls === 2) f.output(daemonSetupMenu);
    return codexViaNode;
  };
  await assert.rejects(f.broker.prompt(f.id, { text: 'must not become a startup selection', request_key: 'late-daemon-menu' }), error => error.code === 'waiting_for_agent');
  assert.deepEqual(f.writes, []);
});

test('clearing the daemon setup menu permits one ordinary task without replaying a menu choice', async () => {
  const f = await fixture(codexViaNode), body = { text: 'continue the project task', request_key: 'after-daemon-menu' };
  f.output(daemonSetupMenu);
  await assert.rejects(f.broker.prompt(f.id, body), error => error.code === 'waiting_for_agent');
  assert.deepEqual(f.writes, []);
  f.output('\x1b[2J\x1b[HCodex\r\n› ');
  const view = await f.broker.snapshot(f.id);
  assert.equal(view.agent_ready, true);
  assert.equal(view.blocked_reason, null);
  assert.equal((await f.broker.prompt(f.id, body)).state, 'submitted');
  assert.equal((await f.broker.prompt(f.id, body)).deduplicated, true);
  assert.deepEqual(f.writes, ['\x1b[200~continue the project task\x1b[201~\r']);
});

test('daemon compatibility prose or unrelated numbered choices alone are not setup menus', async () => {
  const screens = [
    'The background server has incompatible feature settings.\r\n'
      + 'You can run without daemon this time or restart with these settings.\r\n› ',
    'Options discussed in the report:\r\n  1. Run without daemon this time\r\n'
      + '  2. Restart with these settings\r\n› ',
  ];
  for (const screen of screens) {
    const f = await fixture(codexViaNode);
    f.output(screen);
    const view = await f.broker.snapshot(f.id);
    assert.equal(view.agent_ready, true);
    assert.equal(view.blocked_reason, null);
    await f.broker.prompt(f.id, { text: 'ordinary task', request_key: 'daemon-report-prose' });
    assert.deepEqual(f.writes, ['\x1b[200~ordinary task\x1b[201~\r']);
  }
});

test('update startup choices block readiness and task prose without installing or skipping', async () => {
  const screens = [updateSetupMenu,
    updateSetupMenu.replace('  1. Update', '› 1. Update').replace('› 2. Skip', '  2. Skip'),
    updateSetupMenu.replace('› 2. Skip', '  2. Skip').replace('  3. Skip', '› 3. Skip'),
    '\x1b[?1049hUPDATE\r\nAVAILABLE · 0.160.0 →\r\n0.160.1\r\n'
      + '❯ 1. UPDATE\r\nNOW (runs `npm install -g @openai/codex`)\r\n'
      + '  2. SKIP\r\n  3. SKIP UNTIL NEXT VERSION\r\n'];
  for (const screen of screens) {
    const f = await fixture(codexViaNode);
    f.output(screen);
    const view = await f.broker.snapshot(f.id);
    assert.equal(view.state, 'agent');
    assert.equal(view.agent_ready, false);
    assert.equal(view.blocked_reason, 'setup_menu');
    await assert.rejects(f.broker.prompt(f.id, { text: 'do work', request_key: 'update-startup' }), error => error.code === 'waiting_for_agent');
    assert.deepEqual(f.writes, [], 'No task text, installer command, or skip choice may be automatic');
  }
});

test('update startup menu appearing during final identity probe blocks task input', async () => {
  const f = await fixture(codexViaNode); let calls = 0;
  f.broker.probe = async () => {
    if (++calls === 2) f.output(updateSetupMenu);
    return codexViaNode;
  };
  await assert.rejects(f.broker.prompt(f.id, { text: 'must not become an update choice', request_key: 'late-update-menu' }), error => error.code === 'waiting_for_agent');
  assert.deepEqual(f.writes, []);
});

test('clearing the update menu permits one task without replaying a startup choice', async () => {
  const f = await fixture(codexViaNode), body = { text: 'continue without upgrading', request_key: 'after-update-menu' };
  f.output(updateSetupMenu);
  await assert.rejects(f.broker.prompt(f.id, body), error => error.code === 'waiting_for_agent');
  assert.deepEqual(f.writes, []);
  f.output('\x1b[2J\x1b[HCodex\r\n› ');
  const view = await f.broker.snapshot(f.id);
  assert.equal(view.agent_ready, true);
  assert.equal(view.blocked_reason, null);
  assert.equal((await f.broker.prompt(f.id, body)).state, 'submitted');
  assert.equal((await f.broker.prompt(f.id, body)).deduplicated, true);
  assert.deepEqual(f.writes, ['\x1b[200~continue without upgrading\x1b[201~\r']);
});

test('update prose or numbered choices without the startup title are not setup menus', async () => {
  const screens = [
    'Update available · 0.160.0 → 0.160.1\r\n'
      + 'The release notes describe Update now and Skip until next version.\r\n› ',
    'Project checklist:\r\n  1. Update now\r\n  2. Skip\r\n› ',
    'Update available · 0.160.0 → 0.160.1\r\n  1. Update now\r\n'
      + '  2. Review release notes\r\n› ',
  ];
  for (const screen of screens) {
    const f = await fixture(codexViaNode);
    f.output(screen);
    const view = await f.broker.snapshot(f.id);
    assert.equal(view.agent_ready, true);
    assert.equal(view.blocked_reason, null);
    await f.broker.prompt(f.id, { text: 'ordinary project task', request_key: 'update-report-prose' });
    assert.deepEqual(f.writes, ['\x1b[200~ordinary project task\x1b[201~\r']);
  }
});

test('approval prompt remains human-owned after node-launcher recognition', async () => {
  const f = await fixture(codexViaNode);
  f.output('Would you like to run this command?\r\n1. Yes, proceed\r\n');
  await assert.rejects(f.broker.prompt(f.id, { text: 'yes', request_key: 'approval-fence' }), error => error.code === 'waiting_for_agent');
  assert.deepEqual(f.writes, []);
});

test('human unfinished input takes precedence over verified Codex foreground', async () => {
  const f = await fixture(codexViaNode);
  await f.broker.input(f.id, { origin: 'ui', data: 'unfinished question' });
  f.current.humanLeaseUntil = 0;
  const before = f.writes.length;
  await assert.rejects(f.broker.prompt(f.id, { text: 'CEO task', request_key: 'human-fence' }), error => error.code === 'human_busy');
  assert.equal(f.writes.length, before);
});

test('unknown geometry prevents both task prose and raw keys before any foreground probe', async () => {
  const f = await fixture(codexViaNode);
  f.current.geometryUnknown = true;
  await assert.rejects(f.broker.prompt(f.id, { text: 'CEO task', request_key: 'geometry-fence' }), error => error.code === 'geometry_unknown');
  await assert.rejects(f.broker.input(f.id, { origin: 'plugin', data: '\x03', request_key: 'raw-geometry-fence' }), error => error.code === 'geometry_unknown');
  assert.equal(f.stats.probes, 0);
  assert.deepEqual(f.writes, []);
});
