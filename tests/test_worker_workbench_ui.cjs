'use strict';
// Inert UI contracts for the shipped workbench: no browser, SSH, PTY or Codex.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
function element(tag = 'div') {
  return {tagName: tag, value: '', textContent: '', checked: false, hidden: false, disabled: false, clientWidth: 900, children: [], childElementCount: 0, style: {}, dataset: {}, listeners: {}, classList: {add() {}, remove() {}, toggle() {}},
    append(...items) { this.children.push(...items); this.childElementCount = this.children.length; }, replaceChildren(...items) { this.children = items; this.childElementCount = items.length; }, setAttribute() {}, addEventListener(type, handler) { this.listeners[type] = handler; }, removeEventListener() {}, contains() { return false; }, focus() { this.focusCount = (this.focusCount || 0) + 1; }, select() {}, reset() {}, showModal() { this.open = true; }, close() { this.open = false; }, getBoundingClientRect: () => ({left: 0, top: 0, right: 900, bottom: 600})};
}
const worker = (host, id, alias, extra = {}) => ({host, thread_id: id, terminal_id: `terminal-${id}`, mode: 'terminal', alias, cwd: host === 'local' ? 'C:/work/project' : '/home/example/workspace/project', connected: true, active: true, command: 'codex', state: 'agent', agent_ready: true, management: {status: 'ready', label: 'Codex 输入就绪', primary_action: 'open_terminal', needs_attention: false}, ...extra});
function descendants(root) { return [root, ...(root.children || []).flatMap(descendants)]; }
const cardButton = (card, label) => descendants(card).find(item => item.tagName === 'button' && item.textContent === label);
const cardTitles = get => get('#agents').children.filter(c => /agent-card/.test(c.className || '')).map(c => descendants(c).find(item => item.tagName === 'strong')?.textContent);
async function settle() { for (let i = 0; i < 15; i++) await Promise.resolve(); }
async function harness(options = {}) {
  const elements = new Map(), calls = [], storage = options.storage || new Map(), windowListeners = {}, timers = new Map(), terminals = [], clipboard = []; let uuid = 0, timerId = 0;
  let agents = options.agents || [worker('local', 'one', 'Alpha'), worker('remote', 'two', 'Beta'), worker('remote', 'closed', 'Archived', {closed: true, status: 'closed', state: 'closed', connected: false, active: false})];
  const hosts = [{id: 'local', label: 'My computer', transport: 'local', connected: true}, {id: 'remote', label: 'Remote machine', transport: 'ssh', connected: true}];
  const get = selector => { if (!elements.has(selector)) { const item = element(); item.id = selector.slice(1); elements.set(selector, item); } return elements.get(selector); };
  class FakeTerminal {
    constructor() { this.cols = 100; this.rows = 30; this.options = {}; this.focusCount = 0; this.pastes = []; this.parser = {registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {}}; terminals.push(this); }
    open() {} loadAddon() {} focus() { this.focusCount++; } blur() {} reset() {} dispose() { this.disposed = true; } attachCustomKeyEventHandler(handler) { this.keyHandler = handler; } attachCustomWheelEventHandler() {} onData(handler) { this.dataHandler = handler; } onResize(handler) { this.resizeHandler = handler; } resize(cols, rows) { this.cols = cols; this.rows = rows; this.resizeHandler?.({cols, rows}); } write(data, callback) { callback?.(); } hasSelection() { return true; } getSelection() { return 'worker output'; } paste(text) { this.pastes.push(text); }
  }
  const location = {origin: 'http://fixture', pathname: '/', search: '', hash: options.hash || ''};
  const context = vm.createContext({console, URLSearchParams, crypto: {randomUUID: () => `workbench-operation-${++uuid}`}, navigator: {clipboard: {writeText: async text => clipboard.push(text), readText: async () => 'clipboard text'}},
    document: {querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element, createTextNode: text => ({textContent: text}), addEventListener() {}},
    window: {Terminal: FakeTerminal, FitAddon: {FitAddon: class {fit() {}}}, addEventListener(type, handler) { windowListeners[type] = handler; }, location, history: {replaceState(_state, _title, url) { location.hash = url.includes('#') ? '#' + url.split('#')[1] : ''; }}, localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}, PronController: options.controller},
    ResizeObserver: class {observe() {} disconnect() {}}, requestAnimationFrame: callback => callback(), setInterval() {}, setTimeout: callback => { timers.set(++timerId, callback); return timerId; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, opts = {}) => {
      const body = opts.body ? JSON.parse(opts.body) : undefined; const call = {url: String(url), options: opts, body}; calls.push(call);
      const reply = value => ({ok: true, status: 200, json: async () => value});
      if (options.handle) { const result = await options.handle(call, {agents, hosts, reply}); if (result) return result; }
      if (url.startsWith('/api/state')) return reply({hosts, agents, outbox: [], events: []});
      if (url.startsWith('/api/terminals/output')) { const query = new URLSearchParams(url.split('?')[1]); const agent = agents.find(a => a.host === query.get('host') && a.thread_id === query.get('thread_id')); return reply({data: '', seq: 1, reset: true, cols: 100, rows: 30, connected: agent?.connected !== false}); }
      if (url === '/api/agents/update') { const agent = agents.find(a => a.host === body.host && a.thread_id === body.thread_id); Object.assign(agent, body); return reply({...agent}); }
      if (url === '/api/agents/reconnect') { const agent = agents.find(a => a.host === body.host && a.thread_id === body.thread_id); agent.connected = true; return reply({...agent, recovery: {code: 'connected', message: '已重新连接原 tmux 显示，未运行 resume 或任务。'}}); }
      if (url === '/api/agents/read') return reply({messages: [], signals: [], content_complete: true, has_more: false});
      return reply({accepted: true});
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context); await context.refresh(); await settle();
  return {context, get, calls, agents, hosts, storage, terminals, windowListeners, location, clipboard};
}
function setFilter(h, field, value) { h.get('#agent-' + (field === 'query' ? 'search' : field + '-filter')).value = value; h.get('#agent-' + (field === 'query' ? 'search' : field + '-filter'))[field === 'query' ? 'oninput' : 'onchange'](); }

test('default workbench is cross-machine and closed records are hidden with honest counts', async () => {
  const h = await harness(); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta']); assert.equal(h.get('#registry-count').textContent, '2');
  assert.match(h.get('#agent-count-caption').textContent, /2 \/ 3.*1.*已关闭.*不是任务完成/); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
});

test('fresh default working includes connected, pinned or management-enabled objects without old inactive noise', async () => {
  const h = await harness({agents: [worker('local', 'one', 'Alpha', {active: false}), worker('remote', 'two', 'Beta'), worker('remote', 'old', 'Old offline', {active: false, connected: false}), {host: 'local', thread_id: 'history', alias: 'Old history', mode: 'observe', active: false}]});
  assert.equal(h.get('#agent-status-filter').value, 'working'); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta']);
  setFilter(h, 'status', 'all'); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta', 'Old history', 'Old offline']);
  h.agents[2].pinned = true; h.context.clearAgentFilters(); assert.deepEqual(cardTitles(h.get), ['Old offline', 'Alpha', 'Beta']);
  h.agents[1].connected = false; await h.context.refresh(true); assert.ok(cardTitles(h.get).includes('Beta'), 'Management-enabled disconnected worker remains visible');
});

test('previous explicit all preference stays all, rather than silently changing existing user choice', async () => {
  const storage = new Map([['pron-hub.worker-workbench.v1', JSON.stringify({status: 'all'})]]);
  const h = await harness({storage, agents: [worker('remote', 'old', 'Old offline', {active: false, connected: false})]});
  assert.equal(h.get('#agent-status-filter').value, 'all'); assert.deepEqual(cardTitles(h.get), ['Old offline']);
});

test('machine, name/directory, connection and attention filters compose; closed requires explicit filter', async () => {
  const h = await harness({agents: [worker('local', 'one', 'Alpha'), worker('remote', 'two', 'Beta', {connected: false, management: {needs_attention: true, label: '断开', primary_action: 'reconnect'}}), worker('remote', 'closed', 'Archived', {closed: true, connected: false})]});
  setFilter(h, 'host', 'remote'); assert.deepEqual(cardTitles(h.get), ['Beta']); setFilter(h, 'query', 'workspace'); assert.deepEqual(cardTitles(h.get), ['Beta']);
  setFilter(h, 'status', 'connected'); assert.deepEqual(cardTitles(h.get), []); setFilter(h, 'status', 'attention'); assert.deepEqual(cardTitles(h.get), ['Beta']);
  setFilter(h, 'query', ''); setFilter(h, 'status', 'closed'); assert.deepEqual(cardTitles(h.get), ['Archived']);
  h.context.clearAgentFilters(); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta']);
});

test('pinned ordering is stable across registry order changes; pin mutation updates metadata only', async () => {
  const h = await harness({agents: [worker('remote', 'b', 'Beta'), worker('local', 'a', 'Alpha', {pinned: true})]});
  assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta']); h.agents.reverse(); await h.context.refresh(true); assert.deepEqual(cardTitles(h.get), ['Alpha', 'Beta']);
  const beta = h.get('#agents').children[1]; await cardButton(beta, '☆').onclick();
  assert.deepEqual(h.calls.find(c => c.url === '/api/agents/update').body, {host: 'remote', thread_id: 'b', pinned: true});
  assert.equal(h.calls.some(c => /activate|input|\/create|\/connect/.test(c.url)), false);
});

test('inline rename keeps draft during polling and preserves opened worker terminal', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); const session = vm.runInContext('terminalSession', h.context); session.input = 'worker draft';
  h.context.openAgentRename(h.agents[0]); const target = vm.runInContext('renameTarget', h.context); target.value = 'New display name'; const before = h.get('#agents').children[0];
  await h.context.refresh(true); assert.equal(h.get('#agents').children[0], before); await h.context.saveAgentRename(h.agents[0]);
  assert.equal(vm.runInContext('terminalSession', h.context), session); assert.equal(session.input, 'worker draft'); assert.equal(session.term.focusCount, 0);
  assert.deepEqual(h.calls.find(c => c.url === '/api/agents/update').body, {host: 'local', thread_id: 'one', alias: 'New display name'});
});

