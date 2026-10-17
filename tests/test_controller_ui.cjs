'use strict';
// The shipped manager UI is evaluated with inert DOM, PTY, clipboard and API
// stubs. These contracts never start Codex, access credentials, or touch SSH.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function element(tag = 'div') {
  return {tagName: tag, id: '', hidden: false, disabled: false, checked: false, value: '', textContent: '', children: [], childElementCount: 0, clientWidth: 900, dataset: {}, style: {}, listeners: {},
    classList: {add() {}, remove() {}, toggle() {}}, append(...items) { this.children.push(...items); this.childElementCount = this.children.length; }, replaceChildren(...items) { this.children = items; this.childElementCount = items.length; }, setAttribute() {},
    addEventListener(type, handler, options) { this.listeners[type] = {handler, options}; }, removeEventListener() {}, contains() { return false; }, showModal() { this.open = true; }, close() { this.open = false; this.listeners.close?.handler(); }, reset() {}, focus() { this.focused = true; }, select() {}, getBoundingClientRect: () => ({left: 0, top: 0, right: 900, bottom: 600})};
}
const ready = {id: 'manager-1', title: '我的管理会话', state: 'ready', phase: 'verified', session_id: '019abcdef-abcd-1234-abcd-019abcdefabc', workspace: 'C:/private/manager-1', terminal_id: 'manager-terminal-1', input_owner: 'hub', tools_ready: true, worker_access: 'automatic', effective_worker_count: 2, scope_count: 2, scope: ['local::worker-1', 'remote::inactive-worker'], allowed_hosts: {}, desktop_verified: false, connected: true, agent_ready: true, error: null};
async function harness(options = {}) {
  const elements = new Map(), calls = [], intervals = [], timers = new Map(), stored = options.stored || new Map(), clipboard = {writes: [], readCount: 0}; let requestNumber = 0, uuid = 0, manager = options.manager ? {...options.manager} : null;
  const get = selector => { if (!elements.has(selector)) { const e = element(); e.id = selector.slice(1); elements.set(selector, e); } return elements.get(selector); };
  get('#controller-panel').hidden = true;
  const document = {querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element, createTextNode: text => ({textContent: text}), addEventListener() {}};
  class FakeTerminal {
    constructor() { this.cols = 100; this.rows = 30; this.options = {}; this.selection = 'management output'; this.focusCount = 0; this.pastes = []; this.writes = []; this.fits = 0; this.buffer = {active: {viewportY: 15, baseY: 45}}; this.scrolls = []; this.parser = {registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {}}; }
    attachCustomKeyEventHandler(handler) { this.keyHandler = handler; } attachCustomWheelEventHandler(handler) { this.wheelHandler = handler; }
    hasSelection() { return !!this.selection; } getSelection() { return this.selection; } paste(text) { this.pastes.push(text); this.dataHandler?.(text); } clearSelection() { this.selection = ''; }
    open() {} loadAddon(addon) { addon.terminal = this; } focus() { this.focusCount++; } blur() {} dispose() { this.disposed = true; } reset() { this.resetCount = (this.resetCount || 0) + 1; }
    onData(handler) { this.dataHandler = handler; } onResize(handler) { this.resizeHandler = handler; } resize(cols, rows) { this.cols = cols; this.rows = rows; this.resizeHandler?.({cols, rows}); }
    write(data, callback) { this.writes.push(data); callback?.(); } scrollLines(amount) { this.scrolls.push(amount); }
  }
  const workers = [{host: 'local', thread_id: 'worker-1', mode: 'terminal', terminal_id: 'worker-1', alias: '业务工作', active: true, connected: true, cwd: 'C:/work'}, {host: 'remote', thread_id: 'inactive-worker', mode: 'terminal', active: false, cwd: '/work'}];
  const context = vm.createContext({console, document, URLSearchParams, structuredClone, crypto: {randomUUID: () => `operation-${++uuid}`}, navigator: {clipboard: {writeText: async text => { clipboard.writes.push(text); }, readText: async () => { clipboard.readCount++; return 'exact\nmanager text'; }}},
    window: {Terminal: FakeTerminal, FitAddon: {FitAddon: class {fit() { this.terminal.fits++; }}}, addEventListener() {}, confirm: () => true, sessionStorage: {getItem: key => stored.get(key), setItem: (key, value) => stored.set(key, value), removeItem: key => stored.delete(key)}},
    ResizeObserver: class {observe() {} disconnect() {}}, requestAnimationFrame: callback => callback(), setInterval: callback => { intervals.push(callback); return intervals.length; }, setTimeout: callback => { timers.set(++requestNumber, callback); return requestNumber; }, clearTimeout: id => timers.delete(id),
    fetch: async (url, opts = {}) => {
      const body = opts.body ? JSON.parse(opts.body) : undefined; const call = {url: String(url), options: opts, body}; calls.push(call);
      if (options.handle) { const custom = await options.handle(call, {get manager() { return manager; }, set manager(value) { manager = value; }}); if (custom) return custom; }
      const reply = data => ({ok: true, status: 200, json: async () => data});
      if (url.startsWith('/api/state')) return reply({hosts: [{id: 'local', label: '本机', transport: 'local', connected: true}, {id: 'remote', label: '远程', transport: 'ssh', connected: false}], agents: workers, events: [], outbox: []});
      if (url === '/api/controllers') return reply({controllers: manager ? [manager] : []});
      if (url.startsWith('/api/controllers/get')) return reply(manager);
      if (url === '/api/controllers/prepare') { manager = {...ready, title: body.title, state: 'prepared', phase: 'prepared', session_id: null, terminal_id: null, input_owner: 'none', tools_ready: false, allowed_hosts: body.host_roots}; return reply(manager); }
      if (url === '/api/controllers/start' || url === '/api/controllers/resume') { manager = {...ready, title: manager?.title || ready.title, scope: manager?.scope || ready.scope, allowed_hosts: manager?.allowed_hosts || {}, scope_count: manager?.scope?.length ?? 1}; return reply(manager); }
      if (url.startsWith('/api/controllers/output')) return reply({data: 'real manager PTY', ansi: 'real manager PTY', seq: 2, reset: true, cols: 100, rows: 30, connected: manager.connected, command: 'codex'});
      if (url === '/api/controllers/input' || url === '/api/controllers/resize') return reply({ok: true});
      if (url === '/api/controllers/scope') { manager = {...manager, allowed_hosts: body.host_roots}; return reply(manager); }
      if (url === '/api/controllers/desktop') { manager = {...manager, state: 'desktop_unverified', input_owner: 'desktop', connected: false}; return reply({...manager, url: `codex://threads/${manager.session_id}`}); }
      if (url === '/api/controllers/close') { manager = {...manager, state: 'closed', connected: false, input_owner: 'none'}; return reply(manager); }
      if (url === '/api/directories/list') return reply({path: body.path || 'C:/Users/example', parent: body.path ? 'C:/Users/example' : 'C:/Users', directories: [{name: 'project', path: 'C:/Users/example/project'}]});
      return reply({});
    }});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context);
  await settle();
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/controller.js'), 'utf8'), context);
  await settle();
  return {context, ui: context.window.PronController, get, calls, clipboard, stored, timers, intervals, workers};
}
async function settle() { for (let i = 0; i < 8; i++) await Promise.resolve(); }
const failedResponse = (status, message) => ({ok: false, status, json: async () => ({error: message})});

