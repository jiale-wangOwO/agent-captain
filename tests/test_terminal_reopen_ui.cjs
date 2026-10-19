'use strict';
// Actual shipped UI with inert DOM/terminal/HTTP doubles. No live sessions.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const storageKey = 'agent-captain.pending-terminal-reopen.v1';
const source = fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8');
function element(tagName = 'div') {
  return {tagName, value: '', checked: false, hidden: false, disabled: false, textContent: '', children: [], childElementCount: 0, clientWidth: 900, dataset: {}, style: {}, listeners: new Map(),
    classList: {add() {}, remove() {}, toggle() {}}, append(...items) { this.children.push(...items); this.childElementCount = this.children.length; }, replaceChildren(...items) { this.children = items; this.childElementCount = items.length; }, setAttribute() {}, contains() { return false; }, reset() {}, focus() {}, select() {},
    addEventListener(type, callback) { const group = this.listeners.get(type) || []; group.push(callback); this.listeners.set(type, group); }, removeEventListener() {},
    showModal() { this.open = true; }, close() { this.open = false; this.emit('close'); }, emit(type, event = {}) { for (const callback of this.listeners.get(type) || []) callback(event); },
    getBoundingClientRect: () => ({left: 0, top: 0, right: 900, bottom: 600})};
}
const worker = (id = 'a', extra = {}) => ({host: 'remote', thread_id: id, terminal_id: 'ceohub-old-' + id, mode: 'terminal', alias: 'Named work ' + id, pinned: true, cwd: '/home/example/project', connected: true, agent_ready: true, command: 'codex', ...extra});
const response = (value, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => value});
const descendants = item => [item, ...(item.children || []).flatMap(descendants)];
const posts = h => h.calls.filter(call => call.url === '/api/terminals/reopen');
const view = h => vm.runInContext('terminalSession', h.context);
const pending = h => JSON.parse(h.storage.get(storageKey) || '[]');
const op = h => vm.runInContext('reopenTarget.operation', h.context);
const tick = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
function confirm(h) { h.get('#reopen-approval').checked = true; h.get('#reopen-approval').onchange(); }
function receipt(body, extra = {}) { return {host: body.host, thread_id: body.thread_id, terminal_id: 'ceohub-new-' + body.thread_id, previous_terminal_id: body.expected_terminal_id, operation: 'reopen_terminal', operation_id: body.request_key, operation_state: 'done', reopened: true, connected: true, state: 'shell', status: 'shell', agent_ready: false, command: 'bash', recovery: {code: 'reopened', message: 'New shell only'}, ...extra}; }
async function harness(options = {}) {
  const elements = new Map(), calls = [], storage = options.storage || new Map(), timers = new Map(), terminals = [], windowListeners = {};
  const agents = options.agents || [worker(), worker('b')], hosts = [{id: 'remote', label: 'Lab server', transport: 'ssh', connected: true}];
  let uuid = 0, timer = 0;
  const get = selector => { if (!elements.has(selector)) { const item = element(); item.id = selector.slice(1); elements.set(selector, item); } return elements.get(selector); };
  const cancels = [element('button'), element('button')]; for (const item of cancels) item.dataset.close = 'reopen-dialog';
  class Terminal {
    constructor() { this.cols = 100; this.rows = 30; this.options = {}; this.focusCount = 0; this.writes = []; this.pastes = []; this.parser = {registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {}}; terminals.push(this); }
    open() {} loadAddon() {} focus() { this.focusCount++; } blur() {} reset() {} dispose() { this.disposed = true; } attachCustomKeyEventHandler() {} attachCustomWheelEventHandler() {} onData(callback) { this.data = callback; } onResize() {} write(text, callback) { this.writes.push(text); callback?.(); } hasSelection() { return false; } paste(text) { this.pastes.push(text); }
  }
  const context = vm.createContext({console, URLSearchParams, crypto: {randomUUID: () => `reopen-fixture-${options.generation || 1}-${++uuid}`}, navigator: {clipboard: {}},
    document: {querySelector: get, getElementById: id => get('#' + id), querySelectorAll: selector => selector === '[data-close]' || selector === '#reopen-dialog [data-close]' ? cancels : selector === 'dialog' ? [get('#reopen-dialog')] : [], createElement: element, createTextNode: text => ({textContent: text}), addEventListener() {}},
    window: {Terminal, FitAddon: {FitAddon: class {fit() {}}}, addEventListener: (type, callback) => { windowListeners[type] = callback; }, location: {hash: '', origin: 'http://fixture'}, history: {replaceState() {}},
      localStorage: {getItem: () => null, setItem() {}}, sessionStorage: options.sessionStorage || {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)}},
    requestAnimationFrame: callback => callback(), ResizeObserver: class {observe() {} disconnect() {}}, setInterval() {}, setTimeout: callback => { timers.set(++timer, callback); return timer; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, opts = {}) => {
      const call = {url: String(url), options: opts, body: opts.body ? JSON.parse(opts.body) : undefined}; calls.push(call);
      if (options.handle) { const custom = await options.handle(call, {agents, hosts, storage}); if (custom !== undefined) return custom; }
      if (call.url.startsWith('/api/state')) return response({hosts, agents, outbox: [], events: []});
      if (call.url.startsWith('/api/terminals/output')) return response({data: 'original frame', seq: 1, reset: true, cols: 100, rows: 30, connected: true});
      if (call.url === '/api/terminals/reopen') { const result = receipt(call.body); Object.assign(agents.find(a => a.thread_id === call.body.thread_id), result); return response(result); }
      return response({accepted: true});
    }});
  vm.runInContext(source, context); await context.refresh(); await tick();
  return {context, get, calls, agents, hosts, storage, timers, terminals, cancels, windowListeners};
}

