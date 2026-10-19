/* Isolated real browser/backend integration; inference double is labelled. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {spawn,execFileSync}=require('node:child_process');
const readline=require('node:readline');
const {chromium}=require('playwright-core');
const {AxeBuilder}=require('@axe-core/playwright');
const root=path.resolve(__dirname,'../..');
const python=process.env.UI_TEST_PYTHON||path.join(root,'.venv',process.platform==='win32'?'Scripts/python.exe':'bin/python');
const output=process.env.GE_BROWSER_OUTPUT||path.join(root,'evidence/phase2');
fs.mkdirSync(output,{recursive:true});
const folder=fs.mkdtempSync(path.join(root,'.runtime/centre-browser-'));
const home=path.join(folder,'controller');
const receipt={at:new Date().toISOString(),kind:'browser-integration-with-labelled-inference-double',checks:[],accessibility:[],errors:[]};
let server,browser;
function check(name){receipt.checks.push({name,passed:true});console.log('PASS '+name);}
async function audit(page,view){const result=await new AxeBuilder({page}).withTags(['wcag2a','wcag2aa','wcag21aa']).analyze();receipt.accessibility.push({view,violations:result.violations});assert.equal(result.violations.length,0,JSON.stringify(result.violations.map(v=>({id:v.id,nodes:v.nodes.map(n=>n.target)}))));}
async function startServer(){server=spawn(python,['-u',path.join(__dirname,'centre_fixture.py'),home],{cwd:root,windowsHide:true,stdio:['ignore','pipe','pipe']});let errors='';server.stderr.on('data',data=>{errors+=data;});return new Promise((resolve,reject)=>{const timer=setTimeout(()=>reject(Error(errors||'Server startup timeout')),30000);readline.createInterface({input:server.stdout}).once('line',line=>{clearTimeout(timer);resolve(JSON.parse(line));});server.once('exit',code=>{clearTimeout(timer);reject(Error(`Server ${code}: ${errors}`));});});}
function step(){execFileSync(python,['-c',`from pathlib import Path;from graph_engineering.tickets import Tickets;from graph_engineering.scheduler import claim,execute_assignment;t=Tickets(Path(${JSON.stringify(home)}));a=claim(t);assert a;t.store.close();assert execute_assignment(Path(${JSON.stringify(home)}),a['id'],a['lease_owner'])==0`],{cwd:root,windowsHide:true});}
(async()=>{
 const fixture=await startServer();const url=`http://127.0.0.1:${fixture.port}`;
 browser=await chromium.launch({channel:process.env.UI_TEST_BROWSER||'msedge',headless:true});receipt.browser=browser.version();
 const context=await browser.newContext({viewport:{width:1600,height:1050}});const page=await context.newPage();
 page.on('pageerror',e=>receipt.errors.push(e.message));page.on('console',m=>{if(['error','warning'].includes(m.type()))receipt.errors.push(m.text());});
 await page.goto(url);await page.waitForFunction(()=>document.querySelectorAll('.ticket-card').length===3);
 assert.equal(await page.locator('.kanban-column').count(),10);await audit(page,'board-dark');check('Ten authoritative board columns including cancellation and three persistent tickets');
 await page.locator(`[data-ticket="${fixture.reviewed}"]`).click();await page.getByRole('button',{name:'View ticket journey'}).waitFor();
 assert.match(await page.locator('#ticket-detail').innerText(),/Local ticket/);assert.match(await page.locator('#ticket-detail').innerText(),/Repair/);
 await page.getByRole('button',{name:'View ticket journey'}).click();await page.locator('#network canvas').first().waitFor();
 await page.getByRole('button',{name:'Fit view',exact:true}).click();assert.match(await page.locator('#network-mode').innerText(),new RegExp(fixture.reviewed));
 await page.getByRole('button',{name:'Replay history'}).click();await page.waitForFunction(()=>document.querySelector('#replay-label').textContent.includes('HISTORICAL REPLAY'));
 await page.getByRole('button',{name:'Return to live'}).click();assert.match(await page.locator('#replay-label').innerText(),/Live/);check('Ticket journey includes actual injected review loop and explicit historical replay');
 await page.locator('#node-picker').selectOption('approval_gate');assert.match(await page.locator('#node-inspector').innerText(),new RegExp(fixture.reviewed));
 await page.locator('#node-inspector .inspector-ticket').click();await page.getByRole('button',{name:'Approve tested commit'}).waitFor();
 await page.getByRole('button',{name:'Close ticket',exact:true}).click();check('Node inspector opens its actual ticket; unapproved merge remains held');
 await page.getByRole('button',{name:'Clear journey'}).click();
 step();await page.waitForFunction(()=>document.querySelector('#network').dataset.lastTransition);
 const eventId=Number(await page.locator('#network').getAttribute('data-last-transition'));
 const history=await (await context.request.get(url+'/api/centre/history?after=0')).json();assert(history.events.some(e=>e.event_id===eventId&&e.ticket_id===fixture.queued));
 check('Ticket animation is correlated to a real persisted scheduler transition');
 await audit(page,'network-dark');await page.screenshot({path:path.join(output,'centre-network-tested.png'),fullPage:true});
 await page.getByRole('button',{name:'Queues & workers'}).click();assert.match(await page.locator('#monitor').innerText(),/Waiting for Dependency/);assert.match(await page.locator('#monitor').innerText(),new RegExp(fixture.dependent));
 await page.getByRole('button',{name:'Pause admission',exact:true}).click();await page.getByRole('button',{name:'Resume admission',exact:true}).waitFor();
 let snapshot=await (await context.request.get(url+'/api/centre')).json();assert.equal(snapshot.scheduler.paused,true);
 await page.getByRole('button',{name:'Resume admission',exact:true}).click();await page.getByRole('button',{name:'Pause admission',exact:true}).waitFor();
 await page.getByRole('button',{name:'Worker limit',exact:true}).click();await page.getByLabel('Maximum concurrent node executions (1–8)').fill('3');await page.getByRole('button',{name:'Save limit',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#scheduler-summary').textContent.includes('/3 workers'));
 await audit(page,'monitor-dark');check('Dependency reason, pause/resume and concurrency controls persist in backend');
 await page.getByRole('button',{name:'Ticket board'}).click();await page.locator('#ticket-search').fill(fixture.queued);assert.equal(await page.locator('.ticket-card').count(),1);await page.locator('#ticket-search').fill('');
 await page.locator(`[data-ticket="${fixture.queued}"]`).click();await page.getByRole('button',{name:'Edit triage'}).click();await page.locator('#operator-dialog').getByLabel('Priority',{exact:true}).selectOption('P0');await page.getByRole('button',{name:'Save triage',exact:true}).click();await page.getByRole('button',{name:'Close ticket',exact:true}).click();
 await page.reload();await page.waitForFunction(()=>document.querySelectorAll('.ticket-card').length===3);assert.match(await page.locator(`[data-ticket="${fixture.queued}"]`).innerText(),/P0/);check('Search, triage and browser refresh share persistent backend state');
 await page.getByRole('button',{name:'Toggle dark mode'}).click();await audit(page,'board-light');
 for(const width of [1600,1024,768,320]){await page.setViewportSize({width,height:1000});const dimensions=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth}));assert(dimensions.scroll<=width+1,JSON.stringify(dimensions));if(width===320)await audit(page,'board-mobile');}
 await page.screenshot({path:path.join(output,'centre-mobile-tested.png'),fullPage:true});check('Light/dark accessibility and responsive layout at 320–1600px');
 assert.deepEqual(receipt.errors,[]);check('Zero browser console warnings or errors');
})().catch(error=>{receipt.failure=error.stack;process.exitCode=1;console.error(error);}).finally(async()=>{if(browser)await browser.close();if(server&&server.exitCode===null){server.kill();await new Promise(resolve=>server.once('exit',resolve));}receipt.passed=!receipt.failure;fs.writeFileSync(path.join(output,'browser-tests.json'),JSON.stringify(receipt,null,2));});