test('startup checks list only; no implicit prepare, history scan, tools or worker input', async () => {
  const {get, calls, ui} = await harness();
  assert.equal(get('#controller-primary').textContent, '启动管理会话'); assert.equal(get('#controller-primary').disabled, false);
  assert.equal(ui.model.controller, null);
  assert.deepEqual(calls.filter(c => c.url.startsWith('/api/controllers')).map(c => c.url), ['/api/controllers']);
  assert.equal(calls.some(c => /discover|history|input|\/start|\/resume|\/close/.test(c.url)), false);
});

test('Captain wording preserves existing manager identity, title, and saved operation namespace', async () => {
  const {get, ui} = await harness({manager: ready});
  assert.equal(get('#controller-owner').textContent, '当前入口：AgentCaptain');
  assert.equal(get('#controller-title').textContent, ready.title);
  assert.equal(ui.model.controller.session_id, ready.session_id);
  assert.match(get('#controller-description').textContent, /Captain.*派发任务.*跟进进度.*审查交付/);
  assert.match(get('#controller-description').textContent, /最终决策权/);
  const source = fs.readFileSync(path.join(__dirname, '../web/controller.js'), 'utf8');
  assert.match(source, /const storageKey = 'pron-hub\.management-operation\.v1'/);
  assert.match(source, /window\.PronController =/);
  assert.doesNotMatch(source, /PRON Hub|CEO/);
});

