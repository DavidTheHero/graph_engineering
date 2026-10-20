import {state,$,el,human,tag,command,facts,codeSection,showError,toast,categories,stamp,elapsed,json} from './centre-state.js';
import {safeLink} from './dom.js';
import {createTicketDialog,renderIntake} from './centre-intake.js';
let selectedRequest=0,fieldSequence=0,renderedVersion='',evidenceRefreshing=false;

export function autonomyPolicy(refresh){
 const repo=state.snapshot.repositories.find(r=>r.id===$('#repository').value);if(!repo){showError(Error('Choose one repository to configure its policy.'));return;}
 formDialog('Repository autonomy policy',root=>{
  const p=repo.autonomy||{},development=field('Automatically develop accepted tickets','checkbox'),publish=field(repo.backend==='github'?'Automatically publish issues and PRs to the configured GitHub repository':'Automatically create local issue / PR previews','checkbox'),dependencies=field('Install locked npm dependencies in isolated worktrees (scripts disabled)','checkbox'),actor=field('Policy operator');
  development.input.checked=!!p.development;publish.input.checked=!!p.publish;dependencies.input.checked=!!p.prepare_dependencies;actor.input.value=p.actor||'';actor.input.required=true;
  development.input.addEventListener('change',()=>{if(!development.input.checked){publish.input.checked=false;dependencies.input.checked=false;}});
  root.append(el('p','form-note',repo.label+': accepted tickets will execute code/tests and edit isolated branches under this policy. Retries and repairs remain bounded. Missing information and failed verification can still stop work. Merge approval and branch protections remain enforced.'),development.wrapper,publish.wrapper,dependencies.wrapper,actor.wrapper);return {development,publish,dependencies,actor};
 },async v=>{await command('/api/centre/repository-policy',{repository_id:repo.id,development:v.development.input.checked,publish:v.publish.input.checked,prepare_dependencies:v.dependencies.input.checked,actor:v.actor.input.value});toast('Repository policy saved. Accepted tickets follow this policy without repeated execution prompts.');await refresh();},'Save repository policy');
}

export async function refreshTicketEvidence(refresh,journey){
 const ticket=state.snapshot.tickets.find(t=>t.id===state.selected);
 if(!ticket||evidenceRefreshing||!$('#ticket-dialog').open||$('#operator-dialog').open||ticket.updated===renderedVersion)return;
 evidenceRefreshing=true;const request=selectedRequest;
 try{const detail=await json('/api/centre/ticket?id='+encodeURIComponent(ticket.id));if(request!==selectedRequest)return;
  const root=$('#ticket-detail'),scroll=root.scrollTop,open=new Set([...root.querySelectorAll('details[open]>summary')].map(n=>n.textContent));
  state.detail=detail;renderTicket(ticket,detail,refresh,journey);for(const receipt of root.querySelectorAll('details'))receipt.open=open.has(receipt.firstElementChild.textContent);root.scrollTop=scroll;
 }catch(error){showError(error);}finally{evidenceRefreshing=false;}
}

export function refreshTicketSummary(){
 const status=$('#ticket-live-status');if(!status||!$('#ticket-dialog').open)return;
 const ticket=state.snapshot.tickets.find(t=>t.id===state.selected);if(ticket)status.textContent=`${ticket.board_status} · ${human(ticket.node)} · ${ticket.model||ticket.queue_reason}`;
}

export function field(label,type='text',value=''){
 const wrapper=el('label','form-field');wrapper.append(el('span','',label));
 const input=el('input');input.type=type;input.value=value;wrapper.append(input);return {wrapper,input};
}
export function selectField(label,choices,value){
 const wrapper=el('div','form-field'),name=el('label','',label);wrapper.append(name);const input=el('select');input.id='operator-field-'+(++fieldSequence);name.htmlFor=input.id;
 for(const [key,text] of choices){const o=el('option','',text);o.value=key;input.append(o);}input.value=value;wrapper.append(input);return {wrapper,input};
}
export function formDialog(title,build,onSubmit,submit='Continue'){
 const dialog=$('#operator-dialog'),root=$('#dialog-fields');root.replaceChildren();$('#dialog-title').textContent=title;$('#dialog-submit').textContent=submit;$('#dialog-error').hidden=true;
 const values=build(root);
 $('#operator-form').onsubmit=async event=>{
  event.preventDefault();$('#dialog-submit').disabled=true;$('#dialog-error').hidden=true;
  try{await onSubmit(values);dialog.close();}catch(error){$('#dialog-error').textContent=error.message;$('#dialog-error').hidden=false;}
  finally{$('#dialog-submit').disabled=false;}
 };
 dialog.showModal();
}
$('#dialog-close').addEventListener('click',()=>$('#operator-dialog').close());
$('#dialog-cancel').addEventListener('click',()=>$('#operator-dialog').close());
$('#ticket-close').addEventListener('click',()=>$('#ticket-dialog').close());

