import {el,modelName} from './dom.js';
export {el};
export const $ = selector => document.querySelector(selector);
export const columns = ['Backlog','To Do','In Progress','Blocked','Testing','Code Review','Ready for Merge','Done','Failed','Cancelled'];
export const categories = ['bug','security','performance','reliability','refactoring','test_coverage','technical_debt','documentation','feature','build_ci','architecture_investigation'];
export const state = {snapshot:null,selected:null,detail:null,view:'board',events:[],control:false,csrf:null,replaying:false};
export const human = value => String(value ?? '').replaceAll('_',' ').replace(/\b\w/g,c=>c.toUpperCase());
// Labels and tiers come from the backend model registry (models.json), never from model-id guessing.
export const registryModel = key => state.snapshot?.models?.find(m=>m.key===key);
export const shortModel = (value,key) => registryModel(key)?.label || state.snapshot?.models?.find(m=>m.model_id===value)?.label || value || 'Not assigned';
export const recordedModel = (id,key) => (registryModel(key)?.model_id===id?registryModel(key).label:null)
  || state.snapshot?.models?.find(m=>m.model_id===id)?.label || modelName(id);
export const tierBadge = key => ({large:'L',small:'S'})[registryModel(key)?.tier] || '•';
export function tag(text,kind='') {return el('span','tag '+kind,text);}
export function stamp(value) {return new Date(typeof value==='number'?value*1000:value).toLocaleTimeString();}
export function elapsed(value) {if(!value)return '—'; const s=Math.max(0,Math.floor(Date.now()/1000-value));return s<60?s+'s':Math.floor(s/60)+'m '+s%60+'s';}
export function filtered() {
  if(!state.snapshot)return [];
  const repository=$('#repository').value, query=$('#ticket-search').value.trim().toLowerCase();
  return state.snapshot.tickets.filter(t=>(repository==='all'||state.snapshot.repositories.find(r=>r.id===repository)?.source===t.repository)
    && `${t.id} ${t.title} ${t.branch||''} ${t.run_id}`.toLowerCase().includes(query)
    && ($('#priority-filter').value==='all'||t.priority===$('#priority-filter').value)
    && ($('#model-filter').value==='all'||(t.planned_model||'tool')===$('#model-filter').value)
    && ($('#category-filter').value==='all'||t.category===$('#category-filter').value)
    && ($('#agent-filter').value==='all'||t.agent===$('#agent-filter').value)
    && ($('#severity-filter').value==='all'||t.severity===$('#severity-filter').value)
    && ($('#board-filter').value==='all'||t.board_status===$('#board-filter').value));
}
export async function json(path,options={}) {
  const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),20000);
  try {const response=await fetch(path,{cache:'no-store',signal:controller.signal,...options});const data=await response.json();if(!response.ok)throw Error(data.error||`Request failed (${response.status})`);return data;}
  finally {clearTimeout(timer);}
}
export function command(path,data,key=crypto.randomUUID()) {
  if(!state.control)throw Error('Operator controls are disabled. Start the centre command.');
  return json(path,{method:'POST',headers:{'Content-Type':'application/json','X-GE-CSRF':state.csrf,'Idempotency-Key':key},body:JSON.stringify(data)});
}
export function toast(text) {$('#toast').textContent=text;$('#toast').hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>{$('#toast').hidden=true;},5000);}
export function showError(error) {$('#centre-error').textContent=error.message||String(error);$('#centre-error').hidden=false;}
export function facts(pairs) {const dl=el('dl','detail-facts');for(const [label,value] of pairs){const row=el('div');row.append(el('dt','',label),el('dd','',value??'Not recorded'));dl.append(row);}return dl;}
export function codeSection(title,value,open=false) {const d=el('details','receipt');d.open=open;d.append(el('summary','',title),el('pre','',typeof value==='string'?value:JSON.stringify(value,null,2)));return d;}
