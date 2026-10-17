'use strict';
// Reproduce geometry recovery with the shipped UI, without SSH, PTYs, model
// requests, service restarts, or worker input.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const reply = data => ({ok: true, json: async () => data});
async function until(condition) {
  for (let i = 0; i < 40 && !condition(); i++) await Promise.resolve();
  assert.ok(condition(), 'expected asynchronous UI step');
}
function element() {
  return {value: '', textContent: '', hidden: false, disabled: false, checked: false, clientWidth: 900, children: [], dataset: {}, style: {},
    classList: {add() {}, remove() {}, toggle() {}}, append(...children) { this.children.push(...children); this.childElementCount = this.children.length; },
    replaceChildren(...children) { this.children = children; this.childElementCount = children.length; }, setAttribute() {}, addEventListener() {},
    showModal() { this.open = true; }, close() { this.open = false; }, focus() {}, select() {}, reset() {}};
}
function harness() {
  const nodes = new Map(), requests = [], timers = new Map(); let id = 0, route;
  const get = selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
  const agent = {host: '316', thread_id: 'ceohub-geometry-fixture', terminal_id: 'ceohub-geometry-fixture', mode: 'terminal', connected: true};
  const term = {cols: 85, rows: 39, pending: [], parser: {registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {}},
    reset() {}, blur() {}, focus() {}, dispose() {}, hasSelection() { return false; },
    resize(cols, rows) { this.cols = cols; this.rows = rows; },
    write(data, callback) { this.pending.push(callback); }, finishWrite() { assert.equal(this.pending.length, 1); this.pending.shift()(); }};
  const fixture = {key: '316:ceohub-geometry-fixture', agent, term, fit: {fit() {}}, seq: 9, initialized: true, disposed: false,
    closing: false, closed: false, input: 'preserved human draft', inputChain: Promise.resolve(), size: '85:39', targetSize: '',
    frameGeneration: 0, resizeGeneration: 0, resizePending: false, forceSnapshot: false, writingFrame: false, restoringFrame: false,
    observer: {disconnect() {}}, geometryUnknown: false, resizeRecoveryAttempts: 0, resizeRecoveryRetryAt: 0, clock: 100000};
  const fallback = url => reply(url.startsWith('/api/state')
    ? {hosts: [{id: '316', transport: 'ssh', connected: true}], agents: [agent], outbox: [], events: []}
    : url.startsWith('/api/terminals/output')
      ? {reset: true, ansi: 'fixture', seq: 10, cols: 85, rows: 39, connected: true, geometry_unknown: false, blocked_reason: null}
      : {accepted: true, geometry_unknown: false});
  const context = vm.createContext({console, URLSearchParams, crypto: {randomUUID: () => 'geometry-fixture'}, navigator: {clipboard: {}},
    document: {querySelector: get, querySelectorAll: () => [], getElementById: name => get('#' + name), createElement: element,
      createTextNode: text => ({textContent: text}), addEventListener() {}},
    window: {addEventListener() {}}, requestAnimationFrame() {}, setInterval: () => 1,
    ResizeObserver: class {observe() {} disconnect() {}},
    setTimeout(callback, delay) { const key = ++id; timers.set(key, {callback, delay}); return key; }, clearTimeout: key => timers.delete(key),
    fetch: async (url, options) => { requests.push({url, options}); return route ? route(url, options, fallback) : fallback(url); }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context, {filename: 'ui.js'});
  context.fixture = fixture;
  vm.runInContext('terminalSession=fixture; selectedAgent=fixture.key; selectedHost="316"; state.agents=[fixture.agent]; Date.now=()=>fixture.clock;', context);
  const runResize = async () => {
    const [key, timer] = [...timers].find(([, value]) => value.delay === 140) || [];
    assert.ok(timer, 'expected geometry confirmation request'); timers.delete(key); await timer.callback();
  };
  const poll = async () => {
    const pending = context.pollTerminal(fixture); await until(() => term.pending.length === 1); term.finishWrite(); await pending;
  };
  return {context, fixture, term, requests, timers, runResize, poll, route: next => { route = next; },
    resizeCount: () => requests.filter(item => item.url === '/api/terminals/resize').length,
    pendingResize: () => [...timers.values()].some(timer => timer.delay === 140)};
}

test('canonical same-size unknown geometry still requests a confirming resize', async () => {
  const h = harness();
  h.route((url, options, fallback) => url.startsWith('/api/terminals/output')
    ? reply({reset: true, ansi: 'canonical fixture', seq: 10, cols: 85, rows: 39, connected: true, geometry_unknown: true, blocked_reason: 'geometry_unknown'})
    : fallback(url));
  await h.poll();
  assert.equal(h.fixture.geometryUnknown, true);
  assert.equal(h.fixture.size, '85:39');
  assert.equal(h.fixture.resizePending, true, 'matching snapshot dimensions do not prove the remote PTY size');
  await h.runResize();
  assert.equal(h.resizeCount(), 1);
  assert.equal(h.fixture.geometryUnknown, false, 'only a successful resize acknowledgement clears the local fence');
  assert.equal(h.fixture.forceSnapshot, true);
  assert.equal(h.fixture.input, 'preserved human draft');
  assert.equal(h.requests.some(item => /\/(?:input|prompt|connect|create|close)$/.test(item.url)), false);
});

test('older snapshot blocked_reason preserves the same geometry fence', async () => {
  const h = harness();
  h.route((url, options, fallback) => url.startsWith('/api/terminals/output')
    ? reply({reset: true, ansi: 'old adapter fixture', seq: 10, cols: 85, rows: 39, connected: true, blocked_reason: 'geometry_unknown'})
    : fallback(url));
  await h.poll();
  assert.equal(h.fixture.geometryUnknown, true);
  assert.equal(h.pendingResize(), true);
});

test('known same-size geometry still avoids unnecessary SSH resize requests', async () => {
  const h = harness();
  await h.poll(); h.context.scheduleResize(h.fixture, 85, 39);
  assert.equal(h.fixture.geometryUnknown, false);
  assert.equal(h.pendingResize(), false);
  assert.equal(h.resizeCount(), 0);
});

test('failed same-size recovery backs off and stops after three attempts', async () => {
  const h = harness(); h.fixture.geometryUnknown = true;
  h.route((url, options, fallback) => url === '/api/terminals/resize'
    ? {ok: false, status: 409, json: async () => ({error: 'Remote resize was not confirmed'})}
    : fallback(url));
  for (let attempt = 1; attempt <= 3; attempt++) {
    h.context.scheduleResize(h.fixture, 85, 39); await h.runResize();
    assert.equal(h.fixture.geometryUnknown, true);
    assert.equal(h.fixture.resizeRecoveryAttempts, attempt);
    for (let poll = 0; poll < 20; poll++) h.context.scheduleResize(h.fixture, 85, 39);
    assert.equal(h.pendingResize(), false, 'ordinary polling cannot create a retry storm');
    h.fixture.clock = h.fixture.resizeRecoveryRetryAt;
  }
  h.fixture.clock += 60000; h.context.scheduleResize(h.fixture, 85, 39);
  assert.equal(h.resizeCount(), 3); assert.equal(h.pendingResize(), false);
  assert.match(h.fixture.resizeError, /自动重试已暂停.*刷新画面/);
  assert.equal(h.fixture.input, 'preserved human draft');
  assert.equal(h.requests.some(item => /\/(?:input|prompt|connect|create|close)$/.test(item.url)), false);
});

test('pending same-size confirmation coalesces and closing cancels without a request', async () => {
  const h = harness(); h.fixture.geometryUnknown = true;
  h.context.scheduleResize(h.fixture, 85, 39);
  const generation = h.fixture.resizeGeneration;
  h.context.scheduleResize(h.fixture, 85, 39);
  assert.equal(h.fixture.resizeGeneration, generation);
  h.fixture.closing = true; await h.runResize();
  assert.equal(h.resizeCount(), 0); assert.equal(h.fixture.resizePending, false);
  assert.equal(h.fixture.geometryUnknown, true);
});

test('disposed terminal does not issue its pending geometry confirmation', async () => {
  const h = harness(); h.fixture.geometryUnknown = true;
  h.context.scheduleResize(h.fixture, 85, 39); h.fixture.disposed = true; await h.runResize();
  assert.equal(h.resizeCount(), 0); assert.equal(h.fixture.geometryUnknown, true);
});
