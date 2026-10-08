'use strict';
// Browser-code contracts with inert DOM/network sinks; no SSH, PTY, worker, or
// real clipboard access. A lost display link must not imply a dead worker.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  return { value: '', textContent: '', hidden: false, disabled: false, checked: false, clientWidth: 900, children: [], dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    append(...nodes) { this.children.push(...nodes); this.childElementCount = this.children.length; },
    replaceChildren(...nodes) { this.children = nodes; this.childElementCount = nodes.length; },
    setAttribute() {}, addEventListener() {}, showModal() {}, close() {}, focus() {}, select() {}, reset() {} };
}

function harness() {
  const elements = new Map(), network = [], timers = new Map(), clipboard = [], terminals = [];
  let timerId = 0, route;
  const get = selector => { if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector); };
  const host = { id: '316', label: '316', transport: 'ssh', connected: true };
  const agent = { host: '316', thread_id: 'work-1', terminal_id: 'ceohub-fixture', mode: 'terminal', connected: true, command: 'node', state: 'agent' };
  const box = { cols: 73, rows: 33 };
  class FakeTerminal {
    constructor() { this.cols = 73; this.rows = 33; this.options = {}; this.text = 'retained worker output'; this.resets = 0; this.pastes = []; this.parser = { registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {} }; terminals.push(this); }
    loadAddon(fit) { fit.term = this; } open() {} focus() {} blur() {} dispose() {}
    attachCustomKeyEventHandler() {} attachCustomWheelEventHandler() {} hasSelection() { return true; } getSelection() { return 'retained worker output'; }
    onData(handler) { this.dataHandler = handler; } onResize(handler) { this.resizeHandler = handler; }
    resize(cols, rows) { this.cols = cols; this.rows = rows; this.resizeHandler?.({ cols, rows }); }
    reset() { this.resets++; this.text = ''; }
    write(data, callback) { this.text += data; callback?.(); }
    paste(text) { this.pastes.push(text); this.dataHandler?.(text); }
  }
  class FakeFit { constructor() { this.calls = 0; } fit() { this.calls++; this.term.resize(box.cols, box.rows); } }
  const term = new FakeTerminal(), fit = new FakeFit(); fit.term = term;
  const fixture = { key: '316:work-1', agent, term, fit, seq: 9, initialized: true, disposed: false, displayConnected: true,
    closing: false, closed: false, input: '', inputChain: Promise.resolve(), size: '73:33', frameGeneration: 0, resizeGeneration: 0,
    resizePending: false, forceSnapshot: false, writingFrame: false, restoringFrame: false, observer: { disconnect() {} } };
  const reply = value => ({ ok: true, json: async () => value });
  const fallback = url => reply(url.startsWith('/api/state') ? { hosts: [host], agents: [agent], events: [], outbox: [] }
    : url.startsWith('/api/terminals/output') ? { reset: true, ansi: 'restored view', seq: 10, cols: 73, rows: 33, connected: true } : { accepted: true });
  const context = vm.createContext({ console, URLSearchParams, crypto: { randomUUID: () => 'request-key' },
    document: { querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element, createTextNode: text => ({ textContent: text }), addEventListener() {} },
    navigator: { clipboard: { writeText: async text => clipboard.push(text), readText: async () => 'clipboard text' } },
    window: { Terminal: FakeTerminal, FitAddon: { FitAddon: FakeFit }, addEventListener() {} },
    ResizeObserver: class { observe() {} disconnect() {} }, requestAnimationFrame() {}, setInterval: () => 1,
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay }); return id; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => { network.push({ url, options }); return route ? route(url, options, fallback) : fallback(url); } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context, { filename: 'ui.js' });
  context.fixture = fixture;
  vm.runInContext('terminalSession=fixture; selectedAgent=fixture.key; selectedHost="316"; state.hosts=[{id:"316",transport:"ssh",connected:true}]; state.agents=[fixture.agent];', context);
  term.onResize(({ cols, rows }) => context.scheduleResize(fixture, cols, rows));
  return { context, fixture, host, agent, box, network, timers, clipboard, terminals, get,
    route: next => { route = next; },
    runResize: async () => { const [id, entry] = [...timers].find(([, value]) => value.delay === 140) || []; assert.ok(entry); timers.delete(id); await entry.callback(); } };
}

const writes = h => h.network.filter(call => call.options?.method === 'POST');

test('known disconnected output preserves the cached picture, declares worker unconfirmed, and never resizes or reconnects', async () => {
  const h = harness(); h.box.cols = 90; h.box.rows = 40;
  h.route((url, options, fallback) => url.startsWith('/api/terminals/output') ? { ok: true, json: async () => ({ data: '\nSSH reset', seq: 10, cols: 73, rows: 33, connected: false, state: 'exited' }) } : fallback(url));
  await h.context.pollTerminal(h.fixture);
  assert.equal(h.fixture.displayConnected, false);
  assert.ok(h.fixture.term.text.includes('retained worker output'));
  assert.ok(h.fixture.term.text.includes('SSH reset'));
  assert.equal(h.fixture.term.resets, 0);
  assert.equal(h.fixture.fit.calls, 0);
  assert.equal(writes(h).length, 0);
  assert.match(h.get('#terminal-status').textContent, /显示连接已断开.*工作进程未确认/);
  assert.equal(h.get('#terminal-connect').textContent, '重新连接');
  assert.equal(h.get('#terminal-connect').disabled, false);
  assert.equal(h.get('#terminal-paste').disabled, true);
  assert.equal(h.get('#terminal-copy').disabled, false);
  assert.equal(h.fixture.term.options.disableStdin, true);
});

