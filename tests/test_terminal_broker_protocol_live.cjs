'use strict';

// Explicit transport diagnostic. No model and no project writes: Python borrows
// its own SSH PTY in raw mode, queries the canonical headless terminal, records
// the returned protocol bytes, restores the TTY, and exits.
const assert = require('node:assert/strict');
const path = require('node:path');
const os = require('node:os');
const pty = require('node-pty');
const { Terminal } = require('@xterm/headless');
const { shellQuote, spawnSshPipe } = require('../terminal/terminal_broker.cjs');
const target = process.env.CEO_HUB_TEST_SSH_TARGET;
if (!target) throw new Error('Set CEO_HUB_TEST_SSH_TARGET to a test SSH target');
const ssh = process.platform === 'win32' ? path.join(process.env.WINDIR || 'C:\\Windows', 'System32', 'OpenSSH', 'ssh.exe') : 'ssh';
const python = `import os,select,time,tty,termios
old=termios.tcgetattr(0)
data=b''
try:
 tty.setraw(0)
 os.write(1,b'\\x1b[6n\\x1b[c\\x1b[>c')
 end=time.monotonic()+4
 while time.monotonic()<end:
  ready,_,_=select.select([0],[],[],0.1)
  if ready: data+=os.read(0,4096)
  if data.count(b'c')>=2 and b'R' in data: break
finally:
 termios.tcsetattr(0,termios.TCSANOW,old)
print('PROTOCOL_BYTES='+data.hex(),flush=True)
`;

async function probe(name, spawn) {
  const terminal = new Terminal({ cols: 100, rows: 30, allowProposedApi: true });
  const child = spawn(ssh, ['-tt', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', target, `python3 -c ${shellQuote(python)}`], {
    cwd: os.homedir(), env: { ...process.env, TERM: 'xterm-256color' }, cols: 100, rows: 30,
    name: 'xterm-256color', useConpty: true, useConptyDll: true,
  });
  if (child.ready) await child.ready;
  const emitted = [];
  terminal.onData(data => { emitted.push(Buffer.from(data).toString('hex')); child.write(data); });
  let output = '';
  let rendering = Promise.resolve();
  child.onData(data => { output += data; rendering = rendering.then(() => new Promise(resolve => terminal.write(data, resolve))); });
  const result = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.kill(); reject(new Error(`${name} transport diagnostic timed out`)); }, 18000);
    child.onExit(async ({ exitCode }) => {
      clearTimeout(timer);
      await rendering;
      const encoded = /PROTOCOL_BYTES=([a-f0-9]*)/.exec(output)?.[1];
      if (encoded == null) { reject(new Error(`${name}: missing Python protocol result (exit ${exitCode})`)); return; }
      resolve({ name, bytes: Buffer.from(encoded, 'hex').toString('utf8'), hex: encoded, generated: emitted });
    });
  });
  terminal.dispose();
  const intact = /\x1b\[\d+;\d+R/.test(result.bytes) && /\x1b\[\?1;2c/.test(result.bytes) && /\x1b\[>0;\d+;0c/.test(result.bytes);
  console.log(JSON.stringify({ transport: name, intact, received_hex: result.hex, generated_hex: result.generated }));
  return intact;
}

(async () => {
  if (process.platform === 'win32') await probe('Windows-ConPTY-SSH', pty.spawn);
  const intact = await probe('SSH-raw-pipe-remote-PTY', spawnSshPipe);
  assert.equal(intact, true, 'Raw SSH pipe must preserve DSR, primary DA and secondary DA replies byte-for-byte');
})().then(() => process.exit(0), error => { console.error(error.message); process.exit(1); });
