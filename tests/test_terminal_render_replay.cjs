'use strict';
// Rendering-only regression tests. No PTY, shell, machine, credentials, or user
// conversation is opened/read/written by this test file.
const test = require('node:test');
const assert = require('node:assert/strict');
const { Terminal } = require('@xterm/headless');
const { SerializeAddon } = require('@xterm/addon-serialize');

const write = (terminal, data) => new Promise(resolve => terminal.write(data, resolve));
function create(cols = 40, rows = 10) {
  const terminal = new Terminal({ cols, rows, scrollback: 100, allowProposedApi: true });
  const serializer = new SerializeAddon(); terminal.loadAddon(serializer);
  return { terminal, serializer };
}
function screen(terminal) {
  const buffer = terminal.buffer.active;
  return {
    type: buffer.type, cursor: [buffer.cursorX, buffer.cursorY],
    lines: Array.from({ length: terminal.rows }, (_, row) => buffer.getLine(buffer.baseY + row)?.translateToString(true) || ''),
  };
}
function dots(terminal) {
  return screen(terminal).lines.reduce((count, line) => count + (line.match(/[.·]/g) || []).length, 0);
}
async function replay(source, cols = source.terminal.cols, rows = source.terminal.rows) {
  const restored = create(cols, rows);
  await write(restored.terminal, source.serializer.serialize({ scrollback: 100 }));
  return restored;
}

test('same-geometry normal buffer restores colored shell history without creating dots', async t => {
  const source = create(); t.after(() => source.terminal.dispose());
  await write(source.terminal, '\x1b[32muser@server\x1b[0m:/workspace$ ls\r\n\x1b[34mpyCircuit  worktrees\x1b[0m\r\nUnicode 中文 e\u0301 ┌─┐\r\n$ ');
  const restored = await replay(source); t.after(() => restored.terminal.dispose());
  assert.deepEqual(screen(restored.terminal), screen(source.terminal));
  assert.equal(dots(restored.terminal), 0);
});

test('same-geometry alternate buffer redraw restores menu and then applies delta at same cursor', async t => {
  const source = create(); t.after(() => source.terminal.dispose());
  await write(source.terminal, 'normal shell history\r\n$ command\x1b[?1049h\x1b[2J\x1b[H┌──────────────┐\r\n│ Choose model │\r\n└──────────────┘\x1b[5;3H> menu');
  const restored = await replay(source); t.after(() => restored.terminal.dispose());
  assert.deepEqual(screen(restored.terminal), screen(source.terminal));
  await Promise.all([write(source.terminal, '\x1b[2;3Hready'), write(restored.terminal, '\x1b[2;3Hready')]);
  assert.deepEqual(screen(restored.terminal), screen(source.terminal));
  assert.equal(dots(restored.terminal), 0);
});

test('serialized reset at canonical geometry discards stale dotted browser-side cells', async t => {
  const source = create(); const restored = create(80, 20);
  t.after(() => { source.terminal.dispose(); restored.terminal.dispose(); });
  await write(source.terminal, 'user@server:/workspace$ ls\r\npyCircuit  worktrees\r\n$ ');
  await write(restored.terminal, Array.from({ length: 15 }, () => '·'.repeat(26)).join('\r\n'));
  assert.equal(dots(restored.terminal), 390);
  restored.terminal.reset(); restored.terminal.resize(source.terminal.cols, source.terminal.rows);
  await write(restored.terminal, source.serializer.serialize({ scrollback: 100 }));
  assert.deepEqual(screen(restored.terminal), screen(source.terminal));
  assert.equal(dots(restored.terminal), 0);
});

test('replaying canonical snapshot at a different width damages cell coordinates but does not invent dot characters', async t => {
  const source = create(40, 10); t.after(() => source.terminal.dispose());
  await write(source.terminal, '0123456789'.repeat(7) + '\r\n\x1b[3;29Hfile-name\x1b[5;1H$ ');
  const restored = await replay(source, 26, 10); t.after(() => restored.terminal.dispose());
  assert.notDeepEqual(screen(restored.terminal), screen(source.terminal));
  assert.equal(dots(restored.terminal), 0);
});