test('reopen is explicit destructive confirmation and not an alternate reconnect or resume', async () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /id="terminal-reopen"[^>]*>重开终端…/);
  assert.match(html, /这不是重新连接/); assert.match(html, /关闭旧终端及其前台工作进程.*删除.*tmux/);
  assert.match(html, /不会启动或恢复 Codex，不重放任务/);
  assert.match(html, /id="reopen-approval" type="checkbox"/); assert.match(html, /id="reopen-draft"[^>]*readonly/);
  const h = await harness(); await h.context.selectAgent(h.agents[0]); h.context.openReopenTerminal();
  assert.equal(h.get('#reopen-name').textContent, 'Named work a'); assert.equal(h.get('#reopen-host').textContent, 'Lab server (remote)');
  assert.equal(h.get('#reopen-cwd').textContent, h.agents[0].cwd); assert.equal(h.get('#reopen-terminal-id').textContent, 'ceohub-old-a');
  assert.equal(h.get('#reopen-confirm').disabled, true); await assert.rejects(h.context.submitReopenTerminal(), /明确确认/);
  assert.equal(posts(h).length, 0);
});

test('more actions allow disconnected or missing original workers, never manager or closed/history records', async () => {
  const h = await harness({agents: [worker('offline', {connected: false}), worker('missing', {recovery: {code: 'remote_session_missing'}}), worker('closed', {closed: true}), {host: 'remote', thread_id: 'history', mode: 'observe'}, worker('manager', {kind: 'controller'})]});
  const actions = descendants(h.get('#agents')).filter(item => item.textContent === '重开终端…'); assert.equal(actions.length, 2);
  for (const agent of h.agents.slice(2)) { h.context.openReopenTerminal(agent); assert.notEqual(h.get('#reopen-dialog').open, true); }
  assert.equal(posts(h).length, 0);
});

test('opening and cancelling per-worker dialog preserves another selected view and its draft', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[1]); const previous = view(h); previous.input = 'keep draft';
  h.context.openReopenTerminal(h.agents[0]); h.cancels[0].onclick();
  assert.equal(view(h), previous); assert.equal(previous.input, 'keep draft'); assert.equal(previous.disposed, false);
  assert.equal(posts(h).length, 0);
});

