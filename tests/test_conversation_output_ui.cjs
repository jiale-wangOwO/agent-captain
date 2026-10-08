'use strict';
// Run the shipped UI against inert DOM/network fixtures. No browser, PTY, SSH,
// worker keys, actual rollout files, or human session are touched.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sessionId = '11111111-1111-4111-8111-111111111111';
const otherSessionId = '22222222-2222-4222-8222-222222222222';
function element() {
  return {value: '', textContent: '', hidden: false, disabled: false, checked: false, clientWidth: 900,
    children: [], dataset: {}, style: {}, attributes: {}, classList: {add() {}, remove() {}, toggle() {}},
    append(...children) {this.children.push(...children); this.childElementCount = this.children.length;},
    replaceChildren(...children) {this.children = children; this.childElementCount = children.length;},
    setAttribute(name, value) {this.attributes[name] = value;}, addEventListener() {},
    showModal() {this.open = true;}, close() {this.open = false;}, reset() {}, focus() {}, select() {}};
}
const textOf = element => [element.textContent || '', ...(element.children || []).map(textOf)].join('');
const reply = value => ({ok: true, json: async () => value});
const actual = (extra = {}) => ({source: 'codex_rollout', output_binding: {state: 'bound', session_id: sessionId, live_session_verified: true},
  messages: [{message_id: 'm1', role: 'assistant', text: '完整报告的开头\n完整报告的末尾', chunk_offset: 0, total_chars: 21, content_complete: true}],
  recent_signals: [], content_complete: true, history_complete: false, truncated: false, has_more: false, ...extra});

async function harness() {
  const elements = new Map(), network = [];
  const get = selector => {if (!elements.has(selector)) elements.set(selector, element()); return elements.get(selector);};
  const agent = {host: 'spark', thread_id: 'ceohub-fixture', terminal_id: 'ceohub-fixture', mode: 'terminal',
    alias: 'Spark fixture', connected: true, active: true, command: 'node', state: 'agent', cwd: '/project'};
  const host = {id: 'spark', transport: 'ssh', connected: true};
  const fixture = {key: 'spark:ceohub-fixture', agent, disposed: false, initialized: true, input: 'human draft preserved',
    inputChain: Promise.resolve(), observer: {disconnect() {}}, term: {text: 'ONLY SCREEN TAIL', focus() {}, blur() {}, dispose() {throw new Error('Output view must not dispose terminal');}}};
  const sentinel = element(); sentinel.textContent = 'existing terminal DOM'; get('#terminal').append(sentinel);
  let route;
  const context = vm.createContext({console, document: {querySelector: get, querySelectorAll: () => [],
    getElementById: id => get('#' + id), createElement: element, createTextNode: text => ({textContent: text}), addEventListener() {}},
    URLSearchParams, crypto: {randomUUID: () => 'fixture-key'}, navigator: {clipboard: {}}, window: {addEventListener() {}},
    setInterval() {}, setTimeout: () => 1, clearTimeout() {}, requestAnimationFrame() {},
    fetch: async (url, options) => {network.push({url, options});
      const fallback = () => reply(url.startsWith('/api/state') ? {hosts: [host], agents: [agent], outbox: [], events: []} : actual());
      return route ? route(url, options, fallback) : fallback();}});
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/ui.js'), 'utf8'), context, {filename: 'ui.js'});
  await context.refreshTask;
  // refreshTask is lexical; ensure the initial inert /api/state read settled.
  await new Promise(resolve => setImmediate(resolve));
  context.fixture = fixture;
  vm.runInContext('terminalSession=fixture; selectedAgent=fixture.key; selectedHost="spark"; state.agents=[fixture.agent];', context);
  network.length = 0;
  return {context, get, fixture, agent, network, sentinel, route: next => {route = next;}};
}
const readCalls = h => h.network.filter(call => call.url === '/api/agents/read');
const bodyOf = call => JSON.parse(call.options.body);
const forbiddenCalls = h => h.network.filter(call => /\/api\/terminals\/(?:input|create|connect|resize|close)/.test(call.url));

