'use strict';
// Real parser + inert UI contracts only. No browser, PTY, SSH or worker input.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Terminal} = require('@xterm/headless');

function element(tag = 'div') {
  return {tagName: tag, id: '', value: '', textContent: '', hidden: false, disabled: false, checked: false,
    open: false, children: [], childElementCount: 0, clientWidth: 900, dataset: {}, style: {}, listeners: new Map(),
    classList: {add() {}, remove() {}, toggle() {}},
    append(...items) { this.children.push(...items); this.childElementCount = this.children.length; },
    replaceChildren(...items) { this.children = items; this.childElementCount = items.length; },
    setAttribute() {}, removeAttribute() {}, contains() { return true; },
    addEventListener(type, handler, options) { const list = this.listeners.get(type) || []; list.push({handler, options}); this.listeners.set(type, list); },
    removeEventListener(type, handler) { this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item.handler !== handler)); },
    focus() {}, select() {}, reset() {}, showModal() { this.open = true; }, close() { this.open = false; },
    getBoundingClientRect: () => ({left: 0, top: 0, right: 900, bottom: 600}),
  };
}
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };
async function harness() {
  const elements = new Map(), calls = [], terminals = [];
  const get = selector => { if (!elements.has(selector)) { const item = element(); item.id = selector.slice(1); elements.set(selector, item); } return elements.get(selector); };
  get('#controller-panel').hidden = true;
  class FakeTerminal {
    constructor() {
      this.cols = 100; this.rows = 30; this.options = {}; this.textarea = element('textarea');
      this.buffer = {active: {type: 'normal', viewportY: 0, baseY: 0}};
      this.csi = []; this.dcs = []; this.osc = [];
      this.parser = {registerCsiHandler: (id, handler) => this.csi.push({id, handler}),
        registerDcsHandler: (id, handler) => this.dcs.push({id, handler}), registerOscHandler: (id, handler) => this.osc.push({id, handler})};
      terminals.push(this);
    }
    open(container) { this.element = container; }
    focus() { this.focusEvent('focus'); } blur() { this.focusEvent('blur'); }
    focusEvent(type) {
      for (const entry of this.element.listeners.get(type) || []) entry.handler({type, target: this.textarea});
      this.dataHandler?.(type === 'focus' ? '\x1b[I' : '\x1b[O');
    }
    onData(handler) { this.dataHandler = handler; }
    paste(text) { this.dataHandler?.(text); }
    loadAddon() {} attachCustomKeyEventHandler() {} attachCustomWheelEventHandler() {} onResize() {}
    hasSelection() { return false; } getSelection() { return ''; }
    write(_text, callback) { callback?.(); } resize(cols, rows) { this.cols = cols; this.rows = rows; }
    reset() {} dispose() {} scrollLines() {} scrollToLine() {}
  }
  const worker = {host: 'local', thread_id: 'ceohub-protocol-fixture', terminal_id: 'ceohub-protocol-fixture', mode: 'terminal', connected: true};
  const manager = {id: 'manager-protocol-fixture', terminal_id: 'manager-terminal-fixture', state: 'ready',
    session_id: 'synthetic-manager-session', input_owner: 'hub', tools_ready: true, connected: true, allowed_hosts: {}};
  const context = vm.createContext({
    console, URLSearchParams, structuredClone, crypto: {randomUUID: () => 'synthetic-protocol-request'},
    navigator: {clipboard: {readText: async () => '', writeText: async () => {}}},
    document: {querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element,
      createTextNode: text => ({textContent: text}), addEventListener() {}, removeEventListener() {}},
    window: {Terminal: FakeTerminal, FitAddon: {FitAddon: class {fit() {}}}, addEventListener() {},
      location: {hash: '', pathname: '/', search: ''}, history: {replaceState() {}},
      localStorage: {getItem() {}, setItem() {}}, sessionStorage: {getItem() {}, setItem() {}, removeItem() {}}},
    ResizeObserver: class {observe() {} disconnect() {}}, requestAnimationFrame: callback => callback(),
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    fetch: async (url, options = {}) => {
      calls.push({url: String(url), options, body: options.body ? JSON.parse(options.body) : undefined});
      const data = String(url).startsWith('/api/state') ? {hosts: [{id: 'local', transport: 'local', label: 'Local', connected: true}], agents: [worker], events: [], outbox: []}
        : url === '/api/controllers' ? {controllers: [manager]}
        : String(url).startsWith('/api/controllers/get?') ? manager
        : String(url).includes('/output?') ? {reset: true, ansi: '', seq: 1, connected: true, cols: 100, rows: 30} : {accepted: true};
      return {ok: true, status: 200, json: async () => data};
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context);
  await settle();
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/controller.js'), 'utf8'), context);
  await settle();
  return {context, calls, worker, manager, terminals, get};
}
const write = (terminal, data) => new Promise(resolve => terminal.write(data, resolve));
const inputCalls = h => h.calls.filter(call => /\/input$/.test(call.url));

test('real xterm DECRQM automatically reports without a key; shared browser guards prevent duplicate replies', async () => {
  const h = await harness();
  for (const query of ['\x1b[4$p', '\x1b[?1004$p']) {
    const original = new Terminal({allowProposedApi: true}), guarded = new Terminal({allowProposedApi: true});
    try {
      const before = [], after = [];
      original.onData(data => before.push(data)); guarded.onData(data => after.push(data));
      await write(original, query);
      assert.equal(before.length, 1); assert.match(before[0], /^\x1b\[\??\d+;[0-4]\$y$/);
      h.context.installTerminalProtocolGuards(guarded);
      await write(guarded, query);
      assert.deepEqual(after, []);
    } finally { original.dispose(); guarded.dispose(); }
  }
});

for (const kind of ['worker', 'controller']) {
  async function mounted(h) {
    if (kind === 'worker') { await h.context.selectAgent(h.worker); return vm.runInContext('terminalSession', h.context); }
    await h.context.window.PronController.showTerminal(); return h.context.window.PronController.model.terminal;
  }
  test(`${kind} installs both shared DECRQM handlers and leaves ordinary mode changes alone`, async () => {
    const h = await harness(), session = await mounted(h);
    const identifiers = session.term.csi.map(entry => JSON.stringify(entry.id));
    assert.ok(identifiers.includes(JSON.stringify({intermediates: '$', final: 'p'})));
    assert.ok(identifiers.includes(JSON.stringify({prefix: '?', intermediates: '$', final: 'p'})));
    assert.equal(session.term.csi.some(entry => entry.id.final === 'h' || entry.id.final === 'l'), false);
  });
  test(`${kind} DOM focus/blur reports never enqueue human input or flush a draft`, async () => {
    const h = await harness(), session = await mounted(h);
    session.input = 'retained human draft';
    session.term.focus(); session.term.blur();
    assert.equal(session.input, 'retained human draft'); assert.equal(session.inputTimer, null);
    assert.equal(inputCalls(h).length, 0);
    await settle(); assert.equal(session.focusProtocolEvent, null);
  });
  test(`${kind} preserves literal focus-report bytes pasted outside the DOM focus event and real typing during frames`, async () => {
    const h = await harness(), session = await mounted(h);
    session.term.focus();
    const pasted = '\x1b[I';
    session.term.paste(pasted);
    session.writingFrame = true; session.restoringFrame = true;
    session.term.dataHandler('真实输入\x7f');
    assert.equal(session.input, pasted + '真实输入\x7f');
    h.context.flushTerminalInput(session); await session.inputChain;
    assert.equal(inputCalls(h).length, 1); assert.equal(inputCalls(h)[0].body.data, pasted + '真实输入\x7f');
    await settle(); assert.equal(session.focusProtocolEvent, null);
  });
  test(`${kind} focus filter is exact and disposal removes both captured listeners`, async () => {
    const h = await harness(), session = await mounted(h);
    session.term.focus(); session.term.dataHandler('x\x1b[I');
    assert.equal(session.input, 'x\x1b[I');
    const listeners = session.inputFocusListeners;
    assert.equal(listeners.container.listeners.get('focus').some(entry => entry.handler === listeners.markFocus && entry.options === true), true);
    h.context.disposeTerminalInput(session);
    assert.equal(session.focusProtocolEvent, null);
    assert.equal(listeners.container.listeners.get('focus').some(entry => entry.handler === listeners.markFocus), false);
    assert.equal(listeners.container.listeners.get('blur').some(entry => entry.handler === listeners.markFocus), false);
  });
}
