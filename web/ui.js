'use strict';
const $ = selector => document.querySelector(selector);
const node = (tag, text, className) => { const element = document.createElement(tag); if (text != null) element.textContent = String(text); if (className) element.className = className; return element; };
const state = {hosts: [], agents: [], outbox: []};
const activity = new Map();
let selectedHost = null, selectedAgent = null, createTarget = null, discoveryHost = null;
let candidates = [], cursor = 0, historyGeneration = 0, toastTimer, refreshing = false, refreshTask = null;
let terminalSession = null;
let outputView = null, outputGeneration = 0, bindOutputTarget = null;
let directoryTarget = null, directoryGeneration = 0, closeTarget = null, clipboardTarget = null, contextTerminal = null;
const rendered = {hosts: '', agents: '', outbox: '', detail: ''};
const workbenchStorageKey = 'pron-hub.worker-workbench.v1';
let workbenchSaved = {};
try { workbenchSaved = JSON.parse(window.localStorage?.getItem(workbenchStorageKey) || '{}') || {}; } catch (_) {}
const agentFilters = {host: typeof workbenchSaved.host === 'string' ? workbenchSaved.host : '', query: typeof workbenchSaved.query === 'string' ? workbenchSaved.query : '', status: ['working', 'all', 'connected', 'attention', 'closed'].includes(workbenchSaved.status) ? workbenchSaved.status : 'working', active: !!workbenchSaved.active};
let restoreAgentKey = agentKeyFromHash() || (typeof workbenchSaved.agent === 'string' ? workbenchSaved.agent : null), renameTarget = null;
const reconnectOperations = new Map(Array.isArray(workbenchSaved.reconnect) ? workbenchSaved.reconnect.filter(entry => Array.isArray(entry) && typeof entry[0] === 'string' && typeof entry[1]?.requestKey === 'string') : []);
const agentRecoveryNotices = new Map();
const deliveryLabels = {queued: '已排队', waiting_for_agent: '等待在终端中启动工作 agent', human_busy: '等待你完成当前终端输入', blocked: '等待处理终端中的登录或审批', not_sent: '未发送', submitting: '输入提交中', submitted: '已写入终端', delivered: '已写入终端', completed: '该轮已结束', failed: '送达失败', interrupted: '已中断', scope_revoked: '已取消 · CEO 激活已撤销', unknown: '送达结果不确定 · 请核对终端', awaiting_connection: '等待终端连接'};
const statusLabels = {shell: 'Shell 已就绪', running: '终端运行中', connected: '终端已连接', disconnected: '显示连接已断开', not_connected: '尚未连接终端', exited: '显示连接已结束', closed: '终端已关闭', closing: '正在关闭终端', close_unknown: '关闭结果未确认', error: '连接错误', unknown: '终端状态未知'};
const disconnectedLabel = '显示连接已断开 · 工作进程未确认';
const agentKey = agent => `${agent.host}:${agent.thread_id}`;
const currentAgent = () => state.agents.find(agent => agentKey(agent) === selectedAgent);
const currentHost = () => state.hosts.find(host => host.id === selectedHost);
const isTerminal = agent => agent?.mode === 'terminal' || !!agent?.terminal_id;
const isClosedAgent = agent => !!agent && (agent.closed || agent.lifecycle === 'closed' || agent.status === 'closed' || agent.state === 'closed');
const isClosingAgent = agent => !!agent && (['closing', 'unknown'].includes(agent.close_state) || ['closing', 'close_unknown'].includes(agent.status) || ['closing', 'close_unknown'].includes(agent.state));
const sessionConnected = session => typeof session?.displayConnected === 'boolean' ? session.displayConnected : session?.agent?.connected !== false;
function displayDisconnected(agent) {
  const session = terminalSession?.key === agentKey(agent) && !terminalSession.disposed ? terminalSession : null;
  return session ? !sessionConnected(session) : agent.connected === false || ['exited', 'disconnected', 'not_connected'].includes(agent.status || agent.state);
}
function hostDisplayStatus(host) {
  if (host.transport !== 'ssh') return {connected: !!host.connected, label: host.connected ? '本机已连接' : '本机未连接'};
  const workers = state.agents.filter(agent => agent.host === host.id && isTerminal(agent) && !isClosedAgent(agent) && !isClosingAgent(agent));
  const count = workers.filter(agent => !displayDisconnected(agent)).length;
  return {connected: count > 0, label: workers.length ? `显示连接 ${count} / ${workers.length}` : host.connected ? '上次 SSH 检查成功' : 'SSH 尚未验证'};
}
const shortId = id => id ? String(id).slice(0, 8) + '…' + String(id).slice(-6) : '';
const resumeCommand = id => `codex resume ${id}`;
function agentKeyFromHash() { try { return new URLSearchParams(String(window.location?.hash || '').replace(/^#/, '')).get('agent'); } catch (_) { return null; } }
function persistWorkbench() {
  try { window.localStorage?.setItem(workbenchStorageKey, JSON.stringify({...agentFilters, agent: selectedAgent || restoreAgentKey, selectedHost, reconnect: [...reconnectOperations]})); } catch (_) {}
}
function updateAgentLocation() {
  if (!window.history?.replaceState || !window.location) return;
  const params = new URLSearchParams(String(window.location.hash || '').replace(/^#/, ''));
  if (selectedAgent) params.set('agent', selectedAgent); else params.delete('agent');
  window.history.replaceState(null, '', (window.location.pathname || '/') + (window.location.search || '') + (params.toString() ? '#' + params : ''));
}
function registeredAgent(key) { return state.agents.find(agent => agentKey(agent) === key || agent.agent_id === key || `${agent.host}::${agent.thread_id}` === key); }
function agentNeedsAttention(agent) { return !isClosedAgent(agent) && (!!agent.management?.needs_attention || isClosingAgent(agent) || displayDisconnected(agent) || !!agent.blocked_reason || !!agent.error || ['setup_menu', 'approval', 'login', 'error', 'unknown'].includes(agent.state || agent.status) || !isTerminal(agent)); }
function agentManagerGrant(agent) { const manager = window.PronController?.model?.controller; if (!manager) return null; const scope = manager.scope || manager.agent_ids || []; return scope.includes(agent.agent_id || `${agent.host}::${agent.thread_id}`); }
function filteredAgents() {
  const query = agentFilters.query.trim().toLocaleLowerCase();
  return state.agents.filter(agent => (!agentFilters.host || agent.host === agentFilters.host)
    && (!query || [agent.alias, agent.title, agent.cwd, agent.host_label, state.hosts.find(host => host.id === agent.host)?.label, agent.host].some(value => String(value || '').toLocaleLowerCase().includes(query)))
    && (!agentFilters.active || agent.active)
    && (agentFilters.status === 'closed' ? isClosedAgent(agent) : !isClosedAgent(agent))
    && (agentFilters.status !== 'working' || agent.active || agent.pinned || isTerminal(agent) && !isClosingAgent(agent) && !displayDisconnected(agent))
    && (agentFilters.status !== 'connected' || isTerminal(agent) && !isClosingAgent(agent) && !displayDisconnected(agent))
    && (agentFilters.status !== 'attention' || agentNeedsAttention(agent)))
    .sort((a, b) => Number(!!b.pinned) - Number(!!a.pinned) || String(a.alias || a.title || a.thread_id).localeCompare(String(b.alias || b.title || b.thread_id), 'zh-CN') || agentKey(a).localeCompare(agentKey(b)));
}
function renderWorkbenchFilters() {
  const select = $('#agent-host-filter'); select.replaceChildren();
  const all = node('option', '全部机器'); all.value = ''; select.append(all);
  for (const host of state.hosts) { const option = node('option', host.label || host.id); option.value = host.id; select.append(option); }
  select.value = agentFilters.host; $('#agent-status-filter').value = agentFilters.status;
  $('#agent-search').value = agentFilters.query; $('#active-only').checked = agentFilters.active;
  $('#all-machines').classList.toggle('selected', !agentFilters.host);
}
function dateText(value) { if (!value) return ''; const date = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value); return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN'); }
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 7500); }
async function request(path, body) {
  const response = await fetch(path, body === undefined ? {credentials: 'same-origin', cache: 'no-store'} : {method: 'POST', credentials: 'same-origin', keepalive: path === '/api/terminals/input', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({})); if (!response.ok) { const error = new Error(typeof data.error === 'string' ? data.error : data.error?.message || `请求失败 (${response.status})`); error.status = response.status; error.code = data.code || data.error?.code; error.recovery = data.recovery; throw error; } return data;
}
async function action(element, task) { element.disabled = true; try { await task(); } catch (error) { toast(error.message); } finally { element.disabled = false; if (['terminal-connect', 'terminal-copy', 'terminal-paste'].includes(element.id)) renderDetail(); } }
function button(text, className, onClick) { const element = node('button', text, className); element.type = 'button'; element.onclick = () => action(element, onClick); return element; }
function empty(parent, text) { parent.append(node('p', text, 'empty-copy')); }
function inline(parent, text) { for (const part of String(text).split(/(`[^`]+`|\*\*[^*]+\*\*)/g)) { if (part.startsWith('`') && part.endsWith('`')) parent.append(node('code', part.slice(1, -1))); else if (part.startsWith('**') && part.endsWith('**')) parent.append(node('strong', part.slice(2, -2))); else parent.append(document.createTextNode(part)); } }
function formatted(parent, text) { let code = null; for (const line of String(text).split('\n')) { if (line.trim().startsWith('```')) { if (code) { parent.append(code); code = null; } else code = node('pre', '', 'code-block'); } else if (code) code.textContent += line + '\n'; else { const paragraph = node('div', null, 'prose-line'); inline(paragraph, line); parent.append(paragraph); } } if (code) parent.append(code); }
function renderHosts() {
  const list = $('#hosts'); list.replaceChildren(); if (!state.hosts.length) empty(list, '尚未配置机器。点击 + 添加本机或 SSH 服务器。');
  for (const host of state.hosts) {
    const display = hostDisplayStatus(host);
    const card = node('div', null, 'host-card' + (host.id === agentFilters.host ? ' selected' : '')); const select = button('', 'host-select', async () => selectHost(host.id));
    const name = node('span', null, 'host-name'); name.append(node('span', '', 'connection-dot' + (display.connected ? ' connected' : '')), node('strong', host.label || host.id));
    select.append(name, node('span', host.transport === 'local' ? '本机' : (host.target || host.targetAlias || 'SSH'), 'host-target')); card.append(select);
    const controls = node('div', null, 'host-controls'); controls.append(node('span', display.label, 'muted'), button('连接', 'text-button', async () => {
      const result = await request('/api/hosts/connect', {host: host.id}); await refresh(true);
      if (result.connected === false) throw new Error(result.error || '机器连接失败。请检查 SSH 配置。');
      toast(`${host.label || host.id} 已连接，可以打开工作终端。`);
    }), button('配置', 'text-button', async () => openMachine(host)));
    card.append(controls); if (host.error) card.append(node('p', host.error, 'host-error')); list.append(card);
  }
  $('#new-agent').disabled = !selectedHost; $('#discover-button').disabled = !selectedHost;
  $('#machine-caption').textContent = agentFilters.host ? `仅看：${state.hosts.find(host => host.id === agentFilters.host)?.label || agentFilters.host}` : '跨机器查看 · 新终端使用当前选定机器';
  renderWorkbenchFilters();
}
function selectHost(hostId) {
  selectedHost = hostId; agentFilters.host = hostId;
  candidates = []; discoveryHost = null; $('#candidates-section').hidden = true;
  persistWorkbench(); renderHosts(); renderAgents(true); renderDetail();
}
async function selectAgent(agent, options = {}) {
  if (selectedAgent === agentKey(agent)) { if (options.focus && terminalSession) terminalSession.term.focus(); return; }
  selectedAgent = agentKey(agent); selectedHost = agentFilters.host || agent.host; restoreAgentKey = null; historyGeneration++; resetConversationOutput(); disposeTerminal(); persistWorkbench(); updateAgentLocation(); renderHosts(); renderAgents(true); renderDetail();
  if (isTerminal(agent)) await openTerminal(agent, {focus: !!options.focus}); else if (!options.restore) await readHistory();
}
function renderAgents(force = false) {
  // A live registry update should not destroy a name being edited or steal its
  // focus. Explicit filter/selection actions may redraw from the retained draft.
  if (renameTarget?.rendered && !force) return;
  const list = $('#agents'); list.replaceChildren(); const matches = filteredAgents();
  $('#registry-count').textContent = String(matches.length); $('#active-count').textContent = String(state.agents.filter(agent => agent.active).length);
  const closedCount = state.agents.filter(isClosedAgent).length;
  $('#agent-count-caption').textContent = `显示 ${matches.length} / ${state.agents.length} 个登记${agentFilters.status !== 'closed' && closedCount ? ` · ${closedCount} 个已关闭记录默认收起` : ''} · 连接状态不是任务完成状态`;
  if (!matches.length) {
    const copy = !state.agents.length ? '还没有登记工作会话。选择机器打开新终端，再按平常方式启动 Codex。' : agentFilters.status === 'closed' ? '没有符合筛选的已关闭记录。' : '没有符合筛选的工作会话；调整机器、名称或状态，已关闭记录可单独查看。';
    empty(list, copy);
    if (state.agents.length) list.append(button('清除筛选，查看工作会话', 'subtle agent-clear-filter', async () => clearAgentFilters()));
  }
  for (const agent of matches) {
    const terminal = isTerminal(agent), card = node('div', null, 'agent-card workbench-agent-card' + (agentKey(agent) === selectedAgent ? ' selected' : '') + (agent.pinned ? ' pinned' : ''));
    const heading = node('div', null, 'agent-card-topline'); heading.append(node('span', agent.host_label || state.hosts.find(host => host.id === agent.host)?.label || agent.host, 'agent-host-label'));
    const pin = button(agent.pinned ? '★' : '☆', 'agent-pin-button' + (agent.pinned ? ' pinned' : ''), async () => updateAgent(agent, {pinned: !agent.pinned})); pin.setAttribute('aria-label', `${agent.pinned ? '取消置顶' : '置顶'} ${agent.alias || agent.title || shortId(agent.thread_id)}`); pin.setAttribute('aria-pressed', String(!!agent.pinned)); heading.append(pin); card.append(heading);
    const open = button('', 'agent-select', async () => selectAgent(agent));
    const status = terminal ? isClosedAgent(agent) || isClosingAgent(agent) ? statusLabels[agent.status || agent.state] || '终端已暂停' : displayDisconnected(agent) ? disconnectedLabel : agent.command ? `显示连接已连接 · ${agent.command}` : '显示连接已连接' : '尚未连接终端';
    open.append(node('strong', agent.alias || agent.title || shortId(agent.thread_id)), node('span', status, 'agent-meta'), node('span', agent.cwd || shortId(agent.thread_id), 'agent-id')); card.append(open);
    if (agent.management?.label) card.append(node('span', agent.management.label, 'agent-management-label' + (agentNeedsAttention(agent) ? ' attention' : '')));
    const grant = agentManagerGrant(agent), grantRow = node('div', null, 'agent-scope-row');
    grantRow.append(node('span', grant === true ? '当前主控已授权' : grant === false ? '当前主控未授权' : '管理会话范围尚未设置', 'badge' + (grant ? ' active-badge' : '')));
    if (grant !== null && window.PronController?.openSettings) grantRow.append(button('管理范围', 'text-button', async () => window.PronController.openSettings(true))); card.append(grantRow);
    const quick = node('div', null, 'agent-quick-actions');
    quick.append(button(terminal ? '打开终端' : '查看记录', 'subtle', async () => selectAgent(agent)));
    if (terminal) quick.append(button('会话输出', 'subtle', async () => { await selectAgent(agent); if (selectedAgent === agentKey(agent)) await readConversationOutput(); }));
    if (terminal && !isClosedAgent(agent) && !isClosingAgent(agent) && displayDisconnected(agent) && agent.management?.primary_action !== 'none') quick.append(button('重连原终端', 'agent-reconnect-button', async () => { await selectAgent(agent); if (selectedAgent === agentKey(agent)) await reconnectTerminal(); }));
    card.append(quick);
    const more = node('details', null, 'agent-more'), summary = node('summary', '更多'); more.append(summary);
    const moreActions = node('div', null, 'agent-more-actions'); moreActions.append(button('重命名', 'text-button', async () => openAgentRename(agent)), button('复制直达链接', 'text-button', async () => copyAgentLink(agent)));
    if (terminal && !isClosedAgent(agent) && !isClosingAgent(agent)) moreActions.append(button('关闭终端…', 'text-button agent-close-link', async () => { await selectAgent(agent); if (selectedAgent === agentKey(agent)) openCloseTerminal(); })); more.append(moreActions); card.append(more);
    if (renameTarget?.key === agentKey(agent)) {
      const form = node('form', null, 'agent-inline-rename'), input = node('input'); input.value = renameTarget.value; input.maxLength = 200; input.required = true; input.setAttribute('aria-label', '工作会话新名称'); input.disabled = renameTarget.saving;
      input.oninput = () => { renameTarget.value = input.value; };
      const save = node('button', '保存'); save.type = 'submit'; save.disabled = renameTarget.saving;
      form.onsubmit = event => { event.preventDefault(); void action(save, () => saveAgentRename(agent)); };
      form.append(input, save, button('取消', 'text-button', async () => { renameTarget = null; renderAgents(true); })); card.append(form); renameTarget.rendered = true;
      if (renameTarget.focus) { renameTarget.focus = false; requestAnimationFrame(() => input.focus()); }
    }
    if (terminal) {
      const label = node('label', null, 'activation'), checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = !!agent.active;
      checkbox.disabled = isClosedAgent(agent) || isClosingAgent(agent);
      checkbox.setAttribute('aria-label', `允许 ${agent.alias || agent.thread_id} 进入管理授权名单`);
      checkbox.onchange = async () => { checkbox.disabled = true; try { await request('/api/agents/activate', {host: agent.host, thread_id: agent.thread_id, active: checkbox.checked}); await refresh(true); } catch (error) { checkbox.checked = !!agent.active; toast(error.message); } finally { checkbox.disabled = false; } };
      const activationLabel = isClosedAgent(agent) ? '已关闭 · 保留记录' : isClosingAgent(agent) ? '已暂停 · 待核对关闭结果' : agent.active ? '可供 CEO 管理 · 旧插件仍全局' : '仅直接使用 · 未加入管理名单';
      label.append(checkbox, node('span', activationLabel)); card.append(label);
    } else card.append(button('在终端中继续', 'text-button history-open', async () => openCreate(agent, agent.host)));
    list.append(card);
  }
}
function clearAgentFilters() { agentFilters.host = ''; agentFilters.query = ''; agentFilters.status = 'working'; agentFilters.active = false; persistWorkbench(); renderHosts(); renderAgents(true); }
async function updateAgent(agent, fields) { const result = await request('/api/agents/update', {host: agent.host, thread_id: agent.thread_id, ...fields}); const updated = result.agent || result; if (updated.host === agent.host && updated.thread_id === agent.thread_id) Object.assign(agent, updated); await refresh(true); renderAgents(true); renderDetail(); }
function openAgentRename(agent) { renameTarget = {key: agentKey(agent), value: agent.alias || agent.title || '', focus: true, saving: false, rendered: false}; renderAgents(true); }
async function saveAgentRename(agent) {
  const target = renameTarget; if (!target || target.key !== agentKey(agent) || target.saving) return;
  const alias = target.value.trim(); if (!alias || alias.length > 200) throw new Error('名称不能为空，最多 200 个字符。');
  target.saving = true;
  try { await updateAgent(agent, {alias}); if (renameTarget === target) { renameTarget = null; renderAgents(true); } }
  finally { target.saving = false; }
}
async function copyAgentLink(agent) {
  if (!window.location?.origin) throw new Error('当前浏览器地址不可用。');
  const url = window.location.origin + (window.location.pathname || '/') + '#agent=' + encodeURIComponent(agentKey(agent));
  if (!navigator.clipboard?.writeText) throw new Error('浏览器未允许复制链接，请使用地址栏中的直达地址。');
  await navigator.clipboard.writeText(url); toast('工作会话直达链接已复制；打开链接只显示原对象，不会启动或接管。');
}
function renderCandidates() {
  $('#candidates-section').hidden = false; const list = $('#candidates'); list.replaceChildren();
  if (!candidates.length) empty(list, '没有找到匹配的对话。试试更短的标题或项目目录。');
  for (const thread of candidates) {
    const id = thread.id || thread.thread_id, card = node('div', null, 'candidate-card');
    card.append(node('strong', thread.title || thread.name || shortId(id)), node('span', thread.cwd || '项目目录未记录', 'candidate-path'), node('span', `${shortId(id)}${thread.updated_at ? ' · ' + dateText(thread.updated_at) : ''}`, 'agent-id'));
    const existing = state.agents.find(agent => agent.host === discoveryHost && agent.thread_id === id);
    card.append(button(isTerminal(existing) ? '打开终端' : '在终端中继续', 'candidate-action', async () => { if (isTerminal(existing)) await selectAgent(existing); else openCreate(thread, discoveryHost); })); list.append(card);
  }
}
function renderDetail() {
  const agent = currentAgent(); renderAgentRecoveryNotice(agent); $('#detail-empty').hidden = !!agent; $('#agent-detail').hidden = !agent; if (!agent) return;
  if (outputView?.key === selectedAgent && outputView.terminalId !== agent.terminal_id) resetConversationOutput();
  const terminal = isTerminal(agent), host = state.hosts.find(item => item.id === agent.host);
  $('#detail-host').textContent = host?.label || agent.host; $('#detail-name').textContent = agent.alias || agent.title || shortId(agent.thread_id);
  const meta = $('#detail-meta'); meta.replaceChildren();
  meta.append(node('span', terminal ? '共享终端' : '对话历史', 'badge'), node('span', agent.active && terminal ? 'CEO 已激活' : 'CEO 未激活', 'badge' + (agent.active && terminal ? ' active-badge' : '')));
  if (agent.cwd) meta.append(node('span', agent.cwd, 'muted')); meta.append(node('code', shortId(agent.terminal_id || agent.thread_id), 'thread-id'));
  $('#terminal-panel').hidden = !terminal; $('#legacy-panel').hidden = terminal; $('#refresh-history').hidden = terminal; $('#terminal-focus').hidden = !terminal; $('#terminal-connect').hidden = !terminal;
  const closed = isClosedAgent(agent), closing = isClosingAgent(agent) || !!(terminalSession?.key === selectedAgent && terminalSession.closing), disconnected = terminal && displayDisconnected(agent);
  for (const id of ['terminal-focus', 'terminal-connect', 'terminal-copy', 'terminal-paste', 'terminal-close']) $(`#${id}`).disabled = closed || closing;
  if (disconnected) { $('#terminal-focus').disabled = true; $('#terminal-paste').disabled = true; }
  if (terminalSession?.key === selectedAgent) { terminalSession.closed = closed; if (closed || closing) { terminalSession.closing = true; cancelPendingResize(terminalSession); terminalSession.term.blur(); } }
  $('#terminal-help').textContent = closed ? '终端已关闭，保留的输出仍可查看。需要继续工作时请新建终端；这个已关闭的进程不会重新启动。' : closing ? '终端正在关闭或关闭结果尚未确认，输入与重连已暂停。保留的输出仍可查看，请先核对进程状态。' : disconnected ? '显示连接已断开，输入已暂停。保留画面仍可查看和复制；这不能证明远端工作进程退出。可以点击「重新连接」接回原终端，会话和进程是否仍在需重新核对。' : '选中文字后 Ctrl+C 复制，无选中时 Ctrl+C 中断。Ctrl+V 粘贴，也可用 Ctrl+Shift+C/V 或右键菜单。关闭页面会保留任务；「关闭终端」会结束其中的工作进程。';
  $('#terminal-connect').textContent = disconnected ? '重新连接' : '刷新画面';
  const sourceId = agent.resume_thread_id || agent.source_thread_id || (terminal && /^[0-9a-f]{8}-[0-9a-f-]+$/i.test(agent.thread_id) ? agent.thread_id : null);
  const runningAgent = agent.agent_ready === true || agent.state === 'agent' || agent.status === 'agent';
  $('#resume-guide').hidden = !sourceId || !terminal || disconnected || runningAgent;
  if (sourceId) $('#resume-command').textContent = resumeCommand(sourceId);
  if (terminal && closed) $('#terminal-status').textContent = '终端已关闭';
  else if (terminal && disconnected && !closing) $('#terminal-status').textContent = disconnectedLabel;
  else if (terminal && (!terminalSession || terminalSession.key !== selectedAgent)) $('#terminal-status').textContent = agent.connected ? '正在读取终端画面……' : '终端尚未连接，点击重新连接';
  $('#terminal-output').disabled = !terminal;
  $('#terminal-bind-output').disabled = !terminal;
  if (outputView?.key === selectedAgent && outputView.open) renderOutputBinding(agent);
}
function renderAgentRecoveryNotice(agent = currentAgent()) {
  const message = agent ? agentRecoveryNotices.get(agentKey(agent)) || agent.recovery?.message || '' : '';
  $('#agent-recovery-notice').textContent = message; $('#agent-recovery-notice').hidden = !message;
}
function showTerminalError(text) { $('#terminal-error').textContent = text; $('#terminal-error').hidden = !text; }
function renderTerminalErrors(session) {
  if (session.kind === 'controller') { window.PronController?.renderTerminalErrors(session); return; }
  if (session.disposed || terminalSession !== session) return;
  showTerminalError(session.inputError || session.resizeError || session.pollError || '');
}
function disposeTerminal() {
  if (!terminalSession) return;
  if (terminalSession.input) flushTerminalInput(terminalSession);
  terminalSession.disposed = true; clearTimeout(terminalSession.timer); clearTimeout(terminalSession.inputTimer); clearTimeout(terminalSession.resizeTimer);
  terminalSession.observer.disconnect(); terminalSession.term.dispose(); terminalSession = null; $('#terminal').replaceChildren(); showTerminalError('');
  hideTerminalContext();
}
function isCurrentTerminal(session) {
  if (session?.kind === 'controller') return !!window.PronController?.isTerminalCurrent(session) && session.initialized && !session.closing && !session.closed;
  return !!session && terminalSession === session && selectedAgent === session.key && !session.disposed && !session.closing && !session.closed && session.initialized;
}
function isTerminalDisplayCurrent(session) { return session?.kind === 'controller' ? !!window.PronController?.isTerminalCurrent(session) : terminalSession === session; }
function canWriteTerminal(session) { return isCurrentTerminal(session) && sessionConnected(session) && (session.kind !== 'controller' || window.PronController?.canWriteTerminal(session)); }
function terminalApiPath(session, operation) { return `/api/${session.kind === 'controller' ? 'controllers' : 'terminals'}/${operation}`; }
function terminalApiIdentity(session) { return session.kind === 'controller' ? {id: session.controllerId} : {host: session.agent.host, thread_id: session.agent.thread_id}; }
function terminalStatusElement(session) { return $(session.kind === 'controller' ? '#controller-terminal-status' : '#terminal-status'); }
function updateDisplayConnection(session, connected) {
  if (session.kind === 'controller') { window.PronController?.updateDisplayConnection(session, connected); return; }
  // Canonical output can reconcile a lost successful reconnect response for
  // this exact registered display. Never use a cached machine check for this.
  const recoveryOperation = reconnectOperations.get(session.key);
  if (connected && recoveryOperation && !recoveryOperation.acknowledged) { recoveryOperation.acknowledged = true; persistWorkbench(); }
  const changed = session.displayConnected !== connected;
  session.displayConnected = connected;
  if (!connected) {
    cancelPendingResize(session); session.resizeError = '';
    clearTimeout(session.inputTimer); session.inputTimer = null; session.input = '';
    session.term.blur();
  }
  if (session.term.options) session.term.options.disableStdin = !connected || session.closing || session.closed;
  if (changed && !session.disposed && terminalSession === session) { renderDetail(); renderAgents(); renderHosts(); }
}
function isDisconnectedError(error) { return /(?:Terminal (?:is )?disconnected|SSH transport disconnected)/i.test(error.message || ''); }
function classifyTerminalKey(event, hasSelection) {
  if (event.type !== 'keydown' || event.altKey || event.metaKey) return null;
  const key = String(event.key || '').toLowerCase();
  if (!event.ctrlKey && event.shiftKey && key === 'insert') return 'native-paste';
  if (!event.ctrlKey) return null;
  if (key === 'v') return event.shiftKey ? 'paste' : 'native-paste';
  if (key === 'c') return event.shiftKey ? 'copy' : hasSelection ? 'native-copy' : null;
  return null;
}
function handleTerminalKey(session, event) {
  const intent = classifyTerminalKey(event, session.term.hasSelection());
  if (!intent) return true;
  // xterm must not turn Ctrl+V into ^V or selected Ctrl+C into ^C.
  // Leaving the browser default intact triggers xterm's native clipboard handler once.
  if (intent.startsWith('native-')) return false;
  event.preventDefault(); event.stopPropagation();
  if (intent === 'copy') void copyTerminal(session); else void pasteTerminal(session);
  return false;
}
function terminalWheelLineHeight(term) {
  const screen = term.element?.querySelector('.xterm-screen');
  const measured = Number(screen?.getBoundingClientRect?.().height) / Math.max(1, Number(term.rows) || 1);
  if (Number.isFinite(measured) && measured > 0) return measured;
  return Math.max(1, (Number(term.options?.fontSize) || 13) * (Number(term.options?.lineHeight) || 1.15));
}
function handleTerminalWheel(session, event) {
  // This capture handler runs before xterm's viewport and mouse listeners.
  // xterm otherwise turns wheels in alternate buffers into cursor-key input,
  // or into mouse/SGR reports. Wheels here only change the local display.
  event.stopImmediatePropagation();
  if (!session || session.disposed || !isTerminalDisplayCurrent(session) || event.ctrlKey) return;
  const delta = Number(event.deltaY); if (!Number.isFinite(delta) || delta === 0) return;
  const buffer = session.term.buffer?.active; if (!buffer) return;
  if (session.wheelBuffer !== buffer) { session.wheelBuffer = buffer; session.wheelRemainder = 0; }
  const position = Math.max(0, Number(buffer.viewportY) || 0), bottom = Math.max(0, Number(buffer.baseY) || 0);
  if ((delta < 0 && position <= 0) || (delta > 0 && position >= bottom)) {
    // Do not trap the page at the terminal's scrollback boundary. Propagation
    // remains stopped so xterm cannot send input, but browser scrolling is free.
    session.wheelRemainder = 0; return;
  }
  const units = event.deltaMode === 1 ? 1 : event.deltaMode === 2 ? Math.max(1, Number(session.term.rows) || 1) : 1 / terminalWheelLineHeight(session.term);
  session.wheelRemainder = (session.wheelRemainder || 0) + delta * units;
  const wholeLines = Math.trunc(session.wheelRemainder);
  event.preventDefault();
  if (!wholeLines) return;
  session.wheelRemainder -= wholeLines;
  const amount = Math.max(-position, Math.min(wholeLines, bottom - position));
  if (amount) session.term.scrollLines(amount);
}
function openClipboardFallback(session, mode, text = '') {
  if (!isCurrentTerminal(session) || mode === 'paste' && !canWriteTerminal(session)) { toast('终端已切换、关闭或断开，本次剪贴板操作已取消。'); return; }
  clipboardTarget = {session, mode};
  $('#clipboard-heading').textContent = mode === 'copy' ? '手动复制选中文字' : '手动粘贴到这个终端';
  $('#clipboard-note').textContent = mode === 'copy' ? '浏览器未允许写入剪贴板。下面已选中终端文本，请按 Ctrl+C 复制。' : '浏览器未允许直接读取剪贴板。请在下面输入框按 Ctrl+V，然后点击粘贴；目标仍是刚才选中的工作终端。';
  $('#clipboard-text').value = text; $('#clipboard-text').readOnly = mode === 'copy';
  $('#clipboard-confirm').textContent = mode === 'copy' ? '完成' : '粘贴到这个终端';
  $('#clipboard-dialog').showModal(); $('#clipboard-text').focus(); if (mode === 'copy') $('#clipboard-text').select();
}
async function copyTerminal(session = terminalSession) {
  if (!isCurrentTerminal(session)) { toast('请先连接一个正在运行的终端。'); return; }
  const text = session.term.getSelection(); if (!text) { toast('请先用鼠标选中要复制的终端文字。'); return; }
  try {
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
    await navigator.clipboard.writeText(text); toast('终端选中文字已复制。');
  } catch (error) { openClipboardFallback(session, 'copy', text); }
}
async function pasteTerminal(session = terminalSession) {
  if (!canWriteTerminal(session)) { toast('请先重新连接这个终端，再粘贴输入。'); return; }
  if (session.pasteReading) return;
  session.pasteReading = true;
  try {
    if (!navigator.clipboard?.readText) throw new Error('Clipboard unavailable');
    const text = await navigator.clipboard.readText();
    if (!canWriteTerminal(session)) { toast('终端已切换、关闭或断开，本次粘贴已取消。'); return; }
    if (!text) { toast('剪贴板没有文本。'); return; }
    session.term.paste(text); session.term.focus();
  } catch (error) { openClipboardFallback(session, 'paste'); }
  finally { session.pasteReading = false; }
}
function hideTerminalContext() { $('#terminal-context-menu').hidden = true; contextTerminal = null; }
function showTerminalContext(event) {
  if (event.shiftKey) return;
  event.preventDefault();
  const session = terminalSession; if (!isCurrentTerminal(session)) { hideTerminalContext(); return; }
  contextTerminal = session;
  $('#context-copy').disabled = !session.term.hasSelection();
  const menu = $('#terminal-context-menu'); menu.hidden = false;
  menu.style.left = `${Math.max(6, Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 6))}px`;
}
function safeFit(session) {
  if (session.disposed || !isTerminalDisplayCurrent(session) || !sessionConnected(session)) return;
  if (session.kind === 'controller' && !window.PronController?.isDisplayVisible(session)) return;
  if (session.writingFrame) { session.fitPending = true; return; }
  session.fitPending = false;
  session.fit.fit();
}
async function writeTerminalFrame(session, data, reset, cols, rows) {
  session.writingFrame = true;
  session.restoringFrame = !!reset;
  try {
    if (reset) {
      session.term.reset();
      if (cols >= 2 && rows >= 2) session.term.resize(cols, rows);
    }
    await new Promise(resolve => { if (data) session.term.write(data, resolve); else resolve(); });
  } finally {
    session.restoringFrame = false;
    session.writingFrame = false;
  }
}
async function openTerminal(agent, options = {}) {
  if (terminalSession?.key === agentKey(agent)) return;
  disposeTerminal();
  if (!window.Terminal || !window.FitAddon?.FitAddon) throw new Error('终端组件未加载。请刷新页面；若仍失败，请检查后台的本地 xterm 资源。');
  const term = new window.Terminal({fontFamily: 'Cascadia Mono, Consolas, monospace', fontSize: 13, lineHeight: 1.15, cursorBlink: true, scrollback: 5000, convertEol: false, allowProposedApi: true, theme: {background: '#101517', foreground: '#dde7e0', cursor: '#b9e9c7', selectionBackground: '#45634f99'}});
  // The broker's canonical terminal answers device queries even with no UI.
  // Consuming them here avoids duplicate replies being mistaken for user input.
  for (const identifier of [{final: 'n'}, {prefix: '?', final: 'n'}, {final: 'c'}, {prefix: '>', final: 'c'}, {final: 't'}]) term.parser.registerCsiHandler(identifier, () => true);
  for (const identifier of [{intermediates: '$', final: 'q'}, {intermediates: '+', final: 'q'}]) term.parser.registerDcsHandler(identifier, () => true);
  for (const identifier of [4, 10, 11, 12]) term.parser.registerOscHandler(identifier, data => data.split(';').includes('?'));
  const fit = new window.FitAddon.FitAddon(); term.loadAddon(fit); term.open($('#terminal'));
  const session = {key: agentKey(agent), agent, term, fit, seq: 0, initialized: false, disposed: false, displayConnected: agent.connected !== false, closing: false, closed: isClosedAgent(agent), pasteReading: false, wheelRemainder: 0, wheelBuffer: null, frameGeneration: 0, forceSnapshot: true, writingFrame: false, restoringFrame: false, fitPending: false, resizeGeneration: 0, resizePending: false, targetSize: '', timer: null, inputTimer: null, resizeTimer: null, input: '', inputChain: Promise.resolve(), size: '', observer: null, inputError: '', pollError: '', resizeError: ''};
  terminalSession = session;
  term.attachCustomKeyEventHandler(event => handleTerminalKey(session, event));
  term.attachCustomWheelEventHandler?.(() => false);
  term.onData(data => { if (!canWriteTerminal(session)) return; session.input += data; if (!session.inputTimer) session.inputTimer = setTimeout(() => flushTerminalInput(session), 12); });
  term.onResize(({cols, rows}) => scheduleResize(session, cols, rows));
  session.observer = new ResizeObserver(() => { if (!session.disposed && $('#terminal').clientWidth > 0) safeFit(session); }); session.observer.observe($('#terminal'));
  requestAnimationFrame(() => safeFit(session));
  await pollTerminal(session);
  if (options.focus && !session.disposed && !session.closed && sessionConnected(session)) term.focus();
}
function flushTerminalInput(session) {
  clearTimeout(session.inputTimer); session.inputTimer = null;
  const data = session.input; session.input = ''; if (!data || session.disposed || session.closing || session.closed || !sessionConnected(session)) return;
  const body = {...terminalApiIdentity(session), data, request_key: crypto.randomUUID()};
  session.inputChain = session.inputChain.then(() => session.closing || session.closed || !sessionConnected(session) || session.kind === 'controller' && !canWriteTerminal(session) ? undefined : request(terminalApiPath(session, 'input'), body)).then(() => { session.inputError = ''; renderTerminalErrors(session); }).catch(error => {
    if (isDisconnectedError(error)) updateDisplayConnection(session, false);
    session.inputError = `输入未确认送达：${error.message}。请核对终端后再操作，避免重复输入。`;
    renderTerminalErrors(session);
  });
}
function cancelPendingResize(session) {
  if (!session.resizePending) return;
  clearTimeout(session.resizeTimer); session.resizeTimer = null; session.resizePending = false; session.targetSize = '';
  session.resizeGeneration = (session.resizeGeneration || 0) + 1;
  session.frameGeneration = (session.frameGeneration || 0) + 1;
  session.forceSnapshot = true;
}
function scheduleResize(session, cols, rows) {
  if (session.disposed || session.closing || session.closed || session.restoringFrame || !session.initialized || !sessionConnected(session) || cols < 2 || rows < 2) return;
  if (session.kind === 'controller' && !window.PronController?.isDisplayVisible(session)) return;
  const size = `${cols}:${rows}`;
  if ((session.resizePending && session.targetSize === size) || (!session.resizePending && session.size === size)) return;
  session.targetSize = size;
  const generation = session.resizeGeneration = (session.resizeGeneration || 0) + 1;
  session.frameGeneration = (session.frameGeneration || 0) + 1;
  session.resizePending = true;
  clearTimeout(session.resizeTimer); session.resizeTimer = setTimeout(async () => {
    if (session.disposed || !isTerminalDisplayCurrent(session) || generation !== session.resizeGeneration) return;
    session.resizeTimer = null;
    if (session.closing || session.closed || !sessionConnected(session)) { cancelPendingResize(session); return; }
    try {
      await request(terminalApiPath(session, 'resize'), {...terminalApiIdentity(session), cols, rows});
      if (session.disposed || !isTerminalDisplayCurrent(session) || generation !== session.resizeGeneration) return;
      session.size = size; session.resizePending = false; session.resizeError = '';
      session.frameGeneration = (session.frameGeneration || 0) + 1;
      session.forceSnapshot = true; renderTerminalErrors(session);
    } catch (error) {
      if (session.disposed || !isTerminalDisplayCurrent(session) || generation !== session.resizeGeneration) return;
      session.resizePending = false; session.targetSize = ''; session.forceSnapshot = true;
      if (isDisconnectedError(error)) { updateDisplayConnection(session, false); renderTerminalErrors(session); return; }
      session.resizeError = `调整终端尺寸失败：${error.message}`; renderTerminalErrors(session);
    }
  }, 140);
}
async function pollTerminal(session) {
  if (session.disposed) return;
  if (session.kind === 'controller' && !window.PronController?.canReadDisplay(session)) return;
  if (session.resizePending) { session.timer = setTimeout(() => pollTerminal(session), 200); return; }
  let delay = 350;
  const generation = session.frameGeneration || 0, snapshot = !!session.forceSnapshot || !session.initialized;
  try {
    const query = new URLSearchParams({...terminalApiIdentity(session), after: String(snapshot ? 0 : session.seq)});
    const result = await request(terminalApiPath(session, 'output') + '?' + query);
    if (session.disposed || !isTerminalDisplayCurrent(session) || generation !== (session.frameGeneration || 0) || session.resizePending) return;
    const reset = result.reset || snapshot;
    if (!reset && result.cols >= 2 && result.rows >= 2 && session.size && session.size !== `${result.cols}:${result.rows}`) {
      session.frameGeneration = (session.frameGeneration || 0) + 1; session.forceSnapshot = true;
      return;
    }
    const data = result.reset && result.ansi != null ? result.ansi : result.data ?? result.ansi ?? '';
    await writeTerminalFrame(session, data, reset, result.cols, result.rows);
    if (session.disposed || !isTerminalDisplayCurrent(session) || generation !== (session.frameGeneration || 0) || session.resizePending) return;
    session.seq = Number(result.seq ?? result.current_seq ?? session.seq);
    session.forceSnapshot = false;
    if (reset && result.cols >= 2 && result.rows >= 2) session.size = `${result.cols}:${result.rows}`;
    session.initialized = true;
    const outputState = result.status || result.state;
    if (typeof result.connected === 'boolean') updateDisplayConnection(session, result.connected);
    if (['exited', 'disconnected', 'not_connected'].includes(outputState)) updateDisplayConnection(session, false);
    if (['closed', 'closing', 'close_unknown'].includes(outputState) || ['closed', 'closing', 'unknown'].includes(result.close_state)) {
      session.closed = outputState === 'closed' || result.close_state === 'closed'; session.closing = true; session.term.blur(); if (session.kind === 'controller') window.PronController?.render(); else renderDetail();
    }
    if (result.connected === false || ['exited', 'closed', 'disconnected', 'closing', 'close_unknown'].includes(outputState)) {
      terminalStatusElement(session).textContent = ['closed', 'closing', 'close_unknown'].includes(outputState) ? statusLabels[outputState] : disconnectedLabel; delay = 1500;
    } else terminalStatusElement(session).textContent = result.command ? `终端已连接 · ${result.command}` : '终端已连接';
    session.pollError = result.error || ''; renderTerminalErrors(session);
    if (sessionConnected(session)) { safeFit(session); scheduleResize(session, session.term.cols, session.term.rows); }
  } catch (error) {
    if (session.disposed || !isTerminalDisplayCurrent(session)) return;
    terminalStatusElement(session).textContent = '终端连接暂时不可用';
    session.pollError = `${error.message}。可尝试「重新连接」；重新连接不会自动创建第二个终端。`;
    renderTerminalErrors(session); delay = 2000;
  } finally {
    if (!session.disposed && isTerminalDisplayCurrent(session) && (session.kind !== 'controller' || window.PronController?.canReadDisplay(session))) session.timer = setTimeout(() => pollTerminal(session), delay);
  }
}
async function reconnectTerminal() {
  const agent = currentAgent(); if (!isTerminal(agent)) return;
  if (!displayDisconnected(agent)) { disposeTerminal(); await openTerminal(agent); return; }
  const key = agentKey(agent), previous = reconnectOperations.get(key);
  const operation = previous && !previous.acknowledged ? previous : {requestKey: crypto.randomUUID(), acknowledged: false}; reconnectOperations.set(key, operation); persistWorkbench();
  let result;
  try { result = await request('/api/agents/reconnect', {host: agent.host, thread_id: agent.thread_id, request_key: operation.requestKey}); }
  catch (error) {
    const recovery = error.recovery;
    const message = recovery?.message || `重连结果未确认：${error.message}。保留原请求键，请核对这个终端；不会新建、resume 或重发任务。`;
    agentRecoveryNotices.set(key, message); agent.recovery = recovery || {code: 'unknown', message};
    if (selectedAgent === key) renderAgentRecoveryNotice(agent);
    throw error;
  }
  const updated = result.agent || result;
  if (updated.host === agent.host && updated.thread_id === agent.thread_id) Object.assign(agent, updated);
  const recovery = result.recovery || {};
  agentRecoveryNotices.set(key, recovery.message || '已核对原终端的重连请求。重新连接不代表 Codex 已恢复或任务已完成。');
  if (selectedAgent === key) renderAgentRecoveryNotice(agent);
  if (!['unknown', 'reconnect_unknown', 'delivery_unknown'].includes(recovery.code) && recovery.next_action !== 'reconcile') operation.acknowledged = true;
  persistWorkbench();
  await refresh(true);
  if (selectedAgent !== key) return result;
  const current = currentAgent() || agent;
  const connected = result.connected === true || updated.connected === true || ['connected', 'already_connected', 'reattached', 'reconnected'].includes(recovery.code);
  if (connected) { disposeTerminal(); await openTerminal(current); }
  else { if (terminalSession?.key === key) updateDisplayConnection(terminalSession, false); renderDetail(); }
  return result;
}
function messageText(message) { if (message.text != null) return String(message.text); if (typeof message.content === 'string') return message.content; return (message.content || []).map(item => item.text || '').filter(Boolean).join('\n'); }
function resetConversationOutput() {
  outputGeneration++; outputView = null;
  $('#conversation-output-panel').hidden = true;
  $('#terminal-output').setAttribute('aria-expanded', 'false');
  $('#output-messages').replaceChildren(); $('#output-signals').replaceChildren();
  $('#output-source').textContent = ''; $('#output-note').textContent = '';
  $('#output-more').hidden = true; $('#output-pagination-note').textContent = '';
  $('#output-refresh').disabled = false; $('#output-more').disabled = false;
  $('#output-include-tools').checked = false; $('#output-include-tools').disabled = false;
}
function renderOutputBinding(agent) {
  if (outputView?.key !== agentKey(agent) || outputView.result) return;
  const binding = agent.output_binding || {};
  $('#output-source').textContent = binding.session_id ? `已记录关联 · ${binding.session_id}（尚未验证当前运行会话）` : '实际会话输出尚未读取';
  $('#output-note').textContent = binding.reason || '点击刷新，只读取这个终端对应的会话记录；不会输入命令或扫描其他会话。';
}
function outputChunkKey(item) {
  if (!item.message_id) return null;
  return `${item.message_id}:${item.byte_offset ?? ''}:${item.chunk_offset ?? 0}`;
}
function appendOutputMessages(view, messages) {
  const container = $('#output-messages');
  if (!view.messageCount && messages.length) container.replaceChildren();
  for (const message of messages) {
    const key = outputChunkKey(message); if (key && view.messageKeys.has(key)) continue;
    if (key) view.messageKeys.add(key);
    const article = node('article', null, 'message ' + (message.role === 'user' ? 'user' : message.role === 'tool' ? 'tool' : 'assistant'));
    article.dataset.messageId = message.message_id || ''; article.dataset.chunkOffset = String(message.chunk_offset ?? 0);
    const label = node('div', null, 'message-label');
    label.append(node('strong', message.role === 'user' ? '用户' : message.role === 'assistant' ? '工作 agent' : message.role === 'tool' ? '工具记录' : message.role || '记录'), node('span', dateText(message.time || message.timestamp)));
    if ((message.chunk_offset || 0) > 0 || message.content_complete === false) {
      const continuation = message.has_more_content === true ? ' · 后文待继续读取' : message.has_more_content === false ? ' · 已读到该消息末尾' : ' · 请核对分页状态';
      label.append(node('span', `消息片段 · 字符 ${message.chunk_offset || 0}${message.total_chars != null ? ' / ' + message.total_chars : ''}${continuation}`, 'output-chunk-label'));
    }
    const body = node('div', null, 'message-body'); formatted(body, messageText(message));
    article.append(label, body); container.append(article); view.messageCount++;
  }
  if (!view.messageCount) { container.replaceChildren(); empty(container, '当前读取范围没有对话消息。空结果不代表工作完成。'); }
}
function appendOutputSignals(view, signals) {
  const container = $('#output-signals');
  if (!view.signalCount && signals.length) container.replaceChildren();
  for (const signal of signals) {
    const key = outputChunkKey(signal); if (key && view.signalKeys.has(key)) continue;
    if (key) view.signalKeys.add(key);
    const box = node('article', null, 'signal');
    box.dataset.chunkOffset = String(signal.chunk_offset ?? 0);
    box.append(node('div', `${signal.type || '输出'} · ${dateText(signal.time || signal.timestamp)}${signal.chunk_offset ? ' · 续片 ' + signal.chunk_offset : ''}`, 'message-label'), node('pre', signal.text || JSON.stringify(signal, null, 2)));
    container.append(box); view.signalCount++;
  }
  if (!view.signalCount) { container.replaceChildren(); empty(container, '当前读取范围暂无记录的工具输出。'); }
}
function renderConversationOutput(view, result) {
  const data = result.thread || result, binding = result.output_binding || data.output_binding || {}, actual = result.source === 'codex_rollout' || data.source === 'codex_rollout';
  view.sessionId = actual ? binding.session_id || data.session_id || null : null;
  view.result = result; view.nextCursor = result.next_cursor ?? data.next_cursor ?? null;
  view.afterCursor = result.after_cursor ?? data.after_cursor ?? null;
  const hasMore = result.has_more ?? data.has_more ?? false;
  const verifiedLive = binding.live_session_verified === true;
  $('#output-source').textContent = actual ? `${verifiedLive ? '真实会话记录' : '已关联的会话记录'} · ${binding.session_id || data.session_id || '已关联会话'}` : '未读到实际会话输出 · 当前仅可获取终端画面';
  if (!actual) {
    view.nextCursor = null; view.messageCount = 0; view.signalCount = 0;
    $('#output-messages').replaceChildren(); $('#output-signals').replaceChildren();
    empty($('#output-messages'), '这里不会把终端当前画面当成完整回复。请使用「关联会话」提供这个终端中 Codex 的精确会话 ID，再读取实际记录。');
    $('#output-note').textContent = [binding.reason, data.note || result.note, '没有读到完整记录，不能据此认定报告完整或任务完成。'].filter(Boolean).join(' ');
  } else {
    appendOutputMessages(view, data.messages || result.messages || []);
    appendOutputSignals(view, data.recent_signals || result.recent_signals || []);
    const incomplete = data.content_complete === false || result.content_complete === false;
    const truncated = data.truncated || result.truncated;
    $('#output-note').textContent = [data.note || result.note, !verifiedLive ? '当前进程未暴露会话 ID；若在 Codex 内切换会话，请重新关联，不将此记录认作新会话结果。' : '', incomplete ? '本页含分段内容，请按片段与分页标识核对。' : '', hasMore ? '还有记录可继续读取。' : truncated ? '返回范围有限，不是完整历史。' : '已读到本次范围的末尾；会话记录不证明任务成功，也不证明终端当前在线。'].filter(Boolean).join(' ');
  }
  $('#output-more').hidden = !actual || !hasMore || !view.nextCursor;
  $('#output-pagination-note').textContent = actual && hasMore ? view.nextCursor ? '继续读取会保留当前消息与片段顺序。' : '还有记录，但后台未返回继续读取标识，请刷新后重试。' : actual ? '刷新最新输出会重新读取最新范围。' : '请先关联当前工作终端的会话。';
}
async function readConversationOutput(append = false) {
  const agent = currentAgent(); if (!isTerminal(agent)) return;
  const key = agentKey(agent);
  if (!outputView || outputView.key !== key || outputView.terminalId !== agent.terminal_id) outputView = {key, terminalId: agent.terminal_id, open: true, busy: false, messageKeys: new Set(), signalKeys: new Set(), messageCount: 0, signalCount: 0};
  const view = outputView; if (view.busy || append && !view.nextCursor) return;
  if (!append) {
    view.result = null; view.nextCursor = null; view.sessionId = null; view.messageKeys.clear(); view.signalKeys.clear(); view.messageCount = 0; view.signalCount = 0;
    view.includeTools = $('#output-include-tools').checked;
    $('#output-messages').replaceChildren(node('p', '正在读取这个终端的实际会话记录……', 'empty-copy')); $('#output-signals').replaceChildren();
    $('#output-source').textContent = '正在确认并读取实际会话输出……'; $('#output-note').textContent = '';
    $('#output-more').hidden = true; $('#output-pagination-note').textContent = '';
  }
  const generation = ++outputGeneration, body = {host: agent.host, thread_id: agent.thread_id, limit: 30};
  body.include_tools = !!view.includeTools;
  if (append) body.cursor = view.nextCursor;
  view.open = true; view.busy = true;
  $('#conversation-output-panel').hidden = false; $('#terminal-output').setAttribute('aria-expanded', 'true');
  $('#output-refresh').disabled = true; $('#output-more').disabled = true; $('#output-include-tools').disabled = true;
  try {
    const result = await request('/api/agents/read', body);
    if (selectedAgent !== key || currentAgent()?.terminal_id !== view.terminalId || outputView !== view || generation !== outputGeneration) return;
    const returnedSession = result.output_binding?.session_id || result.thread?.output_binding?.session_id || result.thread?.session_id || result.session_id;
    if (append && view.sessionId && returnedSession && returnedSession !== view.sessionId) throw new Error('会话关联已变化，请刷新最新输出后再继续读取，不会混合两个会话的内容');
    renderConversationOutput(view, result);
  } catch (error) {
    if (selectedAgent !== key || currentAgent()?.terminal_id !== view.terminalId || outputView !== view || generation !== outputGeneration) return;
    $('#output-note').textContent = `会话输出读取失败：${error.message}。不能把现有终端画面当成完整报告。`;
    if (!append) { $('#output-messages').replaceChildren(); empty($('#output-messages'), '请检查关联的会话 ID，或稍后重新读取。'); }
    throw error;
  } finally {
    view.busy = false;
    if (selectedAgent === key && currentAgent()?.terminal_id === view.terminalId && outputView === view && generation === outputGeneration) { $('#output-refresh').disabled = false; $('#output-more').disabled = false; $('#output-include-tools').disabled = false; }
  }
}
function openBindOutput() {
  const agent = currentAgent(); if (!isTerminal(agent)) return;
  bindOutputTarget = {key: agentKey(agent), host: agent.host, thread_id: agent.thread_id, terminal_id: agent.terminal_id};
  $('#bind-output-form').reset(); $('#bind-output-session').value = agent.output_binding?.session_id || '';
  $('#bind-output-target').textContent = `${agent.alias || agent.title || shortId(agent.thread_id)} · ${agent.host}`;
  $('#bind-output-error').hidden = true; $('#bind-output-confirm').disabled = false;
  $('#bind-output-dialog').showModal();
}
async function bindConversationOutput(target = bindOutputTarget) {
  if (!target || selectedAgent !== target.key || currentAgent()?.terminal_id !== target.terminal_id) throw new Error('所选终端已变化，请重新确认要关联的终端。');
  const sessionId = $('#bind-output-session').value.trim();
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(sessionId)) throw new Error('请填写 /status 中完整的 Codex 会话 ID（UUID）。');
  const result = await request('/api/agents/bind-output', {host: target.host, thread_id: target.thread_id, session_id: sessionId});
  if (selectedAgent !== target.key || currentAgent()?.terminal_id !== target.terminal_id || bindOutputTarget !== target) { toast('此前终端的会话关联请求已处理；不会读取新选中的终端。'); return; }
  const agent = currentAgent(), binding = result.output_binding || result.agent?.output_binding;
  if (agent && binding) agent.output_binding = binding;
  $('#bind-output-dialog').close(); resetConversationOutput();
  await readConversationOutput();
}
function renderHistory(result) {
  const messages = $('#messages'); messages.replaceChildren(); const data = result.thread || result, history = data.messages || result.messages || [];
  if (!history.length) empty(messages, '这个对话还没有可显示的历史消息。');
  for (const message of history) {
    const article = node('article', null, 'message ' + (message.role === 'user' ? 'user' : 'assistant')), label = node('div', null, 'message-label');
    label.append(node('strong', message.role === 'user' ? '用户' : message.role === 'assistant' ? '工作 agent' : message.role || '记录'), node('span', dateText(message.time || message.timestamp)));
    const body = node('div', null, 'message-body'); formatted(body, messageText(message)); article.append(label, body); messages.append(article);
  }
  const signals = $('#signals'); signals.replaceChildren(); for (const signal of data.recent_signals || result.recent_signals || []) { const box = node('article', null, 'signal'); box.append(node('div', `${signal.type || '输出'} · ${dateText(signal.time || signal.timestamp)}`, 'message-label'), node('pre', signal.text || JSON.stringify(signal, null, 2))); signals.append(box); }
  if (!signals.childElementCount) empty(signals, '暂无记录的工具输出。');
  $('#history-note').textContent = [data.note || result.note, data.truncated ? '只显示最近一部分记录。' : '', '历史记录不代表原终端当前在线。'].filter(Boolean).join(' ');
}
async function readHistory() {
  const agent = currentAgent(); if (!agent || isTerminal(agent)) return; const generation = ++historyGeneration, key = agentKey(agent);
  $('#refresh-history').disabled = true; $('#messages').replaceChildren(node('p', '正在读取所选对话的记录……', 'empty-copy'));
  try { const data = await request('/api/agents/read', {host: agent.host, thread_id: agent.thread_id, limit: 30}); if (selectedAgent === key && generation === historyGeneration) renderHistory(data); }
  catch (error) { if (selectedAgent === key && generation === historyGeneration) $('#messages').replaceChildren(node('p', error.message, 'empty-copy')); throw error; }
  finally { if (generation === historyGeneration) $('#refresh-history').disabled = false; }
}
function renderOutbox() {
  const box = $('#outbox'); box.replaceChildren(); const records = state.outbox.slice().sort((a, b) => (b.time || 0) - (a.time || 0)).slice(0, 8); if (!records.length) empty(box, '暂无 CEO 发送记录。你在终端直接输入时不会生成任务卡。');
  for (const message of records) { const card = node('article', null, 'feedback-card'), agent = state.agents.find(item => item.host === message.host && item.thread_id === message.thread_id); card.append(node('strong', deliveryLabels[message.state] || message.state || '已记录'), node('div', `${agent?.alias || shortId(message.thread_id)} · ${message.host}`, 'muted'), node('p', message.text || '')); if (message.error) card.append(node('p', message.error, 'error-text')); box.append(card); }
}
function renderEvents(events) {
  for (const item of events || []) { const id = item.id ?? `${item.kind}:${JSON.stringify(item.data)}`; activity.set(id, item); if (typeof item.id === 'number') cursor = Math.max(cursor, item.id); }
  while (activity.size > 60) activity.delete(activity.keys().next().value); const list = $('#activity'); list.replaceChildren();
  const kinds = {terminal_created: '打开工作终端', terminal_connected: '终端重新连接', terminal_exited: '显示连接已结束', terminal_closed: '关闭工作终端', terminal_state: '终端状态更新', terminal_output: '终端输出更新', terminal_input_submitted: 'CEO 终端输入送达', message_submitted: 'CEO 消息提交', agent_created: '创建工作终端', agent_attached: '连接工作终端', agent_updated: '工作名称或置顶已更新', agent_reconnected: '已附着原工作终端', agent_activated: '纳入 CEO 管理', agent_deactivated: '移出 CEO 管理', host_connected: '机器连接', host_unavailable: '机器连接不可用', host_configured: '机器配置更新', error: '后台错误'};
  for (const item of Array.from(activity.values()).reverse().slice(0, 12)) { const data = item.data || {}, row = node('article', null, 'event-row'); row.append(node('strong', kinds[item.kind] || item.kind || '后台事件'), node('span', [data.host, shortId(data.thread_id), dateText(item.time || item.created_at || item.timestamp)].filter(Boolean).join(' · '), 'muted')); if (data.text || data.reason) row.append(node('p', String(data.text || data.reason).slice(0, 280))); list.append(row); }
  if (!list.childElementCount) empty(list, '暂无连接事件。');
}
async function refresh(force = false) {
  if (refreshing) { await refreshTask; if (!force) return; }
  refreshing = true;
  refreshTask = (async () => {
  try {
    const next = await request('/api/state?after=' + cursor);
    for (const field of ['hosts', 'agents', 'outbox']) state[field] = Array.isArray(next[field]) ? next[field] : [];
    if (!selectedHost || !state.hosts.some(host => host.id === selectedHost)) selectedHost = state.hosts.find(host => host.id === workbenchSaved.selectedHost)?.id || state.hosts[0]?.id || null;
    if (agentFilters.host && !state.hosts.some(host => host.id === agentFilters.host)) agentFilters.host = '';
    if (restoreAgentKey && !selectedAgent) { const restored = registeredAgent(restoreAgentKey); if (restored) await selectAgent(restored, {restore: true}); }
    if (selectedAgent && !currentAgent()) { selectedAgent = null; historyGeneration++; resetConversationOutput(); disposeTerminal(); }
    // Registry updates never replace the terminal DOM or focus it: typing, selection and dimensions stay intact.
    // Host cards depend on live display counts, not just the saved machine
    // configuration. Keep output sequence/text out of this cache signature.
    const hostDisplays = state.hosts.map(host => [host.id, hostDisplayStatus(host)]);
    const parts = {hosts: JSON.stringify([state.hosts, selectedHost, hostDisplays, agentFilters.host]), agents: JSON.stringify([state.agents, selectedAgent, agentFilters]), outbox: JSON.stringify(state.outbox), detail: JSON.stringify(currentAgent() || null)};
    for (const [key, render] of Object.entries({hosts: renderHosts, agents: renderAgents, outbox: renderOutbox, detail: renderDetail})) if (rendered[key] !== parts[key]) { rendered[key] = parts[key]; render(); }
    renderEvents(next.events); $('#status').textContent = `后台服务在线${next.version ? ' · ' + next.version : ''}`; $('#status').classList.add('online');
  } catch (error) { $('#status').textContent = '后台服务未连接'; $('#status').classList.remove('online'); }
  finally { refreshing = false; }
  })();
  await refreshTask;
}
function openMachine(host) { $('#machine-form').reset(); $('#machine-id').value = host?.id || ''; $('#machine-id').readOnly = !!host; $('#machine-label').value = host?.label || ''; $('#machine-transport').value = host?.transport || 'ssh'; $('#machine-target').value = host?.target || host?.targetAlias || ''; updateMachineTransport(); $('#machine-dialog').showModal(); }
function updateMachineTransport() { const local = $('#machine-transport').value === 'local'; $('#machine-target').disabled = local; $('#machine-target').required = !local; }
function openCreate(thread = null, hostId = selectedHost) {
  createTarget = {host: hostId, thread_id: thread?.thread_id || thread?.id || null, requestKey: crypto.randomUUID(), fingerprint: null}; const host = state.hosts.find(item => item.id === hostId);
  $('#create-form').reset(); $('#create-heading').textContent = thread ? '在终端中继续对话' : '打开工作终端'; $('#create-host').textContent = `运行位置：${host?.label || hostId}`;
  $('#create-title').value = thread?.alias || thread?.title || thread?.name || `${host?.label || hostId} 工作终端`; $('#create-cwd').value = thread?.cwd || '';
  $('#create-resume-guide').hidden = !thread; if (thread) $('#create-resume-command').textContent = resumeCommand(createTarget.thread_id);
  $('#create-active').checked = !!thread?.active; $('#create-dialog').showModal();
}
function openDirectories() {
  if (!createTarget || !$('#create-dialog').open) return;
  directoryTarget = {create: createTarget, host: createTarget.host, view: null};
  const host = state.hosts.find(item => item.id === directoryTarget.host);
  $('#directory-host').textContent = `浏览位置：${host?.label || directoryTarget.host}`;
  $('#directory-dialog').showModal();
  void loadDirectory($('#create-cwd').value.trim());
}
function isCurrentDirectory(target, generation) {
  return directoryTarget === target && createTarget === target.create && directoryGeneration === generation && $('#directory-dialog').open;
}
async function loadDirectory(path = '') {
  const target = directoryTarget; if (!target) return; const generation = ++directoryGeneration;
  target.view = null; $('#directory-select').disabled = true; $('#directory-parent').disabled = true;
  $('#directory-loading').textContent = '读取目录中…'; $('#directory-error').hidden = true;
  $('#directory-path').value = path; $('#directory-list').replaceChildren(); $('#directory-roots').replaceChildren(); $('#directory-note').textContent = '';
  try {
    const view = await request('/api/directories/list', {host: target.host, path});
    if (!isCurrentDirectory(target, generation)) return;
    target.view = view; $('#directory-path').value = view.path; $('#directory-loading').textContent = '';
    $('#directory-parent').disabled = !view.parent; $('#directory-select').disabled = !view.path;
    for (const root of view.roots || []) $('#directory-roots').append(button(root.name || root.path, 'subtle', async () => loadDirectory(root.path)));
    const list = $('#directory-list');
    for (const directory of view.directories || []) list.append(button(directory.name, 'directory-row', async () => loadDirectory(directory.path)));
    if (!list.childElementCount) empty(list, '这个目录没有可浏览的子目录。可以选择当前目录。');
    $('#directory-note').textContent = view.truncated ? '子目录较多，只显示部分结果。也可以直接在上方输入完整路径。' : '点击子目录进入，再选择当前目录。目录浏览不会启动工作 agent。';
  } catch (error) {
    if (!isCurrentDirectory(target, generation)) return;
    $('#directory-loading').textContent = ''; $('#directory-error').hidden = false;
    $('#directory-error').textContent = `目录读取失败：${error.message}`;
    $('#directory-note').textContent = '可以直接使用主目录创建终端，再手动 cd；也可以回到创建窗口输入绝对路径。';
  }
}
function selectDirectory(path) {
  const target = directoryTarget;
  if (!target || createTarget !== target.create || !$('#create-dialog').open) { $('#directory-dialog').close(); return; }
  $('#create-cwd').value = path; $('#directory-dialog').close(); $('#create-cwd').focus();
}
function openCloseTerminal() {
  const agent = currentAgent(); if (!isTerminal(agent) || isClosedAgent(agent) || isClosingAgent(agent)) return;
  const host = state.hosts.find(item => item.id === agent.host);
  closeTarget = {host: agent.host, thread_id: agent.thread_id, terminal_id: agent.terminal_id, key: agentKey(agent), alias: agent.alias || agent.title || shortId(agent.thread_id), command: agent.command && agent.command !== 'xterm-256color' ? agent.command : '当前进程名称未确认（可能仍在运行）'};
  $('#close-name').textContent = closeTarget.alias; $('#close-host').textContent = host?.label || agent.host; $('#close-command').textContent = closeTarget.command;
  setCloseControls(false);
  $('#close-dialog').showModal();
}
function setCloseControls(busy) {
  $('#close-confirm').disabled = busy;
  document.querySelectorAll('#close-dialog [data-close]').forEach(element => { element.disabled = busy; });
}
async function closeTerminal(target = closeTarget) {
  if (!target || selectedAgent !== target.key || currentAgent()?.terminal_id !== target.terminal_id) throw new Error('所选终端已变化，请重新确认要关闭的终端。');
  if (target.submitting) return;
  target.submitting = true; setCloseControls(true);
  const session = terminalSession?.key === target.key ? terminalSession : null;
  try {
  if (session) {
    session.closing = true; cancelPendingResize(session); clearTimeout(session.inputTimer); session.inputTimer = null; session.input = '';
    // Finish any already submitted keyboard writes before the close request.
    // No buffered draft or asynchronous clipboard read can reach the closed process.
    await session.inputChain;
  }
    if (selectedAgent !== target.key || (session && terminalSession !== session)) throw new Error('终端已切换，本次关闭请求已取消，请重新确认。');
    const result = await request('/api/terminals/close', {host: target.host, thread_id: target.thread_id, confirm: true});
    if (session && !session.disposed && terminalSession === session) { session.closed = !!result.closed; session.term.blur(); }
    await refresh(true);
    if (!result.closed) throw new Error('关闭结果尚未确认，输入与重连已暂停。请核对这个终端的进程状态。' + (result.error ? ' ' + result.error : ''));
    if (closeTarget === target) $('#close-dialog').close();
    toast(`已关闭 ${target.alias}，进程已结束，登记与保留输出仍可查看。`);
    return result;
  } catch (error) {
    // A failed response cannot prove that the remote close did not execute.
    // Keep input paused until the user verifies the same terminal's state.
    if (session && terminalSession === session) { session.term.blur(); renderDetail(); }
    throw error;
  } finally {
    target.submitting = false;
    if (closeTarget === target) setCloseControls(false);
  }
}
$('#machine-settings').onclick = () => openMachine(currentHost()); $('#add-machine').onclick = () => openMachine(); $('#machine-transport').onchange = updateMachineTransport;
document.querySelectorAll('[data-close]').forEach(element => { element.onclick = () => document.getElementById(element.dataset.close).close(); });
document.querySelectorAll('dialog').forEach(dialog => { dialog.addEventListener('click', event => { if (event.target === dialog && !(dialog.id === 'close-dialog' && closeTarget?.submitting)) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } }); });
$('#machine-form').onsubmit = event => { event.preventDefault(); action($('#machine-save'), async () => {
  const id = $('#machine-id').value.trim(); await request('/api/hosts/upsert', {id, label: $('#machine-label').value.trim(), transport: $('#machine-transport').value, target: $('#machine-transport').value === 'ssh' ? $('#machine-target').value.trim() : '', live: true}); selectHost(id); $('#machine-dialog').close(); await refresh(true); toast('机器配置已保存，可以打开工作终端。');
}); };
$('#discover-form').onsubmit = event => { event.preventDefault(); action($('#discover-button'), async () => {
  if (!selectedHost) return; const host = selectedHost; $('#candidates-section').hidden = false; $('#candidates').replaceChildren(node('p', '正在读取当前机器的候选记录……', 'empty-copy'));
  const result = await request('/api/agents/discover', {host, query: $('#discovery-query').value.trim(), cwd: $('#discovery-cwd').value.trim(), limit: 20});
  if (selectedHost !== host) return; discoveryHost = host; candidates = result.threads || []; $('#candidate-note').textContent = '这里只查找历史记录；连接会打开 shell，由你选择模型和运行 resume。'; renderCandidates();
}); };
$('#clear-candidates').onclick = () => { $('#candidates-section').hidden = true; candidates = []; };
$('#active-only').onchange = () => { agentFilters.active = $('#active-only').checked; persistWorkbench(); renderAgents(true); };
$('#agent-search').oninput = () => { agentFilters.query = $('#agent-search').value; persistWorkbench(); renderAgents(true); };
$('#agent-host-filter').onchange = () => { agentFilters.host = $('#agent-host-filter').value; if (agentFilters.host) selectedHost = agentFilters.host; persistWorkbench(); renderHosts(); renderAgents(true); };
$('#agent-status-filter').onchange = () => { agentFilters.status = $('#agent-status-filter').value; persistWorkbench(); renderAgents(true); };
$('#all-machines').onclick = () => { agentFilters.host = ''; persistWorkbench(); renderHosts(); renderAgents(true); };
$('#new-agent').onclick = () => { if (currentHost()) openCreate(null, agentFilters.host || selectedHost); };
$('#continue-terminal').onclick = () => { const agent = currentAgent(); if (agent) openCreate(agent, agent.host); };
$('#browse-directory').onclick = openDirectories;
$('#directory-path-form').onsubmit = event => { event.preventDefault(); void loadDirectory($('#directory-path').value.trim()); };
$('#directory-home').onclick = () => void loadDirectory('');
$('#directory-parent').onclick = () => { if (directoryTarget?.view?.parent) void loadDirectory(directoryTarget.view.parent); };
$('#directory-select').onclick = () => { if (directoryTarget?.view?.path) selectDirectory(directoryTarget.view.path); };
$('#directory-use-home').onclick = () => selectDirectory('');
$('#directory-dialog').addEventListener('close', () => { directoryTarget = null; directoryGeneration++; });
$('#create-dialog').addEventListener('close', () => { createTarget = null; if ($('#directory-dialog').open) $('#directory-dialog').close(); });
$('#create-form').onsubmit = event => { event.preventDefault(); action($('#create-save'), async () => {
  if (!createTarget) return; const target = createTarget, body = {host: target.host, cwd: $('#create-cwd').value.trim(), title: $('#create-title').value.trim(), active: $('#create-active').checked, cols: 120, rows: 32, request_key: target.requestKey};
  if (target.thread_id) body.thread_id = target.thread_id;
  const fingerprint = JSON.stringify(body);
  if (target.fingerprint && target.fingerprint !== fingerprint) throw new Error('这个打开请求已经提交过。请先取消并重新打开对话框，再提交修改后的配置；不会用同一个请求重复创建不同终端。');
  target.fingerprint = fingerprint;
  const result = await request('/api/terminals/create', body), agent = result.agent || result, id = agent.thread_id || result.thread_id;
  if (!id) throw new Error('后台没有返回终端标识。请查看连接记录后重试。');
  if (createTarget !== target) { await refresh(true); toast('此前的终端打开请求已处理，结果保留在对应机器的登记表中。'); return; }
  selectedHost = body.host; selectedAgent = null; $('#create-dialog').close(); await refresh(true);
  const registered = state.agents.find(item => item.host === body.host && item.thread_id === id);
  if (registered) await selectAgent(registered); else toast('终端已创建，等待登记表刷新。');
  if (result.created === false) toast('终端创建结果未确认。已保留同一终端的登记，请用「重新连接」核对；不会再次创建不同终端。' + (result.delivery?.error ? ' ' + result.delivery.error : ''));
}); };
$('#refresh-history').onclick = () => action($('#refresh-history'), readHistory);
$('#terminal-connect').onclick = () => action($('#terminal-connect'), reconnectTerminal);
$('#terminal-focus').onclick = () => terminalSession?.term.focus();
$('#terminal-copy').onclick = () => action($('#terminal-copy'), () => copyTerminal(terminalSession));
$('#terminal-paste').onclick = () => action($('#terminal-paste'), () => pasteTerminal(terminalSession));
$('#terminal-close').onclick = openCloseTerminal;
$('#terminal-output').onclick = () => action($('#terminal-output'), () => readConversationOutput());
$('#terminal-bind-output').onclick = openBindOutput;
$('#output-refresh').onclick = () => action($('#output-refresh'), () => readConversationOutput());
$('#output-more').onclick = () => action($('#output-more'), () => readConversationOutput(true));
$('#output-include-tools').onchange = () => { if (outputView?.open && !$('#conversation-output-panel').hidden) void action($('#output-refresh'), () => readConversationOutput()); };
$('#output-hide').onclick = () => { if (outputView) outputView.open = false; $('#conversation-output-panel').hidden = true; $('#terminal-output').setAttribute('aria-expanded', 'false'); };
$('#bind-output-form').onsubmit = event => { event.preventDefault(); action($('#bind-output-confirm'), async () => {
  try { await bindConversationOutput(bindOutputTarget); } catch (error) { $('#bind-output-error').textContent = error.message; $('#bind-output-error').hidden = false; throw error; }
}); };
$('#bind-output-dialog').addEventListener('close', () => { bindOutputTarget = null; });
$('#close-form').onsubmit = event => { event.preventDefault(); action($('#close-confirm'), () => closeTerminal(closeTarget)); };
$('#close-dialog').addEventListener('close', () => { closeTarget = null; });
$('#close-dialog').addEventListener('cancel', event => { if (closeTarget?.submitting) event.preventDefault(); });
$('#terminal').addEventListener('contextmenu', showTerminalContext);
$('#terminal').addEventListener('wheel', event => handleTerminalWheel(terminalSession, event), {capture: true, passive: false});
$('#context-copy').onclick = () => { const session = contextTerminal; hideTerminalContext(); void copyTerminal(session); };
$('#context-paste').onclick = () => { const session = contextTerminal; hideTerminalContext(); void pasteTerminal(session); };
document.addEventListener('click', event => { if (!$('#terminal-context-menu').contains(event.target)) hideTerminalContext(); });
document.addEventListener('keydown', event => { if (event.key === 'Escape') hideTerminalContext(); });
$('#clipboard-form').onsubmit = event => { event.preventDefault(); action($('#clipboard-confirm'), async () => {
  const target = clipboardTarget; if (!target) return;
  if (target.mode === 'copy') { $('#clipboard-dialog').close(); return; }
  if (!canWriteTerminal(target.session)) throw new Error('显示连接已断开，请重新连接后再粘贴。');
  if (!isCurrentTerminal(target.session)) throw new Error('终端已切换或关闭，没有发送这段文本。');
  const text = $('#clipboard-text').value; if (!text) throw new Error('请输入要粘贴的文本。');
  target.session.term.paste(text); $('#clipboard-dialog').close(); target.session.term.focus();
}); };
$('#clipboard-dialog').addEventListener('close', () => { clipboardTarget = null; $('#clipboard-text').value = ''; $('#clipboard-text').readOnly = false; });
$('#copy-resume').onclick = () => action($('#copy-resume'), async () => { await navigator.clipboard.writeText($('#resume-command').textContent); toast('命令已复制。请在终端中自行粘贴并执行。'); });
window.addEventListener('beforeunload', () => { if (terminalSession?.input) flushTerminalInput(terminalSession); });
window.addEventListener('hashchange', () => { const key = agentKeyFromHash(), agent = key ? registeredAgent(key) : null; if (agent && agentKey(agent) !== selectedAgent) void selectAgent(agent, {restore: true}).catch(error => toast(error.message)); });
refresh(); setInterval(refresh, 3000);