test('confirmed reopen stores request before POST, replaces only terminal generation, opens shell without focus or input', async () => {
  let savedAtPost;
  const h = await harness({handle: (call, backend) => { if (call.url === '/api/terminals/reopen') savedAtPost = JSON.parse(backend.storage.get(storageKey)); }});
  await h.context.selectAgent(h.agents[0]); const original = view(h); h.context.openReopenTerminal(); confirm(h);
  await h.context.submitReopenTerminal();
  const body = posts(h)[0].body;
  assert.deepEqual(Object.keys(body).sort(), ['confirmed', 'expected_terminal_id', 'host', 'request_key', 'thread_id']);
  assert.equal(body.expected_terminal_id, 'ceohub-old-a'); assert.equal(body.confirmed, true);
  assert.equal(savedAtPost[0].state, 'unknown'); assert.equal(savedAtPost[0].body.request_key, body.request_key);
  assert.equal(h.agents[0].alias, 'Named work a'); assert.equal(h.agents[0].pinned, true); assert.equal(h.agents[0].cwd, '/home/example/project');
  assert.equal(h.agents[0].thread_id, 'a'); assert.equal(h.agents[0].agent_ready, false); assert.equal(h.agents[0].state, 'shell');
  assert.equal(original.disposed, true); assert.notEqual(view(h), original); assert.equal(view(h).terminalId, 'ceohub-new-a');
  assert.equal(view(h).term.focusCount, 0); assert.deepEqual(view(h).term.pastes, []); assert.equal(pending(h)[0].state, 'done');
  assert.equal(h.calls.some(call => /\/input|\/resume|\/create/.test(call.url)), false);
});

test('inflight operation prevents duplicate POST, escape, cancel button, and backdrop close', async () => {
  let resolve;
  const h = await harness({handle: call => call.url === '/api/terminals/reopen' ? new Promise(done => { resolve = () => done(response(receipt(call.body))); }) : undefined});
  await h.context.selectAgent(h.agents[0]); h.context.openReopenTerminal(); confirm(h);
  const first = h.context.submitReopenTerminal(); await tick(); assert.equal(posts(h).length, 1);
  assert.equal(h.get('#reopen-confirm').disabled, true); assert.ok(h.cancels.every(item => item.disabled));
  let prevented = false; h.get('#reopen-dialog').emit('cancel', {preventDefault() { prevented = true; }}); assert.equal(prevented, true);
  h.cancels[0].onclick(); h.get('#reopen-dialog').emit('click', {target: h.get('#reopen-dialog'), clientX: -1, clientY: -1});
  assert.equal(h.get('#reopen-dialog').open, true); await h.context.submitReopenTerminal(); assert.equal(posts(h).length, 1);
  resolve(); await first;
});

test('lost response and reload keep exact old body/key, no automatic replay or acknowledgment from changed registry', async () => {
  const storage = new Map();
  const first = await harness({storage, handle: call => { if (call.url === '/api/terminals/reopen') throw new Error('response lost'); }});
  await first.context.selectAgent(first.agents[0]); first.context.openReopenTerminal(); confirm(first);
  await assert.rejects(first.context.submitReopenTerminal()); const originalBody = posts(first)[0].body;
  assert.equal(view(first).reopening, true); first.get('#reopen-dialog').close();
  const second = await harness({storage, generation: 2, agents: [worker('a', {terminal_id: 'ceohub-new-a', command: 'bash', agent_ready: false})]});
  await second.context.selectAgent(second.agents[0]); await second.context.refresh();
  assert.equal(posts(second).length, 0); assert.equal(pending(second)[0].state, 'unknown'); assert.equal(view(second).reopening, true);
  second.context.openReopenTerminal(); assert.match(second.get('#reopen-confirm').textContent, /核对原重开/);
  await second.context.submitReopenTerminal(); assert.deepEqual(posts(second)[0].body, originalBody); assert.equal(pending(second)[0].state, 'done');
});

