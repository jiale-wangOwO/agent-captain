'use strict';
// Run the actual shipped UI in an inert VM. All requests and browser storage
// below are synthetic: no browser, SSH, PTY, worker, or service is contacted.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const pendingKey = 'pron-hub.pending-terminal-create.v1';
const sourcePath = path.join(__dirname, '../web/ui.js');

function element(tag = 'div') {
  return {
    tagName: tag, value: '', textContent: '', hidden: false, disabled: false,
    checked: false, readOnly: false, open: false, clientWidth: 900, children: [],
    childElementCount: 0, style: {}, dataset: {}, listeners: new Map(),
    classList: {add() {}, remove() {}, toggle() {}},
    append(...items) { this.children.push(...items); this.childElementCount = this.children.length; },
    replaceChildren(...items) { this.children = items; this.childElementCount = items.length; },
    setAttribute() {}, removeAttribute() {}, contains() { return false; },
    addEventListener(type, handler) { const handlers = this.listeners.get(type) || []; handlers.push(handler); this.listeners.set(type, handlers); },
    removeEventListener() {}, focus() {}, select() {}, reset() {},
    showModal() { this.open = true; },
    close() { this.open = false; for (const handler of this.listeners.get('close') || []) handler({target: this}); },
    getBoundingClientRect: () => ({left: 0, top: 0, right: 900, bottom: 600}),
  };
}

async function settle() { await new Promise(resolve => setImmediate(resolve)); }
const response = (value, status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => value});
const malformedResponse = (status = 200) => ({ok: status >= 200 && status < 300, status, json: async () => { throw new SyntaxError('synthetic non-JSON response'); }});

async function harness(options = {}) {
  const elements = new Map(), calls = [], storage = options.storage || new Map();
  const localStorage = new Map();
  const hosts = [{id: 'local', label: 'Local', transport: 'local', connected: true}, {id: 'spark', label: 'Spark', transport: 'ssh', connected: true}, {id: '316', label: '316', transport: 'ssh', connected: true}];
  const agents = [];
  let uuid = 0;
  const get = selector => { if (!elements.has(selector)) { const item = element(); item.id = selector.slice(1); elements.set(selector, item); } return elements.get(selector); };
  class FakeTerminal {
    constructor() { this.cols = 120; this.rows = 32; this.options = {}; this.parser = {registerCsiHandler() {}, registerDcsHandler() {}, registerOscHandler() {}}; }
    open() {} loadAddon() {} focus() {} blur() {} reset() {} dispose() {}
    attachCustomKeyEventHandler() {} attachCustomWheelEventHandler() {}
    onData() {} onResize() {} write(_data, callback) { callback?.(); }
    hasSelection() { return false; } getSelection() { return ''; }
  }
  const sessionStorage = options.sessionStorage || {
    getItem: key => storage.get(key) ?? null,
    setItem: (key, value) => { storage.set(key, value); },
    removeItem: key => { storage.delete(key); },
  };
  const fixture = {get, calls, storage, hosts, agents, handle: null, pendingAtPost: []};
  const context = vm.createContext({
    console, URLSearchParams, crypto: {randomUUID: () => `synthetic-create-key-${options.generation || 1}-${++uuid}`},
    navigator: {clipboard: {writeText: async () => {}, readText: async () => ''}},
    document: {querySelector: get, querySelectorAll: () => [], getElementById: id => get('#' + id), createElement: element, createTextNode: text => ({textContent: text}), addEventListener() {}},
    window: {
      Terminal: FakeTerminal, FitAddon: {FitAddon: class {fit() {}}},
      addEventListener() {}, location: {origin: 'http://inert-fixture', pathname: '/', search: '', hash: ''}, history: {replaceState() {}},
      localStorage: {getItem: key => localStorage.get(key), setItem: (key, value) => localStorage.set(key, value)}, sessionStorage,
    },
    sessionStorage,
    ResizeObserver: class {observe() {} disconnect() {}},
    requestAnimationFrame: callback => callback(),
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    fetch: async (url, opts = {}) => {
      const call = {url: String(url), options: opts, body: opts.body ? JSON.parse(opts.body) : undefined};
      calls.push(call);
      if (call.url === '/api/terminals/create') fixture.pendingAtPost.push(storage.get(pendingKey));
      if (fixture.handle) { const result = await fixture.handle(call); if (result !== undefined) return result; }
      if (call.url.startsWith('/api/state')) return response({hosts, agents, outbox: [], events: []});
      if (call.url.startsWith('/api/terminals/output')) return response({data: '', seq: 1, connected: true, cols: 120, rows: 32});
      return response({});
    },
  });
  fixture.context = context;
  vm.runInContext(fs.readFileSync(sourcePath, 'utf8'), context, {filename: 'ui.js'});
  await settle();
  return fixture;
}

