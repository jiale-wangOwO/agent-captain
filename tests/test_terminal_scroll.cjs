'use strict';
// Real xterm-buffer rendering with inert browser/transport contracts. No PTY,
// SSH, model call, live conversation, clipboard or worker input is used.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Terminal} = require('@xterm/headless');
const {SerializeAddon} = require('@xterm/addon-serialize');

function element() {
  return {value: '', textContent: '', hidden: false, disabled: false, checked: false, children: [], dataset: {}, style: {}, attributes: {},
    listeners: new Map(),
    classList: {add() {}, remove() {}, toggle() {}}, append(...nodes) { this.children.push(...nodes); this.childElementCount = this.children.length; },
    replaceChildren(...nodes) { this.children = nodes; this.childElementCount = nodes.length; }, setAttribute(name, value) { this.attributes[name] = value; },
    addEventListener(type, handler, capture) { this.listeners.set(type, {handler, capture}); },
    removeEventListener(type, handler) { if (this.listeners.get(type)?.handler === handler) this.listeners.delete(type); },
    showModal() {}, close() {}, focus() {}, select() {}, reset() {}};
}
function harness(t, options = {}) {
  const elements = new Map(), network = [], timers = [], get = key => { if (!elements.has(key)) elements.set(key, element()); return elements.get(key); };
  const term = new Terminal({cols: 45, rows: 8, scrollback: options.scrollback || 100, allowProposedApi: true});
  const serializer = new SerializeAddon(); term.loadAddon(serializer);
  let focused = 0, resized = 0, fit = 0;
  term.focus = () => focused++; term.hasSelection = () => false; term.onResize(() => resized++);
  const fixture = {key: '316:scroll-fixture', agent: {host: '316', thread_id: 'scroll-fixture', terminal_id: 'scroll-fixture', mode: 'terminal', connected: true},
    term, fit: {fit() { fit++; }}, initialized: true, disposed: false, displayConnected: true, input: 'retained human draft', inputTimer: 123,
    inputChain: Promise.resolve(), observer: {disconnect() {}}, size: '45:8'};
  const context = vm.createContext({console, URLSearchParams, crypto: {randomUUID: () => 'scroll-test'}, navigator: {clipboard: {}},
    document: {querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element,
      createTextNode: text => ({textContent: text}), addEventListener() {}}, window: {addEventListener() {}},
    setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; }, clearTimeout() {}, setInterval() {}, requestAnimationFrame() {},
    fetch: async (url, request) => { network.push({url, request}); return {ok: true, json: async () => url.startsWith('/api/state')
      ? {hosts: [{id: '316', transport: 'ssh', label: '316'}], agents: [fixture.agent], events: [], outbox: []} : {running: true}}; }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context, {filename: 'ui.js'});
  context.fixture = fixture;
  vm.runInContext('terminalSession=fixture; selectedAgent=fixture.key; state.agents=[fixture.agent];', context);
  context.initializeTerminalScroll(fixture);
  t.after(() => { context.disposeTerminalScroll(fixture); term.dispose(); });
  return {context, fixture, term, serializer, network, timers, get, counts: () => ({focused, resized, fit})};
}
const write = (term, data) => new Promise(resolve => term.write(data, resolve));
const lines = (start, count) => Array.from({length: count}, (_, index) => `unique-row-${String(start + index).padStart(3, '0')}\r\n`).join('');
const top = term => term.buffer.active.getLine(term.buffer.active.viewportY)?.translateToString(true);
const key = (value, extra = {}) => ({type: 'keydown', key: value, prevented: false, stopped: false, ...extra,
  preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; }});

test('bare and Shift page keys plus Shift Home/End navigate real local buffer without input, fit, resize or focus', async t => {
  const h = harness(t); await write(h.term, lines(0, 40)); h.context.rememberTerminalScroll(h.fixture);
  const bottom = h.term.buffer.active.baseY;
  for (const shiftKey of [false, true]) {
    h.context.scrollTerminalView(h.fixture, 'latest');
    const up = key('PageUp', {shiftKey}); assert.equal(h.context.handleTerminalKey(h.fixture, up), false);
    assert.equal(up.prevented, true); assert.equal(up.stopped, true); assert.equal(h.term.buffer.active.viewportY, bottom - 7);
    const down = key('PageDown', {shiftKey}); assert.equal(h.context.handleTerminalKey(h.fixture, down), false);
    assert.equal(h.term.buffer.active.viewportY, bottom);
  }
  h.context.handleTerminalKey(h.fixture, key('Home', {shiftKey: true})); assert.equal(h.term.buffer.active.viewportY, 0);
  assert.match(h.get('#terminal-scroll-status').textContent, /正在查看历史/);
  h.context.handleTerminalKey(h.fixture, key('End', {shiftKey: true})); assert.equal(h.term.buffer.active.viewportY, bottom);
  assert.match(h.get('#terminal-scroll-status').textContent, /正在跟随最新/);
  assert.equal(h.fixture.input, 'retained human draft'); assert.equal(h.fixture.inputTimer, 123);
  assert.deepEqual(h.counts(), {focused: 0, resized: 0, fit: 0}); assert.equal(h.network.some(call => /input|resize|connect|create/.test(call.url)), false);
});

