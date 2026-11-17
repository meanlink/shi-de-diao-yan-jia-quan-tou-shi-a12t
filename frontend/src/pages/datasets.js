import {datasets,handovers} from '../api/entities.js';
import {workspace} from '../stores/workspace.js';
import {session} from '../stores/session.js';
import {heading} from '../components/common/shell.js';
import {table} from '../components/common/table.js';
import {batchBadge,canReconcile} from '../components/common/batch.js';
import {esc,notify} from '../utils/dom.js';
import {submit} from '../hooks/forms.js';
import {parseCSV} from '../utils/csv.js';
export async function dataPage(){
 const [list,handoverList,statuses]=await Promise.all([datasets.list(),handovers.list(),handovers.statuses()]);workspace.datasets=list;workspace.handovers=handoverList;workspace.batchCounts=statuses.counts;
 let selected=list[0]?await datasets.get(list[0].id):null,m=workspace.meta;
 return heading('02 / DATA INTAKE','数据接入与批次追溯','保留原始业务字段、来源和内容指纹。分析任务始终绑定一个不可变数据批次；纸质清单与日表在此对账。')+'<div class="status-strip">'+[['pending_reconcile','待对账'],['pending_complete','待补齐'],['ready','可确认'],['confirmed','已确认'],['legacy','旧批次']].map(([k])=>'<span>'+batchBadge(k)+' '+(statuses.counts[k]||0)+'</span>').join('')+'</div>'+'<div class="dataset-grid"><section class="panel"><div class="section-title"><h2>业务批次</h2><span>'+list.length+' 批</span></div><div class="batch-list">'+list.map(d=>'<button class="batch-item" data-dataset="'+d.id+'"><div class="batch-line"><strong>'+esc(d.name)+'</strong>'+batchBadge(d.reconciliation?.status||'legacy')+'</div><span>'+d.rows+' 条 · '+esc(d.source)+' · '+(d.reconciliation?.handover_name?('清单：'+esc(d.reconciliation.handover_name)):'未挂清单')+'</span><small>SHA256 '+esc(d.checksum.slice(0,18))+'…</small></button>').join('')+'</div><div class="section-title"><h2>接入新数据</h2></div><form id="dataset-form"><label>批次名称<input name="name" required minlength="2" placeholder="例如：11 月复测批次"></label><label>交接清单<select name="handover_id"><option value="">不挂清单（旧批次，直接分析）</option>'+handoverList.map(h=>'<option value="'+h.id+'">'+esc(h.name)+' · 待对账</option>').join('')+'</select></label><label>上传 CSV 或 JSON<input type="file" id="file-input" accept=".json,.csv"></label><label>记录 JSON<textarea name="records" rows="6" placeholder="粘贴 JSON 数组，或先选择上方文件" required></textarea></label><p class="hint">字段：'+m.columns.map(c=>esc(c.key)+' / '+esc(c.label)).join('，')+'。5–5,000 条记录。</p><button type="submit" class="primary" '+(session.user.role==='viewer'?'disabled':'')+'>校验并接入批次</button></form></section><section class="panel"><div class="section-title"><h2 id="preview-name">'+esc(selected?.name||'尚无批次')+'</h2><span id="preview-status">'+batchBadge(selected?.reconciliation?.status||'legacy')+'</span></div><div id="data-reconcile">'+reconcilePanel(selected,handoverList)+'</div><div class="section-title"><h2>数据预览</h2><span>前 30 条</span></div><div id="data-preview">'+table(m.columns,selected?.records.slice(0,30)||[])+'</div></section></div>';
}
function reconcilePanel(selected,handoverList){
 if(!selected)return '<p class="empty">接入批次后可在此挂接清单并进入逐行认领。</p>';
 const r=selected.reconciliation||{status:'legacy'};
 return '<div class="attach-row"><div>'+batchBadge(r.status)+'<p class="hint">'+(r.handover_name?'清单去向：'+esc(r.handover_name)+'；已认领 '+(r.claimed_rows||0)+' / '+(r.total_rows||0)+' 行，待处理 '+(r.issue_count||0)+' 项。'+(r.status==='confirmed'?' 分析已开放。':''):'旧批次无需对账，可照常分析。')+'</p></div>'
  +(r.handover_id?'<a class="button small" href="#/reconcile/'+selected.id+'">逐行认领与确认 ↗</a>':(handoverList.length?'<form id="attach-form" class="inline-form" data-dataset="'+selected.id+'"><select name="handover_id" required>'+handoverList.map(h=>'<option value="'+h.id+'">'+esc(h.name)+'</option>').join('')+'</select><button type="submit" class="button small" '+(canReconcile()?'':'disabled')+'>挂接清单</button></form>':'<a class="text-link" href="#/handovers">先登记一份交接清单 ↗</a>'))+'</div>'
  +(r.issues?.length?'<ul class="issues">'+r.issues.slice(0,6).map(i=>'<li>'+esc(i.message)+'</li>').join('')+'</ul>':'');
}
export function bindData(refresh){
 let source='manual.json';
 document.querySelector('#file-input').addEventListener('change',async e=>{try{const file=e.target.files[0];if(!file)return;source=file.name;const text=await file.text(),records=file.name.endsWith('.csv')?parseCSV(text):JSON.parse(text);document.querySelector('[name=records]').value=JSON.stringify(records,null,2);}catch(error){notify(error.message,true);}});
 document.querySelectorAll('[data-dataset]').forEach(button=>button.onclick=async()=>{try{const d=await datasets.get(button.dataset.dataset);document.querySelector('#preview-name').textContent=d.name;document.querySelector('#preview-status').innerHTML=batchBadge(d.reconciliation?.status||'legacy');document.querySelector('#data-reconcile').innerHTML=reconcilePanel(d,workspace.handovers||[]);document.querySelector('#data-preview').innerHTML=table(workspace.meta.columns,d.records.slice(0,30));bindAttach(refresh);}catch(error){notify(error.message,true);}});
 submit(document.querySelector('#dataset-form'),async f=>{const handoverId=f.get('handover_id');await datasets.create({name:f.get('name'),source,records:JSON.parse(f.get('records')),...(handoverId?{handover_id:Number(handoverId)}:{})});notify(handoverId?'数据已接入并挂接清单，请逐行认领':'数据已接入，可前往工作台执行分析');await refresh();});
 bindAttach(refresh);
}
function bindAttach(refresh){
 const form=document.querySelector('#attach-form');
 if(!form)return;
 submit(form,async()=>{await handovers.attach(Number(form.dataset.dataset),Number(form.querySelector('[name=handover_id]').value));notify('已挂接清单，请逐行认领');await refresh();});
}
