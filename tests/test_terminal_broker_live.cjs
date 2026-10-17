'use strict';

// Explicit smoke test against the running broker. Uses its own temporary shell
// sessions, only echo/stty/sleep, and closes them on completion.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const { descriptorPath } = require('../terminal/terminal_broker.cjs');
const descriptor = JSON.parse(fs.readFileSync(descriptorPath(), 'utf8'));
const sessions = [];
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
async function api(endpoint, body) {
  const response = await fetch(descriptor.url + endpoint, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { Authorization: `Bearer ${descriptor.token}`, 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    signal: AbortSignal.timeout(20000),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(`${result.error}: ${result.message}`);
  return result;
}
async function waitScreen(id, expression, timeout = 20000, state) {
  const end = Date.now() + timeout;
  let screen;
  while (Date.now() < end) {
    screen = await api(`/sessions/${id}/snapshot?lines=120`);
    if (expression.test(screen.screen) && (state == null || screen.state === state)) return screen;
    await delay(250);
  }
  throw new Error(`Expected ${expression} not found in smoke session: ${screen?.screen}`);
}
function assertCleanRemotePrompt(view) {
  const last = view.screen.trimEnd().split('\n').at(-1);
  assert.match(last, /[$#]\s*$/, 'Shell prompt must be empty; device replies must never become a readline draft');
  assert.ok(!/(?:1;2c|0;\d+;0c)/.test(last), 'Leaked terminal DA reply payload in shell draft');
}

(async () => {
  try {
    const local = await api('/sessions/create', { host: 'local', transport: 'local', cwd: __dirname, title: 'Local terminal smoke test', cols: 90, rows: 24 });
    sessions.push(local.id);
    await waitScreen(local.id, /PS /);
    await api(`/sessions/${local.id}/input`, { origin: 'ui', data: "Write-Output ('LOCAL_' + 'PTY_OK')\r" });
    const localView = await waitScreen(local.id, /^LOCAL_PTY_OK$/m);
    assert.equal(localView.connected, true);
    assert.equal(localView.state, 'shell');
    assert.ok(localView.ansi.includes('LOCAL_PTY_OK'));
    await api(`/sessions/${local.id}/resize`, { cols: 100, rows: 30 });
    const newClientView = await api(`/sessions/${local.id}/snapshot`);
    assert.equal(newClientView.cols, 100);
    assert.ok(newClientView.screen.includes('LOCAL_PTY_OK'));
    console.log(`Local ConPTY verified: ${local.id}; independent client reconnect and resize preserved output`);

    if (process.env.CEO_HUB_TEST_SSH_TARGET) {
      const target = process.env.CEO_HUB_TEST_SSH_TARGET;
      const remote = await api('/sessions/create', { host: 'smoke-remote', transport: 'ssh', target, cwd: '/tmp', title: 'Remote terminal smoke test', cols: 90, rows: 24 });
      sessions.push(remote.id);
      await waitScreen(remote.id, /[$#]\s*$/m);
      await delay(800);
      assertCleanRemotePrompt(await api(`/sessions/${remote.id}/snapshot`));
      await api(`/sessions/${remote.id}/input`, { origin: 'ui', data: "printf 'REMOTE_%s\\n' 'PTY_OK'; stty size\r" });
      const remoteView = await waitScreen(remote.id, /^REMOTE_PTY_OK$/m, 20000, 'shell');
      assert.equal(remoteView.state, 'shell');
      assert.ok(/24 90/.test(remoteView.screen));
      await api(`/sessions/${remote.id}/resize`, { cols: 104, rows: 32 });
      await delay(300);
      assertCleanRemotePrompt(await api(`/sessions/${remote.id}/snapshot`));
      await api(`/sessions/${remote.id}/input`, { origin: 'ui', data: "printf 'RESIZED_%s\\n' \"$(stty size)\"\r" });
      await waitScreen(remote.id, /^RESIZED_32 104$/m);
      // A fresh client attaches to the exact owned session, rather than creating
      // another shell; the original tmux session and terminal ID are unchanged.
      const same = await api('/sessions/create', { host: 'smoke-remote', transport: 'ssh', target, cwd: '/tmp', session_id: remote.id });
      assert.equal(same.id, remote.id);
      assert.equal(same.pid, remote.pid);
      await api(`/sessions/${remote.id}/input`, { origin: 'ui', data: "CEO_HUB_SMOKE_TOKEN='persistent-value'\r" });
      await delay(300);
      process.kill(remote.pid);
      const end = Date.now() + 8000;
      while (Date.now() < end && (await api(`/sessions/${remote.id}/snapshot`)).connected) await delay(100);
      assert.equal((await api(`/sessions/${remote.id}/snapshot`)).connected, false);
      const reattached = await api('/sessions/create', { host: 'smoke-remote', transport: 'ssh', target, cwd: '/tmp', session_id: remote.id });
      assert.equal(reattached.id, remote.id);
      assert.notEqual(reattached.pid, remote.pid);
      await waitScreen(remote.id, /[$#]\s*$/m);
      await delay(800);
      assertCleanRemotePrompt(await api(`/sessions/${remote.id}/snapshot`));
      await api(`/sessions/${remote.id}/input`, { origin: 'ui', data: "printf 'RESUMED_%s\\n' \"$CEO_HUB_SMOKE_TOKEN\"\r" });
      await waitScreen(remote.id, /^RESUMED_persistent-value$/m);
      console.log(`Remote SSH/tmux verified: ${remote.id}; TTY=24x90→32x104; empty prompts after attach/resize/reconnect; killed SSH transport, shell variable survived`);
    }
  } finally {
    for (const id of sessions.reverse()) await api(`/sessions/${id}/close`, {}).catch(error => console.error(`Cleanup ${id}: ${error.message}`));
  }
})().catch(error => { console.error(error.message); process.exitCode = 1; });
