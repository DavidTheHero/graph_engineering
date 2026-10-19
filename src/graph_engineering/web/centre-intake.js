import {state,$,el,human,tag,command,facts,codeSection,toast,showError} from './centre-state.js';

function textarea(label,value='',required=false,max=4000){
 const wrapper=el('label','form-field'),input=el('textarea');wrapper.append(el('span','',label));input.rows=5;input.maxLength=max;input.value=value;input.required=required;wrapper.append(input);return {wrapper,input};
}

export function createTicketDialog(ui,refresh){
 ui.formDialog('Describe a problem',root=>{
  const selected=$('#repository').value;
  const repository=ui.selectField('Repository',state.snapshot.repositories.map(r=>[r.id,r.label]),selected==='all'?state.snapshot.repositories[0]?.id:selected);
  const description=textarea('Describe the Problem','',true,6000);description.input.placeholder='The agent sometimes gets stuck when calling tools. Please investigate.';
  const optional=el('details','intake-options');optional.append(el('summary','','Optional context'));
  const logs=textarea('Logs or stack traces'),expected=textarea('Expected behaviour'),context=textarea('Additional context');
  const priority=ui.selectField('Priority override',[['','Use scheduling policy'],...['P0','P1','P2','P3'].map(x=>[x,x])],'');
  const related=ui.selectField('Related ticket',[['','None'],...state.snapshot.tickets.map(t=>[t.id,t.id+' · '+t.title])],'');
  const reproduce=ui.field('I authorise the configured test command in an isolated snapshot','checkbox');
  const updateTrust=()=>{const r=state.snapshot.repositories.find(r=>r.id===repository.input.value);reproduce.input.disabled=!r?.trusted;if(reproduce.input.disabled)reproduce.input.checked=false;};repository.input.addEventListener('change',updateTrust);updateTrust();
  optional.append(expected.wrapper,logs.wrapper,context.wrapper,priority.wrapper,related.wrapper,reproduce.wrapper,
   el('p','form-note','Investigation reads committed source by default. Optional reproduction requires an already trusted repository. No implementation or publication starts when you submit.'));
  root.append(repository.wrapper,description.wrapper,optional);return {repository,description,logs,expected,context,priority,related,reproduce};
 },async v=>{
  const result=await command('/api/centre/create-ticket',{repository_id:v.repository.input.value,description:v.description.input.value,
   logs:v.logs.input.value,expected:v.expected.input.value,context:v.context.input.value,priority:v.priority.input.value,
   related_ticket:v.related.input.value,reproduce:v.reproduce.input.checked,trust_execution:v.reproduce.input.checked});
  state.selected=result.ticket_id;toast(result.ticket_id+' created. Investigation is queued.');await refresh();
 },'Create & Investigate');
}

