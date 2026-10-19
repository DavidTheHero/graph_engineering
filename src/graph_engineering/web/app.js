import {$, el, badge, duration, date, safeLink, modelName} from './dom.js';
import {renderGraph, selectGraphNode, drawEdges} from './graph.js';
import {renderInspector, renderEvidence, renderActivity, renderProofs} from './details.js';

let overview, detail, selection, selectedNode, pending = false, lastPayload = '', sourceOptions = '';
let listSignature = '';
let runRead = 0;
const params = new URLSearchParams(location.search);
let tab = ['workflow', 'evidence', 'activity'].includes(params.get('view')) ? params.get('view') : 'workflow';
let requested = params.get('run'), requestedSource = params.get('source');

async function read(path) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch(path, {signal: controller.signal, cache: 'no-store'});
    const payload = await response.json();
    if (!response.ok) throw Error(payload.error || 'Unable to read workflow data.');
    return payload;
  } finally { clearTimeout(timer); }
}

function matchingRuns() {
  const query = $('#search').value.trim().toLowerCase(), source = $('#source').value, status = $('#status-filter').value;
  return overview.runs.filter(run => (source === 'all' || run.source === source)
    && `${run.repo_name} ${run.title} ${run.id}`.toLowerCase().includes(query)
    && (status === 'all' || (status === 'attention' ? ['blocked', 'failed', 'escalated', 'waiting_approval'].includes(run.status)
      : status === 'active' ? ['running', 'ready', 'scanned'].includes(run.status) : run.status === status)));
}

function renderList() {
  if (!overview) return [];
  const list = $('#run-list'), runs = matchingRuns();
  const signature = JSON.stringify([selection, runs]);
  if (signature === listSignature) return runs;
  listSignature = signature;
  const focusedKey = document.activeElement?.dataset.runKey;
  list.replaceChildren();
  $('#run-count').textContent = runs.length;
  for (const run of runs) {
    const key = `${run.source}:${run.id}`;
    const button = el('button', `run-item${key === selection ? ' active' : ''}`); button.type = 'button';
    button.dataset.runKey = key;
    if (key === selection) button.setAttribute('aria-current', 'true');
    button.append(el('span', 'run-item-repo', run.repo_name.split('/').at(-1)), el('strong', 'run-item-title', run.title));
    const meta = el('span', 'run-item-meta'); meta.append(badge(run.status), el('span', 'source-label', run.source_kind === 'live' ? 'Live' : 'Recorded')); button.append(meta);
    button.title = `${run.repo_name}\n${run.id}`;
    button.addEventListener('click', () => selectRun(run)); list.append(button);
  }
  if (!runs.length) list.append(el('p', 'sidebar-empty', 'No matching runs. Try another source or clear your filters.'));
  if (focusedKey) [...list.children].find(button => button.dataset.runKey === focusedKey)?.focus();
  return runs;
}

function syncURL() {
  const url = new URL(location.href); url.search = '';
  if (detail) { url.searchParams.set('source', detail.run.source); url.searchParams.set('run', detail.run.id); }
  if (tab !== 'workflow') url.searchParams.set('view', tab);
  if (selectedNode) url.searchParams.set('step', selectedNode);
  history.replaceState(null, '', url);
}

function showTab(name, focus = false) {
  tab = name;
  for (const view of ['workflow', 'evidence', 'activity']) {
    const button = $(`#tab-${view}`); button.setAttribute('aria-selected', String(view === tab)); button.tabIndex = view === tab ? 0 : -1;
    $(`#panel-${view}`).hidden = view !== tab;
  }
  if (focus) $(`#tab-${name}`).focus();
  requestAnimationFrame(drawEdges); syncURL();
}

function chooseNode(name) {
  selectedNode = name; selectGraphNode(name); renderInspector(detail, overview.contracts, name); syncURL();
  $('#announcement').textContent = `Selected ${name.replaceAll('_', ' ')}.`;
}