test('close_unknown refresh and reload retain an inspect-only route using the exact saved unknown request', async () => {
  const storage = new Map();
  const first = await harness({storage, handle: call => call.url === '/api/terminals/reopen' ? response(receipt(call.body, {operation_state: 'unknown', reopened: false, terminal_id: call.body.expected_terminal_id, recovery: {message: 'close outcome unknown'}})) : undefined});
  await first.context.selectAgent(first.agents[0]); first.context.openReopenTerminal(); confirm(first); await first.context.submitReopenTerminal();
  const originalBody = posts(first)[0].body;
  Object.assign(first.agents[0], {close_state: 'unknown', status: 'close_unknown', state: 'close_unknown', connected: false});
  first.get('#reopen-dialog').close(); await first.context.refresh(true);
  assert.equal(vm.runInContext("canReopenAgent(state.agents[0])", first.context), false); assert.equal(first.context.canInspectPendingReopen(first.agents[0]), true);
  assert.equal(first.get('#terminal-reopen').hidden, false); assert.match(first.get('#terminal-reopen').textContent, /核对原重开结果/);
  const cardActions = descendants(first.get('#agents')).filter(item => item.textContent === '核对原重开结果…'); assert.equal(cardActions.length, 1);
  first.context.openReopenTerminal(); await first.context.submitReopenTerminal();
  assert.equal(posts(first).length, 2); assert.deepEqual(posts(first)[1].body, originalBody); assert.equal(pending(first)[0].state, 'unknown');
  const second = await harness({storage, generation: 2, agents: [worker('a', {close_state: 'unknown', status: 'close_unknown', state: 'close_unknown', connected: false})]});
  await second.context.selectAgent(second.agents[0]); assert.equal(posts(second).length, 0);
  second.context.openReopenTerminal(); assert.equal(op(second).body.request_key, originalBody.request_key); assert.equal(second.get('#reopen-approval').disabled, true);
  await second.context.submitReopenTerminal(); assert.deepEqual(posts(second)[0].body, originalBody); assert.equal(pending(second)[0].state, 'done');
});

test('closing workers without an exact saved unknown operation never gain new reopen authority', async () => {
  const h = await harness({agents: [worker('a', {close_state: 'unknown', state: 'close_unknown'})]});
  await h.context.selectAgent(h.agents[0]); h.context.openReopenTerminal(); assert.notEqual(h.get('#reopen-dialog').open, true);
  assert.equal(h.get('#terminal-reopen').hidden, true); assert.equal(posts(h).length, 0);
  assert.equal(h.context.canInspectPendingReopen(h.agents[0]), false);
  const body = {host: 'remote', thread_id: 'different', expected_terminal_id: 'ceohub-old-a', confirmed: true, request_key: 'unrelated-operation'};
  vm.runInContext(`reopenOperations.set('remote:a', ${JSON.stringify({version: 1, state: 'unknown', body})})`, h.context);
  assert.equal(h.context.canInspectPendingReopen(h.agents[0]), false); h.context.openReopenTerminal(); assert.notEqual(h.get('#reopen-dialog').open, true);
});

test('known blocked result has no same-dialog retry; closing and reopening requires fresh explicit confirmation', async () => {
  const h = await harness({handle: call => call.url === '/api/terminals/reopen' ? response(receipt(call.body, {operation_state: 'blocked', reopened: false, terminal_id: call.body.expected_terminal_id, recovery: {message: 'Before mutation blocked'}})) : undefined});
  await h.context.selectAgent(h.agents[0]); h.context.openReopenTerminal(); confirm(h); await h.context.submitReopenTerminal(); const first = posts(h)[0].body;
  assert.equal(pending(h)[0].state, 'blocked'); assert.equal(view(h).reopening, false); await h.context.submitReopenTerminal(); assert.equal(posts(h).length, 1);
  h.get('#reopen-dialog').close(); h.context.openReopenTerminal(); assert.notEqual(op(h).body.request_key, first.request_key);
  assert.equal(h.get('#reopen-approval').checked, false); await assert.rejects(h.context.submitReopenTerminal()); assert.equal(posts(h).length, 1);
});

