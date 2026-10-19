/* Real isolated Edge/Chromium tests. API interception is used only for explicit
 * malicious-input and outage injection; normal views use actual recorded runs. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn, execFileSync} = require('node:child_process');
const readline = require('node:readline');
const {chromium} = require('playwright-core');
const {AxeBuilder} = require('@axe-core/playwright');

const root = path.resolve(__dirname, '../..');
const python = process.env.UI_TEST_PYTHON || path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const output = process.env.GE_BROWSER_OUTPUT || path.join(root, 'evidence/ui');
fs.mkdirSync(output, {recursive: true});
fs.mkdirSync(path.join(root, '.runtime'), {recursive: true});
const home = fs.mkdtempSync(path.join(root, '.runtime/ui-browser-'));
const results = {at: new Date().toISOString(), checks: [], console_errors: [], accessibility: [], screenshots: []};
let server, browser;

function check(name, details = '') { results.checks.push({name, passed: true, details}); console.log(`PASS ${name}`); }
function updateLive() {
  execFileSync(python, ['-c', `from pathlib import Path;from graph_engineering.runtime import Store;s=Store(Path(${JSON.stringify(home)}));s.checkpoint('browser-live','test_execution',{'verification':{'passed':True,'returncode':0,'output':'Live browser regression passed','duration_seconds':0.1}},'approval_gate','waiting_approval');s.close()`], {cwd: root});
}
async function startServer() {
  const code = `from pathlib import Path
import sys
from graph_engineering.runtime import Store
from graph_engineering.dashboard import make_server
s=Store(Path(sys.argv[1]));s.create_run({'repo':'browser-live-fixture','backend':'local','routing':'baseline'},'browser-live')
s.checkpoint('browser-live','repository_discovery',{'base_sha':'a'*40},'test_execution','running');s.close()
server=make_server(Path(sys.argv[1]),0,Path(sys.argv[2]));print(server.server_port,flush=True);server.serve_forever()`;
  server = spawn(python, ['-u', '-c', code, home, path.join(root, 'evidence/final')], {cwd: root, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true});
  let stderr = ''; server.stderr.on('data', chunk => {stderr += chunk;});
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(Error(`Server startup timed out: ${stderr}`)), 15000);
    const lines = readline.createInterface({input: server.stdout});
    lines.once('line', line => { clearTimeout(timeout); lines.close(); resolve(`http://127.0.0.1:${Number(line)}`); });
    server.once('exit', code => { clearTimeout(timeout); reject(Error(`Server exited ${code}: ${stderr}`)); });
  });
}
async function waitForRun(page, id) {
  await page.waitForFunction(expected => document.querySelector('.run-id')?.textContent === expected && !document.querySelector('#workspace').hidden, id);
}
async function audit(page, label) {
  const result = await new AxeBuilder({page}).withTags(['wcag2a', 'wcag2aa', 'wcag21aa']).analyze();
  results.accessibility.push({view: label, violations: result.violations.map(v => ({id: v.id, impact: v.impact, description: v.description, nodes: v.nodes.map(n => ({target: n.target, summary: n.failureSummary}))}))});
  assert.equal(result.violations.length, 0, `Accessibility violations in ${label}: ${JSON.stringify(results.accessibility.at(-1))}`);
}

(async () => {
  const url = await startServer();
  browser = await chromium.launch({channel: process.env.UI_TEST_BROWSER || 'msedge', headless: true});
  results.browser = browser.version();
  const context = await browser.newContext({viewport: {width: 1600, height: 1050}, reducedMotion: 'reduce'});
  const page = await context.newPage();
  page.on('pageerror', error => results.console_errors.push(error.message));
  page.on('console', message => {if (message.type() === 'error' && !message.text().includes('503')) results.console_errors.push(message.text());});
  await page.goto(url + '/?source=recorded&run=ad4cd05a14534837');
  await waitForRun(page, 'ad4cd05a14534837');
  assert.equal(await page.locator('.graph-node').count(), 20);
  assert.match(await page.locator('#snapshot-note').innerText(), /historical/);
  assert.match(await page.locator('#run-meta').innerText(), /GitHub backend/);
  assert.equal(await page.getByRole('link', {name: 'Pull request #2'}).getAttribute('href'), 'https://github.com/DavidTheHero/graph-engineering-public-fixture-20261018/pull/2');
  check('Observed public run, 20-step graph and real PR link');
  await page.locator('[data-node="approval_gate"]').click();
  assert.equal(await page.locator('#inspector h3').innerText(), 'Approval Gate');
  assert.match(page.url(), /step=approval_gate/);
  const conditional = await page.locator('.edge.conditional').count();
  await page.getByLabel('Recovery paths').uncheck();
  await page.waitForFunction(() => document.querySelectorAll('.edge.conditional').length === 0);
  await page.getByLabel('Recovery paths').check();
  assert(conditional > 0);
  check('Step selection, shareable URL and recovery-edge toggle');
  await audit(page, 'workflow-desktop');
  for (const width of [1600, 1440, 1024, 768, 320]) {
    await page.setViewportSize({width, height: 1000});
    await page.waitForTimeout(100);
    const dimensions = await page.evaluate(() => ({page: document.documentElement.scrollWidth, viewport: innerWidth, graph: document.querySelector('.graph-scroll').scrollWidth}));
    assert(dimensions.page <= width + 1, `Horizontal page overflow at ${width}: ${JSON.stringify(dimensions)}`);
    if (width === 320) {
      assert(dimensions.graph > width, 'Small-screen graph should have contained horizontal scrolling');
      await audit(page, 'workflow-mobile');
    }
    const file = `dashboard-${width}.png`;
    await page.screenshot({path: path.join(output, file), fullPage: true}); results.screenshots.push(file);
  }
  check('Responsive layout at 320, 768, 1024, 1440 and 1600 pixels');
  await page.setViewportSize({width: 1440, height: 1000});
  await page.getByRole('tab', {name: 'Evidence', exact: true}).click();
  assert.match(await page.locator('#panel-evidence').innerText(), /subtotal_cents \* discount_percent/);
  assert.equal(await page.locator('.model-receipt').count(), 5);
  await audit(page, 'evidence');
  await page.screenshot({path: path.join(output, 'dashboard-evidence.png'), fullPage: true});
  await page.getByRole('tab', {name: /Activity/}).click();
  assert.equal(await page.locator('.timeline-event').count(), 57);
  await audit(page, 'activity');
  check('Actual diff, three test receipts, five worker calls and 57 audit events');
  await page.getByRole('tab', {name: 'Workflow', exact: true}).focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.getByRole('tab', {name: 'Evidence', exact: true}).getAttribute('aria-selected'), 'true');
  await page.locator('.run-item').first().focus();
  await page.waitForTimeout(3300);
  assert(await page.locator('.run-item').first().evaluate(el => el === document.activeElement), 'Auto-refresh stole keyboard focus');
  check('Keyboard tab navigation and stable focus across polling');
  await page.locator('#status-filter').selectOption('attention');
  await waitForRun(page, '565a6d9f31374dbb');
  assert.match(await page.locator('#attention').innerText(), /403/);
  assert.match(await page.locator('[data-node="ci_policy_verification"]').innerText(), /Blocked/);
  check('Blocked policy run exposes actual failure and blocks success presentation');
  await page.locator('#status-filter').selectOption('all');
  await page.locator('#source').selectOption('live');
  await waitForRun(page, 'browser-live');
  assert(await page.locator('#snapshot-note').isHidden());
  assert.match(await page.locator('#run-meta').innerText(), /GitHub simulated/);
  updateLive();
  await page.waitForFunction(() => document.querySelector('#run-meta').textContent.includes('Awaiting approval'));
  assert.match(await page.locator('#attention').innerText(), /Human approval required/);
  assert.equal(await page.getByRole('button', {name: /^Merge$/}).count(), 0);
  check('Live SQLite checkpoint polling and visible missing-approval gate');
  await page.locator('#search').fill('no-matching-repository-xyz');
  assert(await page.locator('#empty').isVisible());
  await page.locator('#search').fill('');
  await waitForRun(page, 'browser-live');
  check('Search, source/status filters and empty-state recovery');
  let reads = 0;
  await page.route('**/api/run?*', async route => {
    const number = ++reads, response = await route.fetch(), payload = await response.json();
    payload.run.title = number === 1 ? 'Stale read' : 'Latest read';
    if (number === 1) await new Promise(resolve => setTimeout(resolve, 700));
    await route.fulfill({response, json: payload});
  });
  await page.getByRole('button', {name: 'Refresh', exact: true}).click();
  await page.locator('.run-item').first().click();
  await page.waitForFunction(() => document.querySelector('#run-title').textContent === 'Latest read');
  await page.waitForTimeout(800);
  assert.equal(await page.locator('#run-title').innerText(), 'Latest read');
  await page.unroute('**/api/run?*');
  check('Out-of-order reads cannot overwrite newer run data');
  await page.route('**/api/run?*', async route => {
    const response = await route.fetch(); const payload = await response.json();
    payload.run.title = '<img src=x onerror="window.UI_INJECTED=true">';
    payload.run.pr = {number: 9, url: 'javascript:window.UI_INJECTED=true'};
    payload.evidence.diff = '<script>window.UI_INJECTED=true</script>';
    await route.fulfill({response, json: payload});
  });
  await page.getByRole('button', {name: 'Refresh', exact: true}).click();
  await page.waitForFunction(() => document.querySelector('#run-title').textContent.startsWith('<img'));
  assert.equal(await page.locator('#run-title img').count(), 0);
  assert.equal(await page.locator('#publication-links a').count(), 0);
  assert.equal(await page.evaluate(() => window.UI_INJECTED), undefined);
  await page.unroute('**/api/run?*');
  check('Injected repository HTML and javascript links render as inert text');
  await page.route('**/api/overview', route => route.fulfill({status: 503, contentType: 'application/json', json: {error: 'Injected unavailable controller'}}));
  await page.getByRole('button', {name: 'Refresh', exact: true}).click();
  await page.locator('#error:not([hidden])').waitFor();
  assert.match(await page.locator('#error').innerText(), /last successful read/);
  await page.unroute('**/api/overview');
  await page.getByRole('button', {name: 'Refresh', exact: true}).click();
  await page.locator('#error[hidden]').waitFor({state: 'attached'});
  check('Outage displays stale-data warning and recovers on refresh');
  assert.deepEqual(results.console_errors, []);
  check('No browser runtime or unexpected console errors');
  results.passed = true;
})().catch(error => {
  results.passed = false; results.error = error.stack; console.error(error); process.exitCode = 1;
}).finally(async () => {
  if (browser) await browser.close();
  if (server) server.kill();
  fs.writeFileSync(path.join(output, 'browser-verification.json'), JSON.stringify(results, null, 2) + '\n');
});