test('first settings automatically includes registered workers with no checklist or new-worker roots by default', async () => {
  const {ui, get, calls} = await harness(); ui.openSettings();
  assert.equal(get('#controller-name').value, '我的管理会话'); assert.match(get('#controller-worker-summary').textContent, /2.*自动.*以后连接/);
  assert.equal(Object.hasOwn(ui.model.form, 'selected'), false); assert.equal(Object.keys(ui.model.form.hostRoots).length, 0);
  assert.equal(calls.some(c => /discover|directories/.test(c.url)), false);
  await ui.submitSettings();
  assert.equal(Object.hasOwn(calls.find(c => c.url === '/api/controllers/prepare').body, 'agent_ids'), false);
  assert.deepEqual(calls.find(c => c.url === '/api/controllers/prepare').body.host_roots, {});
  assert.equal(get('#controller-state').textContent, '可对话');
});

test('repeat clicks create one manager and mount its real PTY separately from worker registry without focus', async () => {
  const {ui, context, calls, get} = await harness(); ui.openSettings();
  await Promise.all([ui.submitSettings(), ui.submitSettings()]);
  assert.equal(calls.filter(c => c.url === '/api/controllers/prepare').length, 1); assert.equal(calls.filter(c => c.url === '/api/controllers/start').length, 1);
  assert.equal(ui.model.terminal.kind, 'controller'); assert.equal(ui.model.terminal.term.writes[0], 'real manager PTY'); assert.equal(ui.model.terminal.term.focusCount, 0);
  assert.equal(vm.runInContext('state.agents.some(a => a.thread_id === "manager-1")', context), false); assert.equal(vm.runInContext('terminalSession', context), null);
  assert.equal(get('#controller-panel').hidden, false);
});

test('lost prepare response keeps operation body and keys across retry and reload', async () => {
  let lost = true;
  const h = await harness({handle: async call => { if (call.url === '/api/controllers/prepare' && lost) { lost = false; throw new Error('connection lost'); } }});
  h.ui.openSettings(); await assert.rejects(h.ui.submitSettings(), /connection lost/);
  const key = h.calls.find(c => c.url === '/api/controllers/prepare').body.request_key;
  h.ui.openSettings(); await h.ui.submitSettings();
  assert.equal(h.calls.filter(c => c.url === '/api/controllers/prepare')[1].body.request_key, key);
  const reload = await harness({stored: h.stored}); assert.equal(reload.ui.model.operation.requestKey, key);
  assert.equal(reload.calls.filter(c => c.url === '/api/controllers/prepare').length, 0);
});

test('uncertain legacy prepare keeps its original IDs and key as retry evidence, not an editable worker checklist', async () => {
  const body = {title: 'Legacy manager', agent_ids: ['local::worker-1'], host_roots: {}};
  const operation = {kind: 'prepare', body, fingerprint: JSON.stringify(body), requestKey: 'original-legacy-key', startKey: 'original-start-key'};
  const stored = new Map([['pron-hub.management-operation.v1', JSON.stringify(operation)]]);
  const h = await harness({stored}); h.ui.openSettings(); assert.equal(Object.hasOwn(h.ui.model.form, 'selected'), false);
  await h.ui.submitSettings();
  assert.deepEqual(h.calls.find(c => c.url === '/api/controllers/prepare').body, {...body, request_key: 'original-legacy-key'});
  assert.equal(h.calls.find(c => c.url === '/api/controllers/start').body.request_key, 'original-start-key');
});

test('manager reports automatic backend count rather than an old private grant subset', async () => {
  const h = await harness({manager: {...ready, scope: [], scope_count: 0, effective_worker_count: 7}});
  assert.equal(h.get('#controller-scope-count').textContent, '自动管理 7 个工作会话');
  assert.equal(h.calls.some(c => /activate|scope/.test(c.url)), false);
});

test('lost start response continues with same stored start key, never prepares another manager', async () => {
  let lost = true;
  const {ui, calls} = await harness({handle: async call => { if (call.url === '/api/controllers/start' && lost) { lost = false; throw new Error('lost start result'); } }});
  ui.openSettings(); await assert.rejects(ui.submitSettings(), /lost start/); const key = calls.find(c => c.url === '/api/controllers/start').body.request_key;
  await ui.primary();
  assert.equal(calls.filter(c => c.url === '/api/controllers/start')[1].body.request_key, key);
  assert.equal(calls.filter(c => c.url === '/api/controllers/prepare').length, 1);
});

