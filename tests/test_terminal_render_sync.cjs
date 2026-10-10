'use strict';
// Exercise the shipped UI's asynchronous frame/resize order without browser,
// PTY, SSH, model calls, or any actual worker input.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return {promise, resolve}; };
const reply = data => ({ok: true, json: async () => data});
async function until(condition) { for (let index = 0; index < 30 && !condition(); index++) await Promise.resolve(); assert.ok(condition(), 'expected asynchronous UI step'); }

function element() {
  return {value: '', textContent: '', hidden: false, disabled: false, checked: false, clientWidth: 900, children: [], dataset: {}, style: {},
    classList: {add() {}, remove() {}, toggle() {}}, append(...nodes) { this.children.push(...nodes); this.childElementCount = this.children.length; },
    replaceChildren(...nodes) { this.children = nodes; this.childElementCount = nodes.length; }, setAttribute() {}, addEventListener() {},
    showModal() { this.open = true; }, close() { this.open = false; }, focus() {}, select() {}, reset() {}};
}

function harness() {
  const nodes = new Map(), network = [], timers = new Map(), observers = [], raf = [], terminals = [];
  const box = {cols: 73, rows: 34}; let timerId = 0, route;
  const get = selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
  class FakeTerminal {
    constructor() { this.cols = 100; this.rows = 30; this.pending = []; this.writes = []; this.resizes = []; this.resetCount = 0; this.resizeDuringWrite = 0; this.parser = {registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {}}; terminals.push(this); }
    loadAddon(addon) { addon.term = this; } open() {} focus() {} blur() {} dispose() { this.disposed = true; }
    attachCustomKeyEventHandler() {} attachCustomWheelEventHandler() {} hasSelection() { return false; }
    onData(handler) { this.dataHandler = handler; } onResize(handler) { this.resizeHandler = handler; }
    reset() { this.resetCount++; }
    resize(cols, rows) { if (this.cols === cols && this.rows === rows) return; if (this.pending.length) this.resizeDuringWrite++; this.cols = cols; this.rows = rows; this.resizes.push([cols, rows]); this.resizeHandler?.({cols, rows}); }
    write(data, callback) { this.writes.push({data, cols: this.cols, rows: this.rows}); this.pending.push(callback); }
    finishWrite() { const callback = this.pending.shift(); assert.equal(typeof callback, 'function'); callback(); }
  }
  class FakeFit { constructor() { this.calls = 0; } fit() { this.calls++; this.term.resize(box.cols, box.rows); } }
  const agent = {host: '316', thread_id: 'ceohub-frame-fixture', terminal_id: 'ceohub-frame-fixture', mode: 'terminal', connected: true};
  const term = new FakeTerminal(), fit = new FakeFit(); fit.term = term; term.cols = 73; term.rows = 34;
  const fixture = {key: '316:ceohub-frame-fixture', agent, term, fit, seq: 9, initialized: true, disposed: false, closing: false, closed: false,
    input: '', inputChain: Promise.resolve(), size: '73:34', targetSize: '73:34', frameGeneration: 0, resizeGeneration: 0, resizePending: false,
    forceSnapshot: false, writingFrame: false, restoringFrame: false, fitPending: false, observer: {disconnect() {}}};
  const stateReply = () => ({hosts: [{id: '316', label: '316', transport: 'ssh', connected: true}], agents: [agent], events: [], outbox: []});
  const defaultReply = url => reply(url.startsWith('/api/state') ? stateReply() : url.startsWith('/api/terminals/output') ? {reset: true, ansi: 'canonical', data: '', seq: 10, cols: 73, rows: 34, connected: true} : {accepted: true});
  const context = vm.createContext({console, URLSearchParams, crypto: {randomUUID: () => 'render-request'}, navigator: {clipboard: {}},
    document: {querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element, createTextNode: text => ({textContent: text}), addEventListener() {}},
    window: {Terminal: FakeTerminal, FitAddon: {FitAddon: FakeFit}, addEventListener() {}},
    ResizeObserver: class {constructor(callback) { this.callback = callback; observers.push(this); } observe() {} disconnect() {}},
    requestAnimationFrame: callback => raf.push(callback), setInterval: () => 1,
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, delay}); return id; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, options) => { network.push({url, options}); return route ? await route(url, options, defaultReply) : defaultReply(url); }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context, {filename: 'ui.js'});
  context.fixture = fixture;
  vm.runInContext('terminalSession=fixture; selectedAgent=fixture.key; selectedHost="316"; state.agents=[fixture.agent];', context);
  term.onResize(({cols, rows}) => context.scheduleResize(fixture, cols, rows));
  const runTimer = delay => { const [id, entry] = [...timers].find(([, value]) => value.delay === delay) || []; assert.ok(entry, `expected ${delay}ms timer`); timers.delete(id); return entry.callback(); };
  return {context, fixture, agent, box, network, timers, observers, raf, terminals, get, runTimer, route: next => { route = next; }};
}