function renderRun() {
  const focusedNode = document.activeElement?.dataset.node;
  const run = detail.run;
  $('#workspace').hidden = false; $('#empty').hidden = true; $('#loading').hidden = true;
  $('#run-title').textContent = run.title; $('#run-repo').textContent = run.repo_name;
  const meta = $('#run-meta'); meta.replaceChildren(badge(run.status), el('code', 'run-id', run.id), el('span', 'backend-label', run.backend === 'local' ? 'Local backend · GitHub simulated' : run.backend === 'github' ? 'GitHub backend' : 'Publication backend not recorded'));
  $('#snapshot-note').hidden = run.source_kind !== 'recorded';
  $('#attention').hidden = !run.attention && !run.cancel_requested;
  $('#attention-title').textContent = run.cancel_requested ? 'Stop requested' : run.status === 'waiting_approval' ? 'Waiting for human approval' : 'Supervisor attention required';
  $('#attention-message').textContent = run.attention || 'The controller will stop at its next cancellation boundary.';
  const links = $('#publication-links'); links.replaceChildren();
  if (run.issue) links.append(safeLink(run.issue.url, `${run.backend === 'local' ? 'Local ticket' : 'Issue'} #${run.issue.number}`));
  if (run.pr) links.append(safeLink(run.pr.url, `${run.backend === 'local' ? 'Local PR' : 'Pull request'} #${run.pr.number}`));
  const metrics = $('#metrics'); metrics.replaceChildren();
  const completed = Object.values(detail.nodes).filter(n => n.status === 'completed').length;
  const modelIds = [...new Set(detail.model_calls.map(c => c.model_id))];
  for (const [label, value, note] of [
    ['Execution time', duration(run.duration_seconds), run.source_kind === 'recorded' ? 'Recorded elapsed time' : 'At last checkpoint'],
    ['Steps completed', String(completed), `${Object.values(detail.nodes).filter(n => n.status !== 'not_run').length} visited · ${run.repairs} repairs`],
    [detail.events_truncated ? 'Model calls shown' : 'Model calls', String(detail.model_calls.length), modelIds.length ? modelIds.map(modelName).join(' / ') : 'No calls recorded'],
    ['Approval policy', detail.approval ? 'Human approved' : detail.evidence.gate?.allowed ? 'Policy satisfied' : 'Gated', detail.approval ? 'Bound to the exact tested commit' : 'Checks required before merging']
  ]) {
    const item = el('div', 'metric'), description = el('dd', '', value);
    description.append(el('span', 'metric-note', note)); item.append(el('dt', '', label), description); metrics.append(item);
  }
  $('#event-count').textContent = detail.events.length;
  renderGraph(detail, overview.contracts, selectedNode, chooseNode);
  renderInspector(detail, overview.contracts, selectedNode);
  renderEvidence(detail); renderActivity(detail); renderProofs(detail, showTab);
  $('#updated').textContent = `Last checkpoint · ${date(run.updated)}`;
  showTab(tab);
  if (focusedNode) document.querySelector(`[data-node="${focusedNode}"]`)?.focus();
}

async function selectRun(run) {
  const request = ++runRead;
  const next = `${run.source}:${run.id}`, changed = next !== selection;
  selection = next; renderList();
  $('#workspace').setAttribute('aria-busy', 'true');
  try {
    const payload = await read(`/api/run?source=${encodeURIComponent(run.source)}&id=${encodeURIComponent(run.id)}`);
    if (selection !== next || request !== runRead) return;
    const serialized = JSON.stringify(payload);
    if (changed || serialized !== lastPayload) {
      detail = payload; lastPayload = serialized;
      if (changed) selectedNode = params.get('step') && detail.nodes[params.get('step')] ? params.get('step')
        : detail.nodes[run.node] ? run.node : detail.evidence.verification ? 'test_execution' : 'completion';
      renderRun();
    }
    $('#error').hidden = true;
  } catch (error) { if (selection === next && request === runRead) showError(error); }
  finally { if (request === runRead) $('#workspace').removeAttribute('aria-busy'); }
}

function showError(error) {
  $('#error').textContent = `Unable to refresh. ${error.name === 'AbortError' ? 'The local server did not respond in time.' : error.message} Any visible run data is from the last successful read.`;
  $('#error').hidden = false; $('#loading').hidden = true;
  $('#connection').textContent = 'Connection interrupted'; $('#connection').classList.add('disconnected');
}

async function refresh() {
  if (pending) return; pending = true; $('#refresh').disabled = true;
  try {
    overview = await read('/api/overview');
    const optionsKey = JSON.stringify(overview.sources);
    if (optionsKey !== sourceOptions) {
      const value = $('#source').value; $('#source').replaceChildren();
      const all = el('option', '', 'All sources'); all.value = 'all'; $('#source').append(all);
      for (const source of overview.sources) { const option = el('option', '', source.label); option.value = source.id; $('#source').append(option); }
      $('#source').value = ['all', ...overview.sources.map(s => s.id)].includes(value) ? value : 'all'; sourceOptions = optionsKey;
    }
    const runs = renderList();
    const chosen = runs.find(r => `${r.source}:${r.id}` === selection)
      || runs.find(r => r.id === requested && r.source === requestedSource) || runs[0];
    if (chosen) await selectRun(chosen);
    else { $('#workspace').hidden = true; $('#empty').hidden = false; $('#loading').hidden = true; selection = null; detail = null; }
    if ($('#error').hidden) { $('#connection').textContent = '● Auto-refresh · 3s'; $('#connection').classList.remove('disconnected'); }
  } catch (error) { showError(error); }
  finally { pending = false; $('#refresh').disabled = false; }
}

for (const id of ['source', 'status-filter', 'search']) $("#" + id).addEventListener(id === 'search' ? 'input' : 'change', () => {
  const runs = renderList();
  if (!runs.length) { $('#workspace').hidden = true; $('#empty').hidden = false; }
  else if (!runs.some(r => `${r.source}:${r.id}` === selection)) selectRun(runs[0]);
  else { $('#workspace').hidden = false; $('#empty').hidden = true; }
});
const tabs = [...document.querySelectorAll('[role=tab]')];
tabs.forEach((button, index) => {
  button.addEventListener('click', () => showTab(button.id.slice(4)));
  button.addEventListener('keydown', event => {
    const delta = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    if (delta) { event.preventDefault(); showTab(tabs[(index + delta + tabs.length) % tabs.length].id.slice(4), true); }
  });
});
$('#refresh').addEventListener('click', refresh);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
setInterval(() => { if (!document.hidden) refresh(); }, 3000);
refresh();