test('prepared settings cannot be changed to bypass unknown operation result', async () => {
  const {ui, get, calls} = await harness({handle: async call => call.url === '/api/controllers/prepare' ? failedResponse(503, 'unknown result') : null});
  ui.openSettings(); await assert.rejects(ui.submitSettings(), /unknown result/); get('#controller-name').value = 'another manager';
  await assert.rejects(ui.submitSettings(), /此前启动请求/); assert.equal(calls.filter(c => c.url === '/api/controllers/prepare').length, 1);
});

test('connected PID and startup menu never imply ready management tools', async () => {
  const {ui, get} = await harness({manager: {...ready, state: 'needs_attention', phase: 'login_or_trust', tools_ready: false, agent_ready: false}});
  await ui.showTerminal();
  assert.equal(get('#controller-state').textContent, '等待你在终端确认'); assert.equal(get('#controller-desktop').disabled, true);
  assert.equal(ui.model.terminal.displayConnected, true); assert.match(get('#controller-panel-note').textContent, /不会自动审批/);
  ui.accept({...ready, tools_ready: false}); assert.equal(get('#controller-state').textContent, '身份或工具尚未核验');
});

test('unsupported old service is truthful and never starts another server or disrupts workers', async () => {
  const {ui, get, calls} = await harness({handle: async call => call.url === '/api/controllers' ? failedResponse(404, 'Unknown API') : null});
  assert.equal(ui.model.unsupported, true); assert.equal(get('#controller-primary').disabled, true); assert.match(get('#controller-state').textContent, /尚未加载/);
  await ui.primary(); assert.equal(calls.filter(c => c.options.method === 'POST').length, 0);
});

test('controller list exact unauthorized 404 asks to refresh authentication without claiming unsupported API or discarding operation', async () => {
  const operation = {kind: 'resume', id: ready.id, requestKey: 'saved-original-controller-key', acknowledged: false};
  const serialized = JSON.stringify(operation), stored = new Map([['pron-hub.management-operation.v1', serialized]]);
  const {ui, get, calls} = await harness({stored, manager: ready, handle: async call => call.url === '/api/controllers' ? failedResponse(404, 'Not found or unauthorized') : null});
  assert.equal(ui.model.unsupported, false);
  assert.match(ui.model.error, /认证.*失效|失效.*认证/); assert.match(ui.model.error, /刷新/);
  assert.doesNotMatch(ui.model.error, /未提供管理会话 API/);
  assert.doesNotMatch(get('#controller-state').textContent, /尚未加载管理会话/);
  assert.equal(ui.model.controller, null);
  assert.deepEqual(JSON.parse(JSON.stringify(ui.model.operation)), operation);
  assert.equal(stored.get('pron-hub.management-operation.v1'), serialized);
  await ui.refresh();
  assert.equal(ui.model.unsupported, false);
  assert.equal(stored.get('pron-hub.management-operation.v1'), serialized);
  assert.equal(calls.filter(call => call.options.method === 'POST').length, 0);
  assert.equal(calls.some(call => /\/prepare|\/start|\/resume|\/input|\/close/.test(call.url)), false);
});

test('controller get exact unauthorized 404 preserves original mounted manager Session, draft, and operation while asking for refresh', async () => {
  let authExpired = false;
  const operation = {kind: 'resume', id: ready.id, requestKey: 'saved-original-get-key', acknowledged: true};
  const serialized = JSON.stringify(operation), stored = new Map([['pron-hub.management-operation.v1', serialized]]);
  const h = await harness({manager: ready, stored, handle: async call => authExpired && call.url.startsWith('/api/controllers/get?') ? failedResponse(404, 'Not found or unauthorized') : null});
  await h.ui.showTerminal();
  const originalController = h.ui.model.controller, originalTerminal = h.ui.model.terminal, originalOperation = h.ui.model.operation;
  originalTerminal.input = 'retained unsent manager draft';
  const originalFocus = originalTerminal.term.focusCount, originalCalls = h.calls.length;
  authExpired = true;
  await h.ui.refresh();
  assert.equal(h.ui.model.unsupported, false);
  assert.match(h.ui.model.error, /认证.*失效|失效.*认证/); assert.match(h.ui.model.error, /刷新/);
  assert.doesNotMatch(h.ui.model.error, /未提供管理会话 API/);
  assert.equal(h.ui.model.controller, originalController); assert.equal(h.ui.model.controller.session_id, ready.session_id);
  assert.equal(h.ui.model.terminal, originalTerminal); assert.equal(originalTerminal.input, 'retained unsent manager draft');
  assert.equal(originalTerminal.disposed, false); assert.equal(originalTerminal.term.focusCount, originalFocus);
  assert.equal(h.ui.model.operation, originalOperation); assert.equal(stored.get('pron-hub.management-operation.v1'), serialized);
  const reads = h.calls.slice(originalCalls);
  assert.equal(reads.length, 1); assert.equal(reads[0].url, '/api/controllers/get?id=' + ready.id);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
});

