'use strict';
// Evaluate only browser rendering with inert DOM/fetch; no SSH/PTY or input.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element() {
  return { value: '', textContent: '', className: '', hidden: false, disabled: false, checked: false,
    children: [], dataset: {}, style: {}, renders: 0,
    classList: { add() {}, remove() {}, toggle() {} },
    append(...nodes) { this.children.push(...nodes); this.childElementCount = this.children.length; },
    replaceChildren(...nodes) { this.renders++; this.children = nodes; this.childElementCount = nodes.length; },
    setAttribute() {}, addEventListener() {}, showModal() {}, close() {}, reset() {}, focus() {}, select() {} };
}

function harness() {
  const nodes = new Map(), requests = [];
  const get = selector => { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); };
  const host = { id: '316', label: '316', transport: 'ssh', connected: true };
  const hosts = [host];
  const agent = { host: '316', thread_id: 'worker-1', terminal_id: 'ceohub-fixture', mode: 'terminal', connected: false, state: 'exited', current_seq: 10 };
  const context = vm.createContext({ console, URLSearchParams, crypto: { randomUUID: () => 'fixture-key' }, navigator: { clipboard: {} },
    document: { querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element, createTextNode: text => ({ textContent: text }), addEventListener() {} },
    window: { addEventListener() {} }, setTimeout: () => 1, clearTimeout() {}, setInterval: () => 1,
    fetch: async (url, options) => { requests.push({ url, options }); return { ok: true, json: async () => ({ hosts, agents: [agent], events: [], outbox: [] }) }; } });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context, { filename: 'ui.js' });
  const view = () => {
    const card = get('#hosts').children[0];
    return { label: card.children[1].children[0].textContent, dot: card.children[0].children[0].children[0].className };
  };
  return { context, host, hosts, agent, get, requests, view };
}

test('unchanged machine objects still refresh counts and green dot when a worker reconnects then disconnects', async () => {
  const h = harness(), sameHosts = h.hosts, sameHost = h.host;
  await h.context.refresh();
  assert.equal(h.view().label, '显示连接 0 / 1');
  assert.equal(h.view().dot, 'connection-dot');
  h.agent.connected = true; h.agent.state = 'shell'; h.agent.command = 'bash';
  await h.context.refresh(true);
  assert.equal(h.hosts, sameHosts); assert.equal(h.host, sameHost);
  assert.equal(h.view().label, '显示连接 1 / 1');
  assert.equal(h.view().dot, 'connection-dot connected');
  h.agent.connected = false; h.agent.state = 'exited';
  await h.context.refresh(true);
  assert.equal(h.view().label, '显示连接 0 / 1');
  assert.equal(h.view().dot, 'connection-dot');
  assert.equal(h.get('#hosts').renders, 3);
  assert.ok(h.requests.every(request => !request.options?.method), 'Rendering a host count must never reconnect, resize, or send input');
});

test('output-only sequence or text changes do not unnecessarily rerender the machine cards', async () => {
  const h = harness(); h.agent.connected = true; h.agent.state = 'shell';
  await h.context.refresh();
  const renders = h.get('#hosts').renders;
  h.agent.current_seq += 1; h.agent.screen = 'new output';
  await h.context.refresh(true);
  h.agent.current_seq += 1; h.agent.screen = 'more output';
  await h.context.refresh(true);
  assert.equal(h.get('#hosts').renders, renders);
  assert.equal(h.view().label, '显示连接 1 / 1');
});

test('observed current display availability invalidates machine counts even when registry flags stay unchanged', async () => {
  const h = harness(); h.agent.connected = true; h.agent.state = 'shell';
  await h.context.refresh();
  h.context.observed = { key: '316:worker-1', disposed: false, displayConnected: false, agent: h.agent };
  vm.runInContext('terminalSession=observed;', h.context);
  await h.context.refresh(true);
  assert.equal(h.agent.connected, true);
  assert.equal(h.view().label, '显示连接 0 / 1');
  h.context.observed.displayConnected = true;
  await h.context.refresh(true);
  assert.equal(h.view().label, '显示连接 1 / 1');
  assert.equal(h.view().dot, 'connection-dot connected');
});