const creates = h => h.calls.filter(call => call.url === '/api/terminals/create');
const pending = h => { const value = h.storage.get(pendingKey); return value ? JSON.parse(value) : null; };
const clone = value => JSON.parse(JSON.stringify(value));
function open(h, host = 'spark') { h.context.openCreate(null, host); return h; }
function configure(h) { h.get('#create-cwd').value = '/home/example/project'; h.get('#create-title').value = 'Stable synthetic worker'; }
async function submit(h) {
  assert.equal(typeof h.context.submitCreateTerminal, 'function', 'the UI exposes its actual asynchronous create submission');
  try { return {result: await h.context.submitCreateTerminal()}; } catch (error) { return {error}; }
}
function registerCreated(h, id = 'synthetic-terminal') {
  const agent = {host: 'spark', thread_id: id, terminal_id: id, mode: 'terminal', cwd: '/home/example/project', alias: 'Stable synthetic worker', connected: true, status: 'shell'};
  h.agents.push(agent); return agent;
}

test('fetch rejection is a classified local Hub outage, without automatic retry', async () => {
  const h = await harness();
  h.handle = async call => { if (call.url === '/synthetic/request') throw new TypeError('Failed to fetch'); };
  await assert.rejects(h.context.request('/synthetic/request', {host: '950'}), error => error.code === 'hub_unreachable' && !/密码|password|950.*SSH/i.test(error.message));
  assert.equal(h.calls.filter(call => call.url === '/synthetic/request').length, 1);
});

for (const [url, status, error] of [
  ['/synthetic/request', 403, 'Invalid session or origin'],
  ['/api/state', 404, 'Not found or unauthorized'],
  ['/api/state?cursor=7', 404, 'Not found or unauthorized'],
]) test(`only known session failure is classified for ${url}`, async () => {
  const h = await harness(); h.handle = async call => call.url === url ? response({error}, status) : undefined;
  const before = h.calls.length;
  await assert.rejects(h.context.request(url, url.startsWith('/api/state') ? undefined : {host: 'spark'}), result => result.code === 'ui_session_expired' && result.status === status);
  assert.equal(h.calls.length, before + 1);
});

test('domain HTTP 403 preserves domain permission message rather than claiming expired authentication', async () => {
  const h = await harness();
  h.handle = async call => call.url === '/synthetic/denied' ? response({error: 'synthetic directory permission denied', code: 'directory_forbidden'}, 403) : undefined;
  await assert.rejects(h.context.request('/synthetic/denied', {host: 'spark'}), error => error.status === 403 && error.code === 'directory_forbidden' && error.message === 'synthetic directory permission denied');
  assert.equal(h.calls.filter(call => call.url === '/synthetic/denied').length, 1);
});

test('the state unauthorized phrase on another route is not silently promoted to session-expired', async () => {
  const h = await harness();
  h.handle = async call => call.url === '/api/other' ? response({error: 'Not found or unauthorized'}, 404) : undefined;
  await assert.rejects(h.context.request('/api/other'), error => error.code !== 'ui_session_expired' && error.status === 404);
});

for (const route of ['/api/state', '/api/terminal-service/status', '/api/terminals/output', '/api/terminals/history', '/api/controllers', '/api/controllers/get', '/api/controllers/output']) {
  for (const query of ['', '?host=spark&thread_id=original&id=original&after=7']) test(`protected GET ${route}${query} classifies only its fixed unauthorized response without retry`, async () => {
    const h = await harness(), url = route + query;
    h.handle = async call => call.url === url ? response({error: 'Not found or unauthorized'}, 404) : undefined;
    const before = h.calls.length;
    await assert.rejects(h.context.request(url), error => error.code === 'ui_session_expired' && error.status === 404 && /刷新页面/.test(error.message));
    assert.equal(h.calls.length, before + 1);
    assert.equal(h.calls.at(-1).options.method, undefined);
  });
}

