// A retained tmux pane snapshot is not a transcript of alternate-screen redraws.
// This reader never sends terminal input, enters copy mode, or focuses a worker.
(() => {
  let owner = null, generation = 0, pending = false;
  const panel = () => $('#terminal-history-panel');
  const text = () => $('#terminal-history-text');
  const info = () => $('#terminal-history-info');
  function current(session) { return !!session && !session.disposed && isTerminalDisplayCurrent(session); }
  function eligible(session) {
    return current(session) && session.kind !== 'controller' && session.agent?.terminal_id
      && state.hosts.some(host => host.id === session.agent.host && host.transport === 'ssh')
      && !session.closing && !session.closed;
  }
  function isActive(session) { return owner === session && !panel().hidden; }
  function render(session) {
    if (session?.kind === 'controller') return;
    const button = $('#terminal-history');
    button.disabled = !eligible(session) || pending;
    button.hidden = session?.kind === 'controller';
    if (!isActive(session)) {
      if (eligible(session) && Number(session.term.buffer?.active?.baseY) === 0) {
        $('#terminal-page-up').disabled = pending;
        $('#terminal-scroll-status').textContent = '向上翻页可只读查看原 tmux 保留的终端历史';
      }
      return;
    }
    // Connection-state rendering may not re-enable a hidden live input surface.
    if (session.term.options) session.term.options.disableStdin = true;
    $('#terminal').hidden = true;
    $('#terminal-scroll-status').textContent = pending ? '正在读取原终端历史…' : '终端历史 · 只读快照';
    $('#terminal-page-up').disabled = pending || text().scrollTop <= 0;
    $('#terminal-page-down').disabled = pending || text().scrollTop >= Math.max(0, text().scrollHeight - text().clientHeight) - 1;
    $('#terminal-latest').disabled = false;
    $('#terminal-latest').setAttribute('aria-pressed', 'false');
  }
  function close(session) {
    if (owner !== session) return;
    ++generation; owner = null; pending = false;
    panel().hidden = true; text().textContent = ''; info().textContent = '';
    $('#terminal').hidden = false;
    if (current(session) && session.term.options) session.term.options.disableStdin = !canWriteTerminal(session);
    if (current(session)) renderTerminalScroll(session);
    // No focus, input flush, fit, resize, reconnect, or new model launch here.
  }
  function navigate(session, intent) {
    if (!current(session) || !isActive(session)) return false;
    if (intent === 'latest') { close(session); return true; }
    if (pending) return true;
    const pre = text(), bottom = Math.max(0, pre.scrollHeight - pre.clientHeight);
    const pageHeight = Math.max(1, pre.clientHeight - 19);
    pre.scrollTop = intent === 'top' ? 0 : Math.max(0, Math.min(bottom, pre.scrollTop + (intent === 'up' ? -pageHeight : pageHeight)));
    render(session); return true;
  }
  async function open(session, intent = 'up') {
    if (!eligible(session) || pending) return;
    const ticket = ++generation;
    owner = session; pending = true;
    panel().hidden = false; text().textContent = ''; info().textContent = '只读取当前登记的原 tmux/pane；不会向 agent 输入。';
    render(session);
    try {
      const query = new URLSearchParams({...terminalApiIdentity(session), lines: '5000'});
      const result = await request('/api/terminals/history?' + query);
      if (ticket !== generation || !current(session) || owner !== session) return;
      if (result.source !== 'tmux_pane_history' || result.terminal_id !== session.agent.terminal_id || typeof result.text !== 'string' || result.text.length > 2 * 1024 * 1024) throw new Error('没有得到准确原终端的有界历史快照。');
      text().textContent = result.text;
      info().textContent = `原 tmux 保留的终端历史 · ${Number(result.line_count) || 0} 行 · 只读快照${result.truncated ? ' · 已达到读取上限' : ''}。${result.alternate_on ? '当前 pane 为全屏程序，已被重绘或清除的内容可能不在历史中。' : ''}完整 Codex 对话请使用「会话输出」。`;
      pending = false;
      text().scrollTop = intent === 'top' ? 0 : Math.max(0, text().scrollHeight - text().clientHeight - Math.max(1, text().clientHeight - 19));
      render(session);
    } catch (error) {
      if (ticket !== generation || !current(session) || owner !== session) return;
      pending = false;
      info().textContent = `原终端历史暂时不可读取：${error.message}。可返回最新画面，或查看已关联的「会话输出」；没有重连或输入到 agent。`;
      render(session);
    }
  }
  function page(session, intent) {
    intent = intent === 'page-up' ? 'up' : intent === 'page-down' ? 'down' : intent;
    if (isActive(session)) return navigate(session, intent);
    const buffer = session?.term.buffer?.active;
    if (!eligible(session) || !['up', 'top'].includes(intent) || Number(buffer?.baseY) > 0) return false;
    void open(session, intent); return true;
  }
  function wheel(session, event) {
    if (event.ctrlKey || !Number.isFinite(Number(event.deltaY)) || Number(event.deltaY) >= 0) return false;
    if (!isActive(session) && !page(session, 'up')) return false;
    event.preventDefault(); event.stopImmediatePropagation(); return true;
  }
  function selection(session) {
    if (!current(session) || !isActive(session)) return '';
    const selected = window.getSelection?.();
    if (!selected || !selected.rangeCount || !text().contains(selected.anchorNode) || !text().contains(selected.focusNode)) return '';
    return String(selected);
  }
  window.PronTerminalHistory = {page, wheel, render, isActive, dispose: close, open, selection};
  $('#terminal-history').onclick = () => { if (terminalSession) void open(terminalSession, 'up'); };
  $('#terminal-history-close').onclick = () => { if (owner) close(owner); };
  $('#terminal-history-output').onclick = () => { if (owner && current(owner)) $('#terminal-output').click(); };
  text().addEventListener('scroll', () => { if (owner) render(owner); });
  text().addEventListener('keydown', event => {
    if (!owner || event.altKey || event.ctrlKey || event.metaKey) return;
    const intent = event.key === 'PageUp' ? 'up' : event.key === 'PageDown' ? 'down' : event.shiftKey && event.key === 'Home' ? 'top' : event.shiftKey && event.key === 'End' ? 'latest' : null;
    if (intent) { event.preventDefault(); event.stopPropagation(); navigate(owner, intent); }
  });
})();
