'use strict';
// Evaluate the shipped UI with inert DOM/network stubs. These tests never open
// a browser, SSH connection, real PTY, or running worker conversation.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  return {
    value: '', textContent: '', hidden: false, disabled: false, checked: false,
    clientWidth: 100, childElementCount: 0, children: [], dataset: {}, style: {},
    classList: { add() {}, remove() {}, toggle() {} },
    append(...children) { this.children.push(...children); this.childElementCount = this.children.length; },
    replaceChildren(...children) { this.children = children; this.childElementCount = children.length; },
    setAttribute() {}, addEventListener() {}, removeEventListener() {},
    showModal() { this.open = true; }, close() { this.open = false; }, reset() {},
    focus() { this.focused = true; }, select() { this.selected = true; },
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 100, bottom: 100 }),
  };
}

function uiHarness() {
  const elements = new Map(), calls = [], clipboard = { writes: [], readCount: 0 };
  const get = id => { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); };
  const document = {
    querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id),
    createElement: element, createTextNode: text => ({ textContent: text }), addEventListener() {},
  };
  class FakeTerminal {
    constructor() { this.cols = 100; this.rows = 30; this.options = {}; this.selection = 'selected output'; this.pastes = []; this.parser = { registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {} }; }
    attachCustomKeyEventHandler(handler) { this.keyHandler = handler; }
    hasSelection() { return !!this.selection; }
    getSelection() { return this.selection; }
    clearSelection() { this.selection = ''; }
    paste(value) { this.pastes.push(value); }
    open() {} loadAddon() {} focus() {} blur() {} dispose() {} reset() {}
    onData(handler) { this.dataHandler = handler; }
    onResize() {} resize(cols, rows) { this.cols = cols; this.rows = rows; }
    write(data, callback) { callback?.(); }
  }
  const fixture = { key: 'local:ceohub-fixture', agent: { host: 'local', thread_id: 'ceohub-fixture', mode: 'terminal', terminal_id: 'ceohub-fixture', connected: true },
    term: new FakeTerminal(), initialized: true, disposed: false, input: '', inputChain: Promise.resolve(), observer: { disconnect() {} } };
  const context = vm.createContext({
    console, document, URLSearchParams, crypto: { randomUUID: () => 'test-request-key' },
    window: { Terminal: FakeTerminal, FitAddon: { FitAddon: class { fit() {} } }, addEventListener() {}, confirm: () => true },
    navigator: { clipboard: {
      writeText: async value => { clipboard.writes.push(value); },
      readText: async () => { clipboard.readCount++; return 'pasted\ntext'; },
    } },
    ResizeObserver: class { observe() {} disconnect() {} },
    requestAnimationFrame() {}, setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1,
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (url === '/api/terminals/close') {
        fixture.agent.closed = true; fixture.agent.status = 'closed'; fixture.agent.connected = false; fixture.agent.active = false;
        return { ok: true, json: async () => ({ closed: true, terminal_id: fixture.agent.terminal_id }) };
      }
      return { ok: true, json: async () => url.startsWith('/api/state') ? { hosts: [{ id: 'local', transport: 'local', label: 'Local', connected: true }], agents: [fixture.agent], events: [], outbox: [] }
        : url.startsWith('/api/terminals/output') ? { data: '', seq: 1, connected: true, cols: 100, rows: 30 } : {} };
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context, { filename: 'ui.js' });
  context.fixture = fixture;
  vm.runInContext('terminalSession = fixture; selectedAgent = fixture.key; state.agents = [fixture.agent]; selectedHost = "local";', context);
  return { context, fixture, clipboard, calls, elements, get };
}

function key(name, modifiers = {}) {
  let prevented = 0, stopped = 0;
  return { type: 'keydown', key: name, ctrlKey: false, shiftKey: false, altKey: false, metaKey: false, ...modifiers,
    preventDefault() { prevented++; }, stopPropagation() { stopped++; },
    get prevented() { return prevented; }, get stopped() { return stopped; } };
}