test('plain Home End arrows and modified nonlocal page shortcuts keep their previous agent/browser behavior', t => {
  const h = harness(t);
  for (const event of [key('Home'), key('End'), key('ArrowUp'), key('ArrowDown'), key('PageUp', {ctrlKey: true}), key('PageUp', {altKey: true}), key('PageDown', {metaKey: true})]) {
    assert.equal(h.context.handleTerminalKey(h.fixture, event), true, event.key); assert.equal(event.prevented, false);
  }
});

test('toolbar paging is retained-offline compatible, truthful at boundaries, and never mutates a draft', async t => {
  const h = harness(t); await write(h.term, lines(0, 30)); h.context.rememberTerminalScroll(h.fixture);
  h.fixture.displayConnected = false; h.fixture.closed = true; h.fixture.closing = true;
  h.get('#terminal-page-up').onclick(); assert.equal(h.term.buffer.active.viewportY, h.term.buffer.active.baseY - 7);
  assert.equal(h.get('#terminal-latest').disabled, false); assert.equal(h.get('#terminal-page-down').disabled, false);
  h.context.scrollTerminalView(h.fixture, 'top'); assert.equal(h.get('#terminal-page-up').disabled, true);
  h.get('#terminal-latest').onclick(); assert.equal(h.get('#terminal-latest').disabled, true); assert.equal(h.get('#terminal-page-down').disabled, true);
  assert.equal(h.fixture.input, 'retained human draft'); assert.deepEqual(h.counts(), {focused: 0, resized: 0, fit: 0});
  assert.equal(h.network.some(call => /input|resize|connect|create/.test(call.url)), false);
});

test('incremental output retains a real reading anchor even when normal scrollback is trimmed', async t => {
  const h = harness(t, {scrollback: 16}); await write(h.term, lines(0, 30));
  h.term.scrollToLine(12); const anchor = top(h.term);
  await h.context.writeTerminalFrame(h.fixture, lines(30, 5), false);
  assert.equal(top(h.term), anchor); assert.equal(h.term.buffer.active.viewportY, 7);
  assert.match(h.get('#terminal-scroll-status').textContent, /正在查看历史/);
});

test('canonical reset restores the actual retained line anchor, not a raw byte redraw history', async t => {
  const h = harness(t); await write(h.term, lines(0, 40)); h.term.scrollToLine(9); const anchor = top(h.term);
  const source = new Terminal({cols: 45, rows: 8, scrollback: 100, allowProposedApi: true}), serialize = new SerializeAddon(); source.loadAddon(serialize);
  t.after(() => source.dispose()); await write(source, lines(0, 50));
  await h.context.writeTerminalFrame(h.fixture, serialize.serialize({scrollback: 100}), true, 45, 8);
  assert.equal(top(h.term), anchor); assert.equal(h.term.buffer.active.viewportY, 9);
  assert.match(h.get('#terminal-scroll-status').textContent, /正在查看历史/);
});

test('latest follows incoming output only when already following or explicitly requested', async t => {
  const h = harness(t); await write(h.term, lines(0, 20));
  await h.context.writeTerminalFrame(h.fixture, lines(20, 3), false); assert.equal(h.term.buffer.active.viewportY, h.term.buffer.active.baseY);
  h.context.scrollTerminalView(h.fixture, 'page-up'); const anchor = top(h.term);
  await h.context.writeTerminalFrame(h.fixture, lines(23, 4), false); assert.equal(top(h.term), anchor);
  h.context.scrollTerminalView(h.fixture, 'latest');
  await h.context.writeTerminalFrame(h.fixture, lines(27, 4), false); assert.equal(h.term.buffer.active.viewportY, h.term.buffer.active.baseY);
});

test('explicit paging while a write callback is pending wins over the earlier follow-bottom checkpoint', async t => {
  const h = harness(t); await write(h.term, lines(0, 30));
  const original = h.term.write.bind(h.term); let finish, parsed;
  const callbackIntercepted = new Promise(resolve => { parsed = resolve; });
  // Synchronize with xterm's actual asynchronous parse completion, rather than
  // counting event-loop turns (other parallel suites can defer its timer).
  // Deliberately keep the shipped UI's write acknowledgement outstanding.
  h.term.write = (data, callback) => original(data, () => { finish = callback; parsed(); });
  const frame = h.context.writeTerminalFrame(h.fixture, lines(30, 5), false);
  await callbackIntercepted;
  assert.equal(typeof finish, 'function'); assert.equal(h.fixture.writingFrame, true);
  h.context.scrollTerminalView(h.fixture, 'page-up'); const anchor = top(h.term);
  finish(); await frame; assert.equal(top(h.term), anchor);
  assert.equal(h.term.buffer.active.viewportY, h.term.buffer.active.baseY - 7);
});

