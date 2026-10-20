export const $ = (selector, root = document) => root.querySelector(selector);
export function el(tag, className = '', text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null && text !== undefined) node.textContent = String(text);
  return node;
}
export const labels = {
  ready: 'Ready', running: 'Running', completed: 'Completed', blocked: 'Blocked',
  failed: 'Failed', waiting_approval: 'Awaiting approval', no_findings: 'No findings',
  duplicate: 'Duplicate', cancelled: 'Stopped', escalated: 'Escalated', scanned: 'Scanned',
  not_run: 'Not run', needs_repair: 'Needs repair', approved: 'Approved', rejected: 'Rejected'
};
export const icons = {completed: '✓', no_findings: '✓', running: '◉', ready: '○',
  blocked: '!', failed: '×', waiting_approval: 'Ⅱ', escalated: '!', cancelled: '■', needs_repair: '↻'};
export const nodeTitle = name => String(name).split('_').map(s => s[0]?.toUpperCase() + s.slice(1)).join(' ');
export function badge(status, extra = '') {
  return el('span', `badge ${status} ${extra}`, `${icons[status] || '○'} ${labels[status] || nodeTitle(status)}`);
}
export function duration(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${Number(seconds).toFixed(1)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`;
}
export function date(value, timeOnly = false) {
  if (!value) return 'Time unavailable';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : (timeOnly ? d.toLocaleTimeString() : d.toLocaleString());
}
export function pretty(value) { return typeof value === 'string' ? value : JSON.stringify(value, null, 2); }
export function safeLink(url, text) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === 'https:' && parsed.hostname === 'github.com' && !parsed.username && !parsed.password) {
      const a = el('a', 'button secondary', `${text} ↗`);
      a.href = parsed.href; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;
    }
  } catch { /* Local publication objects have no external link. */ }
  return el('span', 'local-reference', text);
}
export function disclosure(title, value, open = false) {
  const d = el('details', 'disclosure'); d.open = open;
  d.append(el('summary', '', title), el('pre', 'code-block', pretty(value)));
  return d;
}
export function fact(label, value) {
  const row = el('div', 'fact'); row.append(el('dt', '', label), el('dd', '', value ?? 'Not recorded')); return row;
}
export function modelName(id = '') {
  const family = id.includes('qwen3.8') ? 'Qwen 3.8' : id.includes('qwen35') || id.includes('qwen3.5') ? 'Qwen 3.5' : null;
  const size = id.match(/[-_:](\d+(?:\.\d+)?)b\b/i)?.[1];  // The size comes from the id, e.g. qwen35-9b:16k.
  if (family) return size ? `${family} · ${size}B` : family;
  return id || 'Model not recorded';
}