for (const url of ['/api/terminals/output/missing', '/api/terminals/history/', '/api/controllers/missing', '/api/controllers/getting', 'https://other.invalid/api/state']) test(`unlisted GET ${url} does not promote a fixed 404 phrase to session expiry`, async () => {
  const h = await harness(); h.handle = async call => call.url === url ? response({error: 'Not found or unauthorized'}, 404) : undefined;
  await assert.rejects(h.context.request(url), error => error.code !== 'ui_session_expired' && error.status === 404 && error.message === 'Not found or unauthorized');
});

for (const route of ['/api/state', '/api/terminals/output', '/api/terminals/history', '/api/controllers/get']) test(`POST ${route} cannot be mistaken for its protected GET authentication failure`, async () => {
  const h = await harness(); h.handle = async call => call.url === route ? response({error: 'Not found or unauthorized'}, 404) : undefined;
  await assert.rejects(h.context.request(route, {id: 'original'}), error => error.code !== 'ui_session_expired' && error.status === 404);
  assert.equal(h.calls.filter(call => call.url === route && call.options.method === 'POST').length, 1);
});

for (const [url, status, data] of [
  ['/api/terminals/output?host=spark&thread_id=missing', 404, {error: 'Terminal not found', code: 'terminal_not_found'}],
  ['/api/controllers/get?id=missing', 404, {error: {message: 'Manager not found', code: 'controller_not_found'}}],
  ['/api/terminals/history?host=spark&thread_id=original', 403, {error: 'Terminal history permission denied', code: 'history_forbidden'}],
  ['/api/controllers/get?id=original', 403, {error: 'Manager owner permission denied', code: 'controller_forbidden'}],
]) test(`protected GET ${url} preserves resource/domain error ${status} instead of expired authentication`, async () => {
  const h = await harness(); h.handle = async call => call.url === url ? response(data, status) : undefined;
  const before = h.calls.length;
  await assert.rejects(h.context.request(url), error => error.status === status && error.code === (data.code || data.error.code) && error.message === (typeof data.error === 'string' ? data.error : data.error.message));
  assert.equal(h.calls.length, before + 1);
});

test('worker output polling reports expired page authentication without suggesting reconnect or changing the original display', async () => {
  const h = await harness(), agent = registerCreated(h, 'original-spark-display');
  await h.context.selectAgent(agent);
  const session = vm.runInContext('terminalSession', h.context);
  session.input = 'unsent human draft';
  h.handle = async call => call.url.startsWith('/api/terminals/output?') ? response({error: 'Not found or unauthorized'}, 404) : undefined;
  const before = h.calls.length;
  await h.context.pollTerminal(session);
  assert.match(session.pollError, /页面认证已失效.*刷新页面/);
  assert.doesNotMatch(session.pollError, /可尝试「重新连接」|密码|password/i);
  assert.equal(session.input, 'unsent human draft');
  assert.equal(vm.runInContext('terminalSession', h.context), session);
  assert.equal(h.calls.length, before + 1);
  assert.equal(h.calls.at(-1).options.method, undefined);
  assert.equal(h.calls.some(call => /\/input|\/reconnect|\/connect|\/resume|\/create/.test(call.url)), false);
});

for (const value of ['non-JSON', null, [], 'synthetic string']) test(`successful ${JSON.stringify(value)} response is protocol failure, never implicit success or retry`, async () => {
  const h = await harness(); h.handle = async call => call.url === '/synthetic/protocol' ? value === 'non-JSON' ? malformedResponse() : response(value) : undefined;
  await assert.rejects(h.context.request('/synthetic/protocol', {host: 'spark'}), error => error.code === 'hub_response_invalid');
  assert.equal(h.calls.filter(call => call.url === '/synthetic/protocol').length, 1);
});