for (const forged of [{operation_id: 'other-key'}, {previous_terminal_id: 'other-generation'}, {host: 'other-host'}, {thread_id: 'other-worker'}, {terminal_id: 'ceohub-old-a'}]) test(`invalid reopen receipt never switches view or releases unknown fence: ${JSON.stringify(forged)}`, async () => {
  const h = await harness({handle: call => call.url === '/api/terminals/reopen' ? response(receipt(call.body, forged)) : undefined});
  await h.context.selectAgent(h.agents[0]); const old = view(h); h.context.openReopenTerminal(); confirm(h);
  await h.context.submitReopenTerminal().catch(() => {});
  assert.equal(view(h), old); assert.equal(h.agents[0].terminal_id, 'ceohub-old-a'); assert.equal(pending(h)[0].state, 'unknown'); assert.equal(old.reopening, true);
});

test('buffered human draft survives in readonly memory but never flushes into replacement shell', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); const old = view(h); old.input = 'private unsent draft';
  h.context.openReopenTerminal(); assert.equal(h.get('#reopen-draft').value, old.input); assert.equal(h.get('#reopen-human-warning').hidden, false);
  confirm(h); await h.context.submitReopenTerminal(); h.windowListeners.beforeunload();
  assert.equal(vm.runInContext("reopenDrafts.get('remote:a')", h.context), 'private unsent draft');
  assert.equal(h.calls.filter(call => /\/input/.test(call.url)).length, 0); assert.ok(!h.storage.get(storageKey).includes('private unsent draft'));
  h.context.openReopenTerminal(); assert.equal(h.get('#reopen-draft').value, 'private unsent draft');
});

test('switching selection while reopen waits cannot replace or focus the other worker', async () => {
  let resolve;
  const h = await harness({handle: call => call.url === '/api/terminals/reopen' ? new Promise(done => { resolve = () => done(response(receipt(call.body))); }) : undefined});
  await h.context.selectAgent(h.agents[0]); h.context.openReopenTerminal(); confirm(h); const request = h.context.submitReopenTerminal(); await tick();
  await h.context.selectAgent(h.agents[1]); const other = view(h); resolve(); await request;
  assert.equal(view(h), other); assert.equal(other.agent.thread_id, 'b'); assert.equal(other.disposed, false); assert.equal(other.term.focusCount, 0);
  assert.equal(posts(h).length, 1); assert.equal(posts(h)[0].body.thread_id, 'a');
});

test('corrupt saved operation and unavailable storage fail closed before destructive HTTP', async () => {
  const corrupt = await harness({storage: new Map([[storageKey, 'not-json']])}); corrupt.context.openReopenTerminal(corrupt.agents[0]);
  assert.notEqual(corrupt.get('#reopen-dialog').open, true); assert.match(corrupt.get('#toast').textContent, /无法读取/); assert.equal(posts(corrupt).length, 0);
  const denied = await harness({sessionStorage: {getItem: () => null, setItem() { throw new Error('denied'); }}});
  await denied.context.selectAgent(denied.agents[0]); const original = view(denied); denied.context.openReopenTerminal(); confirm(denied);
  await assert.rejects(denied.context.submitReopenTerminal(), /可靠保存/);
  assert.equal(posts(denied).length, 0); assert.equal(original.reopening, false); assert.equal(original.disposed, false);
});

