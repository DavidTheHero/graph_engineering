import cytoscape from './vendor/cytoscape.js';
import {quadraticPoint} from './centre-motion.js';
import {state,$,el,human,tag,shortModel,elapsed,toast} from './centre-state.js';

const positions={
 development_setup:[1070,-850],regression_authoring:[1380,-540],regression_review:[1590,-260],regression_validation:[1400,40],development_delivery:[-1200,770],
 manual_intake:[-1020,-600],request_triage:[-500,-660],request_duplicate_check:[-260,-600],
 repository_search:[400,-850],problem_investigation:[690,-700],ticket_enhancement:[960,-520],
 reproduction_check:[1100,330],finding_validation:[1140,520],ticket_finalisation:[-1100,750],
 ticket_publication:[-900,910],development_admission:[-400,800],
 repository_discovery:[-850,-320],evidence_collection:[-650,-240],issue_detection:[-780,-100],
 issue_validation:[-330,-220],deduplication:[-160,-330],ticket_creation:[50,-280],prioritisation:[30,-100],
 repository_investigation:[360,-560],task_planning:[400,-340],model_selection:[610,-260],isolated_implementation:[830,70],
 test_execution:[640,340],independent_review:[850,470],
 pull_request_creation:[-760,260],ci_policy_verification:[-550,360],approval_gate:[-390,530],
 merge:[-650,650],post_merge_verification:[-900,520],completion:[-1030,320],
 repair:[200,540],escalation:[30,650],'model:a':[1030,-160],'model:b':[1040,70]
};
const palette=['#83dab2','#f6bc69','#8ebdff','#e6a1d6','#c6d87a','#8cd9de'];
let cy,onSelect,onInspect,topologySignature='',snapshot,selected,collapsed=new Set(),replayPosition=null;
let renderedTickets=[],pendingFit=false;
const seen=new Set(),animations=new Map();
function color(id){return palette[[...id].reduce((n,c)=>n+c.charCodeAt(0),0)%palette.length];}
function edgeId(a,b){return a+'~'+b;}
function css(){const dark=document.documentElement.dataset.theme==='dark';return [
 {selector:'node',style:{'background-color':dark?'#213b36':'#edf5ef','border-color':dark?'#50736a':'#97aea2','border-width':1.5,'label':'data(label)','color':dark?'#e3eee8':'#243d32','font-size':18,'text-wrap':'wrap','text-max-width':170,'text-valign':'center','width':180,'height':66,'shape':'round-rectangle','text-events':'yes'}},
 {selector:':parent',style:{'background-color':dark?'#122822':'#f0f5ef','background-opacity':.75,'border-color':dark?'#36544a':'#b8cabb','border-style':'dashed','border-width':1,'label':'data(label)','text-valign':'top','text-halign':'left','text-margin-x':12,'text-margin-y':-12,'font-size':22,'font-weight':600,'padding':38,'shape':'round-rectangle'}},
 {selector:'edge',style:{'width':1.5,'line-color':dark?'#466c5e':'#9cb3a4','target-arrow-color':dark?'#466c5e':'#9cb3a4','target-arrow-shape':'triangle','arrow-scale':.7,'curve-style':'unbundled-bezier','control-point-distances':35,'control-point-weights':.5,'opacity':.65}},
 {selector:'.feedback',style:{'line-style':'dashed','line-color':'#bd9868','target-arrow-color':'#bd9868','control-point-distances':100}},
 {selector:'.self-loop',style:{'curve-style':'bezier','loop-direction':'-90deg','loop-sweep':'80deg'}},
 {selector:'.observed',style:{'line-color':'#76b8d2','target-arrow-color':'#76b8d2','line-style':'dotted'}},
 {selector:'.queue',style:{'shape':'round-diamond','width':180,'height':86,'background-color':dark?'#352e21':'#fff2da','border-color':'#a1834a','font-size':16}},
 {selector:'.model',style:{'shape':'hexagon','width':170,'height':92,'background-color':dark?'#22354b':'#e7f0ff','border-color':'#648db8','font-size':18}},
 {selector:'.ticket-token',style:{'shape':'ellipse','width':27,'height':27,'background-color':'data(color)','border-color':dark?'#0d1915':'#fff','border-width':3,'label':'data(label)','font-size':15,'font-weight':700,'color':dark?'#edf7f0':'#153d27','text-valign':'bottom','text-margin-y':8,'z-index':30}},
 {selector:'.instance',style:{'shape':'round-rectangle','width':150,'height':52,'background-color':dark?'#254b3d':'#e1f4e9','border-color':'#86d6ad','border-width':2,'font-size':14}},
 {selector:'.active',style:{'border-color':'#8de6ba','border-width':3,'underlay-color':'#67d7ac','underlay-opacity':.15,'underlay-padding':8}},
 {selector:'.visited',style:{'border-color':'#f2c875','border-width':3}},
 {selector:'edge.visited',style:{'line-color':'#f2c875','target-arrow-color':'#f2c875','opacity':1,'width':3}},
 {selector:'.faded',style:{'opacity':.22}},
 {selector:'.selected-token',style:{'width':38,'height':38,'border-color':'#fff','border-width':4,'z-index':40}},
 {selector:'.low-detail',style:{'label':''}},
 {selector:'node.visited',style:{'label':'data(label)'}}
 ];}