for (const failure of ['network', 'session', 'protocol']) test(`create ${failure} failure saves exact immutable key/body before POST and has no automatic repeat`, async () => {
  const h = open(await harness()); configure(h);
  h.handle = async call => {
    if (call.url !== '/api/terminals/create') return;
    if (failure === 'network') throw new TypeError('Failed to fetch');
    return failure === 'session' ? response({error: 'Invalid session or origin'}, 403) : malformedResponse();
  };
  await submit(h); await settle();
  assert.equal(creates(h).length, 1);
  const body = creates(h)[0].body, record = pending(h);
  assert.ok(record, 'pending create survives response failure');
  assert.equal(record.version, 1); assert.deepEqual(record.body, body);
  assert.equal(body.host, 'spark'); assert.equal(body.cwd, '/home/example/project'); assert.equal(body.title, 'Stable synthetic worker'); assert.ok(body.request_key);
  assert.deepEqual(JSON.parse(h.pendingAtPost[0]).body, body, 'storage is committed before network effects');
  await h.context.refresh(true); await settle();
  assert.equal(creates(h).length, 1, 'refresh and settling do not retry creating');
});

test('page reload restores pending request with original host/key/body and never POSTs until explicit submission', async () => {
  const h = open(await harness()); configure(h); h.handle = async call => { if (call.url === '/api/terminals/create') throw new TypeError('response lost'); };
  await submit(h); const original = clone(creates(h)[0].body);
  const reloaded = await harness({storage: h.storage, generation: 2}); await settle();
  assert.equal(creates(reloaded).length, 0);
  reloaded.context.openCreate(null, '316');
  assert.equal(creates(reloaded).length, 0);
  assert.equal(reloaded.get('#create-cwd').value, original.cwd); assert.equal(reloaded.get('#create-title').value, original.title);
  assert.equal(vm.runInContext('createTarget.host', reloaded.context), original.host);
  reloaded.handle = async call => { if (call.url === '/api/terminals/create') throw new TypeError('still unknown'); };
  await submit(reloaded);
  assert.equal(creates(reloaded).length, 1); assert.deepEqual(creates(reloaded)[0].body, original);
});

test('closing and reopening, even on another machine, cannot discard or bypass the unresolved create key', async () => {
  const h = open(await harness()); configure(h); h.handle = async call => { if (call.url === '/api/terminals/create') throw new TypeError('response lost'); };
  await submit(h); const original = clone(creates(h)[0].body);
  h.get('#create-dialog').close();
  assert.deepEqual(pending(h).body, original);
  h.context.openCreate(null, '316'); await settle();
  assert.equal(creates(h).length, 1, 'opening is read-only');
  assert.equal(vm.runInContext('createTarget.host', h.context), original.host);
  await submit(h);
  assert.equal(creates(h).length, 2); assert.deepEqual(creates(h)[1].body, original);
});

test('pending request locks editable configuration and programmatic field changes fail closed', async () => {
  const h = open(await harness()); configure(h); h.handle = async call => { if (call.url === '/api/terminals/create') throw new TypeError('response lost'); };
  await submit(h); const original = clone(pending(h).body);
  assert.equal(h.get('#create-cwd').disabled || h.get('#create-cwd').readOnly, true);
  assert.equal(h.get('#create-title').disabled || h.get('#create-title').readOnly, true);
  assert.equal(h.get('#browse-directory').disabled, true);
  h.get('#create-cwd').value = '/must-not-create-a-different-project';
  h.get('#create-title').value = 'must not mutate pending body';
  await submit(h);
  assert.equal(creates(h).length, 1, 'a changed draft cannot be sent with old or new key');
  assert.deepEqual(pending(h).body, original);
});

for (const kind of ['missing', 'throws', 'does-not-store', 'cannot-read-back']) test(`unreliable ${kind} pending storage blocks create before POST`, async () => {
  const map = new Map(); let firstRead = true;
  const sessionStorage = kind === 'missing' ? null : {
    getItem(key) {
      if (kind === 'cannot-read-back' && !firstRead) throw new Error('synthetic storage unavailable');
      firstRead = false; return map.get(key) ?? null;
    },
    setItem(key, value) {
      if (kind === 'throws') throw new Error('synthetic quota exceeded');
      if (kind !== 'does-not-store') map.set(key, value);
    },
    removeItem(key) { map.delete(key); },
  };
  const h = await harness({storage: map, sessionStorage: sessionStorage || {getItem() { throw new Error('unavailable storage'); }}});
  open(h); configure(h);
  const outcome = await submit(h);
  assert.equal(creates(h).length, 0);
  if (outcome.error) assert.equal(outcome.error.code, 'create_storage_unavailable');
});