for (const pathKind of ['list', 'get']) test(`actual missing manager ${pathKind} 404 remains unsupported, rather than claiming expired authentication`, async () => {
  let missing = pathKind === 'list';
  const matches = call => pathKind === 'list' ? call.url === '/api/controllers' : call.url.startsWith('/api/controllers/get?');
  const h = await harness({manager: ready, handle: async call => missing && matches(call) ? failedResponse(404, 'manager not found') : null});
  const original = h.ui.model.controller;
  if (pathKind === 'get') { missing = true; await h.ui.refresh(); }
  assert.equal(h.ui.model.unsupported, true);
  assert.match(h.ui.model.error, /未提供管理会话 API/); assert.doesNotMatch(h.ui.model.error, /认证.*失效|失效.*认证/);
  assert.equal(h.ui.model.controller, original);
  assert.equal(h.get('#controller-primary').disabled, true);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
});

for (const pathKind of ['list', 'get']) test(`controller ${pathKind} domain 403 preserves owner permission message instead of authentication or unsupported claims`, async () => {
  let denied = pathKind === 'list';
  const operation = {kind: 'resume', id: ready.id, requestKey: 'saved-domain-permission-key', acknowledged: true};
  const serialized = JSON.stringify(operation), stored = new Map([['pron-hub.management-operation.v1', serialized]]);
  const matches = call => pathKind === 'list' ? call.url === '/api/controllers' : call.url.startsWith('/api/controllers/get?');
  const h = await harness({manager: ready, stored, handle: async call => denied && matches(call) ? failedResponse(403, 'owner permission denied') : null});
  const originalController = h.ui.model.controller, originalOperation = h.ui.model.operation;
  if (pathKind === 'get') { denied = true; await h.ui.refresh(); }
  assert.equal(h.ui.model.unsupported, false); assert.match(h.ui.model.error, /owner permission denied/);
  assert.doesNotMatch(h.ui.model.error, /认证.*失效|失效.*认证|未提供管理会话 API/);
  assert.equal(h.ui.model.controller, originalController); assert.equal(h.ui.model.operation, originalOperation);
  assert.equal(stored.get('pron-hub.management-operation.v1'), serialized);
  assert.equal(h.calls.filter(call => call.options.method === 'POST').length, 0);
});

test('management settings only changes create-project roots, not ordinary existing-worker access', async () => {
  const {ui, calls, get} = await harness({manager: ready}); ui.openSettings(true); await ui.submitSettings();
  assert.deepEqual(calls.find(c => c.url === '/api/controllers/scope').body, {id: 'manager-1', host_roots: {}});
  assert.equal(calls.some(c => /prepare|\/start|\/resume|discover|history/.test(c.url)), false); assert.equal(get('#controller-scope-count').textContent, '自动管理 2 个工作会话');
  assert.equal(get('#controller-dialog-heading').textContent, '管理设置');
});

test('manager wheel uses local display only and shares clipboard protections without worker writes', async () => {
  const {ui, context, get, calls, clipboard} = await harness({manager: ready}); await ui.showTerminal(); const session = ui.model.terminal;
  const event = {deltaY: 3, deltaMode: 1, stopImmediatePropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; }};
  get('#controller-terminal').listeners.wheel.handler(event); assert.equal(event.stopped, true); assert.deepEqual(session.term.scrolls, [3]);
  await context.copyTerminal(session); assert.deepEqual(clipboard.writes, ['management output']); await context.pasteTerminal(session);
  assert.deepEqual(session.term.pastes, ['exact\nmanager text']); context.flushTerminalInput(session); await session.inputChain;
  assert.equal(calls.find(c => c.url === '/api/controllers/input').body.id, 'manager-1'); assert.equal(calls.some(c => c.url === '/api/terminals/input'), false);
});