function ensure(){
 if(cy)return;
 cy=cytoscape({container:$('#network'),elements:[],style:css(),layout:{name:'preset'},minZoom:.2,maxZoom:2.5,boxSelectionEnabled:false});
 cy.on('tap','node',event=>{const n=event.target;if(n.data('ticketId'))onSelect(n.data('ticketId'));else onInspect(n.id());});
 cy.on('dbltap',':parent',event=>toggleCluster(event.target.id().slice(8)));
 cy.on('zoom',()=>{cy.nodes('.station').toggleClass('low-detail',cy.zoom()<.38);});
 cy.on('mouseover','node',event=>{$('#network').title=event.target.data('tooltip')||event.target.data('label');});
 $('#network-fit').addEventListener('click',()=>{cy.resize();cy.fit(cy.elements().not('.ticket-token'),35);});
 $('#network-zoom-in').addEventListener('click',()=>cy.zoom(cy.zoom()*1.25));
 $('#network-zoom-out').addEventListener('click',()=>cy.zoom(cy.zoom()/1.25));
 $('#network-layout').addEventListener('click',()=>{cy.elements().not('.ticket-token,.instance').layout({name:'cose',animate:false,randomize:false,nodeRepulsion:()=>200000,idealEdgeLength:()=>180,gravity:.1,numIter:600}).run();placeTokens();});
 $('#network-save').addEventListener('click',()=>{const saved={};cy.nodes('.station,.queue,.model').forEach(n=>{saved[n.id()]=n.position();});try{localStorage.setItem('ge-network-layout-v2',JSON.stringify(saved));toast('Layout saved on this browser. Execution topology is unchanged.');}catch{toast('Layout storage is unavailable.');}});
}

function addEdge(source,target,kind='observed'){
 if(source===target)return;
 if(!cy.getElementById(source).length||!cy.getElementById(target).length)return;
 const id=edgeId(source,target);
 if(!cy.getElementById(id).length)cy.add({group:'edges',data:{id,source,target},classes:kind});
}

