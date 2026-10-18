'use strict';
// Evaluate only browser rendering with inert DOM/fetch; no SSH/PTY or input.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element(tag = 'div') {
  return { tagName: tag, value: '', textContent: '', className: '', hidden: false, disabled: false, checked: false,
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
    const latency = card.children[0].children.find(child => /(?:^|\s)host-latency(?:\s|$)/.test(child.className));
    return { label: card.children[1].children[0].textContent, dot: card.children[0].children[0].children[0].className,
      latency: latency && {text: latency.textContent, title: latency.title, className: latency.className} };
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

test('latency view distinguishes local and unmeasured hosts without inventing a zero ping', () => {
  const h = harness(), requestsBefore = h.requests.length;
  const local = h.context.hostLatencyView({transport: 'local'}, 1000000);
  assert.equal(local.state, 'local'); assert.match(local.text, /本机/); assert.doesNotMatch(local.text, /0 ms/);
  const unknown = h.context.hostLatencyView({transport: 'ssh'}, 1000000);
  assert.equal(unknown.state, 'unknown'); assert.equal(unknown.text, 'SSH 响应 —');
  assert.match(unknown.title, /不是网络 ping.*不会额外发起探测/);
  assert.equal(h.requests.length, requestsBefore, 'Formatting latency makes no additional HTTP or SSH request');
});

test('successful samples display observed SSH response units and stale or future samples stay historical', () => {
  const h = harness(), now = 2000000, requestsBefore = h.requests.length;
  const sample = {metric: 'ssh_response', state: 'measured', last_ms: 125.4, last_success_at: now / 1000};
  const measured = h.context.hostLatencyView({transport: 'ssh', latency: sample}, now);
  assert.equal(measured.state, 'measured'); assert.equal(measured.text, 'SSH 响应 125 ms');
  assert.match(measured.title, /建连、远端执行和传输/);
  const seconds = h.context.hostLatencyView({transport: 'ssh', latency: {...sample, last_ms: 2450}}, now);
  assert.equal(seconds.text, 'SSH 响应 2.5 s');
  const stale = h.context.hostLatencyView({transport: 'ssh', latency: sample}, now + 120001);
  assert.equal(stale.state, 'stale'); assert.equal(stale.text, '上次 SSH 125 ms'); assert.match(stale.title, /历史样本，不代表当前响应/);
  const future = h.context.hostLatencyView({transport: 'ssh', latency: sample}, now - 1);
  assert.equal(future.state, 'stale'); assert.match(future.text, /^上次 SSH/);
  assert.equal(h.requests.length, requestsBefore);
});

test('failed SSH queries label any previous successful measurement instead of presenting it as current', () => {
  const h = harness(), requestsBefore = h.requests.length;
  const previous = h.context.hostLatencyView({transport: 'ssh', latency: {metric: 'ssh_response', state: 'failed', last_ms: 1500, last_success_at: 1000}}, 2000000);
  assert.equal(previous.state, 'failed'); assert.equal(previous.text, 'SSH 查询失败 · 上次 1.5 s');
  assert.match(previous.title, /最近一次查询未成功.*上次成功/);
  const absent = h.context.hostLatencyView({transport: 'ssh', latency: {metric: 'ssh_response', state: 'failed', last_ms: null, last_success_at: null}}, 2000000);
  assert.equal(absent.state, 'failed'); assert.equal(absent.text, 'SSH 响应暂不可用'); assert.doesNotMatch(absent.text, /0 ms/);
  for (const metric of ['ping', undefined]) {
    const invalidSource = h.context.hostLatencyView({transport: 'ssh', latency: {metric, state: 'failed', last_ms: 1500, last_success_at: 1000}}, 2000000);
    assert.equal(invalidSource.state, 'failed'); assert.equal(invalidSource.text, 'SSH 响应暂不可用');
    assert.doesNotMatch(invalidSource.title, /上次成功|1\.5 s/);
  }
  assert.equal(h.requests.length, requestsBefore);
});

test('invalid latency measurements cannot be rendered as an observed SSH response', () => {
  const h = harness(), requestsBefore = h.requests.length, valid = {metric: 'ssh_response', state: 'measured', last_ms: 123, last_success_at: 1000};
  const invalid = [{last_ms: -1}, {last_ms: NaN}, {last_ms: Infinity}, {last_ms: '123'}, {last_ms: null},
    {last_success_at: 0}, {last_success_at: null}, {last_success_at: '1000'}, {last_success_at: NaN}, {last_success_at: 1e20},
    {metric: 'ping'}, {metric: undefined}, {state: 'unknown'}];
  for (const change of invalid) {
    const view = h.context.hostLatencyView({transport: 'ssh', latency: {...valid, ...change}}, 1000000);
    assert.equal(view.state, 'unknown'); assert.equal(view.text, 'SSH 响应 —');
  }
  assert.equal(h.requests.length, requestsBefore);
});

test('a measurement ages into a historical host-card sample with unchanged objects and no extra probes', async () => {
  const h = harness(), sameHosts = h.hosts, sameHost = h.host;
  h.context.clock = {now: 2000000}; vm.runInContext('Date.now=()=>clock.now;', h.context);
  h.host.latency = {metric: 'ssh_response', state: 'measured', last_ms: 321, last_success_at: 2000};
  const sameSample = h.host.latency;
  await h.context.refresh();
  assert.equal(h.view().latency.text, 'SSH 响应 321 ms'); assert.equal(h.view().latency.className, 'host-latency measured');
  const rendered = h.get('#hosts').renders;
  h.context.clock.now += 120001;
  await h.context.refresh(true);
  assert.equal(h.hosts, sameHosts); assert.equal(h.host, sameHost); assert.equal(h.host.latency, sameSample);
  assert.equal(h.get('#hosts').renders, rendered + 1);
  assert.equal(h.view().latency.text, '上次 SSH 321 ms'); assert.equal(h.view().latency.className, 'host-latency stale');
  assert.match(h.view().latency.title, /不代表当前响应/);
  await h.context.refresh(true);
  assert.equal(h.get('#hosts').renders, rendered + 1, 'Unchanged stale labels do not rebuild machine cards');
  assert.ok(h.requests.every(request => !request.options?.method || request.options.method === 'GET'));
  assert.ok(h.requests.every(request => /^\/api\/(?:state\?|terminal-service\/status)/.test(request.url)),
    'Only existing cached state/status GETs run; no SSH measurement endpoint or worker operation is called');
});
