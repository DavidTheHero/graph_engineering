import {state,$,el,columns,categories,human,tag,shortModel,elapsed,stamp,filtered,json,command,toast,showError,facts,codeSection} from './centre-state.js';
import {renderBoard,renderMonitor} from './centre-board.js';
import {renderNetwork,animateEvents,highlight,replayAt,returnLive,resizeNetwork,themeNetwork,toggleCluster} from './centre-network.js';
import {openTicket,repositoryStart,connectRepository,formDialog,field,refreshTicketSummary,refreshTicketEvidence,createTicket,autonomyPolicy} from './centre-dialogs.js';

let source,refreshing=false,refreshAgain=false,repoSignature='',latestSignature='',refreshTimer,replayTimer;
const url=new URL(location.href),seenEvents=new Set();
state.view=['board','network','monitor'].includes(url.searchParams.get('view'))?url.searchParams.get('view'):'board';
state.selected=url.searchParams.get('ticket');
let waitingAnimations=[],inspectedNode=null;
try{document.documentElement.dataset.theme=localStorage.getItem('ge-theme')||'dark';}catch{/* Preferences are optional. */}

function updateURL(){const u=new URL(location.href);u.search='';u.searchParams.set('view',state.view);if(state.selected)u.searchParams.set('ticket',state.selected);if($('#repository').value!=='all')u.searchParams.set('repository',$('#repository').value);history.replaceState(null,'',u);}
function view(name){state.view=name;for(const v of ['board','network','monitor']){$('#'+v+'-view').hidden=v!==name;const b=document.querySelector(`[data-view="${v}"]`);if(v===name)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');}renderViews();requestAnimationFrame(resizeNetwork);updateURL();}
function renderViews(){if(!state.snapshot)return;const tickets=filtered();$('#ticket-total').textContent=tickets.length;
 if(state.view==='board')renderBoard(tickets,selectTicket);
 if(state.view==='monitor')renderMonitor(tickets,selectTicket,id=>{view('network');inspectNode(id);});
 if(state.view==='network'){renderNetwork(state.snapshot,tickets,selectTicket,inspectNode);if(inspectedNode)inspectNode(inspectedNode);}
 $('#clear-journey').hidden=!state.selected;$('#network-mode').textContent=state.selected?'Ticket journey · '+state.selected:'Overview';$('#replay').hidden=!state.selected;
 const selected=state.snapshot.tickets.find(t=>t.id===state.selected);if(selected){$('#journey-title').textContent=state.selected+' · '+human(selected.node);if(!state.replaying)$('#replay-label').textContent=selected.execution_id?'Live · active '+(selected.model?shortModel(selected.model):human(selected.agent)):'Live · '+selected.queue_reason;}
}

async function selectTicket(id){await openTicket(id,refresh,journey);updateURL();if(state.view==='network'){renderViews();highlight(id,state.detail?.timeline||[]);}}
function journey(id,detail){state.selected=id;state.detail=detail;returnLive();view('network');highlight(id,detail.timeline);$('#replay-cursor').max=Math.max(0,detail.timeline.length-1);$('#replay-cursor').value=0;}

function inspectNode(id){
 inspectedNode=id;
 const node=state.snapshot.topology.nodes.find(n=>n.id===id),queue=state.snapshot.queues.find(q=>'queue:'+q.id===id);const root=$('#node-inspector');root.replaceChildren();$('#node-picker').value=id;
 const title=node?.label||queue?.label||human(id);root.append(el('h2','',title));
 const tickets=filtered().filter(t=>queue?t.queue===queue.id:t.node===id);
 if(queue)root.append(el('p','empty-copy','Ordered by effective priority and admission time. Aging promotes waiting work; dependencies and resource locks still apply.'));
 if(node)root.append(el('p','empty-copy',node.contract.responsible));
 for(const t of tickets.sort((a,b)=>a.effective_rank-b.effective_rank||a.created-b.created)){
  const b=el('button','inspector-ticket');b.append(el('strong','',t.id),tag(t.priority),el('span','',t.title),el('small','',t.execution_id?(t.model?shortModel(t.model):'Worker starting')+' · '+elapsed(t.started):t.queue_reason));b.addEventListener('click',()=>selectTicket(t.id));root.append(b);
 }
 if(!tickets.length)root.append(el('p','empty-copy','No matching tickets currently at this node or queue.'));
 const recent=state.events.filter(e=>e.source_node===id&&['completed','failed','dispatch_checkpoint','worker_failed'].includes(e.event_type)).slice(-8).reverse();
 if(recent.length){root.append(el('h3','','Recent executions'));for(const e of recent){const b=el('button','recent-execution',`${e.ticket_id||'Controller'} · ${human(e.event_type)} · ${stamp(e.timestamp)}`);if(e.ticket_id)b.addEventListener('click',()=>selectTicket(e.ticket_id));root.append(b);}}
 if(node)root.append(codeSection('Node contract and permissions',node.contract));
 if(queue)root.append(facts([['Worker limit',state.snapshot.scheduler.workers],['Active leases',state.snapshot.scheduler.active],['Model capacity',JSON.stringify(state.snapshot.scheduler.model_capacity)],['Admission',state.snapshot.scheduler.paused?'Paused':'Running']]));
}

function renderHeader(){const data=state.snapshot;const signature=JSON.stringify(data.repositories);if(signature!==repoSignature){const previous=$('#repository').value;$('#repository').replaceChildren();const all=el('option','','All repositories');all.value='all';$('#repository').append(all);for(const r of data.repositories){const o=el('option','',r.label);o.value=r.id;$('#repository').append(o);}const wanted=repoSignature?previous:url.searchParams.get('repository')||previous;$('#repository').value=[...$('#repository').options].some(o=>o.value===wanted)?wanted:'all';repoSignature=signature;}
 const service=data.scheduler.service,observed=service&&Date.now()/1000-service.observed<15;
 $('#scheduler-summary').textContent=`${data.scheduler.active}/${data.scheduler.workers} workers · ${observed?(data.scheduler.paused?'Admission paused':'Controller running'):'Controller not observed'}`;
 $('#pause-scheduler').textContent=data.scheduler.paused?'Resume admission':'Pause admission';
 const health=$('#model-health');health.replaceChildren();for(const model of data.models){const seen=model.observed!==undefined;const chip=el('button','health-chip '+(seen?(model.available?'healthy':'unhealthy'):'unknown'));chip.append(el('span','health-dot'),el('span','',model.label),el('strong','',seen?(model.available?'Available':'Unavailable'):'Not checked'));chip.title=(seen?'Observed '+stamp(model.observed):'No observed health check')+' · '+(model.model_id||'no id')+' · '+(model.tier||'tier unset');chip.addEventListener('click',checkHealth);health.append(chip);}
 const modelSignature=JSON.stringify(data.models.map(m=>[m.key,m.label]));if(modelSignature!==renderHeader.models){const filter=$('#model-filter'),previous=filter.value;filter.replaceChildren(...[['all','All models'],...data.models.map(m=>[m.key,m.label]),['tool','Deterministic tools']].map(([value,label])=>{const o=el('option','',label);o.value=value;return o;}));filter.value=[...filter.options].some(o=>o.value===previous)?previous:'all';renderHeader.models=modelSignature;}
 $('#snapshot-time').textContent='State read '+stamp(data.generated_at);for(const id of ['repository-autonomy','create-ticket','register-repository','start-workflow','pause-scheduler','inspect-repository'])$('#'+id).disabled=!state.control;
}

async function refresh(){
 if(refreshing){refreshAgain=true;return;}refreshing=true;
 try{const data=await json('/api/centre');state.snapshot=data;renderHeader();refreshTicketSummary();refreshTicketEvidence(refresh,journey);const signature=JSON.stringify([data.tickets,data.children,data.scheduler,data.models,$('#repository').value]);if(signature!==latestSignature){latestSignature=signature;renderViews();}
  $('#centre-error').hidden=true;
  if(waitingAnimations.length){animateEvents(waitingAnimations);waitingAnimations=[];}
 }catch(error){showError(error);$('#stream-status').textContent='Read interrupted · showing last snapshot';}
 finally{refreshing=false;if(refreshAgain){refreshAgain=false;scheduleRefresh();}}
}
function scheduleRefresh(){clearTimeout(refreshTimer);refreshTimer=setTimeout(refresh,120);}
function addEvents(events,live=true){
 for(const event of events){if(seenEvents.has(event.event_id))continue;seenEvents.add(event.event_id);state.events.push(event);if(live)waitingAnimations.push(event);if(state.detail?.ticket_id===event.ticket_id&&!state.detail.timeline.some(e=>e.event_id===event.event_id)){state.detail.timeline.push(event);$('#replay-cursor').max=state.detail.timeline.length-1;}}
 if(seenEvents.size>10000){seenEvents.clear();state.events.slice(-5000).forEach(e=>seenEvents.add(e.event_id));}
 state.events=state.events.slice(-500);const list=$('#live-console');list.replaceChildren();
 for(const e of state.events.slice(-60).reverse()){const li=el('li','console-event '+(e.status==='failed'?'event-error':''));li.append(el('time','',stamp(e.timestamp)),el('code','',e.ticket_id||'controller'),el('strong','',human(e.event_type)),el('span','',human(e.source_node)+' → '+human(e.destination_node)),el('span','event-model',(e.demonstration_notice||state.snapshot?.tickets.find(t=>t.id===e.ticket_id)?.demonstration_notice)?'SCRIPTED FIXTURE':e.model_id?shortModel(e.model_id):''));list.append(li);}
 $('#console-count').textContent=state.events.length+' observed events · newest first';
}
function connectEvents(cursor){if(source)source.close();source=new EventSource('/api/centre/events?after='+cursor);source.addEventListener('open',()=>{$('#stream-status').textContent='● Live event stream';});source.addEventListener('execution',event=>{try{const data=JSON.parse(event.data);addEvents([data]);scheduleRefresh();}catch{showError(Error('Invalid execution event; snapshot retained.'));}});source.addEventListener('error',()=>{$('#stream-status').textContent='Reconnecting stream · persisted state retained';});}

async function checkHealth(){try{await command('/api/centre/health',{});await refresh();toast('Model health observations refreshed.');}catch(error){showError(error);}}
for(const name of categories){const o=el('option','',human(name));o.value=name;$('#category-filter').append(o);}
for(const name of columns){const o=el('option','',name);o.value=name;$('#board-filter').append(o);}
for(const button of document.querySelectorAll('[data-view]'))button.addEventListener('click',()=>view(button.dataset.view));
for(const id of ['repository','ticket-search','priority-filter','model-filter','category-filter','agent-filter','severity-filter','board-filter','group-by'])$('#'+id).addEventListener(id==='ticket-search'?'input':'change',()=>{renderViews();updateURL();});
$('#create-ticket').addEventListener('click',()=>createTicket(refresh));
$('#start-workflow').addEventListener('click',()=>repositoryStart(refresh));$('#register-repository').addEventListener('click',()=>connectRepository(refresh));
$('#inspect-repository').addEventListener('click',async()=>{try{const id=$('#repository').value;if(id==='all')throw Error('Choose a repository first.');const info=await command('/api/centre/inspect',{repository_id:id});formDialog('Repository inspection',root=>{root.append(facts([['Repository',info.source],['Revision',info.sha],['Working tree',info.clean?'Clean':'Uncommitted work preserved'],['Adapter',info.adapter],['Code execution',info.trusted?'Previously trusted configuration':'Disabled by default'],['Publication backend',info.backend]]),codeSection('Observed repository metadata',info,true));return null;},async()=>{},'Close');}catch(error){showError(error);}});
$('#pause-scheduler').addEventListener('click',async()=>{try{await command('/api/centre/scheduler',{action:state.snapshot.scheduler.paused?'resume':'pause'});await refresh();toast('Admission updated. Active steps finish at their next checkpoint.');}catch(error){showError(error);}});
$('#worker-capacity').addEventListener('click',()=>formDialog('Concurrent worker limit',root=>{const count=field('Maximum concurrent node executions (1–8)','number',state.snapshot.scheduler.workers);count.input.min=1;count.input.max=8;root.append(count.wrapper,el('p','form-note','Lowering capacity lets existing steps finish. Repository locks and model capacities still apply.'));return count;},async count=>{await command('/api/centre/scheduler',{action:'capacity',workers:Number(count.input.value)});await refresh();},'Save limit'));
$('#theme').addEventListener('click',()=>{document.documentElement.dataset.theme=document.documentElement.dataset.theme==='dark'?'light':'dark';try{localStorage.setItem('ge-theme',document.documentElement.dataset.theme);}catch{}themeNetwork();});
$('#node-picker').addEventListener('change',event=>inspectNode(event.target.value));
$('#clear-journey').addEventListener('click',()=>{state.selected=null;state.detail=null;returnLive();clearInterval(replayTimer);renderViews();updateURL();});
$('#replay-cursor').addEventListener('input',event=>{const e=state.detail?.timeline[Number(event.target.value)];if(e){replayAt(e);$('#replay-label').textContent=`HISTORICAL REPLAY · event ${e.event_id} · ${stamp(e.timestamp)} · ${human(e.event_type)}`;}});
$('#replay-play').addEventListener('click',()=>{if(!state.detail?.timeline.length)return;clearInterval(replayTimer);$('#replay-cursor').value=0;$('#replay-cursor').dispatchEvent(new Event('input'));replayTimer=setInterval(()=>{const slider=$('#replay-cursor');if(Number(slider.value)>=Number(slider.max)){clearInterval(replayTimer);return;}slider.value=Number(slider.value)+1;slider.dispatchEvent(new Event('input'));},700);});
$('#replay-live').addEventListener('click',()=>{clearInterval(replayTimer);returnLive();renderViews();});
$('#console-toggle').addEventListener('click',()=>{const hidden=!$('#live-console').hidden;$('#live-console').hidden=hidden;$('#console-toggle').setAttribute('aria-expanded',String(!hidden));$('#console-toggle').textContent=hidden?'Expand':'Collapse';});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(!document.hidden)refresh();},3000);
setInterval(()=>{if(document.hidden)return;for(const node of document.querySelectorAll('[data-running-since]'))node.textContent=elapsed(Number(node.dataset.runningSince));},1000);

(async()=>{try{const session=await json('/api/session');state.control=session.control_enabled;state.csrf=session.csrf;$('#viewer-note').hidden=state.control;await refresh();view(state.view);const history=await json('/api/centre/history?after='+Math.max(0,state.snapshot.cursor-200));addEvents(history.events,false);connectEvents(state.snapshot.cursor);if(state.selected){state.detail=await json('/api/centre/ticket?id='+encodeURIComponent(state.selected));$('#replay-cursor').max=Math.max(0,state.detail.timeline.length-1);renderViews();}}catch(error){showError(error);}})();

$('#repository-autonomy').addEventListener('click',()=>autonomyPolicy(refresh));
