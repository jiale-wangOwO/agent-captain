'use strict';

// Independent reproduction: no broker, SSH, credentials, model, or user input.
// SerializeAddon temporarily prints dashes to preserve wrapped-but-erased rows.
// A resize before write's completion changes where those dashes are erased.
const test = require('node:test');
const assert = require('node:assert/strict');
const { Terminal } = require('@xterm/headless');
const { SerializeAddon } = require('@xterm/addon-serialize');

const write = (term, data) => new Promise(resolve => term.write(data, resolve));
function screen(term) {
  const buffer = term.buffer.active;
  return Array.from({ length: buffer.length }, (_, i) => buffer.getLine(i).translateToString(true)).join('\n');
}
function terminal() { return new Terminal({ cols: 73, rows: 34, allowProposedApi: true }); }

test('resizing during snapshot replay leaks serializer placeholders into erased row prefixes', async () => {
  const source = terminal(), stable = terminal(), racing = terminal();
  try {
    const serializer = new SerializeAddon();
    source.loadAddon(serializer);
    await write(source, '\x1b[?1049h' + '^[[A'.repeat(200));
    let repaint = '';
    for (let row = 1; row <= 12; row++) {
      repaint += `\x1b[${row};1H\x1b[73X\x1b[${row};50Hkept suffix`;
    }
    await write(source, repaint);
    const ansi = serializer.serialize();
    assert.equal((screen(source).match(/-/g) || []).length, 0);
    assert.ok((ansi.match(/-+\x1b\[1D\x1b\[1X/g) || []).length > 0);
    await write(stable, ansi);
    assert.equal((screen(stable).match(/-/g) || []).length, 0, 'Snapshot must parse completely at its original dimensions');
    const pending = write(racing, ansi);
    racing.resize(90, 34);
    await pending;
    assert.equal((screen(racing).match(/-/g) || []).length, 74,
      'Fixture reproduces placeholder artifacts when geometry changes before snapshot write completes');
  } finally {
    source.dispose(); stable.dispose(); racing.dispose();
  }
});