test('filtering and machine navigation preserve selection, terminal DOM and human draft without focus', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); const session = vm.runInContext('terminalSession', h.context); session.input = 'human draft';
  h.context.selectHost('remote'); setFilter(h, 'query', 'Beta'); await h.context.refresh(true);
  assert.equal(vm.runInContext('selectedAgent', h.context), 'local:one'); assert.equal(vm.runInContext('terminalSession', h.context), session); assert.equal(session.input, 'human draft'); assert.equal(session.term.focusCount, 0);
});

test('browser preferences and encoded hash restore exact registered view with GET only, never attach/start/focus', async () => {
  const storage = new Map([['pron-hub.worker-workbench.v1', JSON.stringify({host: 'remote', query: 'Beta', status: 'attention', agent: 'local:one', selectedHost: 'remote'})]]);
  const h = await harness({storage, hash: '#agent=' + encodeURIComponent('remote::two')});
  assert.equal(vm.runInContext('selectedAgent', h.context), 'remote:two'); assert.equal(h.get('#agent-search').value, 'Beta'); assert.equal(h.get('#agent-status-filter').value, 'attention');
  assert.equal(h.terminals[0].focusCount, 0); assert.equal(h.calls.filter(c => c.options.method === 'POST').length, 0);
  assert.equal(h.calls.some(c => /resume|discover|\/create|\/reconnect|\/connect/.test(c.url)), false);
});