test('concurrent explicit submits issue one POST and one stable key', async () => {
  const h = open(await harness()); configure(h); let release;
  h.handle = async call => call.url === '/api/terminals/create' ? new Promise(resolve => { release = resolve; }) : undefined;
  const first = submit(h); await settle(); const second = submit(h); await settle();
  assert.equal(creates(h).length, 1); const original = clone(creates(h)[0].body);
  assert.deepEqual(pending(h).body, original);
  release(response({error: 'Invalid session or origin'}, 403));
  await Promise.all([first, second]);
  assert.equal(creates(h).length, 1); assert.deepEqual(pending(h).body, original);
});

test('confirmed created result clears pending only after returned terminal identity is valid', async () => {
  const h = open(await harness()); configure(h);
  h.handle = async call => call.url === '/api/terminals/create' ? response({created: true, agent: registerCreated(h)}) : undefined;
  const outcome = await submit(h); assert.equal(outcome.error, undefined);
  assert.equal(creates(h).length, 1); assert.equal(pending(h), null);
  assert.equal(vm.runInContext('selectedAgent', h.context), 'spark:synthetic-terminal');
});

for (const result of [{created: true}, {created: false, thread_id: 'not-in-registry'}, {accepted: true}]) test(`unconfirmed ${JSON.stringify(result)} create result retains original pending without repeat`, async () => {
  const h = open(await harness()); configure(h);
  h.handle = async call => call.url === '/api/terminals/create' ? response(result) : undefined;
  await submit(h); await settle();
  assert.equal(creates(h).length, 1); assert.deepEqual(pending(h)?.body, creates(h)[0].body);
});

test('created false with exact registered terminal clears pending and opens only that registration', async () => {
  const h = open(await harness()); configure(h);
  h.handle = async call => call.url === '/api/terminals/create' ? response({created: false, agent: registerCreated(h, 'registered-unknown-display')}) : undefined;
  await submit(h);
  assert.equal(creates(h).length, 1); assert.equal(pending(h), null);
  assert.equal(vm.runInContext('selectedAgent', h.context), 'spark:registered-unknown-display');
  assert.equal(h.calls.some(call => /\/input|\/reconnect|\/connect|\/resume/.test(call.url)), false);
});

test('pending continuation preserves the original Session ID rather than a later selected historical conversation', async () => {
  const h = await harness();
  h.context.openCreate({thread_id: 'original-synthetic-session', title: 'Original session', cwd: '/home/example/original'}, 'spark');
  h.handle = async call => { if (call.url === '/api/terminals/create') throw new TypeError('response lost'); };
  await submit(h); const original = clone(creates(h)[0].body);
  assert.equal(original.thread_id, 'original-synthetic-session');
  h.get('#create-dialog').close();
  h.context.openCreate({thread_id: 'different-synthetic-session', title: 'Other session', cwd: '/other/project'}, '316');
  await submit(h);
  assert.equal(creates(h).length, 2); assert.deepEqual(creates(h)[1].body, original);
  assert.equal(h.calls.some(call => /\/input|\/resume/.test(call.url)), false);
});

for (const record of ['{', JSON.stringify({version: 2, body: {}}), JSON.stringify({version: 1, body: {host: 'spark', cwd: '/home/example', title: 'synthetic', cols: 120, rows: 32}})]) test(`malformed or incomplete saved request ${record.slice(0, 40)} cannot be replaced with a fresh create key`, async () => {
  const storage = new Map([[pendingKey, record]]), h = await harness({storage});
  h.context.openCreate(null, '316'); await submit(h); await settle();
  assert.equal(creates(h).length, 0); assert.equal(storage.get(pendingKey), record);
  assert.equal(h.get('#pending-create-notice').hidden, false);
  assert.equal(vm.runInContext('createTarget', h.context), null);
});

