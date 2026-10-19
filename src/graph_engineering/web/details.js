import {$, el, badge, nodeTitle, duration, date, pretty, disclosure, fact, modelName} from './dom.js';

const nodeEvidence = {
  repository_discovery: ['base_sha'], evidence_collection: ['baseline'],
  issue_detection: ['candidate'], issue_validation: ['validation'],
  ticket_creation: ['issue'], prioritisation: ['priority'], task_planning: ['plan'],
  isolated_implementation: ['branch', 'head'], test_execution: ['verification'],
  independent_review: ['review'], repair: ['repairs'], pull_request_creation: ['pr'],
  ci_policy_verification: ['policy_snapshot'], approval_gate: ['gate'],
  merge: ['merge'], post_merge_verification: ['postmerge'], completion: ['outcome']
};

export function renderInspector(detail, contracts, name) {
  const root = $('#inspector'); root.replaceChildren();
  const contract = contracts.find(c => c.name === name), observed = detail.nodes[name];
  if (!contract || !observed) return;
  root.append(el('div', 'eyebrow', 'STEP INSPECTOR'), el('h3', '', nodeTitle(name)), badge(observed.status));
  const calls = detail.model_calls.filter(c => c.node === name);
  const facts = el('dl', 'facts');
  facts.append(fact(calls.length ? 'Observed worker' : 'Defined responsibility', calls.length ? modelName(calls.at(-1).model_id) : contract.responsible),
    fact('Attempts recorded', observed.attempts), fact('Failed observations', observed.failures));
  if (observed.last_at) facts.append(fact('Last event', date(observed.last_at)));
  root.append(facts);
  if (observed.status === 'not_run') root.append(el('p', 'muted', 'This step was not visited in the retained event history.'));
  if (observed.failures && observed.status === 'completed') root.append(el('p', 'recovery-note', '↻ Recovered after an earlier failed observation.'));
  const evidenceTitle = el('h4', '', 'Retained evidence'); root.append(evidenceTitle);
  let found = false;
  for (const key of nodeEvidence[name] || []) {
    if (key in detail.evidence) { root.append(disclosure(nodeTitle(key), detail.evidence[key], !found)); found = true; }
  }
  if (name === detail.run.node && detail.run.attention) root.append(el('p', 'step-error', detail.run.attention));
  if (name === 'approval_gate' && detail.approval) root.append(disclosure('Human approval receipt', detail.approval));
  if (!found) root.append(el('p', 'muted', 'Select Evidence or Activity for the available execution receipts.'));
  const rules = el('details', 'disclosure contract'); rules.append(el('summary', '', 'Step contract & safety rules'));
  const rulesFacts = el('dl', 'facts'); rulesFacts.append(fact('Permissions', contract.permissions),
    fact('Timeout', `${contract.timeout_seconds}s`), fact('Attempt limit', contract.max_attempts),
    fact('Inputs', Object.keys(contract.inputs).join(', ')), fact('Outputs', Object.keys(contract.outputs).join(', ')),
    fact('Checkpoint', contract.checkpoint)); rules.append(rulesFacts); root.append(rules);
  const history = detail.events.filter(e => e.node === name || (e.kind === 'model_call' && calls.some(c => c.at === e.at)));
  const historyBlock = el('details', 'disclosure'); historyBlock.append(el('summary', '', `Step activity (${history.length})`));
  const list = el('ol', 'step-events'); history.forEach(e => { const li = el('li'); li.append(el('span', '', nodeTitle(e.kind)), el('time', '', date(e.at, true))); list.append(li); });
  historyBlock.append(list); root.append(historyBlock);
}

function testReceipt(label, receipt) {
  const card = el('section', 'test-receipt'); card.append(el('h4', '', label));
  if (!receipt) { card.append(el('p', 'muted', 'Not executed')); return card; }
  card.append(badge(receipt.passed === true ? 'completed' : 'failed'));
  const dl = el('dl', 'facts'); dl.append(fact('Exit code', receipt.returncode), fact('Duration', duration(receipt.duration_seconds)), fact('Command', (receipt.argv || []).join(' ')));
  if (receipt.timed_out) dl.append(fact('Timeout', 'Yes'));
  card.append(dl, disclosure('Execution output', receipt.output || 'No output retained.'));
  return card;
}