test('output reader is a separate pane and markup offers exact UUID association', () => {
  const html = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');
  assert.match(html, /id="terminal-output"[^>]+aria-controls="conversation-output-panel"/);
  assert.match(html, /<section id="conversation-output-panel"/);
  assert.match(html, /同一终端的真实会话记录（不向 agent 输入）/);
  assert.match(html, /id="bind-output-session"[^>]+pattern="\[0-9a-fA-F\]/);
  assert.match(html, /不会按目录或“最新对话”猜测/);
});

test('explicit read displays real response text while preserving terminal DOM and human draft', async () => {
  const h = await harness();
  await h.context.readConversationOutput();
  assert.deepEqual(bodyOf(readCalls(h)[0]), {host: 'spark', thread_id: 'ceohub-fixture', limit: 30, include_tools: false});
  assert.match(textOf(h.get('#output-messages')), /完整报告的开头.*完整报告的末尾/s);
  assert.doesNotMatch(textOf(h.get('#output-messages')), /ONLY SCREEN TAIL/);
  assert.equal(h.get('#terminal').children[0], h.sentinel);
  assert.equal(h.fixture.input, 'human draft preserved');
  assert.equal(h.fixture.term.text, 'ONLY SCREEN TAIL');
  assert.match(h.get('#output-source').textContent, /真实会话记录/);
  assert.equal(forbiddenCalls(h).length, 0);
});

test('terminal fallback explains missing binding without mislabeling a screen as a complete report', async () => {
  const h = await harness();
  h.route(() => reply({source: 'terminal', screen: 'report tail', history_complete: false,
    output_binding: {state: 'unbound', reason: '无法确认当前会话 ID'}, note: '仅有屏幕输出'}));
  await h.context.readConversationOutput();
  assert.match(h.get('#output-source').textContent, /未读到实际会话输出/);
  assert.match(h.get('#output-note').textContent, /无法确认当前会话 ID/);
  assert.match(textOf(h.get('#output-messages')), /关联会话/);
  assert.doesNotMatch(textOf(h.get('#output-messages')), /report tail/);
  assert.equal(h.get('#output-more').hidden, true);
  assert.equal(forbiddenCalls(h).length, 0);
});

test('binding dialog does not guess from source ID or cwd and rejects a partial session ID', async () => {
  const h = await harness(); h.agent.source_thread_id = sessionId;
  h.context.openBindOutput();
  assert.equal(h.get('#bind-output-session').value, '');
  h.get('#bind-output-session').value = '01a115c9';
  await assert.rejects(h.context.bindConversationOutput(), /完整的 Codex 会话 ID/);
  assert.equal(h.network.length, 0);
});

test('exact UUID binding only calls association and readonly records endpoints', async () => {
  const h = await harness();
  h.route((url, options, fallback) => url === '/api/agents/bind-output'
    ? reply({output_binding: {state: 'bound', session_id: sessionId, method: 'human_selected_session'}}) : fallback());
  h.context.openBindOutput(); h.get('#bind-output-session').value = '  ' + sessionId + '  ';
  await h.context.bindConversationOutput();
  const association = h.network.find(call => call.url === '/api/agents/bind-output');
  assert.deepEqual(bodyOf(association), {host: 'spark', thread_id: 'ceohub-fixture', session_id: sessionId});
  assert.equal(readCalls(h).length, 1);
  assert.equal(h.get('#bind-output-dialog').open, false);
  assert.equal(h.fixture.input, 'human draft preserved');
  assert.equal(forbiddenCalls(h).length, 0);
});

test('association confirmation cannot target a newly selected worker', async () => {
  const h = await harness(); h.context.openBindOutput(); h.get('#bind-output-session').value = sessionId;
  vm.runInContext('selectedAgent="spark:other-worker";', h.context);
  await assert.rejects(h.context.bindConversationOutput(), /所选终端已变化/);
  assert.equal(h.network.length, 0);
});

test('chunk pagination preserves response order and offsets and never duplicates the same returned chunk', async () => {
  const h = await harness(); let count = 0;
  const first = {message_id: 'long', role: 'assistant', text: 'REPORT-FIRST', chunk_offset: 0, byte_offset: 100, total_chars: 40, content_complete: false};
  const second = {...first, text: 'REPORT-CONTINUED', chunk_offset: 12, content_complete: true};
  h.route(() => reply(++count === 1 ? actual({messages: [first], has_more: true, next_cursor: 'opaque-page-2', content_complete: false, truncated: true})
    : actual({messages: [first, second, {message_id: 'older', role: 'user', text: 'OLDER-QUESTION', chunk_offset: 0, byte_offset: 1}], after_cursor: 'opaque-end'})));
  await h.context.readConversationOutput();
  assert.equal(h.get('#output-more').hidden, false);
  assert.match(h.get('#output-note').textContent, /本页含分段内容/);
  await h.context.readConversationOutput(true);
  assert.deepEqual(bodyOf(readCalls(h)[1]), {host: 'spark', thread_id: 'ceohub-fixture', limit: 30, include_tools: false, cursor: 'opaque-page-2'});
  const messages = h.get('#output-messages').children;
  assert.equal(messages.length, 3);
  assert.equal(messages[0].dataset.chunkOffset, '0'); assert.equal(messages[1].dataset.chunkOffset, '12');
  assert.match(textOf(messages[0]), /REPORT-FIRST/); assert.match(textOf(messages[1]), /REPORT-CONTINUED/);
  assert.match(textOf(messages[2]), /OLDER-QUESTION/);
  assert.equal(h.get('#output-more').hidden, true);
  assert.equal(forbiddenCalls(h).length, 0);
});

test('refresh starts a new latest range without cursor and does not retain older output', async () => {
  const h = await harness(); let count = 0;
  h.route(() => reply(actual({messages: [{message_id: 'm' + count, role: 'assistant', text: ++count === 1 ? 'OLD OUTPUT' : 'NEW OUTPUT'}], has_more: true, next_cursor: 'page'})));
  await h.context.readConversationOutput(); await h.context.readConversationOutput();
  assert.equal(bodyOf(readCalls(h)[1]).cursor, undefined);
  assert.match(textOf(h.get('#output-messages')), /NEW OUTPUT/);
  assert.doesNotMatch(textOf(h.get('#output-messages')), /OLD OUTPUT/);
});

test('an in-flight read cannot overwrite a newly selected agent output', async () => {
  const h = await harness(); let resolveOld;
  h.route(() => new Promise(resolve => {resolveOld = () => resolve(reply(actual()));}));
  const reading = h.context.readConversationOutput();
  vm.runInContext('selectedAgent="spark:other-worker"; resetConversationOutput();', h.context);
  h.get('#output-messages').append({textContent: 'NEW WORKER CONTENT'});
  resolveOld(); await reading;
  assert.equal(textOf(h.get('#output-messages')), 'NEW WORKER CONTENT');
  assert.equal(h.get('#conversation-output-panel').hidden, true);
});

test('duplicate click while reading is inert and no automatic reads occur in registry rendering', async () => {
  const h = await harness(); let resolveRead;
  h.context.renderDetail(); await h.context.refresh(true);
  assert.equal(readCalls(h).length, 0);
  h.route(() => new Promise(resolve => {resolveRead = () => resolve(reply(actual()));}));
  const reading = h.context.readConversationOutput(); await h.context.readConversationOutput();
  assert.equal(readCalls(h).length, 1);
  resolveRead(); await reading;
});

test('a changed session during pagination is refused instead of mixing session records', async () => {
  const h = await harness(); let count = 0;
  h.route(() => reply(++count === 1 ? actual({has_more: true, next_cursor: 'page'})
    : actual({output_binding: {state: 'bound', session_id: otherSessionId}, messages: [{message_id: 'other', text: 'DIFFERENT SESSION'}]})));
  await h.context.readConversationOutput();
  await assert.rejects(h.context.readConversationOutput(true), /不会混合两个会话/);
  assert.doesNotMatch(textOf(h.get('#output-messages')), /DIFFERENT SESSION/);
  assert.match(h.get('#output-note').textContent, /会话关联已变化/);
});

test('untrusted rollout content renders literally, not as active HTML', async () => {
  const h = await harness(), payload = '<img src=x onerror=alert(1)>';
  h.route(() => reply(actual({messages: [{message_id: 'untrusted', role: 'assistant', text: payload}]})));
  await h.context.readConversationOutput();
  assert.match(textOf(h.get('#output-messages')), /<img src=x onerror=alert\(1\)>/);
  assert.equal(h.get('#output-messages').children[0].innerHTML, undefined);
});

test('full tool records are opt-in, kept through cursor pages, and distinctly labeled', async () => {
  const h = await harness(); let count = 0;
  h.route(() => reply(actual({messages: [{message_id: 'tool' + count++, role: 'tool', text: 'FULL TOOL CONTENT', chunk_offset: 0}], has_more: true, next_cursor: 'page'})));
  h.get('#output-include-tools').checked = true;
  await h.context.readConversationOutput(); await h.context.readConversationOutput(true);
  assert.equal(bodyOf(readCalls(h)[0]).include_tools, true); assert.equal(bodyOf(readCalls(h)[1]).include_tools, true);
  assert.match(textOf(h.get('#output-messages')), /工具记录/);
  assert.equal(h.get('#output-messages').children[0].className, 'message tool');
  assert.equal(forbiddenCalls(h).length, 0);
});

test('toggling tool visibility in the shown pane explicitly restarts the latest range', async () => {
  const h = await harness();
  h.route(() => reply(actual({has_more: true, next_cursor: 'page'})));
  await h.context.readConversationOutput();
  h.get('#output-include-tools').checked = true; h.get('#output-include-tools').onchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(readCalls(h).length, 2);
  assert.equal(bodyOf(readCalls(h)[1]).include_tools, true); assert.equal(bodyOf(readCalls(h)[1]).cursor, undefined);
  h.get('#output-hide').onclick(); h.get('#output-include-tools').checked = false; h.get('#output-include-tools').onchange();
  assert.equal(readCalls(h).length, 2, 'A hidden pane is never a background history reader');
});

test('explicit or cached association without live session identity is not mislabeled as the current running session', async () => {
  const h = await harness();
  h.route(() => reply(actual({output_binding: {state: 'bound', session_id: sessionId, live_session_verified: false, method: 'human_selected_session'}})));
  await h.context.readConversationOutput();
  assert.match(h.get('#output-source').textContent, /已关联的会话记录/);
  assert.match(h.get('#output-note').textContent, /当前进程未暴露会话 ID.*切换会话.*重新关联.*不将此记录认作新会话结果/);
});

test('a final chunk is still a fragment but does not wrongly claim more content remains', async () => {
  const h = await harness();
  h.route(() => reply(actual({messages: [{message_id: 'last', role: 'assistant', text: 'LAST CHUNK', chunk_offset: 900,
    total_chars: 910, content_complete: false, has_more_content: false}], content_complete: false})));
  await h.context.readConversationOutput();
  assert.match(textOf(h.get('#output-messages')), /已读到该消息末尾/);
  assert.doesNotMatch(textOf(h.get('#output-messages')), /后文待继续读取/);
  assert.doesNotMatch(h.get('#output-note').textContent, /内容尚未全部读完/);
});

test('a replaced terminal under the same registration key invalidates an in-flight output response', async () => {
  const h = await harness(); let resolveRead;
  h.route(() => new Promise(resolve => {resolveRead = () => resolve(reply(actual()));}));
  const reading = h.context.readConversationOutput();
  h.agent.terminal_id = 'ceohub-replacement'; h.context.renderDetail();
  resolveRead(); await reading;
  assert.equal(h.get('#conversation-output-panel').hidden, true);
  assert.doesNotMatch(textOf(h.get('#output-messages')), /完整报告/);
});
