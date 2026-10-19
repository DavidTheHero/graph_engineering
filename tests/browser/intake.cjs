/* Browser acceptance tests use real HTTP/Git/SQLite with labelled inference. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {spawn,execFileSync}=require('node:child_process'),readline=require('node:readline');
const {chromium}=require('playwright-core'),{AxeBuilder}=require('@axe-core/playwright');
const root=path.resolve(__dirname,'../..'),python=path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const output=process.env.GE_BROWSER_OUTPUT||path.join(root,'evidence/phase3');fs.mkdirSync(output,{recursive:true});
const folder=fs.mkdtempSync(path.join(root,'.runtime/intake-browser-')),home=path.join(folder,'controller');
const receipt={kind:'browser-acceptance-with-scripted-inference',at:new Date().toISOString(),checks:[],accessibility:[],errors:[]};
let server,browser;
function check(name){receipt.checks.push({name,passed:true});console.log('PASS '+name);}
async function audit(page,view){const a=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();receipt.accessibility.push({view,violations:a.violations});assert.equal(a.violations.length,0,JSON.stringify(a.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}))));}
async function serve(){server=spawn(python,['-u',path.join(__dirname,'intake_fixture.py'),home],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});let errors='';server.stderr.on('data',d=>errors+=d);return new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error(errors||'server timeout')),15000);readline.createInterface({input:server.stdout}).once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});server.once('exit',code=>{clearTimeout(timer);reject(Error('server '+code+': '+errors));});});}
function investigate(id,clarify=false){const code=`import sys
from pathlib import Path
from unittest.mock import patch
from graph_engineering.tickets import Tickets
from graph_engineering.workflow import Workflow
from graph_engineering.scheduler import claim,execute_assignment
from tests.test_intake import InvestigationDouble
def worker(home):
 flow=Workflow(home);flow.worker=InvestigationDouble(clarify=${clarify?'True':'False'});return flow
home=Path(sys.argv[1]);target=sys.argv[2]
with patch('graph_engineering.scheduler.Workflow',worker):
 for step in range(30):
  tickets=Tickets(home)
  if tickets.get(target)['status']!='ready':tickets.store.close();break
  assigned=claim(tickets);tickets.store.close();assert assigned and assigned['id']==target
  assert execute_assignment(home,target,assigned['lease_owner'])==0
`;
execFileSync(python,['-c',code,home,id],{cwd:root,windowsHide:true});}
(async()=>{
 const fixture=await serve(),url=`http://127.0.0.1:${fixture.port}`;
 browser=await chromium.launch({channel:process.env.UI_TEST_BROWSER||'msedge',headless:true});receipt.browser=browser.version();
 const context=await browser.newContext({viewport:{width:1440,height:1000}}),page=await context.newPage();
 page.on('pageerror',e=>receipt.errors.push(e.message));page.on('console',m=>{if(['warning','error'].includes(m.type()))receipt.errors.push(m.text());});
 await page.goto(url);await page.getByRole('button',{name:'+ Create Ticket',exact:true}).click();
 assert.equal(await page.locator('#operator-dialog [required]').count(),1);
 const original='  Adding values gives the wrong total.\nPlease investigate.  ';
 await page.getByLabel('Describe the Problem',{exact:true}).fill(original);await audit(page,'create-form');
 await page.getByRole('button',{name:'Create & Investigate',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.ticket-card').length===1);
 let snapshot=await(await context.request.get(url+'/api/centre')).json(),id=snapshot.tickets[0].id;
 assert.equal(snapshot.tickets[0].lifecycle,'New / Queued for Investigation');
 let detail=await(await context.request.get(url+'/api/centre/ticket?id='+id)).json();assert.equal(detail.intake_report.original,original);assert.equal(detail.model_calls.length,0);check('Plain-English form returns a durable ticket before inference; exact original preserved');
 await page.locator(`[data-ticket="${id}"]`).click();await page.getByRole('button',{name:'View ticket journey'}).click();
 investigate(id);await page.waitForFunction(()=>document.querySelector('#network').dataset.lastTransition);
 await page.getByLabel('Inspect a node or queue',{exact:true}).selectOption('queue:intake_review');await page.waitForFunction(()=>document.querySelector('#node-inspector').textContent.includes('Review the enhanced revision'));
 await page.locator('#node-inspector .inspector-ticket').click();await page.getByText('Addition subtracts the second operand',{exact:true}).first().waitFor();
 assert.equal(await page.locator('.original-description').innerText(),original);assert.match(await page.locator('.intake-enhanced').innerText(),/Acceptance Criteria/);
 assert.match(await page.locator('.intake-enhanced').innerText(),/Hypothesis/);await audit(page,'enhanced-ticket');
 await page.screenshot({path:path.join(output,'enhanced-ticket-tested.png'),fullPage:true});check('Actual graph dispatch, source validation and original/enhanced comparison synchronise');
 await page.getByRole('button',{name:'Edit enhancement',exact:true}).click();await page.locator('#operator-dialog').getByLabel('Title',{exact:true}).fill('Correct calculator addition');await page.getByRole('button',{name:'Save revision',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#ticket-detail-heading').textContent.includes('Correct calculator addition'));
 await page.getByRole('button',{name:'Accept enhancement',exact:true}).click();await page.getByRole('button',{name:'Send to Development Queue',exact:true}).waitFor({state:'visible'});
 await page.waitForFunction(()=>document.querySelector('#ticket-live-status')?.textContent.includes('Accepted ticket'));
 await page.getByRole('button',{name:'Send to Development Queue',exact:true}).click();
 snapshot=await(await context.request.get(url+'/api/centre')).json();assert.equal(snapshot.tickets[0].queue,'development');assert.equal(snapshot.tickets[0].branch,null);
 detail=await(await context.request.get(url+'/api/centre/ticket?id='+id)).json();assert.equal(detail.intake_report.revisions.length,2);assert.equal(detail.intake_report.original,original);check('Editing appends a revision; acceptance and development admission do not implement code');
 await page.getByRole('button',{name:'Close ticket',exact:true}).click();await page.getByRole('button',{name:'Ticket board'}).click();await page.reload();await page.waitForFunction(()=>document.querySelectorAll('.ticket-card').length===1);assert.match(await page.locator('.ticket-card').innerText(),/Development Queued/);check('Reload restores accepted content and authoritative development queue');
 await page.getByRole('button',{name:'+ Create Ticket',exact:true}).click();await page.getByLabel('Describe the Problem',{exact:true}).fill('Sometimes the screen behaves strangely and I cannot explain it');await page.getByRole('button',{name:'Create & Investigate',exact:true}).click();await page.waitForFunction(()=>document.querySelectorAll('.ticket-card').length===2);
 snapshot=await(await context.request.get(url+'/api/centre')).json();const ambiguous=snapshot.tickets.find(t=>t.id!==id).id;investigate(ambiguous,true);
 await page.waitForFunction(ticket=>document.querySelector(`[data-ticket="${ticket}"]`)?.textContent.includes('Needs Information'),ambiguous);
 await page.locator(`[data-ticket="${ambiguous}"]`).click();assert.match(await page.locator('.intake-enhanced').innerText(),/Unverified proposal/);await page.getByRole('button',{name:'Provide information',exact:true}).click();await page.getByLabel('Additional information or investigation request',{exact:true}).fill('The wrong total occurs when adding 2 and 3; it returns -1.');await page.getByRole('button',{name:'Queue investigation',exact:true}).click();await page.getByRole('button',{name:'Close ticket',exact:true}).click();investigate(ambiguous);
 await page.waitForFunction(ticket=>document.querySelector(`[data-ticket="${ticket}"]`)?.textContent.includes('Ready for Review'),ambiguous);check('Ambiguous reports persist, request clarification and re-enter investigation');
 await page.getByLabel('Repository',{exact:true}).selectOption(fixture.repository_id);
 await page.getByRole('button',{name:'Autonomy policy',exact:true}).click();
 await page.getByLabel('Automatically develop accepted tickets',{exact:true}).check();
 await page.getByLabel('Automatically create local issue / PR previews',{exact:true}).check();
 await page.getByLabel('Policy operator',{exact:true}).fill('Browser acceptance operator');await audit(page,'autonomy-policy');
 await page.getByRole('button',{name:'Save repository policy',exact:true}).click();
 snapshot=await(await context.request.get(url+'/api/centre')).json();assert.equal(snapshot.repositories.find(r=>r.id===fixture.repository_id).autonomy.development,true);
 await page.reload();await page.getByRole('button',{name:'Autonomy policy',exact:true}).click();
 assert(await page.getByLabel('Automatically develop accepted tickets',{exact:true}).isChecked());
 await page.getByRole('button',{name:'Cancel',exact:true}).click();check('Repository autonomy consent persists through the real API and browser reload');
 await page.locator(`[data-ticket="${ambiguous}"]`).click();await page.getByRole('button',{name:'Cancel task',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('#ticket-live-status')?.textContent.includes('Cancelled'));
 assert.equal(await page.getByRole('button',{name:'Resume',exact:true}).count(),0);
 assert(await page.getByRole('button',{name:'Send to Development Queue',exact:true}).isDisabled());
 await page.getByRole('button',{name:'Close ticket',exact:true}).click();
 const cancelledColumn=page.locator('.kanban-column[data-status="Cancelled"]');
 assert.equal(await cancelledColumn.locator(`[data-ticket="${ambiguous}"]`).count(),1);check('Cancellation remains distinct and cannot accidentally resume or admit development');
 await page.screenshot({path:path.join(output,'autonomy-board-tested.png'),fullPage:true});
 await page.setViewportSize({width:320,height:900});await audit(page,'mobile-board');const size=await page.evaluate(()=>({width:innerWidth,page:document.documentElement.scrollWidth}));assert(size.page<=size.width+1);check('Mobile layout and accessibility remain valid');
 assert.deepEqual(receipt.errors,[]);check('No browser warnings or runtime errors');
})().catch(error=>{receipt.failure=error.stack;process.exitCode=1;console.error(error);}).finally(async()=>{if(browser)await browser.close();if(server&&server.exitCode===null){server.kill();await new Promise(resolve=>server.once('exit',resolve));}receipt.passed=!receipt.failure;fs.writeFileSync(path.join(output,'browser-tests.json'),JSON.stringify(receipt,null,2));});