test('Windows ordinary paste and Shift+Insert use native clipboard event exactly once', () => {
  const { context } = uiHarness();
  assert.equal(context.classifyTerminalKey(key('v', { ctrlKey: true }), false), 'native-paste');
  assert.equal(context.classifyTerminalKey(key('Insert', { shiftKey: true }), false), 'native-paste');
  assert.equal(context.classifyTerminalKey(key('V', { ctrlKey: true, shiftKey: true }), false), 'paste');
});

test('Ctrl+C copies selected output but still interrupts without a selection', () => {
  const { context } = uiHarness();
  assert.equal(context.classifyTerminalKey(key('c', { ctrlKey: true }), true), 'native-copy');
  assert.equal(context.classifyTerminalKey(key('c', { ctrlKey: true }), false), null);
  assert.equal(context.classifyTerminalKey(key('C', { ctrlKey: true, shiftKey: true }), true), 'copy');
});

test('keyup and ordinary/Alt-modified keys do not trigger clipboard operations', () => {
  const { context } = uiHarness();
  assert.equal(context.classifyTerminalKey(key('v', { type: 'keyup', ctrlKey: true }), true), null);
  assert.equal(context.classifyTerminalKey(key('v', { ctrlKey: true, altKey: true }), true), null);
  assert.equal(context.classifyTerminalKey(key('c'), true), null);
});

test('explicit copy writes only captured terminal selection without sending terminal input', async () => {
  const { context, fixture, clipboard, calls } = uiHarness();
  await context.copyTerminal(fixture);
  assert.deepEqual(clipboard.writes, ['selected output']);
  assert.equal(calls.filter(call => call.url === '/api/terminals/input').length, 0);
});

test('explicit paste uses xterm paste API and does not append Enter or bypass bracketed-paste', async () => {
  const { context, fixture, clipboard, calls } = uiHarness();
  await context.pasteTerminal(fixture);
  assert.equal(clipboard.readCount, 1);
  assert.deepEqual(fixture.term.pastes, ['pasted\ntext']);
  assert.equal(calls.filter(call => call.url === '/api/terminals/input').length, 0);
});

test('clipboard copy denial offers only captured selection in manual copy fallback', async () => {
  const { context, fixture, get, calls } = uiHarness();
  context.navigator.clipboard.writeText = async () => { throw new Error('Permission denied'); };
  await context.copyTerminal(fixture);
  assert.equal(get('#clipboard-dialog').open, true);
  assert.equal(get('#clipboard-text').value, 'selected output');
  assert.equal(get('#clipboard-text').readOnly, true);
  assert.equal(get('#clipboard-text').selected, true);
  assert.equal(vm.runInContext('clipboardTarget.session === fixture && clipboardTarget.mode === "copy"', context), true);
  assert.deepEqual(fixture.term.pastes, []);
  assert.equal(calls.filter(call => call.url === '/api/terminals/input').length, 0);
});

test('clipboard read denial opens a manual paste dialog captured to the same terminal', async () => {
  const { context, fixture, get } = uiHarness();
  context.navigator.clipboard.readText = async () => { throw new Error('Permission denied'); };
  await context.pasteTerminal(fixture);
  assert.equal(get('#clipboard-dialog').open, true);
  assert.equal(get('#clipboard-text').value, '');
  assert.equal(get('#clipboard-text').readOnly, false);
  assert.equal(vm.runInContext('clipboardTarget.session === fixture && clipboardTarget.mode === "paste"', context), true);
  assert.deepEqual(fixture.term.pastes, []);
});

test('pending clipboard permission cannot paste into another selected terminal', async () => {
  const { context, fixture } = uiHarness();
  let resolveRead;
  context.navigator.clipboard.readText = () => new Promise(resolve => { resolveRead = resolve; });
  const paste = context.pasteTerminal(fixture);
  vm.runInContext('terminalSession = null; selectedAgent = "other:terminal";', context);
  resolveRead('must not land in a different worker');
  await paste;
  assert.deepEqual(fixture.term.pastes, []);
});

test('disposed terminal never receives a delayed clipboard paste', async () => {
  const { context, fixture } = uiHarness();
  let resolveRead;
  context.navigator.clipboard.readText = () => new Promise(resolve => { resolveRead = resolve; });
  const paste = context.pasteTerminal(fixture);
  fixture.disposed = true;
  resolveRead('must not reach disposed terminal');
  await paste;
  assert.deepEqual(fixture.term.pastes, []);
});