test('captured native scrollbar drag while the real parsed-frame ACK is held overrides old latest-follow state', async t => {
  const h = harness(t); await write(h.term, lines(0, 30));
  const original = h.term.write.bind(h.term); let finish, parsed;
  const callbackIntercepted = new Promise(resolve => { parsed = resolve; });
  h.term.write = (data, callback) => original(data, () => { finish = callback; parsed(); });
  const frame = h.context.writeTerminalFrame(h.fixture, lines(30, 5), false); await callbackIntercepted;
  assert.equal(h.fixture.writingFrame, true); const version = h.fixture.scrollIntentVersion;
  const listener = h.get('#terminal').listeners.get('pointerdown'); assert.equal(listener.capture, true);
  listener.handler({button: 0, pointerId: 4, target: {closest: () => ({})}});
  h.term.scrollToLine(4); const anchor = top(h.term);
  assert.equal(h.fixture.scrollIntentVersion, version + 1);
  finish(); await frame; assert.equal(h.term.buffer.active.viewportY, 4); assert.equal(top(h.term), anchor);
  assert.match(h.get('#terminal-scroll-status').textContent, /正在查看历史/);
  assert.equal(h.fixture.input, 'retained human draft'); assert.deepEqual(h.counts(), {focused: 0, resized: 0, fit: 0});
});

test('parser-generated auto-scroll during an active scrollbar gesture is not mistaken for human navigation', async t => {
  const h = harness(t); await write(h.term, lines(0, 30));
  h.get('#terminal').listeners.get('pointerdown').handler({button: 0, target: {closest: () => ({})}});
  const version = h.fixture.scrollIntentVersion;
  await h.context.writeTerminalFrame(h.fixture, lines(30, 5), false);
  assert.equal(h.fixture.scrollIntentVersion, version);
  assert.equal(h.term.buffer.active.viewportY, h.term.buffer.active.baseY);
});

test('capped-scrollback parser trim while scrollbar button is held is not a human viewport drag', async t => {
  const h = harness(t, {scrollback: 10}); await write(h.term, lines(0, 30)); h.term.scrollToLine(6);
  const anchor = top(h.term), version = h.fixture.scrollIntentVersion;
  h.get('#terminal').listeners.get('pointerdown').handler({button: 0, target: {closest: () => ({})}});
  await h.context.writeTerminalFrame(h.fixture, lines(30, 3), false);
  assert.equal(h.fixture.scrollIntentVersion, version); assert.equal(top(h.term), anchor);
  assert.equal(h.term.buffer.active.viewportY, 3); assert.equal(h.term.buffer.active.baseY, 10);
});

test('buffer activation never applies a normal history anchor to alternate mode or its following state back to normal', async t => {
  const h = harness(t); await write(h.term, lines(0, 30)); h.term.scrollToLine(7);
  const scroll = h.term.scrollToLine.bind(h.term), restored = [];
  h.term.scrollToLine = position => { restored.push(position); scroll(position); };
  await h.context.writeTerminalFrame(h.fixture, '\x1b[?1049h\x1b[Hfull-screen app', false);
  assert.equal(h.term.buffer.active.type, 'alternate'); assert.deepEqual(restored, []);
  await h.context.writeTerminalFrame(h.fixture, '\x1b[?1049l', false);
  assert.equal(h.term.buffer.active.type, 'normal'); assert.deepEqual(restored, []);
});

test('ordinary fit/resize preserves reading position while its existing geometry path remains independent of paging', async t => {
  const h = harness(t); await write(h.term, lines(0, 35)); h.term.scrollToLine(11); const anchor = top(h.term);
  h.fixture.fit.fit = () => h.term.resize(55, 9);
  h.context.safeFit(h.fixture); assert.equal(top(h.term), anchor);
  assert.equal(h.term.buffer.active.viewportY, 11); assert.equal(h.term.cols, 55); assert.equal(h.term.rows, 9);
  assert.equal(h.fixture.input, 'retained human draft'); assert.equal(h.network.some(call => /input|resize|connect|create/.test(call.url)), false);
});

test('fully trimmed reading anchor falls back to oldest retained history instead of jumping to newest', async t => {
  const h = harness(t, {scrollback: 10}); await write(h.term, lines(0, 25)); h.term.scrollToLine(2);
  await h.context.writeTerminalFrame(h.fixture, lines(25, 16), false);
  assert.equal(h.term.buffer.active.viewportY, 0); assert.equal(h.term.buffer.active.baseY, 10);
});