test('hiding display does not close manager, flush draft, or resize hidden terminal', async () => {
  const {ui, get, calls, context} = await harness({manager: ready}); await ui.showTerminal(); const session = ui.model.terminal;
  session.input = 'draft'; const fits = session.term.fits; get('#controller-hide').onclick(); context.safeFit(session); context.scheduleResize(session, 80, 24);
  assert.equal(session.input, 'draft'); assert.equal(session.disposed, false); assert.equal(session.term.fits, fits); assert.equal(context.canWriteTerminal(session), false);
  assert.equal(calls.some(c => c.url.endsWith('/close') || c.url.endsWith('/input')), false);
});

test('desktop action needs human confirmation and exposes only exact same saved thread URL', async () => {
  const {ui, get, calls, context} = await harness({manager: ready}); await ui.showTerminal();
  await assert.rejects(ui.submitDesktop(), /请先确认/); assert.equal(calls.some(c => c.url.endsWith('/desktop')), false);
  get('#controller-desktop-confirm').checked = true; await ui.submitDesktop();
  assert.deepEqual(calls.find(c => c.url.endsWith('/desktop')).body, {id: 'manager-1', confirm: true});
  assert.equal(get('#controller-desktop-link').href, `codex://threads/${ready.session_id}`); assert.equal(get('#controller-desktop-link').hidden, false);
  assert.equal(get('#controller-state').textContent, '桌面入口已打开 · 接管未核验'); assert.equal(context.canWriteTerminal(ui.model.terminal), false);
  assert.match(get('#controller-desktop-error').textContent, /尚未核验/); assert.equal(calls.some(c => c.url === '/api/terminals/input'), false);
});

test('desktop blocked by unsent terminal draft and mismatched link is never opened', async () => {
  const {ui, get, calls} = await harness({manager: ready, handle: async call => call.url.endsWith('/desktop') ? {ok: true, status: 200, json: async () => ({...ready, url: 'codex://threads/different'})} : null});
  await ui.showTerminal(); get('#controller-desktop-confirm').checked = true; ui.model.terminal.input = 'draft';
  await assert.rejects(ui.submitDesktop(), /待送达/); assert.equal(calls.some(c => c.url.endsWith('/desktop')), false);
  ui.model.terminal.input = ''; await assert.rejects(ui.submitDesktop(), /准确原会话链接/); assert.notEqual(get('#controller-desktop-link').href, 'codex://threads/different');
});

test('return from desktop resumes exact manager and original Session, never new creation or init text', async () => {
  const {ui, calls} = await harness({manager: {...ready, state: 'desktop_unverified', input_owner: 'desktop', connected: false}});
  await ui.primary(); const body = calls.find(c => c.url === '/api/controllers/resume').body;
  assert.equal(body.id, 'manager-1'); assert.ok(body.request_key); assert.equal(ui.model.controller.session_id, ready.session_id);
  assert.equal(calls.some(c => /\/prepare|\/start|\/input/.test(c.url)), false);
});

test('explicit manager close discards draft, awaits submitted input, and never closes worker', async () => {
  const {ui, calls} = await harness({manager: ready}); await ui.showTerminal(); const session = ui.model.terminal; session.input = 'unsent draft';
  let resolveInput; session.inputChain = new Promise(resolve => { resolveInput = resolve; }); const closing = ui.closeManager(); await settle();
  assert.equal(session.input, ''); assert.equal(calls.some(c => c.url.endsWith('/close')), false); resolveInput(); await closing;
  assert.deepEqual(calls.find(c => c.url.endsWith('/close')).body, {id: 'manager-1', confirm: true}); assert.equal(calls.some(c => c.url.startsWith('/api/terminals/close')), false);
});

test('markup loads isolated manager module without path, Python, MCP or Session form fields', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /src="\/controller.js"/); assert.match(html, /id="controller-terminal"/); assert.match(html, /id="controller-desktop-confirm"/);
  assert.doesNotMatch(html, /id="controller-(?:python|mcp|session|cwd)"/);
});

test('asynchronous initialization mounts the exact manager terminal when it arrives, without focus', async () => {
  const h = await harness({manager: {...ready, state: 'bootstrapping', phase: 'codex', terminal_id: null, session_id: null, tools_ready: false}, handle: async call => call.url.startsWith('/api/controllers/get') ? {ok: true, status: 200, json: async () => ready} : null});
  await h.ui.showTerminal(); assert.equal(h.ui.model.terminal, null); assert.equal(h.get('#controller-panel').hidden, false);
  await h.ui.refresh(); assert.equal(h.ui.model.terminal.terminalId, ready.terminal_id); assert.equal(h.ui.model.terminal.term.focusCount, 0);
});

