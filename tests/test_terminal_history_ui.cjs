'use strict';
// Inert read-only history UI; no actual browser, SSH, PTY, or worker input.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return {promise, resolve}; }
function harness() {
  const elements = new Map(), calls = [];
  function element() {
    let body = '', top = 0;
    return {hidden: true, disabled: false, clientHeight: 200, listeners: {},
      get textContent() { return body; }, set textContent(value) { body = value; top = 0; },
      get scrollHeight() { return Math.max(200, body.split('\n').length * 20); },
      get scrollTop() { return top; }, set scrollTop(value) { top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); },
      addEventListener(name, fn) { this.listeners[name] = fn; }, click() { this.onclick?.(); }, contains(node) { return node?.owner === this; }, setAttribute() {}};
  }
  const get = key => { if (!elements.has(key)) elements.set(key, element()); return elements.get(key); };
  const session = {agent: {host: '316', thread_id: 'fixture', terminal_id: 'ceohub-history-fixture'}, disposed: false,
    term: {options: {disableStdin: false}, buffer: {active: {type: 'alternate', baseY: 0}}, focus() { throw new Error('Must not focus'); }}, input: 'preserved draft'};
  let currentSession = session, result = {source: 'tmux_pane_history', terminal_id: session.agent.terminal_id,
    text: Array.from({length: 100}, (_, index) => 'actual retained line ' + index).join('\n'), line_count: 100, alternate_on: false};
  const context = vm.createContext({console, URLSearchParams, window: {}, $: get,
    state: {hosts: [{id: '316', transport: 'ssh'}, {id: 'local', transport: 'local'}]}, terminalSession: session,
    isTerminalDisplayCurrent: s => s === currentSession, canWriteTerminal: s => !s.closed && s.displayConnected !== false,
    renderTerminalScroll: s => context.window.PronTerminalHistory.render(s),
    terminalApiIdentity: s => ({host: s.agent.host, thread_id: s.agent.thread_id}),
    request: async url => { calls.push(url); return await result; }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/terminal_history.js'), 'utf8'), context);
  return {context, session, get, calls, history: context.window.PronTerminalHistory,
    reply: value => { result = value; }, select: value => { currentSession = value; context.terminalSession = value; }};
}
test('history module is inert at startup and reads only explicit selected SSH terminal', async () => {
  const h = harness(); assert.equal(h.calls.length, 0);
  await h.history.open(h.session);
  assert.equal(h.calls.length, 1);
  const query = new URL('http://fixture' + h.calls[0]);
  assert.equal(query.pathname, '/api/terminals/history'); assert.equal(query.searchParams.get('host'), '316');
  assert.equal(query.searchParams.get('thread_id'), 'fixture'); assert.equal(query.searchParams.get('lines'), '5000');
  assert.equal(h.session.input, 'preserved draft'); assert.equal(h.session.term.options.disableStdin, true);
  assert.match(h.get('#terminal-history-info').textContent, /只读快照/);
});
test('PageUp with no local tmux history opens bounded snapshot; repeated pending requests coalesce', async () => {
  const h = harness(), pending = deferred(); h.reply(pending.promise);
  assert.equal(h.history.page(h.session, 'page-up'), true); assert.equal(h.history.page(h.session, 'page-up'), true);
  assert.equal(h.calls.length, 1);
  pending.resolve({source: 'tmux_pane_history', terminal_id: h.session.agent.terminal_id, text: 'one actual line', line_count: 1});
  await Promise.resolve(); await Promise.resolve();
  assert.equal(h.history.isActive(h.session), true);
});
test('paging and latest only affect local reading display, preserve draft and never start an input request', async () => {
  const h = harness(); await h.history.open(h.session);
  const pre = h.get('#terminal-history-text'), initial = pre.scrollTop;
  h.history.page(h.session, 'page-up'); assert.ok(pre.scrollTop < initial);
  h.history.page(h.session, 'page-down'); assert.equal(pre.scrollTop, initial);
  h.history.page(h.session, 'top'); assert.equal(pre.scrollTop, 0);
  h.history.page(h.session, 'latest'); assert.equal(h.history.isActive(h.session), false);
  assert.equal(h.session.term.options.disableStdin, false); assert.equal(h.session.input, 'preserved draft'); assert.equal(h.calls.length, 1);
});
test('normal local scrollback and controller/local terminals do not cause any SSH history read', () => {
  for (const change of [{kind: 'controller'}, {agent: {host: 'local', terminal_id: 'local-fixture'}}, {term: {options: {}, buffer: {active: {baseY: 80}}}}]) {
    const h = harness(); Object.assign(h.session, change);
    assert.equal(h.history.page(h.session, 'page-up'), false); assert.equal(h.calls.length, 0);
  }
});
test('manager navigation rendering cannot hide or disable the selected worker history controls', () => {
  const h = harness(); h.history.render(h.session);
  assert.equal(h.get('#terminal-history').hidden, false); assert.equal(h.get('#terminal-history').disabled, false);
  h.history.render({kind: 'controller'});
  assert.equal(h.get('#terminal-history').hidden, false); assert.equal(h.get('#terminal-history').disabled, false);
});
test('connection render never enables live input behind history and a disconnected return stays disabled', async () => {
  const h = harness(); await h.history.open(h.session);
  h.session.term.options.disableStdin = false; h.history.render(h.session);
  assert.equal(h.session.term.options.disableStdin, true);
  h.session.displayConnected = false; h.history.page(h.session, 'latest');
  assert.equal(h.session.term.options.disableStdin, true);
});
test('changed/disposed target cannot render a pending response into another terminal', async () => {
  const h = harness(), pending = deferred(); h.reply(pending.promise);
  const opening = h.history.open(h.session); h.history.dispose(h.session); h.session.disposed = true; h.select({...h.session, disposed: false});
  pending.resolve({source: 'tmux_pane_history', terminal_id: h.session.agent.terminal_id, text: 'stale', line_count: 1}); await opening;
  assert.equal(h.get('#terminal-history-panel').hidden, true); assert.equal(h.get('#terminal-history-text').textContent, '');
});
test('mismatched pane result and errors stay truthful and never reconnect or retry', async () => {
  const h = harness(); h.reply({source: 'tmux_pane_history', terminal_id: 'another-pane', text: 'wrong'});
  await h.history.open(h.session); assert.match(h.get('#terminal-history-info').textContent, /暂时不可读取/);
  assert.equal(h.get('#terminal-history-text').textContent, ''); assert.equal(h.calls.length, 1);
});
test('retained snapshot explicitly does not claim alternate-screen history is complete', async () => {
  const h = harness(); h.reply({source: 'tmux_pane_history', terminal_id: h.session.agent.terminal_id, text: 'current', line_count: 1, alternate_on: true, truncated: true});
  await h.history.open(h.session); assert.match(h.get('#terminal-history-info').textContent, /重绘或清除/);
  assert.match(h.get('#terminal-history-info').textContent, /读取上限/); assert.match(h.get('#terminal-history-info').textContent, /会话输出/);
});
test('copy selection is restricted to the current read-only history, not stale xterm or other page content', async () => {
  const h = harness(); await h.history.open(h.session);
  const node = {owner: h.get('#terminal-history-text')};
  h.context.window.getSelection = () => ({rangeCount: 1, anchorNode: node, focusNode: node, toString: () => 'selected actual history'});
  assert.equal(h.history.selection(h.session), 'selected actual history');
  h.context.window.getSelection = () => ({rangeCount: 1, anchorNode: node, focusNode: {owner: {}}, toString: () => 'other data'});
  assert.equal(h.history.selection(h.session), '');
  h.history.dispose(h.session); assert.equal(h.history.selection(h.session), '');
});
test('upward wheel can enter read-only history but Ctrl zoom and downward wheel stay native', async () => {
  const h = harness(); let prevented = 0, stopped = 0;
  const event = {deltaY: -100, ctrlKey: false, preventDefault() { prevented++; }, stopImmediatePropagation() { stopped++; }};
  assert.equal(h.history.wheel(h.session, {...event, ctrlKey: true}), false);
  assert.equal(h.history.wheel(h.session, {...event, deltaY: 100}), false);
  assert.equal(h.history.wheel(h.session, event), true); assert.equal(prevented, 1); assert.equal(stopped, 1);
  await Promise.resolve(); await Promise.resolve(); assert.equal(h.calls.length, 1);
});
