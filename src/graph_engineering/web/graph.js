import {$, el, badge, nodeTitle, modelName} from './dom.js';

const phases = [
  ['Discover', 'Find a reproducible problem', ['repository_discovery', 'evidence_collection', 'issue_detection', 'issue_validation', 'deduplication']],
  ['Plan', 'Create a scoped task', ['ticket_creation', 'prioritisation', 'task_planning', 'model_selection']],
  ['Build & verify', 'Implement, test and review', ['isolated_implementation', 'test_execution', 'independent_review', 'repair', 'escalation']],
  ['Deliver', 'Publish with policy checks', ['pull_request_creation', 'ci_policy_verification', 'approval_gate', 'merge', 'post_merge_verification', 'completion']]
];
const shortNames = {repository_discovery: 'Repository discovery', evidence_collection: 'Collect evidence',
  issue_detection: 'Detect issue', issue_validation: 'Validate issue', deduplication: 'Deduplicate',
  ticket_creation: 'Create ticket', prioritisation: 'Prioritise', task_planning: 'Plan task',
  model_selection: 'Select model', isolated_implementation: 'Isolated implementation',
  test_execution: 'Run regression tests', independent_review: 'Independent review',
  repair: 'Repair & retry', escalation: 'Escalate', pull_request_creation: 'Open pull request',
  ci_policy_verification: 'Verify CI & policy', approval_gate: 'Approval gate', merge: 'Merge',
  post_merge_verification: 'Verify merged code', completion: 'Complete & report'};
const extraEdges = [
  ['evidence_collection', 'completion'], ['deduplication', 'completion'],
  ['issue_validation', 'issue_detection'], ['test_execution', 'repair'],
  ['independent_review', 'repair'], ['repair', 'isolated_implementation'],
  ['repair', 'escalation'], ['escalation', 'completion'],
  ['approval_gate', 'ci_policy_verification']
];
let current, contracts = [], choose;
const svgNS = 'http://www.w3.org/2000/svg';

export function renderGraph(detail, definitions, selected, onSelect) {
  current = detail; contracts = definitions; choose = onSelect;
  const lanes = $('#lanes'); lanes.replaceChildren();
  phases.forEach(([title, subtitle, names], i) => {
    const lane = el('section', 'lane');
    const heading = el('div', 'lane-heading'); heading.append(el('span', 'phase-number', `0${i + 1}`), el('strong', '', title), el('p', '', subtitle));
    lane.append(heading);
    names.forEach(name => {
      const observed = detail.nodes[name] || {status: 'not_run'};
      const button = el('button', `graph-node ${observed.status}${name === selected ? ' selected' : ''}${['repair', 'escalation'].includes(name) ? ' recovery-node' : ''}`);
      button.type = 'button'; button.dataset.node = name;
      button.setAttribute('aria-pressed', String(name === selected));
      button.setAttribute('aria-label', `${shortNames[name]}, ${badge(observed.status).textContent}`);
      const calls = detail.model_calls.filter(c => c.node === name);
      const workers = [...new Set(calls.map(c => modelName(c.model_id).split(' · ')[1] || 'Model'))];
      const workerLabel = workers.length ? workers.join(' → ') : observed.status === 'not_run'
        || ['issue_detection', 'task_planning', 'isolated_implementation', 'independent_review'].includes(name) ? '—' : 'Tool';
      const top = el('span', 'node-top'); top.append(el('span', 'node-title', shortNames[name] || nodeTitle(name)));
      button.append(top);
      const bottom = el('span', 'node-bottom'); bottom.append(badge(observed.status), el('span', 'node-worker', workerLabel));
      if (observed.failures && observed.status === 'completed') bottom.append(el('span', 'recovered-mark', '↻'));
      button.append(bottom);
      button.addEventListener('click', () => choose(name));
      lane.append(button);
    });
    lanes.append(lane);
  });
  requestAnimationFrame(drawEdges);
}

export function selectGraphNode(name) {
  document.querySelectorAll('.graph-node').forEach(button => {
    const selected = button.dataset.node === name;
    button.classList.toggle('selected', selected); button.setAttribute('aria-pressed', String(selected));
  });
}

export function drawEdges() {
  if (!current || $('#panel-workflow').hidden) return;
  const root = $('#graph'), svg = $('#edges'), bounds = root.getBoundingClientRect();
  svg.setAttribute('viewBox', `0 0 ${root.clientWidth} ${root.clientHeight}`);
  svg.replaceChildren();
  const defs = document.createElementNS(svgNS, 'defs');
  const marker = document.createElementNS(svgNS, 'marker');
  Object.entries({id: 'arrow', viewBox: '0 0 10 10', refX: '8', refY: '5', markerWidth: '5', markerHeight: '5', orient: 'auto-start-reverse'}).forEach(([k, v]) => marker.setAttribute(k, v));
  const tip = document.createElementNS(svgNS, 'path'); tip.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z'); tip.setAttribute('fill', '#98a5a1'); marker.append(tip); defs.append(marker); svg.append(defs);
  const primary = contracts.filter(c => !['repair', 'escalation', 'completion'].includes(c.name)).map(c => [c.name, c.next]);
  const edges = [...primary.map(pair => [...pair, false]), ...extraEdges.map(pair => [...pair, true])];
  const seen = new Set();
  edges.forEach(([from, to, conditional]) => {
    if (seen.has(`${from}:${to}`)) return; seen.add(`${from}:${to}`);
    if (conditional && !$('#recovery').checked) return;
    const a = $(`[data-node="${from}"]`), b = $(`[data-node="${to}"]`);
    if (!a || !b) return;
    const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
    const x = ar.left - bounds.left + ar.width / 2, y = ar.bottom - bounds.top;
    const tx = br.left - bounds.left + br.width / 2, ty = br.top - bounds.top;
    let path;
    if (Math.abs(x - tx) < 5 && ty > y && !conditional) path = `M${x},${y} L${tx},${ty - 3}`;
    else if (Math.abs(x - tx) < 5) {
      const right = ar.right - bounds.left + 8;
      path = `M${ar.right - bounds.left},${ar.top - bounds.top + ar.height / 2} H${right} V${br.top - bounds.top + br.height / 2} H${br.right - bounds.left + 3}`;
    } else {
      const start = ar.right - bounds.left, sy = ar.top - bounds.top + ar.height / 2;
      const end = br.left - bounds.left, ey = br.top - bounds.top + br.height / 2;
      const middle = start + (end - start) / 2;
      path = `M${start},${sy} H${middle} V${ey} H${end - 3}`;
    }
    const line = document.createElementNS(svgNS, 'path'); line.setAttribute('d', path);
    const observed = current.events.some(e => e.node === from && e.data?.next === to && e.kind !== 'started');
    line.setAttribute('class', `edge${conditional ? ' conditional' : ''}${observed ? ' traversed' : ''}`);
    line.setAttribute('marker-end', 'url(#arrow)'); svg.append(line);
  });
}

new ResizeObserver(() => requestAnimationFrame(drawEdges)).observe($('#graph'));
$('#recovery').addEventListener('change', drawEdges);