test('terminal marked closing never receives a delayed clipboard paste', async () => {
  const { context, fixture } = uiHarness();
  let resolveRead;
  context.navigator.clipboard.readText = () => new Promise(resolve => { resolveRead = resolve; });
  const paste = context.pasteTerminal(fixture);
  fixture.closing = true;
  resolveRead('must not reach a closing worker');
  await paste;
  assert.deepEqual(fixture.term.pastes, []);
});

test('terminal initialization installs native clipboard handling without suppressing native defaults', async () => {
  const { context, fixture } = uiHarness();
  vm.runInContext('terminalSession = null;', context);
  await context.openTerminal(fixture.agent);
  const session = vm.runInContext('terminalSession', context), paste = key('v', { ctrlKey: true }), copy = key('c', { ctrlKey: true });
  assert.equal(session.term.keyHandler(paste), false);
  assert.equal(paste.prevented, 0);
  assert.equal(paste.stopped, 0);
  assert.equal(session.term.keyHandler(copy), false);
  assert.equal(copy.prevented, 0);
  session.term.selection = '';
  assert.equal(session.term.keyHandler(key('c', { ctrlKey: true })), true);
});

test('native paste handler does not read clipboard or enqueue a second write', () => {
  const { context, fixture, clipboard, calls } = uiHarness();
  const event = key('v', { ctrlKey: true });
  assert.equal(context.handleTerminalKey(fixture, event), false);
  assert.equal(event.prevented, 0);
  assert.equal(event.stopped, 0);
  assert.equal(clipboard.readCount, 0);
  assert.deepEqual(fixture.term.pastes, []);
  assert.equal(calls.filter(call => call.url === '/api/terminals/input').length, 0);
});

test('explicit clipboard shortcuts prevent browser default once while keyup remains inert', async () => {
  const { context, fixture, clipboard } = uiHarness();
  const copy = key('C', { ctrlKey: true, shiftKey: true });
  assert.equal(context.handleTerminalKey(fixture, copy), false);
  await Promise.resolve();
  assert.equal(copy.prevented, 1);
  assert.equal(copy.stopped, 1);
  assert.deepEqual(clipboard.writes, ['selected output']);
  const release = key('C', { type: 'keyup', ctrlKey: true, shiftKey: true });
  assert.equal(context.handleTerminalKey(fixture, release), true);
  assert.equal(release.prevented, 0);
  assert.deepEqual(clipboard.writes, ['selected output']);
});

test('confirmed close waits accepted input chain before close request and discards unsent local draft', async () => {
  const { context, fixture, calls } = uiHarness();
  let finishInput;
  fixture.inputChain = new Promise(resolve => { finishInput = resolve; });
  fixture.input = 'unsent human draft';
  const target = { host: 'local', thread_id: fixture.agent.thread_id, terminal_id: fixture.agent.terminal_id,
    key: fixture.key, alias: 'Test terminal', command: 'powershell.exe' };
  const closing = context.closeTerminal(target);
  await Promise.resolve();
  assert.equal(calls.filter(call => call.url === '/api/terminals/close').length, 0);
  assert.equal(fixture.input, '');
  finishInput();
  await closing;
  const writes = calls.filter(call => call.url === '/api/terminals/close');
  assert.equal(writes.length, 1);
  const body = JSON.parse(writes[0].options.body);
  assert.equal(body.host, 'local');
  assert.equal(body.thread_id, fixture.agent.thread_id);
  assert.equal(body.confirm, true);
  assert.equal(calls.filter(call => call.url === '/api/terminals/input').length, 0);
});

test('new terminal leaves directory blank for target home instead of inheriting discovery filter', () => {
  const { context, get } = uiHarness();
  get('#discovery-cwd').value = 'D:\\old-machine\\unrelated-filter';
  context.openCreate(null, '316');
  assert.equal(get('#create-cwd').value, '');
});

test('continuing recorded conversation preserves its exact project directory', () => {
  const { context, get } = uiHarness();
  get('#discovery-cwd').value = '/unrelated-filter';
  context.openCreate({ id: 'legacy-thread', title: 'Old worker', cwd: '/home/user/project' }, '316');
  assert.equal(get('#create-cwd').value, '/home/user/project');
  assert.equal(vm.runInContext('createTarget.host', context), '316');
});

