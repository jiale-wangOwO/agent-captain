'use strict';
// Inert bookkeeping/fake PTY tests only. No machine or user input is touched.
const test = require('node:test');
const assert = require('node:assert/strict');
const { updateHumanInput, humanInputState } = require('../input_activity.cjs');
const { TerminalBroker } = require('../terminal_broker.cjs');
const fresh = () => ({ humanLineLength: 0, humanLeaseUntil: 0, humanPaste: false, inputEscape: '' });
const state = session => humanInputState(session, 5000).state;

test('Backspace and DEL on an empty prompt never create an enduring draft', () => {
  for (const bytes of ['\x7f', '\b', '\x7f\x7f\b']) {
    const s = fresh(); updateHumanInput(s, bytes, 0);
    assert.equal(state(s), 'idle');
    assert.equal(s.humanLineLength, 0);
  }
});

test('simple typing fully erased with backspace releases the draft without a cancel', () => {
  const s = fresh(); updateHumanInput(s, 'draft', 0);
  assert.equal(state(s), 'draft');
  updateHumanInput(s, '\x7f\x7f\x7f\x7f\x7f', 1);
  assert.equal(state(s), 'idle');
});

test('partial erasure protects the remaining real draft indefinitely', () => {
  const s = fresh(); updateHumanInput(s, 'draft\x7f\x7f', 0);
  assert.equal(humanInputState(s, 86400000).state, 'draft');
});

test('complete recognized CSI reports and focus events do not take human leases', () => {
  const replies = ['\x1b[1;2R', '\x1b[?1;2c', '\x1b[>0;276;0c', '\x1b[?2004;1$y',
    '\x1b[4;100;200t', '\x1b[I', '\x1b[O', '\x1b[<0;10;20M'];
  for (const value of replies) {
    const s = fresh(); updateHumanInput(s, value, 1000);
    assert.equal(humanInputState(s, 1000).state, 'idle', JSON.stringify(value));
    assert.equal(s.humanLeaseUntil, 0);
  }
});

test('literal or incomplete control strings are conservative; UI suppresses real automatic replies', () => {
  for (const value of ['\x1b]10;rgb:1111/2222/3333\x07','\x1bP>|XTerm(276)\x1b\\','\x1b]literal','\x1b[12;']) {
    const s=fresh(); updateHumanInput(s,value,0); assert.equal(state(s),'draft');
    updateHumanInput(s,'further human input',1); assert.equal(state(s),'draft');
    updateHumanInput(s,'\x03',2); assert.equal(state(s),'idle');
  }
});

test('protocol replies neither expose nor release an existing draft', () => {
  const s = fresh(); updateHumanInput(s, 'secret-draft', 0);
  updateHumanInput(s, '\x1b]10;rgb:1234/5678/abcd\x07\x1b[I', 10);
  assert.equal(state(s), 'draft');
  assert.equal(JSON.stringify(s).includes('secret-draft'), false);
  assert.equal(JSON.stringify(humanInputState(s)).includes('secret-draft'), false);
});

test('cursor editing, history, completion, search and yank stay conservative', () => {
  for (const bytes of ['a\x1b[D\x7f', '\x1b[A\x7f', '\x1bOA\b', '\t\x7f', '\x12\x7f', '\x19\b', 'abc\x15\x7f']) {
    const s = fresh(); updateHumanInput(s, bytes, 0);
    assert.equal(state(s), 'draft', JSON.stringify(bytes));
    assert.equal(humanInputState(s, 5000).reason, 'uncertain_input');
    updateHumanInput(s, '\x03', 1); assert.equal(state(s), 'idle');
  }
});

test('Unicode and multiline bracketed paste cannot accidentally become an empty draft', () => {
  for (const bytes of ['中文\x7f\x7f', 'e\u0301\b', '👩‍💻\x7f', '\x1b[200~line\n\x03\x1b[201~\x7f']) {
    const s = fresh(); updateHumanInput(s, bytes, 0); assert.equal(state(s), 'draft');
    updateHumanInput(s, '\r', 1); assert.equal(state(s), 'idle');
  }
});

test('empty bracketed paste is not an enduring draft and a short lease is not a long typing claim', () => {
  const s = fresh(); updateHumanInput(s, '\x1b[200~\x1b[201~', 0);
  assert.equal(humanInputState(s, 100).state, 'typing');
  assert.equal(state(s), 'idle');
});

test('legacy dirty state migrates conservatively, never releases on time or output', () => {
  const s = fresh(); s.humanLineLength = 1;
  assert.equal(state(s), 'draft');
  updateHumanInput(s, '\x7f\x7f', 0); assert.equal(state(s), 'draft');
  updateHumanInput(s, '\r', 0); assert.equal(state(s), 'idle');
});