test('alternate buffer reports no local history and page keys never reach TUI mouse or arrow handlers', async t => {
  const h = harness(t); await write(h.term, lines(0, 25) + '\x1b[?1049h\x1b[Hfull-screen app');
  h.context.rememberTerminalScroll(h.fixture);
  const emitted = []; h.term.onData(value => emitted.push(value));
  for (const value of ['PageUp', 'PageDown']) { const event = key(value); assert.equal(h.context.handleTerminalKey(h.fixture, event), false); }
  assert.equal(h.term.buffer.active.baseY, 0); assert.equal(h.get('#terminal-page-up').disabled, true);
  assert.match(h.get('#terminal-scroll-status').textContent, /没有本地历史.*会话输出/); assert.deepEqual(emitted, []);
});

test('manager uses the same paging rules without dispatching remote history or focusing a worker', async t => {
  const h = harness(t); h.fixture.kind = 'controller'; h.fixture.controllerId = 'manager-scroll';
  h.context.window.PronController = {isTerminalCurrent: session => session === h.fixture, canWriteTerminal: () => true};
  await write(h.term, lines(0, 25)); h.context.rememberTerminalScroll(h.fixture);
  assert.equal(h.context.handleTerminalKey(h.fixture, key('PageUp')), false);
  assert.match(h.get('#controller-scroll-status').textContent, /正在查看历史/);
  h.context.handleTerminalKey(h.fixture, key('End', {shiftKey: true})); assert.equal(h.get('#controller-latest').disabled, true);
  assert.deepEqual(h.counts(), {focused: 0, resized: 0, fit: 0}); assert.equal(h.fixture.input, 'retained human draft');
});

test('remote read-only viewer hooks own navigation but cannot trigger fit, resize or enabling stdin', async t => {
  const h = harness(t); const pages = [], wheels = [];
  h.context.window.PronTerminalHistory = {page: (session, intent) => { pages.push([session, intent]); return true; },
    wheel: (session, event) => { wheels.push([session, event]); event.preventDefault(); return true; }, render() {}, isActive: () => true, dispose() {}};
  const event = key('PageUp'); h.context.handleTerminalKey(h.fixture, event); assert.equal(pages[0][1], 'page-up');
  const wheel = {deltaY: -30, stopImmediatePropagation() {}, preventDefault() { this.prevented = true; }};
  h.context.handleTerminalWheel(h.fixture, wheel); assert.equal(wheels.length, 1); assert.equal(wheel.prevented, true);
  h.context.safeFit(h.fixture); h.context.scheduleResize(h.fixture, 90, 30);
  h.context.updateDisplayConnection(h.fixture, true); assert.equal(h.term.options.disableStdin, true); assert.equal(h.context.canWriteTerminal(h.fixture), false);
  assert.deepEqual(h.counts(), {focused: 0, resized: 0, fit: 0}); assert.equal(h.fixture.input, 'retained human draft');
  assert.equal(h.network.some(call => /input|resize|connect|create/.test(call.url)), false);
});

test('copy selected in active read-only history uses that viewer selection, never hidden live xterm selection', async t => {
  const h = harness(t), copied = [];
  h.context.navigator.clipboard.writeText = async text => { copied.push(text); };
  h.term.getSelection = () => { throw new Error('hidden live selection must not be read'); };
  h.context.window.PronTerminalHistory = {isActive: () => true, selection: () => 'selected read-only history'};
  await h.context.copyTerminal(h.fixture); assert.deepEqual(copied, ['selected read-only history']);
  assert.equal(h.fixture.input, 'retained human draft'); assert.equal(h.counts().focused, 0);
  h.context.window.PronTerminalHistory.selection = () => '';
  await h.context.copyTerminal(h.fixture); assert.deepEqual(copied, ['selected read-only history']);
});

test('feature-inert legacy xterm stubs safely skip buffer paging without input or new timers', t => {
  const h = harness(t); const real = h.fixture.term;
  h.fixture.term = {hasSelection: () => false};
  assert.doesNotThrow(() => { h.context.initializeTerminalScroll(h.fixture); h.context.handleTerminalKey(h.fixture, key('PageUp')); h.context.rememberTerminalScroll(h.fixture); });
  assert.equal(h.get('#terminal-page-up').disabled, true); h.fixture.term = real;
});

test('navigation markup exposes explicit accessible worker and manager controls with a history-reading status', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  for (const prefix of ['terminal', 'controller']) for (const suffix of ['page-up', 'page-down', 'latest', 'scroll-status']) assert.match(html, new RegExp(`id="${prefix}-${suffix}"`));
  assert.match(html, /id="terminal-scroll-status"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.match(html, /id="controller-page-up"[^>]*aria-controls="controller-terminal"/);
});