test('returned terminal on the wrong host does not clear the original pending request', async () => {
  const h = open(await harness()); configure(h);
  h.handle = async call => call.url === '/api/terminals/create' ? response({created: true, agent: {host: '316', thread_id: 'wrong-host-terminal', terminal_id: 'wrong-host-terminal'}}) : undefined;
  await submit(h);
  assert.equal(creates(h).length, 1); assert.deepEqual(pending(h)?.body, creates(h)[0].body);
  assert.notEqual(vm.runInContext('selectedAgent', h.context), '316:wrong-host-terminal');
});

test('refresh communicates known expired page session separately from a local backend network outage', async () => {
  const h = await harness();
  h.handle = async call => call.url.startsWith('/api/state') ? response({error: 'Not found or unauthorized'}, 404) : undefined;
  await h.context.refresh(true); assert.match(h.get('#status').textContent, /认证.*失效|失效.*认证/);
  h.handle = async call => { if (call.url.startsWith('/api/state')) throw new TypeError('Failed to fetch'); };
  await h.context.refresh(true); assert.match(h.get('#status').textContent, /后台/); assert.doesNotMatch(h.get('#status').textContent, /认证.*失效|失效.*认证/);
  assert.equal(creates(h).length, 0);
});

for (const created of [undefined, 'unknown', 1]) test(`a terminal ID without explicit boolean creation receipt (${created}) cannot clear pending`, async () => {
  const h = open(await harness()); configure(h);
  h.handle = async call => call.url === '/api/terminals/create' ? response({created, agent: registerCreated(h)}) : undefined;
  await submit(h);
  assert.equal(creates(h).length, 1); assert.deepEqual(pending(h)?.body, creates(h)[0].body);
});

test('created false cannot use cached connected registry after its fresh state request fails', async () => {
  const h = await harness(), agent = registerCreated(h);
  await h.context.refresh(true); open(h); configure(h);
  h.handle = async call => {
    if (call.url === '/api/terminals/create') return response({created: false, agent});
    if (call.url.startsWith('/api/state')) throw new TypeError('synthetic refresh outage');
  };
  await submit(h);
  assert.equal(creates(h).length, 1); assert.deepEqual(pending(h)?.body, creates(h)[0].body);
});

test('created false pre-registered but disconnected display remains pending without automatic reconnect', async () => {
  const h = open(await harness()); configure(h);
  const agent = registerCreated(h); agent.connected = false; agent.status = 'exited';
  h.handle = async call => call.url === '/api/terminals/create' ? response({created: false, agent}) : undefined;
  await submit(h);
  assert.equal(creates(h).length, 1); assert.deepEqual(pending(h)?.body, creates(h)[0].body);
  assert.equal(h.calls.some(call => /\/input|\/reconnect|\/connect|\/resume/.test(call.url)), false);
});

test('created false fresh registry must match the returned physical terminal identity', async () => {
  const h = open(await harness()); configure(h);
  const agent = registerCreated(h);
  h.handle = async call => call.url === '/api/terminals/create' ? response({created: false, agent: {...agent, terminal_id: 'different-physical-terminal'}}) : undefined;
  await submit(h);
  assert.equal(creates(h).length, 1); assert.deepEqual(pending(h)?.body, creates(h)[0].body);
});

test('relative directory validation fails before storage or create submission', async () => {
  const h = open(await harness()); configure(h); h.get('#create-cwd').value = 'relative/project';
  const outcome = await submit(h);
  assert.match(outcome.error?.message || '', /绝对路径/);
  assert.equal(creates(h).length, 0); assert.equal(pending(h), null);
});

test('generic HTTP400 after create submission is not proof of an unexecuted operation', async () => {
  const h = open(await harness()); configure(h);
  h.handle = async call => call.url === '/api/terminals/create' ? response({error: 'synthetic backend failure'}, 400) : undefined;
  const first = await submit(h), original = clone(creates(h)[0].body);
  assert.equal(first.error?.status, 400); assert.deepEqual(pending(h)?.body, original);
  h.get('#create-dialog').close(); h.context.openCreate(null, '316');
  assert.equal(creates(h).length, 1); assert.equal(vm.runInContext('createTarget.host', h.context), original.host);
  await submit(h); assert.equal(creates(h).length, 2); assert.deepEqual(creates(h)[1].body, original);
});