test('unknown deep link never discovers history or guesses another session', async () => {
  const h = await harness({hash: '#agent=' + encodeURIComponent('remote:missing')}); assert.equal(vm.runInContext('selectedAgent', h.context), null);
  assert.equal(h.calls.some(c => /history|discover|output|\/create|\/connect/.test(c.url)), false);
});

test('quick output opens exact existing record without sending input or reconnecting disconnected agent', async () => {
  const h = await harness({agents: [worker('remote', 'offline', 'Disconnected', {connected: false})]});
  await cardButton(h.get('#agents').children[0], '会话输出').onclick();
  const read = h.calls.find(c => c.url === '/api/agents/read'); assert.equal(read.body.host, 'remote'); assert.equal(read.body.thread_id, 'offline');
  assert.equal(h.calls.some(c => /\/input|\/create|\/reconnect|\/connect/.test(c.url)), false); assert.equal(h.terminals[0].focusCount, 0);
});

test('quick close selects and captures intended worker then waits for original explicit confirm dialog', async () => {
  const h = await harness(); await cardButton(h.get('#agents').children[1], '关闭终端…').onclick();
  assert.equal(h.get('#close-dialog').open, true); assert.equal(vm.runInContext('closeTarget.host', h.context), 'remote'); assert.equal(vm.runInContext('closeTarget.thread_id', h.context), 'two');
  assert.equal(h.calls.some(c => c.url.endsWith('/close')), false);
});