export function repositoryStart(refresh){
 const repo=$('#repository').value;
 formDialog('Start a controlled workflow',root=>{
  const repository=selectField('Repository',state.snapshot.repositories.map(r=>[r.id,r.label]),repo==='all'?state.snapshot.repositories[0]?.id:repo);
  const mode=selectField('Operation mode',[['read_only','Read-only scan · metadata and source parsing'],['assisted','Assisted development · approved test execution'],['autonomous','Autonomous · existing scoped merge policy required']],'read_only');
  const routing=selectField('Model routing',[['multimodel','Small-tier triage/review + large-tier engineering'],['baseline','Large-tier model only']],'multimodel');
  const scan=field('Stop after a validated ticket, before implementation','checkbox');scan.input.checked=true;
  const investigate=field('Use a local Qwen worker for bounded read-only source investigation','checkbox');investigate.input.checked=true;
  const parallel=field('Run configured build checks alongside tests (independent commands only)','checkbox');
  const trust=field('I trust this repository code to run locally in an isolated snapshot','checkbox');
  const dependencies=field('Dependency ticket IDs (comma-separated)');
  const note=el('p','form-note','New repositories default to read-only. Development executes repository tests as your OS account. Worktrees are not a hostile-code sandbox. Your source checkout is preserved; publication follows its existing configured backend.');
  root.append(repository.wrapper,mode.wrapper,routing.wrapper,investigate.wrapper,scan.wrapper,dependencies.wrapper,parallel.wrapper,trust.wrapper,note);
  return {repository,mode,routing,investigate,scan,dependencies,parallel,trust};
 },async values=>{
  const result=await command('/api/centre/start',{repository_id:values.repository.input.value,mode:values.mode.input.value,
   parallel_quality:values.parallel.input.checked,routing:values.routing.input.value,scan_only:values.scan.input.checked,trust_execution:values.trust.input.checked,inspect_with_model:values.investigate.input.checked,
   dependencies:values.dependencies.input.value.split(',').map(x=>x.trim()).filter(Boolean)});
  toast(`${result.ticket_id} admitted to the persistent queue.`);await refresh();
 },'Admit workflow');
}

export function connectRepository(refresh){
 formDialog('Connect a Git repository',root=>{const path=field('Absolute repository root path');path.input.placeholder='D:\\git_code\\General_AI_Agent';root.append(path.wrapper,el('p','form-note','Connecting reads Git metadata and creates controller configuration. Code execution and GitHub publication stay disabled by default.'));return path;},async value=>{const repo=await command('/api/centre/register',{path:value.input.value});await refresh();$('#repository').value=repo.id;$('#repository').dispatchEvent(new Event('change'));toast('Repository connected in read-only mode.');},'Connect read-only');
}

export function createTicket(refresh){createTicketDialog({formDialog,field,selectField},refresh);}

export async function openTicket(ticketId,refresh,journey){
 state.selected=ticketId;const request=++selectedRequest;
 const ticket=state.snapshot.tickets.find(t=>t.id===ticketId);if(!ticket)return;
 const heading=$('#ticket-detail-heading');heading.replaceChildren(el('div','eyebrow',ticket.id+' · '+ticket.repository_name),el('h2','',ticket.title));
 $('#ticket-actions').replaceChildren();
 $('#ticket-detail').replaceChildren(el('p','empty-copy','Loading persisted execution evidence…'));
 if(!$('#ticket-dialog').open)$('#ticket-dialog').showModal();
 try{
  const response=await fetch('/api/centre/ticket?id='+encodeURIComponent(ticketId));const detail=await response.json();if(!response.ok)throw Error(detail.error);
  if(request!==selectedRequest)return;state.detail=detail;
  renderTicket(ticket,detail,refresh,journey);
 }catch(error){$('#ticket-detail').replaceChildren(el('p','inline-error',error.message));}
}