export function renderEvidence(detail) {
  const root = $('#panel-evidence'), evidence = detail.evidence; root.replaceChildren();
  root.append(el('h3', '', 'Independent verification'), el('p', 'section-description', 'Process results and the actual Git diff retained by this execution.'));
  const receipts = el('div', 'test-receipts');
  receipts.append(testReceipt('Original regression', evidence.baseline), testReceipt('After implementation', evidence.verification), testReceipt('After merge', evidence.postmerge)); root.append(receipts);
  const diff = el('section', 'evidence-section'); diff.append(el('h3', '', 'Code changes'));
  if (evidence.head) diff.append(el('p', 'commit-reference', `Tested commit · ${evidence.head}`));
  const code = el('pre', 'code-block diff');
  if (evidence.diff) {
    evidence.diff.split('\n').forEach(line => { const span = el('span', line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : '', line + '\n'); code.append(span); });
  } else code.textContent = 'No implementation diff was recorded.';
  diff.append(code); root.append(diff);
  const review = el('section', 'evidence-section'); review.append(el('h3', '', 'Independent review'));
  if (evidence.review) {
    review.append(badge(evidence.review.approved ? 'approved' : 'rejected'), el('p', 'review-rationale', evidence.review.rationale));
    if (evidence.review.findings?.length) review.append(disclosure('Review findings', evidence.review.findings, true));
  } else review.append(el('p', 'muted', 'No review has been recorded.'));
  root.append(review);
  const models = el('section', 'evidence-section'); models.append(el('h3', '', 'Model execution receipts'));
  if (!detail.model_calls.length) models.append(el('p', 'muted', 'No model calls were recorded.'));
  detail.model_calls.forEach(call => {
    const item = el('div', 'model-receipt');
    const heading = el('div', 'model-receipt-heading'); heading.append(el('strong', '', nodeTitle(call.role)), badge(call.success ? 'completed' : 'failed'));
    item.append(heading, el('p', '', `${modelName(call.model_id)} · ${duration(call.latency_seconds)} · OpenCode ${call.opencode_version || 'version not recorded'}`), disclosure('Receipt details', call)); models.append(item);
  }); root.append(models);
  for (const [key, label] of [['candidate', 'Validated issue'], ['plan', 'Implementation plan'], ['policy_snapshot', 'CI & branch policy snapshot'], ['gate', 'Merge decision'], ['merge', 'Merge receipt']]) {
    if (evidence[key]) root.append(disclosure(label, evidence[key]));
  }
  if (detail.approval) root.append(disclosure('Human approval receipt', detail.approval));
}

export function renderActivity(detail) {
  const root = $('#panel-activity'); root.replaceChildren();
  root.append(el('h3', '', 'Execution activity'), el('p', 'section-description', `${detail.events.length} retained events in chronological order. Times use your local timezone.`));
  if (detail.events_truncated) root.append(el('p', 'attention', 'Only the first 2,000 events are shown. Use the CLI for the full history.'));
  const list = el('ol', 'timeline');
  for (const event of detail.events) {
    const li = el('li', `timeline-event ${event.kind}`);
    const time = el('time', '', date(event.at, true)); time.dateTime = event.at || '';
    const content = el('div', 'event-content');
    const header = el('div', 'event-heading'); header.append(el('strong', '', nodeTitle(event.node)), el('span', 'event-kind', nodeTitle(event.kind)));
    content.append(header);
    if (event.kind === 'model_call') content.append(el('p', 'muted', `${modelName(event.data.model_id)} · ${event.data.success ? 'Success' : 'Failed'} · ${duration(event.data.latency_seconds)}`));
    else if (event.data.message) content.append(el('p', '', event.data.message));
    else if (event.data.next && event.kind !== 'started') content.append(el('p', 'muted', `Next: ${nodeTitle(event.data.next)} · ${event.data.status || ''}`));
    content.append(disclosure(`Event #${event.seq} details`, event.data)); li.append(time, content); list.append(li);
  }
  root.append(list);
}

export function renderProofs(detail, showTab) {
  const root = $('#proofs'); root.replaceChildren();
  const evidence = detail.evidence;
  const items = [
    ['Regression tests', evidence.verification ? (evidence.verification.passed ? '✓ Independently passed' : '× Failed') : '○ Not executed', 'evidence'],
    ['Diff review', evidence.review ? (evidence.review.approved ? '✓ Approved in separate session' : '↻ Changes requested') : '○ Not recorded', 'evidence'],
    ['Merge policy', evidence.gate ? (evidence.gate.allowed ? '✓ Gate satisfied at execution' : 'Ⅱ Gate blocked') : '○ Not evaluated', 'activity'],
    ['Post-merge check', evidence.postmerge ? (evidence.postmerge.passed ? '✓ Passed at merged commit' : '× Failed after merge') : '○ Not executed', 'evidence']
  ];
  for (const [label, value, tab] of items) {
    const b = el('button', 'proof'); b.type = 'button'; b.append(el('span', 'proof-label', label), el('strong', '', value), el('span', 'proof-arrow', '↗')); b.addEventListener('click', () => showTab(tab, true)); root.append(b);
  }
}