test('reconnect is shown only for disconnected open terminals and calls only safe exact-agent recovery', async () => {
  const h = await harness({agents: [worker('local', 'one', 'Online'), worker('remote', 'two', 'Offline', {connected: false})]});
  const offline = h.get('#agents').children.find(card => cardButton(card, '重连原终端'));
  assert.ok(offline); assert.equal(h.get('#agents').children.filter(card => cardButton(card, '重连原终端')).length, 1);
  await cardButton(offline, '重连原终端').onclick(); const reconnect = h.calls.find(c => c.url === '/api/agents/reconnect');
  assert.equal(reconnect.body.host, 'remote'); assert.equal(reconnect.body.thread_id, 'two'); assert.ok(reconnect.body.request_key);
  assert.equal(h.calls.some(c => /\/create|\/connect|\/input|resume/.test(c.url)), false); assert.match(h.get('#agent-recovery-notice').textContent, /未运行 resume/);
});

test('uncertain reconnect retains same request key, never silently retries or creates a terminal', async () => {
  let lost = true;
  const h = await harness({agents: [worker('remote', 'two', 'Offline', {connected: false})], handle: async call => { if (call.url === '/api/agents/reconnect' && lost) { lost = false; throw new Error('lost connection'); } }});
  await h.context.selectAgent(h.agents[0]); await assert.rejects(h.context.reconnectTerminal(), /lost connection/);
  const first = h.calls.find(c => c.url === '/api/agents/reconnect').body.request_key; await h.context.refresh(true); assert.equal(h.calls.filter(c => c.url === '/api/agents/reconnect').length, 1);
  await h.context.reconnectTerminal(); assert.equal(h.calls.filter(c => c.url === '/api/agents/reconnect')[1].body.request_key, first);
  assert.equal(h.calls.some(c => /\/create|\/connect|\/input|resume/.test(c.url)), false);
});

test('known blocked reconnect keeps useful classification but never creates shell or resumes stopped Codex', async () => {
  const h = await harness({agents: [worker('remote', 'two', 'Offline', {connected: false})], handle: async (call, backend) => call.url === '/api/agents/reconnect' ? backend.reply({...backend.agents[0], recovery: {code: 'tmux_missing', message: '原持久终端不存在，请由用户选择新建终端。', next_action: 'create_new_terminal'}}) : null});
  await h.context.selectAgent(h.agents[0]); await h.context.reconnectTerminal(); assert.match(h.get('#agent-recovery-notice').textContent, /原持久终端不存在/);
  assert.equal(h.calls.some(c => /\/create|\/connect|\/input|resume/.test(c.url)), false); assert.equal(vm.runInContext('terminalSession.displayConnected', h.context), false);
});

test('global activation and current manager grant are distinct; range quick action only opens settings', async () => {
  let opened = 0;
  const h = await harness({controller: {model: {controller: {scope: ['local::one']}}, openSettings(scopeOnly) { assert.equal(scopeOnly, true); opened++; }}});
  const cards = h.get('#agents').children; assert.equal(h.context.agentManagerGrant(h.agents[0]), true); assert.equal(h.context.agentManagerGrant(h.agents[1]), false);
  assert.ok(descendants(cards[0]).some(item => item.textContent === '当前主控已授权')); assert.ok(descendants(cards[1]).some(item => item.textContent === '当前主控未授权'));
  await cardButton(cards[1], '管理范围').onclick(); assert.equal(opened, 1); assert.equal(h.calls.some(c => /activate|scope/.test(c.url)), false);
});

test('empty registry and empty search have different helpful, non-scanning states', async () => {
  const empty = await harness({agents: []}); assert.match(empty.get('#agents').children[0].textContent, /还没有登记/); assert.equal(empty.calls.some(c => /discover/.test(c.url)), false);
  const h = await harness(); setFilter(h, 'query', 'missing project'); assert.match(h.get('#agents').children[0].textContent, /没有符合筛选/); assert.ok(cardButton(h.get('#agents'), '清除筛选，查看工作会话'));
});

test('copied direct link names exact registry key and text metadata is never interpreted as HTML', async () => {
  const h = await harness({agents: [worker('remote', 'two', '<img onerror=alert(1)>')]}); await h.context.copyAgentLink(h.agents[0]);
  assert.equal(h.clipboard[0], 'http://fixture/#agent=remote%3Atwo'); assert.equal(cardTitles(h.get)[0], '<img onerror=alert(1)>');
});

test('new terminal respects explicit machine filter even when an older selected detail is on another machine', async () => {
  const h = await harness(); await h.context.selectAgent(h.agents[0]); h.context.selectHost('remote'); h.get('#new-agent').onclick();
  assert.equal(vm.runInContext('selectedAgent', h.context), 'local:one'); assert.equal(vm.runInContext('createTarget.host', h.context), 'remote');
  assert.equal(h.calls.some(call => call.url === '/api/terminals/create'), false);
});