test('initial snapshot holds canonical geometry while RAF and ResizeObserver request a fit', async () => {
  const h = harness(); h.box.cols = 90; h.box.rows = 40;
  vm.runInContext('terminalSession=null;', h.context);
  const opening = h.context.openTerminal(h.agent);
  await until(() => h.terminals.at(-1).pending.length === 1);
  const session = vm.runInContext('terminalSession', h.context);
  assert.equal(session.term.cols, 73); assert.equal(session.term.rows, 34);
  h.raf.at(-1)(); h.observers.at(-1).callback(); h.observers.at(-1).callback();
  assert.equal(session.fit.calls, 0); assert.equal(session.term.cols, 73); assert.equal(session.term.resizeDuringWrite, 0);
  assert.equal(session.fitPending, true);
  session.term.finishWrite(); await opening;
  assert.equal(session.fit.calls, 1); assert.equal(session.term.cols, 90); assert.equal(session.resizePending, true);
});

test('raw output also defers local fitting until the xterm write callback', async () => {
  const h = harness(); h.box.cols = 90; h.box.rows = 40;
  h.route((url, options, fallback) => url.startsWith('/api/terminals/output') ? reply({data: 'raw cursor frame', seq: 10, cols: 73, rows: 34}) : fallback(url));
  const polling = h.context.pollTerminal(h.fixture); await until(() => h.fixture.term.pending.length === 1);
  h.context.safeFit(h.fixture); h.context.safeFit(h.fixture);
  assert.equal(h.fixture.term.cols, 73); assert.equal(h.fixture.fit.calls, 0); assert.equal(h.fixture.term.resizeDuringWrite, 0);
  h.fixture.term.finishWrite(); await polling;
  assert.equal(h.fixture.term.cols, 90); assert.equal(h.fixture.fit.calls, 1); assert.equal(h.fixture.resizePending, true);
});

test('successful latest resize ACK triggers exactly one canonical snapshot before incremental polls', async () => {
  const h = harness(); h.box.cols = 90; h.box.rows = 40;
  h.context.scheduleResize(h.fixture, 90, 40); await h.runTimer(140);
  assert.equal(h.fixture.forceSnapshot, true); assert.equal(h.fixture.resizePending, false);
  h.route((url, options, fallback) => url.startsWith('/api/terminals/output') ? reply(new URL('http://fixture' + url).searchParams.get('after') === '0'
    ? {reset: true, ansi: 'new geometry canonical', seq: 22, cols: 90, rows: 40}
    : {data: '', seq: 22, cols: 90, rows: 40}) : fallback(url));
  const polling = h.context.pollTerminal(h.fixture); await until(() => h.fixture.term.pending.length === 1);
  assert.equal(h.fixture.term.cols, 90); h.fixture.term.finishWrite(); await polling;
  await h.context.pollTerminal(h.fixture);
  const queries = h.network.filter(call => call.url.startsWith('/api/terminals/output')).map(call => new URL('http://fixture' + call.url).searchParams.get('after'));
  assert.deepEqual(queries, ['0', '22']); assert.equal(h.fixture.term.resetCount, 1); assert.equal(h.fixture.forceSnapshot, false);
});

test('output response fetched before a resize generation is never replayed after its ACK', async () => {
  const h = harness(), raw = deferred();
  h.route((url, options, fallback) => url.startsWith('/api/terminals/output') ? raw.promise : fallback(url));
  const polling = h.context.pollTerminal(h.fixture); await until(() => h.network.some(call => call.url.startsWith('/api/terminals/output')));
  h.context.scheduleResize(h.fixture, 90, 40); await h.runTimer(140);
  raw.resolve(reply({data: 'old-size raw frame', seq: 100, cols: 73, rows: 34})); await polling;
  assert.equal(h.fixture.term.writes.length, 0); assert.equal(h.fixture.seq, 9); assert.equal(h.fixture.forceSnapshot, true);
});

test('out-of-order older resize ACK cannot overwrite the latest size and generation', async () => {
  const h = harness(), older = deferred(), latest = deferred();
  h.route((url, options, fallback) => url === '/api/terminals/resize' ? (JSON.parse(options.body).cols === 90 ? older.promise : latest.promise) : fallback(url));
  h.context.scheduleResize(h.fixture, 90, 40); const first = h.runTimer(140);
  h.context.scheduleResize(h.fixture, 80, 35); const second = h.runTimer(140);
  latest.resolve(reply({accepted: true})); await second;
  const generation = h.fixture.frameGeneration;
  older.resolve(reply({accepted: true})); await first;
  assert.equal(h.fixture.size, '80:35'); assert.equal(h.fixture.frameGeneration, generation); assert.equal(h.fixture.resizePending, false);
});

test('periodic incremental frames do not reset the screen or force new snapshots', async () => {
  const h = harness(); let sequence = 9;
  h.route((url, options, fallback) => url.startsWith('/api/terminals/output') ? reply({data: 'ordinary delta', seq: ++sequence, cols: 73, rows: 34}) : fallback(url));
  for (let index = 0; index < 2; index++) { const polling = h.context.pollTerminal(h.fixture); await until(() => h.fixture.term.pending.length === 1); h.fixture.term.finishWrite(); await polling; }
  assert.equal(h.fixture.term.resetCount, 0); assert.equal(h.fixture.forceSnapshot, false); assert.equal(h.fixture.seq, 11);
});