async function fixture() {
  const writes = [];
  const fake = {pid: 123, process: 'codex', write: value => writes.push(value), resize() {}, kill() {}, onData() {}, onExit() {}};
  const broker = new TerminalBroker({spawn: () => fake, probe: async () => ({state: 'agent', command: 'codex'})});
  const created = await broker.create({cwd: __dirname});
  return {broker, writes, id: created.id, session: broker.get(created.id)};
}

test('rejected disconnected or unknown-geometry input does not poison draft bookkeeping', async () => {
  for (const change of [{connected: false}, {geometryUnknown: true}]) {
    const f = await fixture(); Object.assign(f.session, change);
    await assert.rejects(f.broker.input(f.id, {origin: 'ui', data: 'not delivered'}));
    assert.equal(f.broker.describe(f.session).human_busy, false);
    assert.deepEqual(f.writes, []);
  }
});

test('a throwing PTY is uncertain, not proof that its input never reached the application', async () => {
  const f = await fixture(); f.session.pty.write = () => { throw Error('sink failure'); };
  await assert.rejects(f.broker.input(f.id, {origin: 'ui', data: 'not written'}));
  assert.equal(f.broker.describe(f.session).human_busy, true);
  assert.equal(f.broker.describe(f.session).human_input.reason, 'uncertain_input');
});

test('OSC-like paste payload and an unterminated paste never disappear as protocol replies', () => {
  for (const bytes of ['\x1b[200~\x1b]11;literal user paste\x07\x1b[201~', '\x1b[200~\x1b]11;partial', '\x1b[200~']) {
    const s = fresh(); updateHumanInput(s, bytes, 0); assert.equal(state(s), 'draft');
  }
});

test('multiple failed queued edits cannot roll back to another uncommitted clear', async () => {
  const f = await fixture(); await f.broker.input(f.id, {origin:'ui', data:'original draft'});
  f.session.pty.write = () => {throw Error('uncertain sink');};
  await Promise.allSettled([f.broker.input(f.id,{origin:'ui',data:'\r'}), f.broker.input(f.id,{origin:'ui',data:'new input'})]);
  f.session.humanLeaseUntil = 0;
  assert.equal(f.broker.describe(f.session).human_busy, true);
});

test('failed pre-write clear plus failed new input preserves original draft without uncertain writes', async () => {
  const f = await fixture(); await f.broker.input(f.id, {origin:'ui',data:'original draft'});
  let release; f.session.writeQueue = new Promise(resolve => {release=resolve;});
  const a=f.broker.input(f.id,{origin:'ui',data:'\r'}), b=f.broker.input(f.id,{origin:'ui',data:'new input'});
  f.session.geometryUnknown = true; release();
  await Promise.allSettled([a,b]); f.session.humanLeaseUntil=0;
  assert.equal(f.session.humanLineLength,14);
  assert.equal(f.broker.describe(f.session).human_busy,true);
  assert.deepEqual(f.writes,['original draft']);
});

test('input retries, including simultaneous ones, count and write only once', async () => {
  const f = await fixture(); const body = {origin: 'ui', data: 'x', request_key: 'same-input'};
  const results = await Promise.all([f.broker.input(f.id, body), f.broker.input(f.id, body)]);
  assert.equal(results[1].deduplicated, true);
  await f.broker.input(f.id, body);
  assert.equal(f.session.humanLineLength, 1);
  await f.broker.input(f.id, {origin: 'ui', data: '\x7f'}); f.session.humanLeaseUntil = 0;
  assert.equal(f.broker.describe(f.session).human_busy, false);
  assert.deepEqual(f.writes, ['x', '\x7f']);
});

test('input reserved while CEO awaits a probe still prevents a task from stealing the terminal', async () => {
  const f = await fixture(); let input;
  f.broker.probe = async () => { input = f.broker.input(f.id, {origin: 'ui', data: 'real draft'}); return {state: 'agent', command: 'codex'}; };
  await assert.rejects(f.broker.prompt(f.id, {text: 'must not send', request_key: 'ceo-race'}), e => e.code === 'human_busy');
  await input; assert.deepEqual(f.writes, ['real draft']);
});

test('queued human Enter retains precedence beyond the short typing timer', async () => {
  const f=await fixture(); await f.broker.input(f.id,{origin:'ui',data:'existing draft'});
  f.session.humanLeaseUntil=0;
  let release; f.broker.probe=()=>new Promise(resolve=>{release=()=>resolve({state:'agent',command:'codex'});});
  // Start from idle just for the CEO's initial gate. A human edit arriving
  // during that probe must win even when it is a clear/Enter, not draft text.
  f.session.humanLineLength=0;
  const task=f.broker.prompt(f.id,{text:'must not steal input',request_key:'slow-probe-clear'});
  await new Promise(resolve=>setImmediate(resolve));
  const input=f.broker.input(f.id,{origin:'ui',data:'\r'});
  assert.equal(humanInputState(f.session,Number.MAX_SAFE_INTEGER).state,'typing');
  release(); await assert.rejects(task,e=>e.code==='human_busy'); await input;
  assert.deepEqual(f.writes,['existing draft','\r']);
});
