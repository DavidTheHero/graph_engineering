import {state,$,el,columns,human,tag,elapsed,shortModel,tierBadge,facts} from './centre-state.js';
const visibleCards=new Map();

export function renderBoard(tickets,select) {
  const root=$('#board');const focused=document.activeElement?.dataset.ticket;root.replaceChildren();
  const group=$('#group-by').value;
  const keys=group==='none'?['All work']:[...new Set(tickets.map(t=>t[group]))].sort();
  if(!keys.length)keys.push('All work');
  for(const key of keys){
    const lane=el('section','board-swimlane');
    if(group!=='none')lane.append(el('h2','swimlane-title',human(key)||'No active model'));
    const grid=el('div','kanban');
    for(const column of columns){
      const section=el('section','kanban-column');section.dataset.status=column;
      const matches=tickets.filter(t=>t.board_status===column&&(group==='none'||t[group]===key));
      const heading=el('h2','column-heading');heading.append(el('span','status-dot '+column.toLowerCase().replaceAll(' ','-')),el('span','',column),el('span','column-count',matches.length));section.append(heading);
      const pageKey=group+':'+key+':'+column,limit=visibleCards.get(pageKey)||50;
      for(const ticket of matches.slice(0,limit)){
        const card=el('button','ticket-card'+(state.selected===ticket.id?' selected':''));card.type='button';card.dataset.ticket=ticket.id;
        const top=el('span','card-top');top.append(el('code','ticket-key',ticket.id),tag(ticket.priority,ticket.priority.toLowerCase()));
        card.append(top,el('strong','card-title',ticket.title));if(ticket.lifecycle)card.append(tag(ticket.lifecycle));if(ticket.demonstration_notice||ticket.labels.includes('failure-injection'))card.append(tag('Labelled failure injection'));
        const category=el('span','card-repository',ticket.repository_name+' · '+human(ticket.category));card.append(category);
        if(ticket.queue_reason&&['dependency','resource','retry','investigation','approval','paused','development','clarification','cancelled'].includes(ticket.queue))card.append(el('span','card-reason',ticket.queue_reason));
        const foot=el('span','card-foot');foot.append(el('span','worker-avatar',ticket.model?tierBadge(ticket.model_key):ticket.execution_id?'↻':'○'),el('span','',ticket.model?shortModel(ticket.model,ticket.model_key):ticket.execution_id?'Starting '+human(ticket.agent):human(ticket.agent)),el('span','card-duration',ticket.execution_id?elapsed(ticket.started):''));if(ticket.execution_id)foot.lastElementChild.dataset.runningSince=ticket.started;card.append(foot);
        const meta=el('span','card-meta');meta.append(tag(ticket.backend==='github'&&ticket.issue?'GitHub-linked':'Local ticket'),el('span','',ticket.repairs+ticket.retries?`↻ ${ticket.repairs+ticket.retries} retries`:human(ticket.node)));card.append(meta);
        card.addEventListener('click',()=>select(ticket.id));section.append(card);
      }
      if(!matches.length)section.append(el('p','column-empty','No tickets'));
      if(matches.length>limit){const more=el('button','button small quiet',`Show more (${matches.length-limit} remaining)`);more.addEventListener('click',()=>{visibleCards.set(pageKey,limit+50);renderBoard(tickets,select);});section.append(more);}
      grid.append(section);
    }
    lane.append(grid);root.append(lane);
  }
  if(focused)[...root.querySelectorAll('[data-ticket]')].find(b=>b.dataset.ticket===focused)?.focus();
}

export function renderMonitor(tickets,select,inspectQueue) {
  const root=$('#monitor');root.replaceChildren();
  const totals=el('div','monitor-metrics');
  for(const [label,value] of [['Tickets',tickets.length],['Active workers',tickets.filter(t=>t.execution_id).length],['Completed',tickets.filter(t=>t.board_status==='Done').length],['Retries & repairs',tickets.reduce((n,t)=>n+t.retries+t.repairs,0)]]){const c=el('section');c.append(el('span','',label),el('strong','',value));totals.append(c);}root.append(totals);
  const workers=el('section','monitor-panel');workers.append(el('h2','','Active executions'));
  for(const ticket of tickets.filter(t=>t.execution_id)){
    const b=el('button','worker-row');b.append(el('strong','',ticket.id),el('span','',human(ticket.node)),el('span','',ticket.model?shortModel(ticket.model):'Starting '+human(ticket.agent)),tag(elapsed(ticket.started)));b.lastElementChild.dataset.runningSince=ticket.started;b.addEventListener('click',()=>select(ticket.id));workers.append(b);
  }
  if(!tickets.some(t=>t.execution_id))workers.append(el('p','empty-copy','No active worker lease. Queued tickets dispatch when admission, dependencies and resources permit.'));root.append(workers);
  const queues=el('section','queue-grid');
  for(const queue of state.snapshot.queues.filter(q=>!['done','active'].includes(q.id))){
    const matches=tickets.filter(t=>t.queue===queue.id).sort((a,b)=>a.effective_rank-b.effective_rank||a.created-b.created);
    const card=el('section','queue-panel');const title=el('button','queue-heading');title.append(el('h2','',queue.label),tag(matches.length));title.addEventListener('click',()=>inspectQueue('queue:'+queue.id));card.append(title);
    for(const ticket of matches.slice(0,12)){const b=el('button','queue-ticket');b.append(el('strong','',ticket.id),tag(ticket.priority),el('span','',ticket.title),el('small','',ticket.queue_reason));b.addEventListener('click',()=>select(ticket.id));card.append(b);}
    if(!matches.length)card.append(el('p','empty-copy','Queue empty'));
    if(matches.length>12)card.append(el('p','empty-copy',`${matches.length-12} more tickets; select the queue to inspect all.`));queues.append(card);
  }root.append(queues);
}