test('server geometry drift discards raw bytes and requests canonical recovery', async () => {
  const h = harness();
  h.route((url, options, fallback) => url.startsWith('/api/terminals/output') ? reply({data: 'unknown-size delta', seq: 20, cols: 80, rows: 35}) : fallback(url));
  await h.context.pollTerminal(h.fixture);
  assert.equal(h.fixture.term.writes.length, 0); assert.equal(h.fixture.forceSnapshot, true); assert.equal(h.fixture.seq, 9);
});

test('connected Refresh Screen reconstructs the same terminal without connect or extra Enter', async () => {
  const h = harness(); h.fixture.input = 'unfinished draft';
  const refreshing = h.context.reconnectTerminal(); await until(() => h.terminals.at(-1).pending.length === 1);
  h.terminals.at(-1).finishWrite(); await refreshing; await h.fixture.inputChain;
  assert.equal(h.network.filter(call => call.url === '/api/terminals/connect').length, 0);
  assert.equal(h.network.filter(call => call.url === '/api/terminals/create').length, 0);
  const input = h.network.find(call => call.url === '/api/terminals/input');
  assert.equal(JSON.parse(input.options.body).data, 'unfinished draft');
  const output = h.network.find(call => call.url.startsWith('/api/terminals/output'));
  assert.equal(new URL('http://fixture' + output.url).searchParams.get('after'), '0');
});

test('offline reconnect still explicitly connects only the same owned terminal', async () => {
  const h = harness(); h.agent.connected = false;
  h.route((url, options, fallback) => { if (url === '/api/agents/reconnect') { h.agent.connected = true; return reply({...h.agent, recovery: {code: 'connected', message: 'Original tmux attached'}}); } return fallback(url); });
  const reconnecting = h.context.reconnectTerminal(); await until(() => h.terminals.at(-1).pending.length === 1);
  h.terminals.at(-1).finishWrite(); await reconnecting;
  const connect = h.network.filter(call => call.url === '/api/agents/reconnect');
  assert.equal(connect.length, 1); assert.deepEqual(JSON.parse(connect[0].options.body), {host: '316', thread_id: h.agent.thread_id, request_key: 'render-request'});
  assert.equal(h.network.filter(call => call.url === '/api/terminals/create').length, 0);
});

test('disposed resize ACK cannot mark a newly selected terminal for reset', async () => {
  const h = harness(), ack = deferred();
  h.route((url, options, fallback) => url === '/api/terminals/resize' ? ack.promise : fallback(url));
  h.context.scheduleResize(h.fixture, 90, 40); const pending = h.runTimer(140);
  h.fixture.disposed = true;
  const newer = {...h.fixture, disposed: false, frameGeneration: 77, forceSnapshot: false, size: '110:45'};
  h.context.newer = newer; vm.runInContext('terminalSession=newer;', h.context);
  ack.resolve(reply({accepted: true})); await pending;
  assert.equal(newer.frameGeneration, 77); assert.equal(newer.forceSnapshot, false); assert.equal(newer.size, '110:45');
});

test('closing during resize debounce cancels its pending flag and still reads retained output', async () => {
  const h = harness();
  h.context.scheduleResize(h.fixture, 90, 40); h.fixture.closing = true;
  await h.runTimer(140);
  assert.equal(h.fixture.resizePending, false); assert.equal(h.fixture.forceSnapshot, true);
  assert.equal(h.network.filter(call => call.url === '/api/terminals/resize').length, 0);
  const polling = h.context.pollTerminal(h.fixture); await until(() => h.fixture.term.pending.length === 1);
  h.fixture.term.finishWrite(); await polling;
  const output = h.network.find(call => call.url.startsWith('/api/terminals/output'));
  assert.equal(new URL('http://fixture' + output.url).searchParams.get('after'), '0');
});

test('failed resize clears pending and recovers canonical state without fitting during the request', async () => {
  const h = harness();
  h.route((url, options, fallback) => url === '/api/terminals/resize' ? {ok: false, status: 503, json: async () => ({error: 'resize unavailable'})} : fallback(url));
  h.context.scheduleResize(h.fixture, 90, 40); await h.runTimer(140);
  assert.equal(h.fixture.resizePending, false); assert.equal(h.fixture.forceSnapshot, true);
  assert.equal(h.fixture.fit.calls, 0); assert.deepEqual(h.fixture.term.resizes, []);
  const polling = h.context.pollTerminal(h.fixture); await until(() => h.fixture.term.pending.length === 1);
  h.fixture.term.finishWrite(); await polling;
  const output = h.network.find(call => call.url.startsWith('/api/terminals/output'));
  assert.equal(new URL('http://fixture' + output.url).searchParams.get('after'), '0');
  assert.equal(h.fixture.term.cols, 73); assert.equal(h.fixture.seq, 10);
});