test('resize requests are suppressed after disconnect, including a previously scheduled debounce', async () => {
  const h = harness();
  h.context.scheduleResize(h.fixture, 90, 40);
  h.fixture.displayConnected = false;
  await h.runResize();
  h.context.scheduleResize(h.fixture, 95, 42);
  assert.equal(h.fixture.resizePending, false);
  assert.equal(writes(h).length, 0);
  assert.equal([...h.timers.values()].filter(timer => timer.delay === 140).length, 0);
});

test('disconnect between resize planning and request becomes a display warning instead of repeated resize errors', async () => {
  const h = harness();
  h.route((url, options, fallback) => url === '/api/terminals/resize' ? { ok: false, status: 409, json: async () => ({ error: 'Terminal is disconnected' }) } : fallback(url));
  h.context.scheduleResize(h.fixture, 90, 40); await h.runResize();
  assert.equal(h.fixture.displayConnected, false);
  assert.equal(h.fixture.resizeError, '');
  assert.match(h.get('#terminal-status').textContent, /显示连接已断开.*工作进程未确认/);
  h.context.scheduleResize(h.fixture, 95, 42);
  assert.equal(writes(h).filter(call => call.url === '/api/terminals/resize').length, 1);
});

test('periodic disconnected reads never turn into an automatic attach, create, resume, or task', async () => {
  const h = harness();
  h.route((url, options, fallback) => url.startsWith('/api/terminals/output') ? { ok: true, json: async () => ({ data: '', seq: 9, connected: false, status: 'exited', cols: 73, rows: 33 }) } : fallback(url));
  await h.context.pollTerminal(h.fixture); await h.context.pollTerminal(h.fixture);
  assert.equal(writes(h).length, 0);
});

test('explicit reconnect uses exact registered terminal even if the registry still says connected', async () => {
  const h = harness();
  h.context.updateDisplayConnection(h.fixture, false);
  assert.equal(h.agent.connected, true, 'Model the stale machine/registry cache');
  await h.context.reconnectTerminal();
  const post = writes(h);
  assert.equal(post.length, 1);
  assert.equal(post[0].url, '/api/terminals/connect');
  assert.deepEqual(JSON.parse(post[0].options.body), { host: '316', thread_id: 'work-1' });
});

test('a cached successful machine check is not drawn as a live SSH display heartbeat', () => {
  const h = harness();
  h.context.updateDisplayConnection(h.fixture, false);
  const status = h.context.hostDisplayStatus(h.host);
  assert.equal(status.connected, false);
  assert.match(status.label, /0.*1/);
  vm.runInContext('state.agents=[];', h.context);
  const cached = h.context.hostDisplayStatus(h.host);
  assert.equal(cached.connected, false);
  assert.equal(cached.label, '上次 SSH 检查成功');
});

test('retained text remains copyable while disconnected, and a delayed clipboard paste is cancelled', async () => {
  const h = harness(); let resolveRead;
  h.context.navigator.clipboard.readText = () => new Promise(resolve => { resolveRead = resolve; });
  const paste = h.context.pasteTerminal(h.fixture);
  h.context.updateDisplayConnection(h.fixture, false);
  resolveRead('must not reach a disconnected worker');
  await paste;
  await h.context.copyTerminal(h.fixture);
  assert.deepEqual(h.fixture.term.pastes, []);
  assert.deepEqual(h.clipboard, ['retained worker output']);
  assert.equal(writes(h).length, 0);
});

test('unsent UI input is discarded on disconnect rather than replayed into a later connection', async () => {
  const h = harness(); let release;
  h.fixture.inputChain = new Promise(resolve => { release = resolve; });
  h.fixture.input = 'draft before disconnect';
  h.context.flushTerminalInput(h.fixture);
  h.context.updateDisplayConnection(h.fixture, false);
  release(); await h.fixture.inputChain;
  assert.equal(h.fixture.input, '');
  assert.equal(writes(h).length, 0);
});

test('resume guidance is hidden for an unconfirmed disconnected worker or running agent but remains for connected shell/setup', () => {
  const h = harness();
  h.agent.source_thread_id = 'legacy-worker-conversation';
  h.agent.state = 'shell'; h.agent.agent_ready = false;
  h.context.updateDisplayConnection(h.fixture, false);
  h.context.renderDetail();
  assert.equal(h.get('#resume-guide').hidden, true, 'A display disconnect does not prove that the original worker stopped');
  h.context.updateDisplayConnection(h.fixture, true);
  h.context.renderDetail();
  assert.equal(h.get('#resume-guide').hidden, false, 'The connected shell can offer the user an ordinary resume command');
  h.agent.state = 'unknown'; h.agent.command = 'python3'; h.context.renderDetail();
  assert.equal(h.get('#resume-guide').hidden, false, 'A connected provider setup menu keeps the manual connection workflow');
  h.agent.state = 'agent'; h.context.renderDetail();
  assert.equal(h.get('#resume-guide').hidden, true, 'An agent already running in the original terminal must not suggest duplicate resume');
  h.agent.state = 'unknown'; h.agent.agent_ready = true; h.context.renderDetail();
  assert.equal(h.get('#resume-guide').hidden, true);
});