function renderTicket(ticket,detail,refresh,journey){
 renderedVersion=ticket.updated;
 $('#ticket-detail-heading').replaceChildren(el('div','eyebrow',ticket.id+' · '+ticket.repository_name),el('h2','',ticket.title));
 const actions=$('#ticket-actions');actions.replaceChildren();
 const add=(label,action,data={})=>{const b=el('button','button small quiet',label);b.disabled=!state.control;b.addEventListener('click',async()=>{b.disabled=true;try{await command('/api/centre/ticket-action',{ticket_id:ticket.id,action,...data});toast(`${human(action)} accepted for ${ticket.id}.`);await refresh();await openTicket(ticket.id,refresh,journey);}catch(error){showError(error);}finally{b.disabled=!state.control;}});actions.append(b);return b;};
 const follow=el('button','button small primary','View ticket journey');follow.addEventListener('click',()=>{$('#ticket-dialog').close();journey(ticket.id,detail);});actions.append(follow);
 if(!['Done','Failed','Cancelled'].includes(ticket.board_status)&&!ticket.cancel_requested){
  add(ticket.paused?'Resume':'Pause',ticket.paused?'resume':'pause');add('Cancel task','cancel');
 }
 if(['blocked','cancelled','escalated','failed'].includes(ticket.run_status)){
  const retry=add('Retry with backoff','retry'),limits=ticket.execution_limits;
  if(limits&&limits.retries_used>=limits.retry_limit){retry.disabled=true;retry.title='Ticket retry limit reached. Review the failure and create a scoped follow-up; adding information does not reset limits.';}
 }
 if(ticket.run_status==='scanned'&&ticket.mode==='scan'){
  const b=el('button','button small quiet','Continue development');b.disabled=!state.control;b.addEventListener('click',()=>formDialog('Continue isolated development',root=>{const trust=field('I approve local execution and scoped implementation for this validated finding','checkbox');root.append(trust.wrapper);return trust;},async trust=>{await command('/api/centre/ticket-action',{ticket_id:ticket.id,action:'develop',trust_execution:trust.input.checked});await refresh();$('#ticket-dialog').close();},'Enable development'));actions.append(b);
 }
 if(ticket.run_status==='waiting_approval'){
  const approve=el('button','button small primary','Approve tested commit');approve.disabled=!state.control;
  approve.addEventListener('click',()=>formDialog('Approve this exact tested commit',root=>{root.append(el('p','form-note',ticket.approval_head),el('p','form-note','Approval permits the existing merge gate to continue. Current checks, independent review and repository policies are still verified again.'));const actor=field('Human approver');root.append(actor.wrapper);return actor;},async actor=>{await command('/api/centre/ticket-action',{ticket_id:ticket.id,action:'approve',head:ticket.approval_head,actor:actor.input.value});await refresh();$('#ticket-dialog').close();},'Record approval'));actions.append(approve);add('Reject / hold','reject');
 }
 const edit=el('button','button small quiet','Edit triage');edit.disabled=!state.control;edit.addEventListener('click',()=>formDialog('Update ticket triage',root=>{const priority=selectField('Priority',['P0','P1','P2','P3'].map(x=>[x,x]),ticket.priority),category=selectField('Issue type',categories.map(x=>[x,human(x)]),ticket.category),labels=field('Labels (comma-separated)','text',ticket.labels.join(', '));root.append(priority.wrapper,category.wrapper,labels.wrapper);return {priority,category,labels};},async v=>{await command('/api/centre/ticket-action',{ticket_id:ticket.id,action:'edit',priority:v.priority.input.value,category:v.category.input.value,labels:v.labels.input.value.split(',').map(x=>x.trim()).filter(Boolean)});await refresh();await openTicket(ticket.id,refresh,journey);},'Save triage'));actions.append(edit);
 if(!ticket.execution_id){const reassign=el('button','button small quiet','Reassign model');reassign.disabled=!state.control;reassign.addEventListener('click',()=>formDialog('Reassign a future worker role',root=>{const role=selectField('Role',['classification','root_investigation','investigation','planning','implementation','review'].map(x=>[x,human(x)]),'investigation'),model=selectField('Model',state.snapshot.models.map(m=>[m.key,m.label+' ('+(m.tier||'tier unset')+')']),state.snapshot.models[0]?.key);root.append(role.wrapper,model.wrapper,el('p','form-note','Root investigation, planning and implementation require a large-tier model. Active worker sessions cannot be reassigned.'));return {role,model};},async v=>{await command('/api/centre/ticket-action',{ticket_id:ticket.id,action:'reassign',role:v.role.input.value,model:v.model.input.value});await refresh();},'Save assignment'));actions.append(reassign);}
 const root=$('#ticket-detail');root.replaceChildren();
 if(ticket.demonstration_notice)root.append(el('p','notice',ticket.demonstration_notice));
 const live=el('p','form-note');live.id='ticket-live-status';live.setAttribute('role','status');root.append(live);refreshTicketSummary();
 const status=el('div','detail-status');status.append(tag(ticket.board_status),tag(ticket.priority),tag(human(ticket.category)),tag(ticket.intake&&!ticket.issue?'Local ticket - not published':ticket.backend==='github'?'GitHub-backed':'Local ticket · GitHub simulated'));root.append(status,el('p','detail-description',ticket.description));
 const limits=ticket.execution_limits;
 if(limits){root.append(el('h3','','Execution retry limits'),facts([['Current step attempts',`${limits.step_used}/${limits.step_limit}`],['Ticket retries used',`${limits.retries_used}/${limits.retry_limit}`],['Implementation repair loops',`${limits.repairs_used}/${limits.repair_limit}`]]),el('p','form-note','These limits prevent repeated execution loops. They are not token, credit or spending budgets. Retry with backoff follows the graph recovery edge and consumes one ticket retry.'));
  if(limits.exhausted)root.append(el('p','notice error',limits.explanation));
 }
 root.append(facts([['Repository',ticket.repository],['Commit',ticket.revision],['Run ID',ticket.run_id],['Current node',human(ticket.node)],['Queue',ticket.queue_reason],['Assigned role',human(ticket.agent)],['Observed current model',ticket.model||'No active model request'],['Execution attempt',ticket.execution_id],['Started',ticket.started?stamp(ticket.started):'Not active'],['Duration',ticket.started?elapsed(ticket.started):'Not active'],['Node attempts',JSON.stringify(ticket.attempts)],['Branch',ticket.branch],['Worktree',ticket.worktree],['Retry / repair count',`${ticket.retries} / ${ticket.repairs}`],['Dependencies',ticket.dependencies.join(', ')||'None']]));
 const links=el('div','detail-links');if(ticket.issue)links.append(safeLink(ticket.issue.url,`Issue #${ticket.issue.number}`));if(ticket.pr)links.append(safeLink(ticket.pr.url,`PR #${ticket.pr.number}`));root.append(links);
 renderIntake(root,actions,ticket,detail,{formDialog,field,selectField},refresh,id=>openTicket(id,refresh,journey));
 for(const [key,label] of [['candidate','Original finding & acceptance'],['investigation','Read-only observations · not confirmed defects'],['baseline','Original check evidence'],['validation','Independent reproduction'],['plan','Implementation plan'],['verification','Independent test results'],['review','Independent review'],['diff','Actual Git diff'],['policy_snapshot','CI and branch policy'],['gate','Merge gate decision'],['merge','Merge receipt'],['postmerge','Post-merge verification']])if(detail.evidence[key])root.append(codeSection(label,detail.evidence[key]));
 if(detail.approval)root.append(codeSection('Human approval receipt',detail.approval));
 for(const artifact of detail.worker_artifacts||[])root.append(codeSection('Actual OpenCode request / response',artifact));
 const timeline=el('section','ticket-timeline');timeline.append(el('h3','','Execution timeline'));
 for(const e of detail.timeline){const row=el('div','timeline-row');row.append(el('time','',new Date(e.timestamp).toLocaleTimeString()),el('strong','',human(e.event_type)),el('span','',`${human(e.source_node)} → ${human(e.destination_node)}`),codeSection(`Event ${e.event_id}`,e));timeline.append(row);}root.append(timeline);
}