test('desktop ownership change invalidates an in-flight old PTY frame and stops polling', async () => {
  let reads = 0, resolveOutput;
  const {ui, context, get} = await harness({manager: ready, handle: async call => { if (call.url.startsWith('/api/controllers/output') && ++reads === 2) return new Promise(resolve => { resolveOutput = resolve; }); }});
  await ui.showTerminal(); const session = ui.model.terminal; const pending = context.pollTerminal(session); await settle();
  get('#controller-desktop-confirm').checked = true; await ui.submitDesktop();
  resolveOutput({ok: true, status: 200, json: async () => ({data: 'stale ownership output', seq: 3, reset: true, cols: 80, rows: 24, connected: true})}); await pending;
  assert.equal(session.term.writes.includes('stale ownership output'), false); assert.equal(session.timer, null);
  const before = reads; await context.pollTerminal(session); assert.equal(reads, before);
});

test('delayed clipboard read cannot paste after manager display is hidden', async () => {
  const {ui, context, get} = await harness({manager: ready}); await ui.showTerminal(); let resolveRead;
  context.navigator.clipboard.readText = () => new Promise(resolve => { resolveRead = resolve; }); const paste = context.pasteTerminal(ui.model.terminal);
  get('#controller-hide').onclick(); resolveRead('cannot enter hidden manager'); await paste;
  assert.deepEqual(ui.model.terminal.term.pastes, []);
});

test('explicit project-directory grant browses selected host only and never grants entire connected host implicitly', async () => {
  const {ui, get, calls} = await harness(); ui.openSettings();
  const row = get('#controller-hosts').children[0], browse = row.children.find(child => child.tagName === 'button'); await browse.onclick(); await settle();
  assert.deepEqual(calls.find(call => call.url === '/api/directories/list').body, {host: 'local', path: ''});
  get('#controller-host-directory-select').onclick(); await ui.submitSettings();
  assert.deepEqual(calls.find(call => call.url === '/api/controllers/prepare').body.host_roots, {local: ['C:/Users/example']});
});

test('periodic management state refresh never replaces worker terminal or steals worker selection and draft', async () => {
  const {ui, context} = await harness({manager: ready});
  vm.runInContext('selectedAgent = "local:worker-1"; terminalSession = {key: selectedAgent, input: "worker draft", disposed:false, agent:state.agents[0], displayConnected:true};', context);
  const worker = vm.runInContext('terminalSession', context); await ui.refresh(); await ui.showTerminal(); await ui.refresh();
  assert.equal(vm.runInContext('terminalSession', context), worker); assert.equal(worker.input, 'worker draft'); assert.equal(vm.runInContext('selectedAgent', context), 'local:worker-1'); assert.equal(ui.model.terminal.term.focusCount, 0);
});

test('close -> resume -> close -> resume allocates a fresh key only after the previous resume was acknowledged', async () => {
  const accepted = new Set(); let attachments = 0;
  const {ui, calls} = await harness({manager: ready, handle: async (call, backend) => {
    if (call.url !== '/api/controllers/resume') return null;
    if (!accepted.has(call.body.request_key)) { accepted.add(call.body.request_key); attachments++; backend.manager = {...ready}; }
    return {ok: true, status: 200, json: async () => backend.manager};
  }});
  await ui.showTerminal(); await ui.closeManager(); await ui.primary(); await ui.closeManager(); await ui.primary();
  const resumes = calls.filter(call => call.url === '/api/controllers/resume');
  assert.equal(resumes.length, 2); assert.notEqual(resumes[0].body.request_key, resumes[1].body.request_key); assert.equal(attachments, 2);
  assert.equal(ui.model.controller.state, 'ready'); assert.equal(ui.model.controller.session_id, ready.session_id);
  assert.equal(calls.some(call => call.url.endsWith('/start') || call.url.endsWith('/prepare')), false);
});

