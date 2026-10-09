'use strict';
// A management conversation is not a worker. Keep its identity, scope, and
// display separate, while sharing the already-tested terminal rendering/input
// protections from ui.js. Viewing or hiding never ends a process.
(() => {
  const storageKey = 'pron-hub.management-operation.v1';
  const labels = {prepared: '已准备 · 等待启动', bootstrapping: '准备本机环境', initializing: '身份与工具初始化中', attaching: '恢复原管理会话中', ready: '可对话', needs_attention: '等待你在终端确认', disconnected: '连接断开 · 原会话保留', desktop_unverified: '桌面入口已打开 · 接管未核验', closed: '管理终端已结束 · 历史保留', failed: '初始化失败 · 待核对', unknown: '结果未知 · 不重复新建'};
  const model = {controller: null, controllers: [], operation: null, form: null, terminal: null, busy: false, checking: false, unsupported: false, loaded: false, error: '', directory: null, directoryGeneration: 0};
  try { model.operation = JSON.parse(window.sessionStorage?.getItem(storageKey) || 'null'); } catch (_) { /* Storage can be disabled; server prepare remains idempotent. */ }
  function persist() { try { if (model.operation) window.sessionStorage?.setItem(storageKey, JSON.stringify(model.operation)); else window.sessionStorage?.removeItem(storageKey); } catch (_) {} }
  const controllerOf = result => result?.controller || result;
  const scopedWorkers = () => state.agents.filter(agent => agent.active && !isClosedAgent(agent) && !isClosingAgent(agent) && agent.mode !== 'controller' && agent.kind !== 'controller');
  const connectedHosts = () => state.hosts.filter(host => host.connected);
  const errorText = error => typeof error === 'string' ? error : error?.message || error?.code || '';
  const displayError = (selector, text) => { $(selector).textContent = text; $(selector).hidden = !text; };
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {credentials: 'same-origin', cache: 'no-store'} : {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await response.json().catch(() => ({}));
    if (!response.ok) { const error = new Error(errorText(data.error) || `请求失败 (${response.status})`); error.status = response.status; error.code = data.code || data.error?.code; throw error; }
    return data;
  }
  function accept(result) {
    const next = controllerOf(result); if (!next?.id) throw new Error('没有返回管理会话身份。请核对原请求；不会新建第二条会话。');
    const previousOwner = model.controller?.input_owner;
    model.controller = next;
    if (model.terminal?.controllerId === next.id) {
      model.terminal.agent = next;
      if (previousOwner !== next.input_owner) { cancelPendingResize(model.terminal); model.terminal.frameGeneration++; model.terminal.forceSnapshot = true; }
      model.terminal.closed = next.state === 'closed';
      model.terminal.closing = next.state === 'closed' || ['closing', 'unknown'].includes(next.close_state);
      if (typeof next.connected === 'boolean') updateDisplayConnection(model.terminal, next.connected);
      model.terminal.term.options.disableStdin = !canWriteTerminal(model.terminal);
      if (!canReadDisplay(model.terminal)) { clearTimeout(model.terminal.timer); model.terminal.timer = null; }
    }
    render(); return next;
  }
  function isReady(c) { return c?.state === 'ready' && !!c.session_id && c.tools_ready === true; }
  function isTerminalCurrent(session) { return !!session && session === model.terminal && session.controllerId === model.controller?.id && session.terminalId === model.controller?.terminal_id && !session.disposed; }
  function isDisplayVisible(session) { return isTerminalCurrent(session) && !$('#controller-panel').hidden; }
  function canReadDisplay(session) { return isTerminalCurrent(session) && model.controller.input_owner === 'hub' && !['closed', 'unknown'].includes(model.controller.state); }
  function canWriteManager(session) { return isDisplayVisible(session) && model.controller.input_owner === 'hub' && !model.busy && model.controller.state !== 'desktop_unverified'; }
  function renderTerminalErrors(session) { if (isTerminalCurrent(session)) displayError('#controller-terminal-error', session.inputError || session.resizeError || session.pollError || ''); }
  function updateManagerConnection(session, connected) {
    if (!isTerminalCurrent(session)) return;
    session.displayConnected = connected;
    if (!connected) { cancelPendingResize(session); session.term.blur(); }
    session.term.options.disableStdin = !canWriteTerminal(session);
    $('#controller-paste').disabled = !canWriteTerminal(session);
    $('#controller-focus').disabled = !canWriteTerminal(session);
  }
  function render() {
    const c = model.controller;
    $('#controller-title').textContent = c?.title || '从一条管理会话，协调你的工作。';
    $('#controller-state').textContent = model.unsupported ? '后台尚未加载管理会话功能' : !model.loaded ? '正在检查管理会话' : !c ? '尚未创建管理会话' : c.state === 'ready' && !isReady(c) ? '身份或工具尚未核验' : labels[c.state] || '状态待核对';
    $('#controller-owner').textContent = c ? c.input_owner === 'hub' ? '当前入口：PRON Hub' : c.input_owner === 'desktop' ? '当前入口：Codex（待核验）' : '当前没有输入入口' : '';
    $('#controller-phase').textContent = c?.phase ? `阶段：${c.phase}` : '';
    $('#controller-scope-count').textContent = c ? `已授权 ${Number(c.scope_count ?? c.scope?.length ?? 0)} 个工作会话` : '只管理你明确授权的工作会话';
    $('#controller-primary').textContent = model.busy ? '正在处理原请求…' : c ? '继续管理' : '启动管理会话';
    $('#controller-primary').disabled = model.busy || model.unsupported || !model.loaded;
    $('#controller-name').disabled = !!model.form?.scopeOnly || model.busy;
    $('#controller-scope').disabled = model.busy || !c || model.unsupported;
    $('#controller-desktop').disabled = model.busy || !c?.session_id || c.input_owner !== 'hub' || !isReady(c);
    displayError('#controller-error', model.error || errorText(c?.error));
    if (model.unsupported) $('#controller-description').textContent = '当前后台尚不支持管理会话。已有工作终端仍可使用；请在空闲时按维护流程更新服务，不会自动重启或创建另一套后台。';
    else $('#controller-description').textContent = c ? '这是一条本机 Codex 的真实管理会话。工作 agent 保持原来的使用方式，你保留最终决策权。' : '在本机创建真实的 Codex 管理会话。PRON Hub 准备角色、工具和授权范围，你只需交代目标。';
    $('#controller-panel-title').textContent = c?.title || '管理会话';
    $('#controller-panel-note').textContent = c?.state === 'desktop_unverified' ? '已释放 Hub 输入入口并提供同一会话链接；尚未验证 Codex 桌面端已接管，Hub 不会继续写入。返回 Hub 时恢复准确的原 Session，不 fork 或重发任务。' : c?.state === 'needs_attention' ? '请在下面的真实终端完成登录、信任或权限确认。系统不会自动审批；完成后继续核验身份与管理工具。' : '这是本机 Codex 的真实终端。只初始化管理角色、工具和范围，首次不派发业务任务；“终端连接”不等于“管理工具已就绪”。';
    const details = $('#controller-details'); details.replaceChildren();
    if (c) for (const [label, value] of [['Session', c.session_id || '身份尚未核验'], ['管理工作区', c.workspace], ['终端 ID', c.terminal_id || '尚未创建'], ['工具握手', c.tools_ready ? '已核验' : '尚未核验'], ['桌面接管', c.desktop_verified ? '已核验' : '尚未核验']]) { details.append(node('dt', label), node('dd', value || '—')); }
    const term = model.terminal;
    if (term?.term.options) term.term.options.disableStdin = !canWriteTerminal(term);
    $('#controller-copy').disabled = !isCurrentTerminal(term);
    $('#controller-paste').disabled = !canWriteTerminal(term);
    $('#controller-focus').disabled = !canWriteTerminal(term);
    $('#controller-close').disabled = model.busy || !c?.terminal_id || c.input_owner !== 'hub' || c.state === 'closed';
  }
  async function refreshController() {
    if (model.checking || model.busy) return;
    model.checking = true;
    try {
      if (!model.controller) {
        const result = await api('/api/controllers'); const list = Array.isArray(result) ? result : result.controllers || [];
        model.controllers = list; const existing = list.find(item => item.id === model.operation?.id) || list[0];
        if (existing) accept(existing);
      } else accept(await api('/api/controllers/get?' + new URLSearchParams({id: model.controller.id})));
      const c = model.controller, op = model.operation;
      // A lost HTTP response can also be reconciled by the service's verified
      // ready state for this exact manager. Otherwise a successfully restored
      // session would keep its uncertain old key through the next lifecycle.
      if (c?.id === op?.id && isReady(c) && c.input_owner === 'hub' && c.connected === true) {
        if (op.kind === 'prepare' && !op.startAcknowledged) acknowledgeOperation('start', c, op.startKey);
        else if (['start', 'resume'].includes(op.kind) && !op.acknowledged) acknowledgeOperation(op.kind, c, op.requestKey);
      }
      if (!$('#controller-panel').hidden && model.controller?.terminal_id && (!model.terminal || model.terminal.terminalId !== model.controller.terminal_id)) await showTerminal(model.controller);
      model.unsupported = false; model.error = '';
    } catch (error) {
      model.unsupported = [404, 405, 501].includes(error.status);
      model.error = model.unsupported ? '当前服务未提供管理会话 API。已有工作终端不受影响。' : `检查管理会话失败：${error.message}。保留原操作，不重复创建。`;
    } finally { model.loaded = true; model.checking = false; render(); }
  }
  function openSettings(scopeOnly = false) {
    if (model.busy) return;
    const c = model.controller;
    model.form = {scopeOnly, selected: new Set(scopeOnly ? c.scope || [] : model.operation?.body?.agent_ids || []), hostRoots: structuredClone(scopeOnly ? c.allowed_hosts || {} : model.operation?.body?.host_roots || {}), submitted: false};
    $('#controller-name').value = scopeOnly ? c.title : model.operation?.body?.title || '我的管理会话'; $('#controller-name').disabled = scopeOnly;
    $('#controller-dialog-heading').textContent = scopeOnly ? '调整管理范围' : '启动管理会话'; $('#controller-save').textContent = scopeOnly ? '保存管理范围' : '准备并启动';
    displayError('#controller-dialog-error', ''); renderChoices(); $('#controller-dialog').showModal();
  }
  function renderChoices() {
    const form = model.form; if (!form) return;
    const workers = $('#controller-workers'); workers.replaceChildren();
    for (const agent of scopedWorkers()) {
      const id = agent.agent_id || `${agent.host}::${agent.thread_id}`, row = node('label', null, 'controller-choice'), checkbox = node('input');
      checkbox.type = 'checkbox'; checkbox.checked = form.selected.has(id); checkbox.disabled = form.submitted;
      checkbox.onchange = () => { if (checkbox.checked) form.selected.add(id); else form.selected.delete(id); };
      const copy = node('span', null, 'controller-choice-copy'); copy.append(node('strong', agent.alias || agent.title || shortId(agent.thread_id)), node('span', `${state.hosts.find(h => h.id === agent.host)?.label || agent.host} · ${agent.cwd || '未记录项目目录'}`)); row.append(checkbox, copy); workers.append(row);
    }
    if (!workers.childElementCount) empty(workers, '没有已激活的工作会话。可以先启动空范围管理者，稍后再授权。');
    // Preserve existing grants even if their worker is presently inactive. They
    // remain visible and removable, instead of disappearing on an unrelated refresh.
    for (const id of form.selected) if (!scopedWorkers().some(a => (a.agent_id || `${a.host}::${a.thread_id}`) === id)) {
      const row = node('label', null, 'controller-choice'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = true; checkbox.disabled = form.submitted; checkbox.onchange = () => { form.selected.delete(id); renderChoices(); }; row.append(checkbox, node('span', `${id} · 已授权，当前未激活`, 'controller-choice-copy')); workers.append(row);
    }
    const hosts = $('#controller-hosts'); hosts.replaceChildren();
    for (const host of connectedHosts()) {
      const roots = form.hostRoots[host.id] || [], row = node('div', null, 'controller-choice'), copy = node('span', null, 'controller-choice-copy'); copy.append(node('strong', host.label || host.id), node('span', roots.length ? roots.join('\n') : '未授权新建工作会话'));
      const browse = button(roots.length ? '修改目录' : '选择授权目录', 'subtle', async () => openHostDirectory(host.id)); browse.disabled = form.submitted;
      row.append(copy, browse);
      if (roots.length) { const remove = button('撤销', 'text-button', async () => { if (form.submitted) return; delete form.hostRoots[host.id]; renderChoices(); }); remove.disabled = form.submitted; row.append(remove); } hosts.append(row);
    }
    if (!hosts.childElementCount) empty(hosts, '当前没有已连接机器；新建工作会话默认禁止。');
    for (const [host, roots] of Object.entries(form.hostRoots)) if (!connectedHosts().some(h => h.id === host)) { const row = node('div', null, 'controller-choice'), remove = button('撤销', 'text-button', async () => { if (form.submitted) return; delete form.hostRoots[host]; renderChoices(); }); remove.disabled = form.submitted; row.append(node('span', `${host} · ${roots.join(', ')} · 当前未连接`, 'controller-choice-copy'), remove); hosts.append(row); }
  }
  async function openHostDirectory(host) {
    if (!model.form || model.form.submitted) return;
    model.directory = {host, form: model.form, view: null}; $('#controller-host-directory-label').textContent = state.hosts.find(h => h.id === host)?.label || host;
    $('#controller-host-directory-dialog').showModal(); await loadHostDirectory('');
  }
  async function loadHostDirectory(path) {
    const target = model.directory; if (!target) return; const generation = ++model.directoryGeneration;
    $('#controller-host-directory-select').disabled = true; $('#controller-host-directory-parent').disabled = true; $('#controller-host-directory-list').replaceChildren(); displayError('#controller-host-directory-error', '');
    try {
      const view = await api('/api/directories/list', {host: target.host, path});
      if (model.directory !== target || generation !== model.directoryGeneration || model.form !== target.form) return;
      target.view = view; $('#controller-host-directory-path').textContent = view.path; $('#controller-host-directory-parent').disabled = !view.parent; $('#controller-host-directory-select').disabled = !view.path;
      for (const directory of view.directories || []) $('#controller-host-directory-list').append(button(directory.name, 'directory-row', async () => loadHostDirectory(directory.path)));
      if (!$('#controller-host-directory-list').childElementCount) empty($('#controller-host-directory-list'), '没有子目录。可以授权当前目录。');
    } catch (error) { if (model.directory === target && generation === model.directoryGeneration) displayError('#controller-host-directory-error', error.message); }
  }
  function operationFor(kind, id) {
    const op = model.operation;
    if (kind === 'start' && op?.kind === 'prepare' && op.id === id && !op.startAcknowledged) return {kind, id, requestKey: op.startKey};
    if (op?.kind === kind && op.id === id && !op.acknowledged) return op;
    model.operation = {kind, id, requestKey: crypto.randomUUID()}; persist(); return model.operation;
  }
  function acknowledgeOperation(kind, c, key) {
    // A response lost in transit, or an explicitly unknown backend outcome,
    // retains the old key. An acknowledged lifecycle operation must not keep
    // shadowing a later user-requested close/disconnect -> resume cycle.
    if (c.state === 'unknown' || ['terminal_unknown', 'bootstrap_unknown'].includes(c.error?.code)) return;
    const op = model.operation;
    if (kind === 'start' && op?.kind === 'prepare' && op.id === c.id && op.startKey === key) op.startAcknowledged = true;
    else if (op?.kind === kind && op.id === c.id && op.requestKey === key) op.acknowledged = true;
    persist();
  }
  async function startOrResume(c) {
    if (model.busy) return;
    if (c.state === 'unknown') throw new Error('原管理操作结果未知；请先核查原连接，不会换键强制恢复或新建会话。');
    const knownUnlaunched = c.state === 'failed' && c.error?.code === 'codex_unavailable' && !c.session_id && !c.terminal_id;
    const actionName = c.state === 'prepared' || !c.terminal_id && !c.session_id ? 'start' : ['closed', 'disconnected', 'desktop_unverified'].includes(c.state) || c.input_owner !== 'hub' ? 'resume' : null;
    if (!actionName) { await showTerminal(c); return; }
    if (c.state === 'failed' && !c.session_id && !knownUnlaunched) throw new Error('启动结果尚未核对，不会用新操作强制创建另一条会话。请查看原状态与错误。');
    const op = operationFor(actionName, c.id); model.busy = true; render();
    try { c = accept(await api(`/api/controllers/${actionName}`, {id: c.id, request_key: op.requestKey})); acknowledgeOperation(actionName, c, op.requestKey); model.error = ''; await showTerminal(c); }
    catch (error) { model.error = `${error.message}。已保留原操作键；继续管理会先核对原会话，不重复派发任务。`; throw error; }
    finally { model.busy = false; render(); }
  }
  async function submitSettings() {
    const form = model.form; if (!form || model.busy) return;
    const body = {title: $('#controller-name').value.trim(), agent_ids: [...form.selected].sort(), host_roots: form.hostRoots};
    if (!body.title) throw new Error('请填写管理会话名称。');
    const fingerprint = JSON.stringify(body);
    if (!form.scopeOnly) {
      if (model.operation?.kind === 'prepare' && model.operation.fingerprint !== fingerprint) throw new Error('此前启动请求的结果尚未核对。请先使用原设置重试并核查，不会提交不同设置创建第二个管理者。');
      if (model.operation?.kind !== 'prepare') model.operation = {kind: 'prepare', body, fingerprint, requestKey: crypto.randomUUID(), startKey: crypto.randomUUID()};
      persist();
    }
    form.submitted = true; model.busy = true; $('#controller-save').disabled = true; renderChoices(); render();
    try {
      if (form.scopeOnly) accept(await api('/api/controllers/scope', {id: model.controller.id, agent_ids: body.agent_ids, host_roots: body.host_roots}));
      else {
        const op = model.operation; const prepared = accept(await api('/api/controllers/prepare', {...body, request_key: op.requestKey})); op.id = prepared.id; persist();
        const started = accept(await api('/api/controllers/start', {id: prepared.id, request_key: op.startKey})); acknowledgeOperation('start', started, op.startKey);
      }
      model.error = ''; $('#controller-dialog').close(); await showTerminal(model.controller);
    } catch (error) { displayError('#controller-dialog-error', `${error.message}。原请求已保留；不要以“新建另一条”绕过未知结果。`); throw error; }
    finally { form.submitted = false; model.busy = false; $('#controller-save').disabled = false; renderChoices(); render(); }
  }
  function disposeDisplay() {
    const session = model.terminal; if (!session) return;
    // There is intentionally no close, resume, or flush-on-destroy request.
    session.disposed = true; clearTimeout(session.timer); clearTimeout(session.inputTimer); clearTimeout(session.resizeTimer); session.observer?.disconnect(); session.term.dispose(); model.terminal = null; $('#controller-terminal').replaceChildren();
  }
  async function showTerminal(c = model.controller) {
    $('#controller-panel').hidden = false;
    if (!c?.terminal_id) { $('#controller-terminal-status').textContent = '尚未取得管理终端，继续核对原启动状态'; render(); return; }
    if (model.terminal?.controllerId === c.id && model.terminal?.terminalId === c.terminal_id) { render(); requestAnimationFrame(() => safeFit(model.terminal)); if (!model.terminal.timer && canReadDisplay(model.terminal)) { model.terminal.forceSnapshot = true; await pollTerminal(model.terminal); } return; }
    disposeDisplay();
    if (!window.Terminal || !window.FitAddon?.FitAddon) throw new Error('终端组件未加载，请刷新页面。');
    const term = new window.Terminal({fontFamily: 'Cascadia Mono, Consolas, monospace', fontSize: 13, lineHeight: 1.15, cursorBlink: true, scrollback: 5000, convertEol: false, allowProposedApi: true, theme: {background: '#101517', foreground: '#dde7e0', cursor: '#b9e9c7', selectionBackground: '#45634f99'}});
    for (const identifier of [{final: 'n'}, {prefix: '?', final: 'n'}, {final: 'c'}, {prefix: '>', final: 'c'}, {final: 't'}]) term.parser.registerCsiHandler(identifier, () => true);
    for (const identifier of [{intermediates: '$', final: 'q'}, {intermediates: '+', final: 'q'}]) term.parser.registerDcsHandler(identifier, () => true);
    for (const identifier of [4, 10, 11, 12]) term.parser.registerOscHandler(identifier, data => data.split(';').includes('?'));
    const fit = new window.FitAddon.FitAddon(); term.loadAddon(fit); term.open($('#controller-terminal'));
    const session = {kind: 'controller', controllerId: c.id, terminalId: c.terminal_id, key: c.id, agent: c, term, fit, seq: 0, initialized: false, disposed: false, displayConnected: c.connected !== false, closing: false, closed: c.state === 'closed', pasteReading: false, wheelRemainder: 0, wheelBuffer: null, frameGeneration: 0, forceSnapshot: true, writingFrame: false, restoringFrame: false, fitPending: false, resizeGeneration: 0, resizePending: false, targetSize: '', timer: null, inputTimer: null, resizeTimer: null, input: '', inputChain: Promise.resolve(), size: '', observer: null, inputError: '', pollError: '', resizeError: ''};
    model.terminal = session;
    term.attachCustomKeyEventHandler(event => handleTerminalKey(session, event)); term.attachCustomWheelEventHandler?.(() => false);
    term.onData(data => { if (!canWriteTerminal(session)) return; session.input += data; if (!session.inputTimer) session.inputTimer = setTimeout(() => flushTerminalInput(session), 12); });
    term.onResize(({cols, rows}) => scheduleResize(session, cols, rows));
    session.observer = new ResizeObserver(() => { if (!session.disposed && !$('#controller-panel').hidden && $('#controller-terminal').clientWidth > 0) safeFit(session); }); session.observer.observe($('#controller-terminal'));
    await pollTerminal(session); render();
    // Never focus on poll/initialization completion: user may now be typing in a worker.
  }
  function openDesktop() {
    $('#controller-desktop-confirm').checked = false; $('#controller-desktop-link').hidden = true; $('#controller-desktop-submit').hidden = false; displayError('#controller-desktop-error', ''); $('#controller-desktop-dialog').showModal();
  }
  async function submitDesktop() {
    const c = model.controller, session = model.terminal;
    if (!$('#controller-desktop-confirm').checked) throw new Error('请先确认管理会话空闲且没有输入草稿。');
    if (!c?.session_id || !isReady(c) || c.input_owner !== 'hub') throw new Error('管理身份、工具或当前输入入口尚未核验，不能移交。');
    if (session?.input || session?.inputTimer) throw new Error('仍有待送达的终端输入，请先核对原管理终端。');
    model.busy = true; render();
    try {
      await session?.inputChain;
      const result = await api('/api/controllers/desktop', {id: c.id, confirm: true});
      if (typeof result.url !== 'string' || result.url !== `codex://threads/${c.session_id}`) throw new Error('后台没有返回准确原会话链接，已停止打开操作。');
      accept(result); const link = $('#controller-desktop-link'); link.href = result.url; link.hidden = false;
      $('#controller-desktop-submit').hidden = true;
      displayError('#controller-desktop-error', 'Hub 输入入口已释放。点击链接打开同一会话；桌面接管与工具可用性尚未核验。');
    } finally { model.busy = false; render(); }
  }
  async function closeManager() {
    const c = model.controller; if (!c || model.busy) return;
    model.busy = true; render();
    const session = model.terminal;
    if (session) { session.closing = true; clearTimeout(session.inputTimer); session.inputTimer = null; session.input = ''; cancelPendingResize(session); session.term.blur(); }
    try { await session?.inputChain; accept(await api('/api/controllers/close', {id: c.id, confirm: true})); $('#controller-close-dialog').close(); }
    finally { model.busy = false; render(); }
  }
  async function primary() {
    if (!model.loaded || model.unsupported || model.busy) return;
    if (!model.controller) openSettings(); else { await refreshController(); await startOrResume(model.controller); }
  }
  window.PronController = {isTerminalCurrent, isDisplayVisible, canReadDisplay, canWriteTerminal: canWriteManager, renderTerminalErrors, updateDisplayConnection: updateManagerConnection, render, refresh: refreshController,
    // Exported as a narrow inert-UI test surface, not an authorization boundary.
    model, accept, openSettings, submitSettings, primary, showTerminal, startOrResume, submitDesktop, closeManager, disposeDisplay, loadHostDirectory};
  $('#controller-primary').onclick = () => action($('#controller-primary'), primary);
  $('#controller-scope').onclick = () => openSettings(true);
  $('#controller-form').onsubmit = event => { event.preventDefault(); void action($('#controller-save'), submitSettings); };
  $('#controller-desktop').onclick = openDesktop;
  $('#controller-desktop-form').onsubmit = event => { event.preventDefault(); void action($('#controller-desktop-submit'), async () => { try { await submitDesktop(); } catch (error) { displayError('#controller-desktop-error', error.message); throw error; } }); };
  $('#controller-close').onclick = () => { displayError('#controller-close-error', ''); $('#controller-close-dialog').showModal(); };
  $('#controller-close-form').onsubmit = event => { event.preventDefault(); void action($('#controller-close-confirm'), async () => { try { await closeManager(); } catch (error) { displayError('#controller-close-error', error.message); throw error; } }); };
  $('#controller-hide').onclick = () => { $('#controller-panel').hidden = true; if (model.terminal) { cancelPendingResize(model.terminal); model.terminal.term.blur(); model.terminal.term.options.disableStdin = true; } render(); };
  $('#controller-focus').onclick = () => { if (canWriteTerminal(model.terminal)) model.terminal.term.focus(); };
  $('#controller-copy').onclick = () => action($('#controller-copy'), () => copyTerminal(model.terminal));
  $('#controller-paste').onclick = () => action($('#controller-paste'), () => pasteTerminal(model.terminal));
  $('#controller-terminal').addEventListener('wheel', event => handleTerminalWheel(model.terminal, event), {capture: true, passive: false});
  $('#controller-host-directory-home').onclick = () => void loadHostDirectory('');
  $('#controller-host-directory-parent').onclick = () => { if (model.directory?.view?.parent) void loadHostDirectory(model.directory.view.parent); };
  $('#controller-host-directory-select').onclick = () => { const target = model.directory; if (target?.view?.path && target.form === model.form) { model.form.hostRoots[target.host] = [target.view.path]; $('#controller-host-directory-dialog').close(); renderChoices(); } };
  $('#controller-host-directory-dialog').addEventListener('close', () => { model.directory = null; model.directoryGeneration++; });
  $('#controller-dialog').addEventListener('close', () => { if (!model.busy) model.form = null; });
  $('#controller-dialog').addEventListener('cancel', event => { if (model.busy) event.preventDefault(); });
  // No unload close/flush: a browser leaving the page is not authority to send
  // a pending draft or terminate the manager (and never touches workers).
  render(); void refreshController(); setInterval(refreshController, 2500);
})();