test('a later confirmed-disconnect recovery uses a new key, while uncertain response survives browser reload', async () => {
  const h = await harness({agents: [worker('remote', 'two', 'Offline', {connected: false})]}); await h.context.selectAgent(h.agents[0]); await h.context.reconnectTerminal();
  const first = h.calls.find(call => call.url === '/api/agents/reconnect').body.request_key;
  h.agents[0].connected = false; h.context.updateDisplayConnection(vm.runInContext('terminalSession', h.context), false); await h.context.reconnectTerminal();
  assert.notEqual(h.calls.filter(call => call.url === '/api/agents/reconnect')[1].body.request_key, first);
  const stored = new Map([['pron-hub.worker-workbench.v1', JSON.stringify({agent: 'remote:two', reconnect: [['remote:two', {requestKey: 'existing-uncertain-key', acknowledged: false}]]})]]);
  const reload = await harness({storage: stored, agents: [worker('remote', 'two', 'Offline', {connected: false})]});
  assert.equal(reload.calls.some(call => call.url === '/api/agents/reconnect'), false); await reload.context.reconnectTerminal();
  assert.equal(reload.calls.find(call => call.url === '/api/agents/reconnect').body.request_key, 'existing-uncertain-key');
});

test('canonical connected output reconciles lost successful reconnect before a later disconnect cycle', async () => {
  let lost = true;
  const h = await harness({agents: [worker('remote', 'two', 'Offline', {connected: false})], handle: async (call, backend) => {
    if (call.url === '/api/agents/reconnect' && lost) { lost = false; backend.agents[0].connected = true; throw new Error('response lost after attach'); }
  }});
  await h.context.selectAgent(h.agents[0]); await assert.rejects(h.context.reconnectTerminal(), /response lost/); const first = h.calls.find(call => call.url === '/api/agents/reconnect').body.request_key;
  const session = vm.runInContext('terminalSession', h.context); await h.context.pollTerminal(session);
  h.agents[0].connected = false; h.context.updateDisplayConnection(session, false); await h.context.reconnectTerminal();
  assert.notEqual(h.calls.filter(call => call.url === '/api/agents/reconnect')[1].body.request_key, first);
});

for (const outcome of ['classified', 'lost']) test(`late ${outcome} recovery for A is cached on A and cannot overwrite selected B notice`, async () => {
  let release, reject;
  const h = await harness({agents: [worker('remote', 'a', 'A offline', {connected: false}), worker('local', 'b', 'B online')], handle: async call => {
    if (call.url === '/api/agents/reconnect') return new Promise((resolve, fail) => { release = resolve; reject = fail; });
  }});
  await h.context.selectAgent(h.agents[0]); const recovering = h.context.reconnectTerminal().catch(error => error); await settle(); await h.context.selectAgent(h.agents[1]);
  if (outcome === 'classified') release({ok: true, status: 200, json: async () => ({...h.agents[0], recovery: {code: 'tmux_missing', message: 'A original tmux missing', next_action: 'create_new_terminal'}})});
  else reject(new Error('A transport outcome unknown'));
  await recovering;
  assert.equal(vm.runInContext('selectedAgent', h.context), 'local:b'); assert.equal(h.get('#agent-recovery-notice').hidden, true); assert.equal(h.get('#agent-recovery-notice').textContent, '');
  await h.context.selectAgent(h.agents[0]); assert.equal(h.get('#agent-recovery-notice').hidden, false); assert.match(h.get('#agent-recovery-notice').textContent, outcome === 'classified' ? /A original tmux/ : /A transport outcome unknown/);
});

test('inline rename uses backend-compatible 200-character limit without accepting oversized names', async () => {
  const h = await harness(); h.context.openAgentRename(h.agents[0]);
  const input = descendants(h.get('#agents')).find(item => item.tagName === 'input' && item.maxLength); assert.equal(input.maxLength, 200);
  vm.runInContext('renameTarget.value = "x".repeat(200)', h.context); await h.context.saveAgentRename(h.agents[0]); assert.equal(h.calls.find(call => call.url === '/api/agents/update').body.alias.length, 200);
  h.context.openAgentRename(h.agents[0]); vm.runInContext('renameTarget.value = "x".repeat(201)', h.context); await assert.rejects(h.context.saveAgentRename(h.agents[0]), /200/);
  assert.equal(h.calls.filter(call => call.url === '/api/agents/update').length, 1);
});