test('lost resume response retains the same key until known acknowledgement, then the next lifecycle gets a new key', async () => {
  let lost = true;
  const {ui, calls, stored} = await harness({manager: {...ready, state: 'closed', connected: false, input_owner: 'none'}, handle: async call => {
    if (call.url === '/api/controllers/resume' && lost) { lost = false; throw new Error('lost resume response'); }
  }});
  await assert.rejects(ui.primary(), /lost resume response/); const first = calls.find(call => call.url === '/api/controllers/resume').body.request_key;
  assert.equal(ui.model.operation.acknowledged, undefined); assert.equal(JSON.parse(stored.get('pron-hub.management-operation.v1')).requestKey, first);
  await ui.primary(); assert.equal(calls.filter(call => call.url === '/api/controllers/resume')[1].body.request_key, first); assert.equal(ui.model.operation.acknowledged, true);
  await ui.closeManager(); await ui.primary(); assert.notEqual(calls.filter(call => call.url === '/api/controllers/resume')[2].body.request_key, first);
});

test('verified ready state reconciles a lost successful resume response before a later close/resume lifecycle', async () => {
  let lost = true;
  const {ui, calls} = await harness({manager: {...ready, state: 'closed', connected: false, input_owner: 'none'}, handle: async (call, backend) => {
    if (call.url === '/api/controllers/resume' && lost) { lost = false; backend.manager = {...ready}; throw new Error('response lost after attach'); }
  }});
  await assert.rejects(ui.primary(), /response lost/); const first = ui.model.operation.requestKey; assert.equal(ui.model.operation.acknowledged, undefined);
  await ui.refresh(); assert.equal(ui.model.operation.acknowledged, true); assert.equal(ui.model.controller.session_id, ready.session_id);
  await ui.closeManager(); await ui.primary(); assert.notEqual(calls.filter(call => call.url === '/api/controllers/resume')[1].body.request_key, first);
});

test('unknown resume result keeps its operation key and refuses a blind force-retry even with a known Session', async () => {
  const {ui, calls} = await harness({manager: {...ready, state: 'closed', connected: false, input_owner: 'none'}, handle: async (call, backend) => {
    if (call.url === '/api/controllers/resume') { backend.manager = {...ready, state: 'unknown', connected: false, input_owner: 'none', error: {code: 'terminal_unknown'}}; return {ok: true, status: 200, json: async () => backend.manager}; }
  }});
  await ui.primary(); const key = ui.model.operation.requestKey; assert.equal(ui.model.operation.acknowledged, undefined);
  await assert.rejects(ui.primary(), /结果未知/); assert.equal(calls.filter(call => call.url === '/api/controllers/resume').length, 1); assert.equal(ui.model.operation.requestKey, key);
});

test('acknowledged codex_unavailable is a known unlaunched failure and allows an explicit fresh-key retry', async () => {
  let unavailable = true;
  const {ui, calls} = await harness({handle: async (call, backend) => {
    if (call.url === '/api/controllers/start' && unavailable) { unavailable = false; backend.manager = {...backend.manager, state: 'failed', phase: 'environment', error: {code: 'codex_unavailable'}}; return {ok: true, status: 200, json: async () => backend.manager}; }
  }});
  ui.openSettings(); await ui.submitSettings(); assert.equal(ui.model.operation.startAcknowledged, true);
  const key = calls.find(call => call.url === '/api/controllers/start').body.request_key;
  await ui.primary(); const starts = calls.filter(call => call.url === '/api/controllers/start');
  assert.equal(starts.length, 2); assert.notEqual(starts[1].body.request_key, key); assert.equal(ui.model.controller.state, 'ready');
  assert.equal(calls.filter(call => call.url === '/api/controllers/prepare').length, 1);
});

test('native identity retained after failed bootstrap without a PTY resumes original Session, not another initialization', async () => {
  const {ui, calls} = await harness({manager: {...ready, state: 'failed', phase: 'bootstrap', terminal_id: null, connected: false, tools_ready: false, input_owner: 'none', error: {code: 'bootstrap_failed'}}});
  await ui.primary(); assert.equal(calls.filter(call => call.url === '/api/controllers/resume').length, 1); assert.equal(ui.model.controller.session_id, ready.session_id);
  assert.equal(calls.some(call => call.url.endsWith('/start') || call.url.endsWith('/prepare')), false);
});

test('failed bootstrap without exact native identity cannot be retried as a fresh start', async () => {
  const {ui, calls} = await harness({manager: {...ready, state: 'failed', phase: 'bootstrap', terminal_id: null, session_id: null, connected: false, tools_ready: false, input_owner: 'none', error: {code: 'bootstrap_failed'}}});
  await assert.rejects(ui.primary(), /启动结果尚未核对/); assert.equal(calls.some(call => call.url.endsWith('/start') || call.url.endsWith('/resume') || call.url.endsWith('/prepare')), false);
});
