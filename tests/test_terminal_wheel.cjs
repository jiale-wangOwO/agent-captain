'use strict';
// Inert DOM/xterm contracts: no browser, PTY, SSH, or worker interaction.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  return {
    value: '', textContent: '', hidden: false, disabled: false, checked: false,
    clientWidth: 600, childElementCount: 0, children: [], dataset: {}, style: {}, listeners: new Map(),
    classList: {add() {}, remove() {}, toggle() {}},
    append(...children) { this.children.push(...children); this.childElementCount = this.children.length; },
    replaceChildren(...children) { this.children = children; this.childElementCount = children.length; },
    setAttribute() {}, addEventListener(type, handler, options) { this.listeners.set(type, {handler, options}); },
    showModal() { this.open = true; }, close() { this.open = false; }, reset() {}, focus() {}, select() {},
    getBoundingClientRect: () => ({left: 0, top: 0, right: 600, bottom: 450, height: 450}),
  };
}

function harness() {
  const elements = new Map(), network = [];
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  class FakeTerminal {
    constructor() {
      this.rows = 30; this.cols = 100; this.options = {fontSize: 15, lineHeight: 1};
      this.buffer = {active: {type: 'normal', viewportY: 50, baseY: 100}};
      this.scrolls = []; this.selection = 'keep this selected'; this.focusCount = 0;
      this.element = {querySelector: () => ({getBoundingClientRect: () => ({height: 450})})};
      this.parser = {registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {}};
    }
    scrollLines(amount) {
      this.scrolls.push(amount);
      this.buffer.active.viewportY = Math.max(0, Math.min(this.buffer.active.baseY, this.buffer.active.viewportY + amount));
    }
    attachCustomKeyEventHandler(handler) { this.keyHandler = handler; }
    attachCustomWheelEventHandler(handler) { this.wheelHandler = handler; }
    onData(handler) { this.dataHandler = handler; }
    onResize() {} hasSelection() { return !!this.selection; } getSelection() { return this.selection; }
    loadAddon() {} open() {} focus() { this.focusCount++; } blur() {} reset() {} dispose() {}
    resize(cols, rows) { this.cols = cols; this.rows = rows; }
    write(data, callback) { callback?.(); }
  }
  const fixture = {key: '316:ceohub-wheel-fixture', agent: {host: '316', thread_id: 'ceohub-wheel-fixture', terminal_id: 'ceohub-wheel-fixture', mode: 'terminal', connected: true},
    term: new FakeTerminal(), initialized: true, disposed: false, input: '', inputChain: Promise.resolve()};
  const context = vm.createContext({
    console, URLSearchParams, crypto: {randomUUID: () => 'wheel-test'},
    document: {querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element,
      createTextNode: text => ({textContent: text}), addEventListener() {}},
    window: {Terminal: FakeTerminal, FitAddon: {FitAddon: class {fit() {}}}, addEventListener() {}},
    navigator: {clipboard: {}}, ResizeObserver: class {observe() {} disconnect() {}}, requestAnimationFrame() {},
    setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1,
    fetch: async (url, options) => {
      network.push({url, options});
      return {ok: true, json: async () => url.startsWith('/api/state') ? {hosts: [{id: '316', transport: 'ssh', label: '316'}], agents: [fixture.agent], events: [], outbox: []}
        : url.startsWith('/api/terminals/output') ? {data: '', seq: 3, connected: true, rows: 30, cols: 100} : {accepted: true}};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context, {filename: 'ui.js'});
  context.fixture = fixture;
  vm.runInContext('terminalSession=fixture; selectedAgent=fixture.key; state.agents=[fixture.agent]; selectedHost="316";', context);
  return {context, fixture, network, get};
}

function wheel(deltaY, deltaMode = 0, extra = {}) {
  return {type: 'wheel', deltaY, deltaX: 0, deltaMode, ctrlKey: false, stopped: false, prevented: false, ...extra,
    stopImmediatePropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; }};
}

test('wheel capture is non-passive and stops xterm native processing before the target', () => {
  const {fixture, get} = harness();
  const listener = get('#terminal').listeners.get('wheel');
  assert.equal(listener.options.capture, true); assert.equal(listener.options.passive, false);
  const event = wheel(3, 1); listener.handler(event);
  assert.equal(event.stopped, true); assert.equal(event.prevented, true);
  assert.deepEqual(fixture.term.scrolls, [3]); assert.equal(fixture.input, '');
});

test('normal history scrolling preserves selection and focus and makes no input request', () => {
  const {context, fixture, network} = harness();
  context.handleTerminalWheel(fixture, wheel(-5, 1));
  assert.equal(fixture.term.buffer.active.viewportY, 45);
  assert.equal(fixture.term.selection, 'keep this selected'); assert.equal(fixture.term.focusCount, 0);
  assert.equal(fixture.input, ''); assert.equal(network.filter(call => call.url === '/api/terminals/input').length, 0);
});

test('normal, alternate, SGR mouse and alternate-scroll modes never receive wheel bytes', () => {
  for (const mode of ['normal', 'alternate', 'SGR-mouse', 'alternateScroll', 'application-cursor']) {
    const {context, fixture} = harness();
    fixture.term.buffer.active = {type: mode === 'normal' ? 'normal' : 'alternate', viewportY: 0, baseY: 0};
    for (const delta of [-120, 120]) {
      const event = wheel(delta); context.handleTerminalWheel(fixture, event);
      // If capture fails, xterm's documented default can emit arrows/mouse reports.
      if (!event.stopped) fixture.input += mode === 'SGR-mouse' ? '\x1b[<64;1;1M' : '\x1b[A';
      assert.equal(fixture.input, '', mode); assert.equal(event.prevented, false, 'page remains scrollable');
    }
  }
});

test('pixel deltas accumulate sub-line motion without dropping trackpad scroll', () => {
  const {context, fixture} = harness();
  context.handleTerminalWheel(fixture, wheel(5)); context.handleTerminalWheel(fixture, wheel(5));
  assert.deepEqual(fixture.term.scrolls, []);
  context.handleTerminalWheel(fixture, wheel(5)); assert.deepEqual(fixture.term.scrolls, [1]);
  assert.equal(fixture.wheelRemainder, 0);
  context.handleTerminalWheel(fixture, wheel(-7.5)); context.handleTerminalWheel(fixture, wheel(-7.5));
  assert.deepEqual(fixture.term.scrolls, [1, -1]);
});

test('line and page delta modes use logical lines and visible terminal rows', () => {
  const {context, fixture} = harness();
  context.handleTerminalWheel(fixture, wheel(2.25, 1)); context.handleTerminalWheel(fixture, wheel(.75, 1));
  context.handleTerminalWheel(fixture, wheel(-1, 2));
  assert.deepEqual(fixture.term.scrolls, [2, 1, -30]);
});

test('fractional reverse motion cancels rather than emitting a cursor input', () => {
  const {context, fixture} = harness();
  context.handleTerminalWheel(fixture, wheel(6)); context.handleTerminalWheel(fixture, wheel(-6));
  assert.deepEqual(fixture.term.scrolls, []); assert.equal(fixture.wheelRemainder, 0); assert.equal(fixture.input, '');
});

test('top and bottom boundaries let the outer page scroll without reaching xterm', () => {
  for (const [position, delta] of [[0, -1], [100, 1]]) {
    const {context, fixture} = harness(); fixture.term.buffer.active.viewportY = position; fixture.wheelRemainder = .4;
    const event = wheel(delta, 1); context.handleTerminalWheel(fixture, event);
    assert.equal(event.stopped, true); assert.equal(event.prevented, false); assert.equal(fixture.wheelRemainder, 0);
    assert.deepEqual(fixture.term.scrolls, []);
  }
});

test('large wheel movement clamps at the local history boundary', () => {
  const {context, fixture} = harness(); fixture.term.buffer.active.viewportY = 2;
  context.handleTerminalWheel(fixture, wheel(-100, 1));
  assert.deepEqual(fixture.term.scrolls, [-2]); assert.equal(fixture.input, '');
});

test('buffer switches do not carry fractional scroll into a different screen', () => {
  const {context, fixture} = harness(); context.handleTerminalWheel(fixture, wheel(7.5));
  fixture.term.buffer.active = {type: 'normal', viewportY: 20, baseY: 80};
  context.handleTerminalWheel(fixture, wheel(7.5)); assert.deepEqual(fixture.term.scrolls, []);
});

test('closed terminal output remains locally scrollable but disposed views do not change', () => {
  const {context, fixture} = harness(); fixture.closed = true; fixture.closing = true;
  context.handleTerminalWheel(fixture, wheel(-2, 1)); assert.deepEqual(fixture.term.scrolls, [-2]);
  fixture.disposed = true; const event = wheel(-2, 1); context.handleTerminalWheel(fixture, event);
  assert.deepEqual(fixture.term.scrolls, [-2]); assert.equal(event.prevented, false);
});

test('horizontal movement and browser zoom keep native defaults but cannot report mouse input', () => {
  const {context, fixture} = harness();
  for (const event of [wheel(0, 0, {deltaX: 80}), wheel(-100, 0, {ctrlKey: true})]) {
    context.handleTerminalWheel(fixture, event); assert.equal(event.stopped, true); assert.equal(event.prevented, false);
  }
  assert.deepEqual(fixture.term.scrolls, []); assert.equal(fixture.input, '');
});

test('initialization adds xterm wheel defense and real keyboard arrow bytes still reach input', async () => {
  const {context, fixture, network} = harness();
  vm.runInContext('terminalSession=null;', context);
  await context.openTerminal(fixture.agent);
  const session = vm.runInContext('terminalSession', context);
  assert.equal(session.term.wheelHandler(wheel(120)), false);
  assert.equal(session.term.keyHandler({type: 'keydown', key: 'ArrowUp'}), true);
  session.term.dataHandler('\x1b[A'); assert.equal(session.input, '\x1b[A');
  context.flushTerminalInput(session); await session.inputChain;
  const input = network.find(call => call.url === '/api/terminals/input');
  assert.equal(JSON.parse(input.options.body).data, '\x1b[A');
});