export function renderNetwork(data,tickets,select,inspect){
 snapshot=data;renderedTickets=tickets;onSelect=select;onInspect=inspect;ensure();
 const signature=JSON.stringify(data.topology);
 if(signature!==topologySignature){
  topologySignature=signature;cy.elements().remove();
  let saved={};try{saved=JSON.parse(localStorage.getItem('ge-network-layout-v2')||'{}');}catch{/* Ignore invalid preferences. */}
  cy.batch(()=>{
   data.topology.clusters.forEach(c=>cy.add({group:'nodes',data:{id:'cluster:'+c.id,label:c.label}}));
   data.topology.nodes.forEach(n=>{const [x,y]=positions[n.id]||[0,0];cy.add({group:'nodes',data:{id:n.id,label:n.label,parent:'cluster:'+n.cluster,tooltip:n.contract.responsible},position:saved[n.id]||{x,y},classes:'station'});});
   data.topology.queues.forEach((q,i)=>cy.add({group:'nodes',data:{id:q.id,label:q.label,parent:'cluster:classification'},position:saved[q.id]||{x:-250+(i%3)*220,y:70+Math.floor(i/3)*120},classes:'queue'}));
   data.models.forEach((m,i)=>{const id='model:'+m.key,[x,y]=positions[id]||[1035,-160+i*230];cy.add({group:'nodes',data:{id,label:m.label,parent:'cluster:engineering'},position:saved[id]||{x,y},classes:'model'});});
   data.topology.edges.forEach(e=>{cy.add({group:'edges',data:e,classes:e.source===e.target?'self-loop':['repair','escalation','issue_detection','repository_search','problem_investigation'].includes(e.target)&&e.source!=='evidence_collection'?'feedback':''});});
  });
  const picker=$('#node-picker');picker.replaceChildren(el('option','','Choose a node…'));
  for(const n of [...data.topology.nodes,...data.topology.queues]){const o=el('option','',n.label);o.value=n.id;picker.append(o);}
  const controls=$('#cluster-controls');controls.replaceChildren();for(const cluster of data.topology.clusters){const button=el('button','button small quiet',cluster.label);button.setAttribute('aria-pressed','false');button.addEventListener('click',()=>{toggleCluster(cluster.id);button.setAttribute('aria-pressed',String(collapsed.has(cluster.id)));});controls.append(button);}
  pendingFit=true;
 }
 cy.batch(()=>{
  cy.nodes().removeClass('active');
  data.queues.forEach(q=>{const node=cy.getElementById('queue:'+q.id);if(node.length)node.data('label',q.label+'\n'+q.count+' waiting');});
  data.models.forEach(m=>{const n=cy.getElementById('model:'+m.key);if(n.length)n.data('label',m.label+'\n'+(m.observed===undefined?'Not checked':m.available?'Available':'Unavailable'));});
  const ids=new Set();
  for(const ticket of tickets.filter(t=>t.execution_id)){
   const id='instance:'+ticket.execution_id;ids.add(id);
   const station=cy.getElementById(ticket.node);station.addClass('active');
   const anchor=cy.getElementById(ticket.model_key?'model:'+ticket.model_key:ticket.node);
   const p=anchor.length?anchor.position():{x:0,y:0};
   const label=ticket.id+'\n'+(ticket.model?shortModel(ticket.model,ticket.model_key):human(ticket.agent)+' starting');
   if(!cy.getElementById(id).length)cy.add({group:'nodes',data:{id,label,ticketId:ticket.id},position:{x:p.x+130,y:p.y+85+ids.size*20},classes:'instance active'});
   else cy.getElementById(id).data('label',label);
   addEdge(ticket.node,id,'observed');
  }
  cy.nodes('.instance').not('.child-task').filter(n=>!ids.has(n.id())).remove();
 });
 placeTokens();renderChildren();highlight(selected,state.detail?.timeline||[]);
}

function renderChildren(){
 const visible=(snapshot.children||[]).filter(c=>c.status==='running'||c.ticket_id===state.selected).slice(0,32);
 const ids=new Set();
 visible.forEach((child,i)=>{
  const id='child:'+child.child_task_id;ids.add(id);
  const p=anchor('test_execution'),position={x:p.x-170+(i%3)*180,y:p.y+145+Math.floor(i/3)*90};
  const label=child.ticket_id+'\nQuality '+child.status;
  if(!cy.getElementById(id).length)cy.add({group:'nodes',data:{id,label,ticketId:child.ticket_id},position,classes:'child-task instance'});
  else cy.getElementById(id).data('label',label);
  cy.getElementById(id).toggleClass('active',child.status==='running');
  const marker='marker:'+child.child_task_id;
  if(!cy.getElementById(marker).length)cy.add({group:'nodes',data:{id:marker,label:child.ticket_id.slice(-4)+'·'+(i+1),ticketId:child.ticket_id,color:color(child.ticket_id)},position:{x:position.x,y:position.y+45},classes:'ticket-token child-token'});
  addEdge('test_execution',id,'observed');if(child.status!=='running')addEdge(id,'test_execution','observed');
 });
 cy.nodes('.child-task').filter(n=>!ids.has(n.id())).remove();
 cy.nodes('.child-token').filter(n=>!ids.has(n.id().replace('marker:','child:'))).remove();
}

function anchor(id){const node=cy.getElementById(id);if(!node.length)return {x:0,y:0};const parent=node.parent();if(parent.length&&collapsed.has(parent.id().slice(8)))return cy.getElementById('fold:'+parent.id().slice(8)).position();return node.position();}
function placeTokens(){
 if(!cy)return;
 const shown=renderedTickets.filter(t=>t.execution_id||t.id===state.selected||!['Done','Failed'].includes(t.board_status)).slice(0,100);
 const ids=new Set(shown.map(t=>'ticket:'+t.id));
 cy.batch(()=>{
  cy.nodes('.ticket-token').not('.child-token').filter(n=>!ids.has(n.id())).remove();
  shown.forEach((t,i)=>{
   const id='ticket:'+t.id, p=anchor(t.position),pos={x:p.x-70+(i%5)*32,y:p.y+45+Math.floor((i%15)/5)*30};
   if(!cy.getElementById(id).length)cy.add({group:'nodes',data:{id,label:t.id.slice(-4),ticketId:t.id,color:color(t.id),tooltip:t.id+' · '+t.title},position:pos,classes:'ticket-token'});
   else if(!animations.has(t.id))cy.getElementById(id).position(replayPosition&&t.id===selected?anchor(replayPosition):pos);
  });
 });
}