test('captured terminal generation fences keyboard, resize and close even if canonical worker later changes', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); const original = view(h);
  original.input = 'human keys'; h.context.flushTerminalInput(original); await original.inputChain;
  const write = h.calls.find(call => call.url === '/api/terminals/input'); assert.equal(write.body.expected_terminal_id, 'ceohub-old-a');
  original.initialized = true; original.restoringFrame = false;
  h.context.scheduleResize(original, 120, 36);
  assert.equal(typeof h.timers.get(original.resizeTimer), 'function', JSON.stringify({size: original.size, disposed: original.disposed, reopening: original.reopening, closing: original.closing, closed: original.closed, initialized: original.initialized, resizePending: original.resizePending, resizeInFlight: original.resizeInFlight, resizeTimer: original.resizeTimer}));
  await h.timers.get(original.resizeTimer)();
  const resize = h.calls.find(call => call.url === '/api/terminals/resize'); assert.equal(resize.body.expected_terminal_id, 'ceohub-old-a');
  h.agents[0].terminal_id = 'ceohub-replaced'; original.input = 'stale keys'; h.context.flushTerminalInput(original); await original.inputChain;
  assert.equal(h.calls.filter(call => call.url === '/api/terminals/input').length, 1);
  assert.equal(h.context.canWriteTerminal(original), false);
  const closing = await harness({handle: call => call.url === '/api/terminals/close' ? response({closed: true}) : undefined});
  await closing.context.selectAgent(closing.agents[0]); closing.context.openCloseTerminal(); await closing.context.closeTerminal();
  const close = closing.calls.find(call => call.url === '/api/terminals/close'); assert.equal(close.body.expected_terminal_id, 'ceohub-old-a');
});

test('late old output cannot paint a reopened generation or change its connection state', async () => {
  let hold = false, resolve;
  const h = await harness({handle: call => hold && call.url.startsWith('/api/terminals/output') ? new Promise(done => { hold = false; resolve = () => done(response({data: 'late-old-generation', terminal_id: 'ceohub-old-a', connected: false, seq: 99})); }) : undefined});
  await h.context.selectAgent(h.agents[0]); const old = view(h); hold = true; const reading = h.context.pollTerminal(old); await tick();
  h.context.openReopenTerminal(); confirm(h); await h.context.submitReopenTerminal(); const replacement = view(h);
  resolve(); await reading;
  assert.notEqual(replacement, old); assert.equal(old.disposed, true); assert.equal(replacement.displayConnected, true);
  assert.equal(replacement.term.writes.some(text => text.includes('late-old-generation')), false);
});

test('queued but not yet sent keyboard data is retained in order and never reaches reopened shell', async () => {
  let release;
  const h = await harness(); await h.context.selectAgent(h.agents[0]); const old = view(h);
  old.inputChain = new Promise(done => { release = done; });
  old.input = 'first '; h.context.flushTerminalInput(old); old.input = 'second '; h.context.flushTerminalInput(old); old.input = 'last';
  h.context.openReopenTerminal(); confirm(h); const reopening = h.context.submitReopenTerminal(); await tick();
  assert.equal(posts(h).length, 0); release(); await reopening;
  assert.equal(vm.runInContext("reopenDrafts.get('remote:a')", h.context), 'first second last');
  assert.equal(h.calls.some(call => call.url === '/api/terminals/input'), false);
});

test('late done receipt never rolls back a newer canonical terminal generation', async () => {
  let resolve;
  const h = await harness({handle: call => call.url === '/api/terminals/reopen' ? new Promise(done => { resolve = () => done(response(receipt(call.body))); }) : undefined});
  await h.context.selectAgent(h.agents[0]); h.context.openReopenTerminal(); confirm(h); const reopening = h.context.submitReopenTerminal(); await tick();
  h.agents[0].terminal_id = 'ceohub-even-newer-a'; resolve(); await reopening;
  assert.equal(h.agents[0].terminal_id, 'ceohub-even-newer-a'); assert.equal(pending(h)[0].state, 'done');
  assert.match(h.get('#agent-recovery-notice').textContent, /另一代终端/);
  assert.equal(h.calls.filter(call => call.url === '/api/terminals/reopen').length, 1);
});

test('failed display mount cannot undo a verified successful lifecycle receipt', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); h.context.openReopenTerminal(); confirm(h);
  h.context.window.Terminal = null; await h.context.submitReopenTerminal();
  assert.equal(pending(h)[0].state, 'done'); assert.equal(h.agents[0].terminal_id, 'ceohub-new-a');
  assert.match(h.get('#agent-recovery-notice').textContent, /Shell 已确认建立.*不要再发新的重开请求/);
  assert.equal(posts(h).length, 1);
});