export function renderIntake(root,actions,ticket,detail,ui,refresh,reopen){
 const original=detail.intake_report;if(!original)return;
 const investigation=detail.investigation||{},draft=investigation.enhanced,validation=investigation.finding_validation||{};
 root.append(el('h3','','Reported problem & investigation'));
 const comparison=el('div','intake-comparison'),raw=el('section','intake-original'),enhanced=el('section','intake-enhanced');
 raw.append(el('h3','','Original User Description'),el('pre','original-description',original.original));
 raw.append(el('p','form-note','Preserved exactly as submitted. Repository inspection uses the pinned committed revision.'));
 enhanced.append(el('h3','','AI-Enhanced Ticket'),tag(ticket.lifecycle||'Queued'));
 if(!draft)enhanced.append(el('p','empty-copy','Investigation is queued or in progress. Your original report is safely stored.'));
 else{
  enhanced.append(el('h3','',draft.title));
  const verified=validation.source_references_valid&&validation.verified_references>0;
  const status=verified?'Source references checked · root cause remains a hypothesis':'Unverified proposal · no complete source validation';
  enhanced.append(el('p',verified?'notice':'notice error',status));
  for(const [key,label] of [['problem_statement','Problem Statement'],['expected_behaviour','Expected Behaviour'],['observed_behaviour','Observed / Reported Behaviour'],['root_cause_hypothesis','Potential Root Cause · Hypothesis'],['proposed_solution','Proposed Solution']]){
   enhanced.append(el('h4','',label),el('p','intake-prose',draft[key]||'Not established'));
  }
  enhanced.append(el('h4','','Investigation Findings'));
  for(const finding of draft.findings||[]){const block=el('blockquote','finding');block.append(el('strong','',`${finding.file}:${finding.line}`),el('code','',finding.quote),el('p','',finding.observation));enhanced.append(block);}
  for(const [key,label] of [['reproduction_steps','Proposed Reproduction Steps'],['acceptance_criteria','Acceptance Criteria'],['validation_plan','Validation Plan'],['limitations','Limitations'],['questions','Unresolved Questions']]){
   enhanced.append(el('h4','',label));const list=el('ul','intake-list');for(const text of draft[key]||[])list.append(el('li','',text));if(!list.childElementCount)list.append(el('li','','None supplied'));enhanced.append(list);
  }
  enhanced.append(facts([['Model confidence (self-reported)',draft.confidence],['Source commit',draft.source_commit],['Current revision',investigation.revision],['Accepted revision',investigation.accepted_revision||'Not accepted'],['Runtime cause verified','No · source validation is not causal proof']]));
 }
 comparison.append(raw,enhanced);root.append(comparison);
 if(investigation.reproduction)root.append(codeSection('Actual reproduction receipt',investigation.reproduction));
 if(investigation.retrieved)root.append(codeSection('Captured Git search / source evidence',investigation.retrieved));
 if(investigation.finding_validation)root.append(codeSection('Deterministic finding validation',investigation.finding_validation));
 if(investigation.duplicate_candidates?.length)root.append(codeSection('Possible duplicates · review required',investigation.duplicate_candidates,true));
 const history=el('details','receipt');history.append(el('summary','',`Enhancement revision history (${original.revisions.length})`));
 for(const rev of original.revisions)history.append(codeSection(`Revision ${rev.number} · ${rev.author} · ${rev.at}`,{content:rev.content,evidence:rev.evidence}));root.append(history);
 const busy=!!ticket.execution_id,revision=investigation.revision||0;
 const send=async(action,extra={})=>{await command('/api/centre/intake-action',{ticket_id:ticket.id,revision,actor:'Local operator',action,...extra});await refresh();await reopen(ticket.id);};
 const add=(label,handler,disabled=false)=>{const b=el('button','button small quiet',label);b.disabled=!state.control||busy||disabled;b.addEventListener('click',async()=>{b.disabled=true;try{await handler();}catch(error){showError(error);}finally{b.disabled=!state.control||busy||disabled;}});actions.append(b);};
 add('Accept enhancement',()=>send('accept'),!draft||ticket.run_status!=='ready_for_review'||!validation.source_references_valid);
 add('Edit enhancement',()=>ui.formDialog('Edit the technical ticket',fields=>{
  const title=ui.field('Title','text',draft.title),problem=textarea('Problem statement',draft.problem_statement,true,8000),expected=textarea('Expected behaviour',draft.expected_behaviour),solution=textarea('Proposed solution',draft.proposed_solution),criteria=textarea('Acceptance criteria · one per line',draft.acceptance_criteria.join('\n'),true,8000);
  fields.append(title.wrapper,problem.wrapper,expected.wrapper,solution.wrapper,criteria.wrapper,el('p','form-note','Your original report and captured evidence remain immutable. Editing clears prior acceptance.'));
  return {title,problem,expected,solution,criteria};
 },async v=>send('edit_enhancement',{changes:{title:v.title.input.value,problem_statement:v.problem.input.value,expected_behaviour:v.expected.input.value,proposed_solution:v.solution.input.value,acceptance_criteria:v.criteria.input.value.split('\n').map(s=>s.trim()).filter(Boolean)}}),'Save revision'),!draft);
 for(const [label,action,required] of [['Investigate further','reinvestigate',false],['Provide information','clarify',true]])add(label,()=>ui.formDialog(label,fields=>{const text=textarea('Additional information or investigation request','',required);fields.append(text.wrapper);return text;},async v=>send(action,{text:v.input.value}),'Queue investigation'));
 add('Reject enhancement',()=>send('reject_enhancement'),!draft);
 add('Mark as duplicate',()=>ui.formDialog('Link an existing duplicate',fields=>{const related=ui.selectField('Existing ticket',state.snapshot.tickets.filter(t=>t.id!==ticket.id).map(t=>[t.id,t.id+' · '+t.title]),'');fields.append(related.wrapper);return related;},async v=>send('mark_duplicate',{related_ticket:v.input.value}),'Mark duplicate'));
 add('Send to Development Queue',()=>send('send_development'),!revision||investigation.accepted_revision!==revision);
 const live=ticket.backend==='github';
 add(live?(ticket.issue?'Synchronise GitHub issue':'Publish GitHub issue'):'Create local issue simulation',()=>ui.formDialog(live?'Approve GitHub publication':'Approve local publication simulation',fields=>{
  fields.append(el('p','form-note',`Revision ${revision} · ${draft.title}. ${live?'This writes to the repository’s configured GitHub issue tracker.':'This creates a labelled local simulation, with no GitHub write.'}`));const actor=ui.field('Approving operator');actor.input.required=true;fields.append(actor.wrapper);return actor;
 },async actor=>send('publish',{actor:actor.input.value,approve_publication:true,simulation:!live}),'Approve publication'),!revision||investigation.accepted_revision!==revision);
}