export function animateEvents(events){
 if(!cy||state.replaying||$('#network-view').hidden)return;
 for(const event of events){
  if(seen.has(event.event_id))continue;seen.add(event.event_id);
  if(!['queue_dispatched','dispatch_checkpoint','child_started','child_completed'].includes(event.event_type))continue;
  const source=event.source_node,target=event.destination_node;
  const token=cy.getElementById(event.child_task_id?'marker:'+event.child_task_id:'ticket:'+event.ticket_id);
  if(!token.length||source===target||!cy.getElementById(source).length||!cy.getElementById(target).length)continue;
  addEdge(source,target,'observed');
  const edge=cy.getElementById(edgeId(source,target));edge.addClass('visited');
  const start=anchor(source),end=anchor(target);
  const animationKey=event.child_task_id||event.ticket_id;
  $('#network').dataset.lastTransition=String(event.event_id);
  $('#network').dataset.transitionTicket=event.ticket_id;
  $('#network').dataset.transitionRoute=source+' → '+target;
  token.stop(true,false);token.position(start);animations.set(animationKey,event.event_id);
  if(matchMedia('(prefers-reduced-motion: reduce)').matches){token.position(end);animations.delete(animationKey);continue;}
  const points=edge.controlPoints()||[],control=points[0]||{x:(start.x+end.x)/2,y:(start.y+end.y)/2};
  const began=performance.now();
  const frame=now=>{
   if(animations.get(animationKey)!==event.event_id||!token.length||token.removed())return;
   const t=Math.min(1,(now-began)/650);
   token.position(quadraticPoint(start,control,end,t));
   if(t<1)requestAnimationFrame(frame);else{animations.delete(animationKey);placeTokens();if(event.ticket_id!==selected)edge.removeClass('visited');}
  };requestAnimationFrame(frame);
 }
 if(seen.size>5000){const values=[...seen].slice(-2500);seen.clear();values.forEach(id=>seen.add(id));}
}

export function highlight(ticketId,timeline){
 selected=ticketId;if(!cy)return;
 cy.elements().removeClass('visited faded selected-token');
 if(!ticketId)return;
 const nodes=new Set();
 for(const e of timeline){
  nodes.add(e.source_node);nodes.add(e.destination_node);
  addEdge(e.source_node,e.destination_node,'observed');cy.getElementById(edgeId(e.source_node,e.destination_node)).addClass('visited');
 }
 const ticket=snapshot?.tickets.find(t=>t.id===ticketId);if(ticket)nodes.add(ticket.node);
 cy.nodes('.station').forEach(n=>n.addClass(nodes.has(n.id())?'visited':'faded'));
 cy.getElementById('ticket:'+ticketId).addClass('selected-token');
}
export function replayAt(event){
 if(!cy||!event)return;state.replaying=true;replayPosition=event.destination_node;
 const token=cy.getElementById('ticket:'+selected);
 if(!token.length)cy.add({group:'nodes',data:{id:'ticket:'+selected,label:selected.slice(-4),ticketId:selected,color:color(selected)},position:anchor(replayPosition),classes:'ticket-token selected-token'});
 else token.stop(true,false).position(anchor(replayPosition));
}
export function returnLive(){state.replaying=false;replayPosition=null;placeTokens();}
export function resizeNetwork(){if(cy){cy.resize();if(pendingFit&&$('#network').clientWidth){cy.fit(cy.elements().not('.ticket-token'),35);pendingFit=false;}placeTokens();}}
export function themeNetwork(){if(cy)cy.style(css());}
export function toggleCluster(id){
 if(!cy)return;const group=cy.getElementById('cluster:'+id);if(!group.length)return;
 if(collapsed.has(id)){collapsed.delete(id);group.children().style('display','element');cy.getElementById('fold:'+id).remove();}
 else{const p=group.position();collapsed.add(id);group.children().style('display','none');cy.add({group:'nodes',data:{id:'fold:'+id,parent:group.id(),label:group.data('label')+'\nExpand group'},position:p,classes:'station'});}
 placeTokens();
}