test('directory chooser requests only selected host and uses real home returned by service', async () => {
  const { context, get, calls } = uiHarness();
  const previousFetch = context.fetch;
  context.fetch = async (url, options) => {
    if (url !== '/api/directories/list') return previousFetch(url, options);
    calls.push({ url, options });
    return { ok: true, json: async () => ({ path: '/home/server-user', home: '/home/server-user', parent: '/home', roots: [], directories: [] }) };
  };
  context.openCreate(null, '316');
  assert.equal(calls.filter(call => call.url === '/api/directories/list').length, 0);
  context.openDirectories();
  await new Promise(resolve => setImmediate(resolve));
  const body = JSON.parse(calls.find(call => call.url === '/api/directories/list').options.body);
  assert.equal(body.host, '316');
  assert.equal(body.path, '');
  assert.equal(get('#directory-path').value, '/home/server-user');
  context.selectDirectory('/home/server-user');
  assert.equal(get('#create-cwd').value, '/home/server-user');
});

test('late directory response cannot overwrite a newer path selection', async () => {
  const { context, get } = uiHarness();
  const previousFetch = context.fetch;
  let resolveOld;
  context.fetch = async (url, options) => {
    if (url !== '/api/directories/list') return previousFetch(url, options);
    const requested = JSON.parse(options.body).path;
    const result = path => ({ ok: true, json: async () => ({ path, home: '/home/server-user', parent: '/home', roots: [], directories: [] }) });
    if (!requested) return new Promise(resolve => { resolveOld = () => resolve(result('/home/server-user')); });
    return result(requested);
  };
  context.openCreate(null, '316'); context.openDirectories();
  await context.loadDirectory('/home/server-user/new-project');
  resolveOld();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(get('#directory-path').value, '/home/server-user/new-project');
});

test('filesystem directory names render as literal text instead of HTML', async () => {
  const { context, get } = uiHarness();
  const previousFetch = context.fetch;
  const name = '<img src=x onerror=alert(1)>';
  context.fetch = async (url, options) => url === '/api/directories/list'
    ? { ok: true, json: async () => ({ path: '/home/server-user', home: '/home/server-user', roots: [], directories: [{ name, path: '/home/server-user/' + name }] }) }
    : previousFetch(url, options);
  context.openCreate(null, '316'); context.openDirectories();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(get('#directory-list').children[0].textContent, name);
  assert.equal(get('#directory-list').children[0].innerHTML, undefined);
});

test('close dialog captures exact terminal and refuses after selection changes', async () => {
  const { context, fixture, calls } = uiHarness();
  context.openCloseTerminal();
  const target = vm.runInContext('closeTarget', context);
  assert.equal(target.host, 'local');
  assert.equal(target.thread_id, fixture.agent.thread_id);
  assert.equal(target.terminal_id, fixture.agent.terminal_id);
  vm.runInContext('selectedAgent = "316:another-worker";', context);
  await assert.rejects(context.closeTerminal(target), /终端已变化/);
  const writes = calls.filter(call => call.url === '/api/terminals/close');
  assert.equal(writes.length, 0);
});

test('manual paste fallback only invokes captured xterm paste with unchanged text', async () => {
  const { context, fixture, get, calls } = uiHarness();
  context.openClipboardFallback(fixture, 'paste');
  get('#clipboard-text').value = 'not a command\nsecond line';
  get('#clipboard-form').onsubmit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(fixture.term.pastes, ['not a command\nsecond line']);
  assert.equal(calls.filter(call => call.url === '/api/terminals/input').length, 0);
});

test('manual paste confirmation cannot target a newly selected worker', async () => {
  const { context, fixture, get } = uiHarness();
  context.openClipboardFallback(fixture, 'paste');
  get('#clipboard-text').value = 'do not send to new selection';
  vm.runInContext('terminalSession = null; selectedAgent = "316:another-worker";', context);
  get('#clipboard-form').onsubmit({ preventDefault() {} });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(fixture.term.pastes, []);
});