test('canonical resize and program redraw followed by snapshot restores active alternate buffer identically', async t => {
  const source = create(60, 15); t.after(() => source.terminal.dispose());
  await write(source.terminal, '\x1b[?1049h\x1b[2J\x1b[H┌' + '─'.repeat(44) + '┐\x1b[4;3Hchoose provider\x1b[8;10H> ');
  source.terminal.resize(40, 10);
  await write(source.terminal, '\x1b[2J\x1b[H┌' + '─'.repeat(36) + '┐\x1b[4;3Hchoose provider\x1b[8;10H> ');
  const restored = await replay(source); t.after(() => restored.terminal.dispose());
  assert.deepEqual(screen(restored.terminal), screen(source.terminal));
});

test('DEC graphics followed by normal charset reset preserves subsequent delta text', async t => {
  const source = create(); t.after(() => source.terminal.dispose());
  await write(source.terminal, '\x1b(0lqqk\x1b(B');
  const restored = await replay(source); t.after(() => restored.terminal.dispose());
  assert.deepEqual(screen(restored.terminal), screen(source.terminal));
  await Promise.all([write(source.terminal, 'q'), write(restored.terminal, 'q')]);
  assert.deepEqual(screen(restored.terminal), screen(source.terminal));
  assert.ok(screen(restored.terminal).lines[0].endsWith('q'));
  assert.equal(dots(restored.terminal), 0);
});

test('plain shell-prefix replay neither invents dotted underline attributes nor dots', async t => {
  const source = create(40, 10); t.after(() => source.terminal.dispose());
  await write(source.terminal, '\x1b[32muser@server\x1b[0m:/workspace$ ls\r\n\x1b[34mpyCircuit  worktrees\x1b[0m\r\n$ ');
  const restored = await replay(source); t.after(() => restored.terminal.dispose());
  for (const terminal of [source.terminal, restored.terminal]) {
    const buffer = terminal.buffer.active;
    for (let row = 0; row < terminal.rows; row++) for (let col = 0; col < terminal.cols; col++) {
      assert.equal(!!buffer.getLine(buffer.baseY + row).getCell(col).isUnderline(), false);
    }
    assert.equal(dots(terminal), 0);
  }
  assert.deepEqual(screen(restored.terminal), screen(source.terminal));
});

test('oversized tmux client ACS outside-fill wraps over shell prefixes in a narrower terminal', async t => {
  const mismatched = create(73, 33), aligned = create(73, 33);
  t.after(() => { mismatched.terminal.dispose(); aligned.terminal.dispose(); });
  const pane = Array.from({ length: 30 }, (_, row) =>
    `\x1b[${row + 1};1Huser@server:/workspace$ row-${row}`).join('');
  // tmux's default CELL_OUTSIDE uses the DEC graphics '~', not literal UTF-8
  // middots. A 100-column client has a border plus 26 outside cells beyond a
  // 73-column window. CUP clips in the actual 73-column terminal and fill then
  // wraps across the next row's already drawn left-hand text.
  const outside = Array.from({ length: 30 }, (_, row) =>
    `\x1b[${row + 1};73H│\x1b(0${'~'.repeat(26)}\x1b(B`).join('');
  assert.equal(outside.includes('·'), false);
  await write(mismatched.terminal, pane + outside);
  await write(aligned.terminal, pane);
  const damaged = screen(mismatched.terminal).lines;
  assert.ok(damaged.slice(1, 30).every(line => line.startsWith('·'.repeat(26))));
  assert.equal(dots(aligned.terminal), 0);
  assert.ok(screen(aligned.terminal).lines.slice(0, 30).every(line => line.startsWith('user@server:')));
});
