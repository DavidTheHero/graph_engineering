// "Needs you": one card per ticket waiting for a person, with only the facts and actions for that decision.
// Items and their valid actions come from the backend (attention.py); the server re-validates every command.
import {state,$,el,human,tag,command,toast,showError} from './centre-state.js';
import {modelName} from './dom.js';

const KIND_LABELS = {clarification:'Questions for you',review:'Review',duplicate_review:'Possible duplicate',
  start_development:'Development approval',publication:'Publication approval',merge_approval:'Merge approval',
  scan_decision:'Finding decision',blocked:'Blocked',dead_letter:'Retry limit reached'};
const drafts = new Map(); // Unsent answers survive the 3-second refresh while you type.

function waited(iso){if(!iso)return '';const s=Math.max(0,(Date.now()-Date.parse(iso))/1000);
  return s<3600?Math.floor(s/60)+' min':s<86400?Math.floor(s/3600)+' h':Math.floor(s/86400)+' d';}

export function attentionItems(){
  if(!state.snapshot)return [];
  const repository=$('#repository').value,source=state.snapshot.repositories.find(r=>r.id===repository)?.source;
  return (state.snapshot.attention||[]).filter(i=>repository==='all'||i.repository===source);
}

function questionBlock(item,question,index){
  const key=item.ticket_id+':'+index,box=el('div','inbox-question');
  const id='answer-'+item.ticket_id+'-'+index;
  const label=el('label','',`${index+1}. ${question.text}`);label.htmlFor=id;
  const input=el('textarea');input.id=id;input.rows=2;input.maxLength=1800;input.placeholder='Your answer (leave empty to skip)';
  input.value=drafts.get(key)||'';input.addEventListener('input',()=>drafts.set(key,input.value));
  box.append(label);
  const proposal=question.proposal;
  if(proposal){
    // Unverified model text is not shown as if it were an answer; only its status is.
    const hint=el('div','inbox-proposal');
    hint.append(tag(modelName(proposal.model)+(proposal.verified?' · verified':' · '+human(proposal.status)+' · not used'),proposal.verified?'good':''));
    if(proposal.verified)hint.append(el('span','',proposal.answer));
    for(const ref of proposal.references||[])hint.append(el('code','',`${ref.file}:${ref.line}`));
    if(proposal.verified){
      const use=el('button','button small quiet','Use as draft');
      use.addEventListener('click',()=>{input.value=proposal.answer;drafts.set(key,input.value);input.focus();});
      hint.append(use);
    }
    box.append(hint);
  }
  box.append(input);
  return {box,input};
}

function card(item,openTicket,refresh){
  const article=el('article','inbox-card');article.dataset.ticket=item.ticket_id;article.dataset.kind=item.kind;
  const head=el('div','inbox-head');
  const title=el('button','inbox-title',`${item.ticket_id} · ${item.title}`);title.addEventListener('click',()=>openTicket(item.ticket_id));
  head.append(tag(KIND_LABELS[item.kind]||human(item.kind),'inbox-kind'),tag(item.priority),title,
    el('span','inbox-age',item.waiting_since?'waiting '+waited(item.waiting_since):''));
  article.append(head,el('h3','inbox-headline',item.headline));
  if(item.context?.length){const list=el('ul','inbox-context');for(const line of item.context)list.append(el('li','',line));article.append(list);}
  const answers=item.questions.map((q,i)=>questionBlock(item,q,i));
  for(const a of answers){if(item.questions_readonly)a.input.remove();article.append(a.box);}
  const freeText=el('textarea','inbox-note');freeText.rows=2;freeText.placeholder='Optional note';freeText.hidden=true;
  freeText.value=drafts.get(item.ticket_id+':note')||'';freeText.addEventListener('input',()=>drafts.set(item.ticket_id+':note',freeText.value));
  freeText.setAttribute('aria-label','Note for '+item.ticket_id);
  const actor=el('input','inbox-actor');actor.placeholder='Your name (recorded as approver)';actor.hidden=true;actor.setAttribute('aria-label','Approver name');
  actor.value=drafts.get('actor')||'';actor.addEventListener('input',()=>drafts.set('actor',actor.value));
  const consentWrap=el('label','inbox-consent');const consent=el('input');consent.type='checkbox';consentWrap.hidden=true;
  const consentText=el('span');consentWrap.append(consent,consentText);
  const bar=el('div','inbox-actions');
  for(const action of item.actions){
    if(action.input==='text'){freeText.hidden=false;freeText.placeholder=`Optional note for “${action.label}”`;freeText.setAttribute('aria-label',`Note for ${action.label} (${item.ticket_id})`);}
    if(action.input==='actor')actor.hidden=false;
    if(action.input==='consent'){consentWrap.hidden=false;consentText.textContent=action.help;}
    const button=el('button','button small '+(action.primary?'primary':'quiet'),action.label);
    if(action.help&&action.input!=='consent')button.title=action.help;
    button.disabled=!state.control||!!action.disabled;
    button.addEventListener('click',async()=>{
      const payload={...action.payload};
      try{
        if(action.input==='answers'){
          const text=item.questions.map((q,i)=>[q.text,answers[i].input.value.trim()]).filter(([,a])=>a).map(([q,a])=>`Q: ${q}\nA: ${a}`).join('\n');
          if(!text)throw Error('Answer at least one question before sending.');
          payload.text=text;
        }
        if(action.input==='text')payload.text=freeText.value;
        if(action.input==='actor'){if(!actor.value.trim())throw Error('Enter your name to record the approval.');payload.actor=actor.value.trim();}
        if(action.input==='consent'){if(!consent.checked)throw Error('Tick the authorisation box first.');payload[action.consent_key]=true;}
        if(action.confirm&&!button.dataset.armed){
          // Inline two-step confirmation: the first click arms the button for a few seconds.
          button.dataset.armed='1';button.textContent='Confirm: '+action.label;button.title=action.confirm;
          setTimeout(()=>{delete button.dataset.armed;button.textContent=action.label;},4000);return;
        }
        button.disabled=true;
        await command(action.endpoint,payload);
        item.questions.forEach((_,i)=>drafts.delete(item.ticket_id+':'+i));drafts.delete(item.ticket_id+':note');
        toast(`${item.ticket_id}: ${action.label} recorded.`);
        await refresh();
      }catch(error){showError(error);button.disabled=!state.control;}
    });
    bar.append(button);
  }
  const open=el('button','button small quiet','Open full ticket');open.addEventListener('click',()=>openTicket(item.ticket_id));
  bar.append(open);
  article.append(freeText,actor,consentWrap,bar);
  return article;
}

export function renderInbox(openTicket,refresh){
  const root=$('#inbox');
  // Rebuilding on focus-out would swallow a click on the card's buttons, so a deferred update is applied by the
  // next poll (centre.js re-renders while #inbox is marked pending) once no field in the tab has focus.
  if(root.contains(document.activeElement)&&['TEXTAREA','INPUT'].includes(document.activeElement.tagName)){
    root.dataset.pending='1';return;
  }
  delete root.dataset.pending;
  const items=attentionItems();root.replaceChildren();
  if(!items.length){root.append(el('p','inbox-empty','Nothing needs you right now. Tickets that wait for a decision appear here.'));return;}
  for(const item of items)root.append(card(item,openTicket,refresh));
}

export function renderInboxCount(){
  const count=attentionItems().length,badge=$('#inbox-total');
  badge.textContent=count;badge.classList.toggle('has-items',count>0);
}
