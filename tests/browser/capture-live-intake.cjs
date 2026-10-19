/* Read-only capture of real controller state. Does not create or dispatch work. */
const fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright-core');
const root=path.resolve(__dirname,'../..'),out=path.join(root,'evidence/phase3');
const url=process.argv[2]||'http://127.0.0.1:8771/?view=network&ticket=GE-DD0C3FE0&repository=b08ad3da5c8b7546';
const prefix=process.argv[3]||'live-general';
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1600,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));await page.goto(url);
  await page.locator('#replay-label').waitFor().catch(()=>page.locator('#replay').waitFor());
  await page.screenshot({path:path.join(out,prefix+'-network.png'),fullPage:true});
  const snapshot=await(await page.request.get(new URL('/api/centre',url).href)).json();
  await page.getByRole('button',{name:'Ticket board',exact:false}).click();
  const id=new URL(url).searchParams.get('ticket');await page.locator(`[data-ticket="${id}"]`).click();
  await page.getByRole('heading',{name:'Original User Description',exact:true}).waitFor();
  await page.getByRole('heading',{name:'Original User Description',exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(out,prefix+'-enhanced.png')});
  fs.writeFileSync(path.join(out,prefix+'-browser-capture.json'),JSON.stringify({kind:'read-only real-controller capture',at:new Date().toISOString(),url,ticket:snapshot.tickets.find(t=>t.id===id),errors},null,2));
  if(errors.length)throw Error(errors.join('\n'));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
